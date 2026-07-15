// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Shared pi-agent update driver — the ACK-early POST /api/system/update + the
// 404-tolerant status poll, factored out of SystemPage so the System window
// (inline card) and the forced PiUpdateGate (startup modal) share ONE copy.
//
// The nuance both consumers need, in one place: the agent recreates the manager
// LAST and may self-update-restart — both 502 the very /api/system proxy this
// polls — so a running job's status endpoint 404s (in-memory job map wiped) or
// the proxy hiccups mid-poll. We keep polling through that window; useVersionCheck
// reloads the SPA on the new buildId once the manager is back.

import { useCallback, useEffect, useRef, useState } from 'react';

// Same-origin agent fetch (mirrors SystemPage.sysFetch). One connection through
// the opi manager's nginx reverse proxy (trailing-slash proxy_pass strips the
// /api/system prefix so /api/system/update reaches the agent's /update).
async function sysFetch(path, { method = 'GET', body } = {}) {
  const opts = { method, cache: 'no-store' };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api/system${path}`, opts);
  let data = {};
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  return { ok: res.ok, status: res.status, data };
}

const POLL_RUNNING_MS = 2000; // steady poll cadence while the job runs
const POLL_RETRY_MS = 3000; // proxy-hiccup / 404-reconnect retry cadence
const MAX_404_RETRIES = 8; // give up ~24 s after the agent self-restart 404s
const MAX_ERROR_RETRIES = 8; // give up ~24 s of proxy-hiccup / network errors

/**
 * Drive one pi-agent update job.
 *
 * @param {object} [opts]
 * @param {(job: object) => void} [opts.onSettled] invoked once with the terminal
 *   job object when the update reaches a non-running state (succeeded/failed).
 * @returns {{
 *   updating: boolean,
 *   updateJob: (object|null),
 *   failed: boolean,
 *   startUpdate: () => Promise<{ok: boolean, inFlight?: boolean, message?: string}>,
 * }}
 */
export function useAgentUpdate({ onSettled } = {}) {
  const [updating, setUpdating] = useState(false);
  const [updateJob, setUpdateJob] = useState(null);
  // True once this job (or its POST) reaches a failed terminal state — the
  // signal the forced PiUpdateGate uses to reveal „Ohne Update fortfahren"
  // (skip appears ONLY after a failed attempt, like the Windows GUI modal).
  const [failed, setFailed] = useState(false);

  const timerRef = useRef(null);
  const retry404Ref = useRef(0);
  const retryErrRef = useRef(0);
  // Keep the latest onSettled without listing it as a poll dep (a new closure
  // each render must not re-key the poll loop).
  const onSettledRef = useRef(onSettled);
  useEffect(() => {
    onSettledRef.current = onSettled;
  }, [onSettled]);

  // Stop the pending poll timer on unmount so a resolved fetch can't setState
  // into an unmounted tree.
  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  const pollUpdate = useCallback((jobId) => {
    // Drive the modal to a recoverable terminal state: stop the spinner, reveal
    // „Ohne Update fortfahren" (failed) and hand the (synthetic) terminal job to
    // onSettled. Called on 404-exhaustion (agent never came back) and on
    // error-exhaustion (proxy/network never recovered) — WITHOUT this the
    // non-closable PiUpdateGate modal has no clickable control (audit 1b/1c).
    const finishFailed = (job) => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setUpdating(false);
      setFailed(true);
      if (job) setUpdateJob(job);
      if (onSettledRef.current) onSettledRef.current(job);
    };
    // Transient proxy hiccup (manager being recreated) or a network throw. Retry
    // on the same cadence as the 404 path, but BOUNDED — an unbounded reschedule
    // spins forever against a permanently-dead agent (audit 1b).
    const scheduleErrorRetry = () => {
      retryErrRef.current += 1;
      if (retryErrRef.current <= MAX_ERROR_RETRIES) {
        timerRef.current = setTimeout(poll, POLL_RETRY_MS);
      } else {
        finishFailed({
          status: 'failed',
          message:
            'Status der Aktualisierung nicht abrufbar. Bitte prüfe die '
            + 'Verbindung und versuche es erneut.',
        });
      }
    };
    // Function declaration (hoisted) so the retry helpers above can reference it.
    async function poll() {
      try {
        const { ok, status, data } = await sysFetch(`/update/status/${jobId}`);
        if (status === 404) {
          // Agent self-update restart wiped the in-memory job map. Show a
          // reconnect note; the SPA reloads on the new buildId (useVersionCheck).
          retry404Ref.current += 1;
          if (retry404Ref.current <= MAX_404_RETRIES) {
            setUpdateJob({
              status: 'running',
              phase: 'agent',
              message:
                'Agent startet neu — die Seite lädt sich neu, sobald die '
                + 'Aktualisierung fertig ist.',
            });
            timerRef.current = setTimeout(poll, POLL_RETRY_MS);
          } else {
            // Agent never returned — unlock the modal so the student can skip
            // (audit 1c: previously only stopped the spinner, no skip button).
            finishFailed({
              status: 'failed',
              message:
                'Der Agent ist nach der Aktualisierung nicht zurückgekehrt. '
                + 'Bitte lade die Seite neu.',
            });
          }
          return;
        }
        if (ok) {
          // A healthy tick clears the transient retry counters so occasional
          // blips during a long-running job don't accumulate to the cap.
          retryErrRef.current = 0;
          retry404Ref.current = 0;
          setUpdateJob(data);
          if (data.status === 'running') {
            timerRef.current = setTimeout(poll, POLL_RUNNING_MS);
          } else {
            setUpdating(false);
            if (data.status !== 'succeeded') setFailed(true);
            if (onSettledRef.current) onSettledRef.current(data);
          }
        } else {
          scheduleErrorRetry();
        }
      } catch {
        scheduleErrorRetry();
      }
    }
    poll();
  }, []);

  const startUpdate = useCallback(async () => {
    setUpdating(true);
    setFailed(false);
    retry404Ref.current = 0;
    retryErrRef.current = 0;
    setUpdateJob({ status: 'running', phase: 'queued', message: 'Aktualisierung wird vorbereitet …' });
    try {
      const { ok, status, data } = await sysFetch('/update', { method: 'POST' });
      if (ok && data.job_id) {
        pollUpdate(data.job_id);
        return { ok: true, job_id: data.job_id };
      }
      if (status === 409) {
        // Multi-client: another browser already started the update (the agent's
        // single-flight guard). We CANNOT poll the sibling's job to completion:
        // the 409 body carries no job_id and there is no shared job-status
        // endpoint (only /update/status/{id}, and /status omits the job). The
        // sibling's update recreates the manager → this SPA reloads on the new
        // buildId (useVersionCheck). But if the sibling FAILS instead, no reload
        // comes — so we must NOT leave the non-closable PiUpdateGate modal with a
        // permanently-disabled button and no skip (audit 1a). Surface the honest
        // in-progress note AND reveal „Ohne Update fortfahren" (failed) so the
        // student is never trapped; keep `updating` false so the button re-enables.
        setUpdating(false);
        setFailed(true);
        setUpdateJob({
          status: 'running',
          phase: 'inflight',
          message:
            data.message
            || 'Eine Aktualisierung läuft bereits — bitte warten. Die Seite lädt '
            + 'automatisch neu, sobald sie abgeschlossen ist.',
        });
        return { ok: true, inFlight: true };
      }
      // Couldn't even start the job.
      setUpdating(false);
      setUpdateJob(null);
      setFailed(true);
      return { ok: false, message: data.message };
    } catch {
      setUpdating(false);
      setUpdateJob(null);
      setFailed(true);
      return { ok: false, message: 'Der Agent ist nicht erreichbar.' };
    }
  }, [pollUpdate]);

  return { updating, updateJob, failed, startUpdate };
}

export default useAgentUpdate;
