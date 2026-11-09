// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The claim screen of an UNCLAIMED Orange Pi (robot login, 2026-11-10). A Pi
// that was never set up — or was factory-reset — answers no robot route until
// a teacher or admin enters the one-time setup code here (owner decision 3).
// StudentApp renders it in place of every page while the Pi is unclaimed
// (utils/authGate → 'claim'). The agent decides everything (role, code,
// attempts); this screen only collects the two inputs and says what happened.

import React, { useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import { useDispatch, useSelector } from 'react-redux';
import LoginForm from './LoginForm';
import { Btn, LogoMark } from './EbUI';
import Icon from './icons/Icon';
import { claimPi, usePiAuth } from '../features/piAuth/piAuth';
import { signOutStudent } from '../utils/signOut';
import { moveToPage } from '../features/ui/uiSlice';
import PageType from '../constants/pageType';

// The root-only command that shows a NEW code on the Pi (pi_agent/setup_code.py).
export const SETUP_CODE_COMMAND = "sudo sh -c 'cd /opt/edubotics && python3 -m pi_agent.setup_code'";

const CLAIM_ROLES = new Set(['teacher', 'admin']);

export const PI_CLAIM_DE = Object.freeze({
  title: 'Diesen Roboter einrichten',
  intro:
    'Dieser Orange Pi ist noch nicht eingerichtet. Eine Lehrkraft oder ein Admin gibt hier '
    + 'einmal den Einrichtungscode ein. Danach kann sich jedes EduBotics-Konto an diesem '
    + 'Roboter anmelden.',
  loginSubtitle: 'Roboter einrichten: mit einem Lehrkräfte- oder Admin-Konto anmelden',
  loginHint:
    'Dieser Roboter ist noch nicht eingerichtet. Nur eine Lehrkraft oder ein Admin kann ihn '
    + 'mit dem Einrichtungscode einrichten.',
  wrongRole:
    'Einrichten kann nur eine Lehrkraft oder ein Admin. Bitte abmelden und mit einem solchen '
    + 'Konto anmelden.',
  codeLabel: 'Einrichtungscode',
  submit: 'Roboter einrichten',
  submitting: 'Wird eingerichtet …',
  where:
    'Der Code wurde am Ende der Installation (setup.sh) angezeigt, nach einem Zurücksetzen '
    + 'im System-Fenster, und beim Start auf einem angeschlossenen Bildschirm. Verloren? '
    + 'Am Pi im Terminal diesen Befehl ausführen — er zeigt einen neuen Code an:',
  none: 'Für diesen Pi gibt es noch keinen Einrichtungscode. Bitte am Pi einen erzeugen:',
  void:
    'Der Einrichtungscode ist nach zu vielen Fehlversuchen gesperrt. Bitte am Pi einen neuen '
    + 'erzeugen:',
  resetCode: 'Neuer Einrichtungscode nach dem Zurücksetzen — bitte notieren, er wird nur jetzt angezeigt:',
  done: 'Roboter eingerichtet.',
  logout: 'Abmelden',
});

export default function PiClaimScreen() {
  const dispatch = useDispatch();
  const piAuth = usePiAuth();
  const session = useSelector((s) => s.auth.session);
  const role = useSelector((s) => s.auth.role);
  const profileLoaded = useSelector((s) => s.auth.profileLoaded);
  const [code, setCode] = useState(piAuth.resetCode || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (piAuth.resetCode) setCode(piAuth.resetCode);
  }, [piAuth.resetCode]);

  if (!session) {
    return (
      <LoginForm subtitle={PI_CLAIM_DE.loginSubtitle} footerHint={PI_CLAIM_DE.loginHint} />
    );
  }

  const wrongRole = profileLoaded && role && !CLAIM_ROLES.has(role);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const res = await claimPi(code);
      if (res.ok) {
        toast.success(res.message || PI_CLAIM_DE.done);
        dispatch(moveToPage(PageType.SYSTEM));
      } else {
        setError(res.message);
      }
    } finally {
      setBusy(false);
    }
  };

  const status = piAuth.setupCode;
  const help = status === 'void' ? PI_CLAIM_DE.void : status === 'none' ? PI_CLAIM_DE.none : PI_CLAIM_DE.where;

  return (
    <div className="su-login h-full w-full flex items-center justify-center grid-dot px-4 py-6 overflow-y-auto">
      <div className="w-full max-w-[460px] flex flex-col items-center gap-5">
        <div className="flex items-center gap-2">
          <LogoMark />
          <span className="font-semibold text-[15px] tracking-tight text-[var(--ink)]">EduBotics</span>
        </div>
        <div className="su-login__plate w-full bg-white border border-[var(--line)] rounded-[var(--radius-lg)] shadow-pop p-5 sm:p-6">
          <h1 className="su-login__title text-[22px] font-semibold tracking-tight text-[var(--ink)] flex items-center gap-2">
            <Icon name="key" size={20} />
            {PI_CLAIM_DE.title}
          </h1>
          <p className="text-sm text-[var(--ink-3)] mt-2">{PI_CLAIM_DE.intro}</p>
          {piAuth.hostname ? (
            <p className="text-sm text-[var(--ink-2)] mt-2">
              Roboter: <span className="font-mono">{piAuth.hostname}</span>
            </p>
          ) : null}

          {piAuth.resetCode ? (
            <div role="status" className="mt-4 rounded-[12px] bg-[var(--bg-sunk)] p-3 text-sm text-[var(--ink)]">
              <p>{PI_CLAIM_DE.resetCode}</p>
              <p className="font-mono text-lg mt-1" data-testid="pi-reset-code">{piAuth.resetCode}</p>
            </div>
          ) : null}

          {wrongRole ? (
            <div className="mt-5 flex flex-col gap-3">
              <p role="alert" className="text-sm text-[color:var(--danger)]">{PI_CLAIM_DE.wrongRole}</p>
              <Btn variant="secondary" onClick={() => dispatch(signOutStudent())}>
                <Icon name="logout" size={16} />
                {PI_CLAIM_DE.logout}
              </Btn>
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="mt-5 flex flex-col gap-3">
              <label className="block">
                <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
                  {PI_CLAIM_DE.codeLabel}
                </span>
                <input
                  type="text"
                  className="su-login__input w-full h-11 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm font-mono uppercase tracking-widest text-[var(--ink)] focus:outline-none focus:border-[var(--accent)]"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="ABCD-EFGH"
                  autoComplete="off"
                  spellCheck={false}
                  autoCapitalize="characters"
                  maxLength={16}
                  required
                />
              </label>
              {error ? (
                <p role="alert" className="text-sm text-[color:var(--danger)]">{error}</p>
              ) : null}
              <Btn variant="primary" size="lg" type="submit" className="w-full justify-center" disabled={busy}>
                {busy ? PI_CLAIM_DE.submitting : PI_CLAIM_DE.submit}
              </Btn>
            </form>
          )}

          <div className="mt-5 pt-4 border-t border-[var(--line)] text-xs text-[var(--ink-3)]">
            <p>{help}</p>
            <p className="font-mono mt-1 break-all select-all">{SETUP_CODE_COMMAND}</p>
          </div>
        </div>
      </div>
    </div>
  );
}
