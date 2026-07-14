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
    const poll = async () => {
      try {
        const { ok, status, data } = await sysFetch(`/update/status/${jobId}`);
        if (status === 404) {
          // Agent self-update restart wiped the in-memory job map. Show a
          // reconnect note; the SPA reloads on the new buildId (useVersionCheck).
          retry404Ref.current += 1;
          setUpdateJob({
            status: 'running',
            phase: 'agent',
            message:
              'Agent startet neu — die Seite lädt sich neu, sobald die '
              + 'Aktualisierung fertig ist.',
          });
          if (retry404Ref.current <= MAX_404_RETRIES) {
            timerRef.current = setTimeout(poll, POLL_RETRY_MS);
          } else {
            setUpdating(false);
          }
          return;
        }
        if (ok) {
          setUpdateJob(data);
          if (data.status === 'running') {
            timerRef.current = setTimeout(poll, POLL_RUNNING_MS);
          } else {
            setUpdating(false);
            if (data.status !== 'succeeded') setFailed(true);
            if (onSettledRef.current) onSettledRef.current(data);
          }
        } else {
          // Transient proxy hiccup (manager being recreated) — retry.
          timerRef.current = setTimeout(poll, POLL_RETRY_MS);
        }
      } catch {
        timerRef.current = setTimeout(poll, POLL_RETRY_MS);
      }
    };
    poll();
  }, []);

  const startUpdate = useCallback(async () => {
    setUpdating(true);
    setFailed(false);
    retry404Ref.current = 0;
    setUpdateJob({ status: 'running', phase: 'queued', message: 'Aktualisierung wird vorbereitet …' });
    try {
      const { ok, status, data } = await sysFetch('/update', { method: 'POST' });
      if (ok && data.job_id) {
        pollUpdate(data.job_id);
        return { ok: true, job_id: data.job_id };
      }
      if (status === 409) {
        // Multi-client: another browser already started the update (the agent's
        // single-flight guard). Surface the in-progress state instead of an
        // error — there is no job_id to poll, but the agent restarts + recreates
        // the manager, so this browser 502s then reloads on the new buildId.
        setUpdateJob({
          status: 'running',
          phase: 'running',
          message: data.message || 'Eine Aktualisierung läuft bereits — bitte warten.',
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
