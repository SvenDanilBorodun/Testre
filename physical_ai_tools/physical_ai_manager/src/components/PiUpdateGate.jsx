// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Orange Pi forced-update gate — the Pi's parity for the Windows GUI's
// `_check_prerequisites` first step (`update_checker.check_for_update` → the
// non-closable update modal). On a Pi that is behind the latest release this
// proactively forces the update BEFORE the student continues, instead of relying
// on someone remembering to press „Jetzt aktualisieren" in the System tab.
//
// Matches the GUI modal's UX exactly:
//   * non-closable (no backdrop-dismiss, no X);
//   * German copy + a prominent „Jetzt aktualisieren" primary button;
//   * a live progress/phase display;
//   * „Ohne Update fortfahren" (skip) that appears ONLY after a FAILED attempt.
//
// Pi-only: it renders NOTHING on Windows / cloud / Jetson (guarded on `piMode`),
// and is mounted app-globally (StudentApp, next to StartupGate) so it gates the
// whole app, not just the System tab. All strings are German (Rule §1).

import React, { useCallback, useEffect, useState } from 'react';
import { usePiMode } from '../utils/piMode';
import { useAgentUpdate } from '../hooks/useAgentUpdate';

// Read-only availability probe (cached agent-side with a short TTL). Same-origin
// through the opi manager's /api/system reverse proxy. A non-2xx / non-JSON body
// (the SPA catch-all's index.html on a non-Pi image) resolves to null → no gate.
async function fetchUpdateCheck() {
  if (typeof fetch !== 'function') return null;
  try {
    const res = await fetch(`/api/system/update/check?_=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) return null;
    const json = await res.json();
    return json && typeof json === 'object' ? json : null;
  } catch {
    return null;
  }
}

// Bounded backoff for the availability probe, mirroring `AGENT_BACKOFF_MS` in
// utils/piMode.js. The pi-agent binds its gateway listener only AFTER its
// boot-time manager `up`, so an early probe 502s through the proxy
// (`fetchUpdateCheck` → null). A one-shot fetch would silently disarm the forced
// gate for the whole page load, so we retry across this schedule and GIVE UP
// permanently once it exhausts (unlike piMode.js's steady loop — this is one
// logical startup probe, never re-checked after it settles).
const UPDATE_CHECK_BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];

export default function PiUpdateGate() {
  const { piMode, piModeResolved } = usePiMode();

  const [available, setAvailable] = useState(false);
  const [latestVersion, setLatestVersion] = useState('');
  const [currentVersion, setCurrentVersion] = useState('');
  const [skipped, setSkipped] = useState(false); // set by „Ohne Update fortfahren"
  const [done, setDone] = useState(false); // set on a succeeded terminal state

  // Auto-dismiss on success (the manager was recreated → useVersionCheck reloads
  // the SPA on the new buildId; if the buildId is unchanged the student simply
  // proceeds on the freshly-updated stack). A failure leaves the modal up and
  // `failed` reveals the skip button.
  const onSettled = useCallback((job) => {
    if (job && job.status === 'succeeded') setDone(true);
  }, []);
  const { updating, updateJob, failed, startUpdate } = useAgentUpdate({ onSettled });

  // One logical availability probe once Pi mode has resolved (mirrors the GUI's
  // single startup check). Never runs off a Pi. A 502 during the agent boot-race
  // returns null → we retry through UPDATE_CHECK_BACKOFF_MS instead of silently
  // disarming the gate for the page load, and settle the instant the agent
  // answers with valid JSON (update or no update) OR the schedule exhausts.
  useEffect(() => {
    if (!piModeResolved || !piMode) return undefined;
    let cancelled = false;
    let timer = null;
    let attempt = 0;

    const probe = async () => {
      const res = await fetchUpdateCheck();
      if (cancelled) return;
      if (res) {
        // Agent answered — settle. Arm the gate only if an update is advertised.
        if (res.update_available) {
          setAvailable(true);
          setLatestVersion(res.latest_version || '');
          setCurrentVersion(res.current_version || '');
        }
        return;
      }
      // Unreachable (agent gateway not yet bound / proxy 502). Retry until the
      // backoff schedule is exhausted, then give up permanently.
      if (attempt >= UPDATE_CHECK_BACKOFF_MS.length) return;
      const delay = UPDATE_CHECK_BACKOFF_MS[attempt];
      attempt += 1;
      timer = setTimeout(probe, delay);
    };
    probe();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [piMode, piModeResolved]);

  const handleSkip = useCallback(() => setSkipped(true), []);

  // Nothing to show: not a Pi, no update advertised, or the student already
  // skipped / finished. Hooks above always run first (rules-of-hooks).
  if (!piMode || !piModeResolved || !available || skipped || done) return null;

  const running = !!updateJob && updateJob.status === 'running';
  const primaryLabel = updating
    ? 'Aktualisierung läuft …'
    : failed
    ? 'Erneut versuchen'
    : 'Jetzt aktualisieren';

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Ein Update ist verfügbar"
    >
      <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-xl">
        <h2 className="text-lg font-bold text-gray-900">Ein Update ist verfügbar!</h2>
        <p className="mt-2 text-sm text-gray-700">
          Neue Version: <span className="font-mono">{latestVersion || '—'}</span>
          {currentVersion ? (
            <span className="text-gray-500">{'  '}(aktuell: {currentVersion})</span>
          ) : null}
        </p>
        <p className="mt-1 text-sm text-gray-500">
          Bitte aktualisiere EduBotics, bevor du fortfährst.
        </p>

        {updateJob && (
          <div className="mt-4 rounded-lg bg-gray-50 p-3 text-sm">
            <div className="text-gray-700">{updateJob.message}</div>
            {typeof updateJob.progress === 'number' && running && (
              <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-gray-200">
                <div
                  className="h-full bg-teal-500 transition-[width] duration-500"
                  style={{ width: `${Math.max(0, Math.min(100, updateJob.progress))}%` }}
                />
              </div>
            )}
          </div>
        )}

        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={startUpdate}
            disabled={updating}
            className="rounded-md bg-teal-500 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-teal-600 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {primaryLabel}
          </button>
          {/* Skip appears ONLY after a failed attempt (the GUI's fail-count rule):
              a student behind a flaky link / blocked asset is never hard-locked. */}
          {failed && (
            <button
              type="button"
              onClick={handleSkip}
              className="rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50"
            >
              Ohne Update fortfahren
            </button>
          )}
        </div>

        {/* Suppress the failure banner while a job is still marked running — the
            409 „another browser is already updating" case sets `failed` (to
            reveal the skip) but is NOT a failure, so its in-progress note in the
            box above must not be contradicted by „fehlgeschlagen". */}
        {failed && !running && (
          <p className="mt-3 text-xs text-orange-600">
            Aktualisierung fehlgeschlagen. Bitte Internetverbindung prüfen und erneut versuchen.
          </p>
        )}
      </div>
    </div>
  );
}
