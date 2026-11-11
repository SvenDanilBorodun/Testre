// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The admin's second factor (owner decision 2026-11-10: TOTP is required for
// admins only; teachers and students never see this). The cloud API refuses
// every admin route without a two-factor session (403 `mfa_required`,
// cloud_training_api/app/auth.py::get_current_admin), so WebApp renders the
// AdminDashboard only through this gate:
//
// * the session is aal2                → the dashboard;
// * aal1 and a verified TOTP factor    → the challenge (a 6-digit code);
// * aal1 and no verified factor        → the enrolment (QR code, the secret as
//   text, a 6-digit check); a factor left unverified by an abandoned
//   enrolment is removed first, so a new one can be made.
//
// A verified code upgrades the session in place (supabase-js saves the aal2
// session and announces MFA_CHALLENGE_VERIFIED, which WebApp's auth listener
// stores). An `mfa_required` answer later (services/apiClient.js →
// features/auth/mfaRequired.js) brings the admin back to the challenge.
// Recovery from a lost authenticator: docs/deploy/DEPLOY.md.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { supabase } from '../../lib/supabaseClient';
import { onMfaRequired } from '../../features/auth/mfaRequired';
import { Btn, LogoMark } from '../EbUI';
import LegalLinks from '../LegalLinks';

export const MFA_DE = {
  checking: 'Zwei-Faktor-Anmeldung wird geprüft…',
  enrollTitle: 'Zwei-Faktor-Anmeldung einrichten',
  enrollText:
    'Der Admin-Bereich ist nur mit einem zweiten Faktor erreichbar. Scanne den QR-Code mit einer Authenticator-App (zum Beispiel Microsoft Authenticator, Google Authenticator oder 2FAS) und gib danach den 6-stelligen Code aus der App ein.',
  secretLabel: 'Kein QR-Scanner? Diesen Schlüssel in der App eingeben:',
  qrAlt: 'QR-Code für die Authenticator-App',
  challengeTitle: 'Zwei-Faktor-Anmeldung',
  challengeText: 'Gib den 6-stelligen Code aus deiner Authenticator-App ein.',
  lostDevice:
    'Kein Zugriff mehr auf die App? Der Plattform-Betreiber kann den zweiten Faktor zurücksetzen; danach richtest du ihn neu ein.',
  codeLabel: '6-stelliger Code',
  confirm: 'Bestätigen',
  verifying: 'Wird geprüft…',
  logout: 'Abmelden',
  retry: 'Erneut versuchen',
  codeShape: 'Bitte genau 6 Ziffern eingeben.',
  wrongCode: 'Der Code stimmt nicht oder ist abgelaufen. Bitte den aktuellen Code aus der App eingeben.',
  disabled:
    'Die Zwei-Faktor-Anmeldung ist für dieses Projekt nicht freigeschaltet. Bitte den Plattform-Betreiber informieren.',
  unavailable: 'Die Zwei-Faktor-Anmeldung ist gerade nicht erreichbar. Bitte versuche es gleich noch einmal.',
};

const FRIENDLY_NAME = 'EduBotics Admin';
const ISSUER = 'EduBotics';

function errorSentence(error) {
  const code = String(error?.code || '');
  if (code.includes('not_enabled') || code.includes('disabled')) return MFA_DE.disabled;
  if (code.includes('verification') || code.includes('expired') || code === 'mfa_challenge_expired') {
    return MFA_DE.wrongCode;
  }
  if (error?.status === 400 || error?.status === 422) return MFA_DE.wrongCode;
  return MFA_DE.unavailable;
}

/** Group a base32 secret in fours so it can be typed by hand. */
function groupSecret(secret) {
  return String(secret || '').replace(/(.{4})/g, '$1 ').trim();
}

export default function AdminMfaGate({ onLogout, children }) {
  // 'checking' | 'ok' | 'challenge' | 'enroll' | 'error'
  const [phase, setPhase] = useState('checking');
  const [factorId, setFactorId] = useState(null);
  const [enrollment, setEnrollment] = useState(null); // { qr, secret }
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const generation = useRef(0);

  const decide = useCallback(async ({ forceChallenge = false } = {}) => {
    const mine = ++generation.current;
    const current = () => generation.current === mine;
    setPhase('checking');
    setMessage(null);
    setCode('');
    try {
      const aal = await supabase.auth.mfa.getAuthenticatorAssuranceLevel();
      if (!current()) return;
      if (aal.error) throw aal.error;
      if (aal.data?.currentLevel === 'aal2' && !forceChallenge) {
        setPhase('ok');
        return;
      }
      const factors = await supabase.auth.mfa.listFactors();
      if (!current()) return;
      if (factors.error) throw factors.error;
      const verified = factors.data?.totp || [];
      if (verified.length > 0) {
        setFactorId(verified[0].id);
        setPhase('challenge');
        return;
      }
      // An enrolment the admin abandoned leaves an UNVERIFIED factor behind;
      // remove it, or the new one would clash with it.
      const stale = (factors.data?.all || []).filter(
        (f) => f.factor_type === 'totp' && f.status !== 'verified',
      );
      await Promise.all(stale.map((f) => supabase.auth.mfa.unenroll({ factorId: f.id })));
      const enrolled = await supabase.auth.mfa.enroll({
        factorType: 'totp',
        friendlyName: FRIENDLY_NAME,
        issuer: ISSUER,
      });
      if (!current()) return;
      if (enrolled.error) throw enrolled.error;
      setFactorId(enrolled.data.id);
      setEnrollment({ qr: enrolled.data.totp?.qr_code, secret: enrolled.data.totp?.secret });
      setPhase('enroll');
    } catch (error) {
      if (!current()) return;
      setMessage(errorSentence(error));
      setPhase('error');
    }
  }, []);

  useEffect(() => {
    decide();
    const off = onMfaRequired(() => decide({ forceChallenge: true }));
    return () => {
      generation.current += 1;
      off();
    };
  }, [decide]);

  const submit = async (e) => {
    e.preventDefault();
    const digits = code.replace(/\s+/g, '');
    if (!/^\d{6}$/.test(digits)) {
      setMessage(MFA_DE.codeShape);
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const { error } = await supabase.auth.mfa.challengeAndVerify({ factorId, code: digits });
      if (error) {
        setMessage(errorSentence(error));
        setCode('');
        return;
      }
      setEnrollment(null);
      await decide();
    } catch (error) {
      setMessage(errorSentence(error));
    } finally {
      setBusy(false);
    }
  };

  if (phase === 'ok') return children;

  const enrolling = phase === 'enroll';
  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-6" style={{ background: 'var(--bg)' }}>
      <div className="w-full max-w-[460px] flex flex-col items-center gap-5">
        <div className="flex items-center gap-2">
          <LogoMark />
          <span className="font-semibold text-[15px] tracking-tight text-[var(--ink)]">EduBotics</span>
        </div>
        <div className="w-full bg-white border border-[var(--line)] rounded-[var(--radius-lg)] shadow-pop p-6">
          {phase === 'checking' && (
            <p className="text-[var(--ink-3)]" role="status">
              {MFA_DE.checking}
            </p>
          )}

          {phase === 'error' && (
            <div className="flex flex-col gap-4">
              <p className="text-sm text-[var(--ink-2)]" role="alert">
                {message}
              </p>
              <div className="flex gap-3">
                <Btn variant="primary" onClick={() => decide()}>
                  {MFA_DE.retry}
                </Btn>
                <Btn variant="secondary" onClick={onLogout}>
                  {MFA_DE.logout}
                </Btn>
              </div>
            </div>
          )}

          {(phase === 'challenge' || enrolling) && (
            // noValidate: the German sentence below answers a malformed code,
            // never the browser's own tooltip.
            <form
              onSubmit={submit}
              noValidate
              className="flex flex-col gap-4"
              aria-label={enrolling ? MFA_DE.enrollTitle : MFA_DE.challengeTitle}
            >
              <h1 className="text-[20px] font-semibold tracking-tight text-[var(--ink)]">
                {enrolling ? MFA_DE.enrollTitle : MFA_DE.challengeTitle}
              </h1>
              <p className="text-sm text-[var(--ink-2)]">{enrolling ? MFA_DE.enrollText : MFA_DE.challengeText}</p>

              {enrolling && enrollment && (
                <div className="flex flex-col items-center gap-3">
                  {enrollment.qr && (
                    <img
                      src={enrollment.qr}
                      alt={MFA_DE.qrAlt}
                      width={192}
                      height={192}
                      className="border border-[var(--line)] rounded-[var(--radius-sm)] bg-white"
                    />
                  )}
                  <div className="w-full">
                    <p className="text-xs text-[var(--ink-3)] mb-1">{MFA_DE.secretLabel}</p>
                    <p className="font-mono text-sm break-all select-all text-[var(--ink)]" data-testid="mfa-secret">
                      {groupSecret(enrollment.secret)}
                    </p>
                  </div>
                </div>
              )}

              <label className="block">
                <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">{MFA_DE.codeLabel}</span>
                <input
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={7}
                  className="w-full h-11 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] font-mono text-lg tracking-[0.3em] text-[var(--ink)] focus:outline-none focus:border-[var(--accent)]"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  autoFocus
                  required
                />
              </label>

              {message && (
                <p className="text-sm text-[var(--danger,#b42318)]" role="alert">
                  {message}
                </p>
              )}

              <div className="flex gap-3">
                <Btn variant="primary" type="submit" disabled={busy}>
                  {busy ? MFA_DE.verifying : MFA_DE.confirm}
                </Btn>
                <Btn variant="secondary" type="button" onClick={onLogout} disabled={busy}>
                  {MFA_DE.logout}
                </Btn>
              </div>

              {!enrolling && <p className="text-xs text-[var(--ink-3)]">{MFA_DE.lostDevice}</p>}
            </form>
          )}
        </div>
        <LegalLinks />
      </div>
    </div>
  );
}
