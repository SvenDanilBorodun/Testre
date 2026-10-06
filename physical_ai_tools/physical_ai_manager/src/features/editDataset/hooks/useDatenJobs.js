// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The jobs THIS page started (spec §D4): `edit`, `delete_dataset`, `download`
// and `keep_both` answer at once with a job id; the page follows each one on
// /edubotics/daten_state (§J.5) and runs its own `onDone`/`onFailed` exactly
// once when the robot reports it finished. While a job is followed but the
// topic has been silent for a while, the page asks `/daten/command state`
// instead (the same payload), so a lost subscription can never lock a progress
// dialog for good.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export const STATE_POLL_MS = 2000;
export const TOPIC_SILENT_MS = 3000;

/** The progress step (0, 1, 2) a job's stage stands for (§D4). */
export function stepOfStage(op, stage) {
  if (op === 'keep_both') {
    if (stage === 'download') return 0;
    if (stage === 'upload') return 2;
    return stage === 'copy' || stage === 'verify' || stage === 'swap' ? 1 : 0;
  }
  if (stage === 'copy') return 1;
  if (stage === 'verify' || stage === 'swap' || stage === 'upload') return 2;
  return 0;
}

/**
 * `payload` with every running BYTE job's `done` held at the most it reached
 * (`peaks`: job_id → bytes, updated here; jobs no longer listed are forgotten).
 * The robot counts a download's bytes in its tmp, which reads 0 for a moment
 * when the finished tmp is swapped into place: a job's progress must never run
 * backwards on the page (V2-14). Idempotent: the same payload gives the same rows.
 */
export function withMonotonicBytes(payload, peaks) {
  if (!payload || !Array.isArray(payload.jobs)) return payload;
  const live = new Set();
  let changed = false;
  const jobs = payload.jobs.map((j) => {
    if (j.unit !== 'bytes' || j.state !== 'running') return j;
    live.add(j.job_id);
    const done = Number(j.done) || 0;
    const peak = Math.max(peaks.get(j.job_id) || 0, done);
    peaks.set(j.job_id, peak);
    if (peak === done) return j;
    changed = true;
    return { ...j, done: peak };
  });
  [...peaks.keys()].forEach((id) => { if (!live.has(id)) peaks.delete(id); });
  return changed ? { ...payload, jobs } : payload;
}

/**
 * @param {{payload: object|null, receivedAt: number|null}} datenState useDatenState's snapshot
 * @param {(action: string, args: object) => Promise<object>} command useDatenCommand's runner
 */
export default function useDatenJobs({ datenState, command, now = Date.now }) {
  const [tracked, setTracked] = useState({}); // job_id → meta
  const trackedRef = useRef(tracked);
  trackedRef.current = tracked;
  const handledRef = useRef(new Set());
  const [polled, setPolled] = useState(null); // a payload from `state` while the topic is silent

  const track = useCallback((jobId, meta) => {
    if (!jobId) return;
    setTracked((t) => ({ ...t, [jobId]: { ...meta, jobId, startedAt: now() } }));
  }, [now]);

  const untrack = useCallback((jobId) => {
    setTracked((t) => {
      if (!t[jobId]) return t;
      const next = { ...t };
      delete next[jobId];
      return next;
    });
  }, []);

  const payload = (datenState && datenState.payload) || null;

  // The freshest payload: the topic's, or the polled one when that is newer —
  // with every byte job's progress held at its peak.
  const fresh = polled && (!payload || (polled.at > (datenState.receivedAt || 0))) ? polled.payload : payload;
  const peaksRef = useRef(new Map());
  const effective = useMemo(() => withMonotonicBytes(fresh, peaksRef.current), [fresh]);

  // Finish what the robot reports finished.
  useEffect(() => {
    if (!effective || !Array.isArray(effective.jobs)) return;
    const rows = {};
    effective.jobs.forEach((j) => { rows[j.job_id] = j; });
    Object.values(trackedRef.current).forEach((meta) => {
      const row = rows[meta.jobId];
      if (!row || handledRef.current.has(meta.jobId)) return;
      if (row.state === 'done' || row.state === 'failed') {
        handledRef.current.add(meta.jobId);
        untrack(meta.jobId);
        try {
          if (row.state === 'done') meta.onDone?.(row);
          else meta.onFailed?.(row);
        } catch (err) {
          console.error('[daten] job handler failed:', err);
        }
      }
    });
  }, [effective, tracked, untrack]);

  // The topic is silent while a job is followed: ask `state` (§J.3) instead.
  const hasTracked = Object.keys(tracked).length > 0;
  useEffect(() => {
    if (!hasTracked) return undefined;
    const id = setInterval(async () => {
      const last = datenState && datenState.receivedAt ? datenState.receivedAt : 0;
      if (now() - last < TOPIC_SILENT_MS) return;
      const r = await command('state', {});
      if (r && r.ok && r.result && Array.isArray(r.result.jobs)) setPolled({ payload: r.result, at: now() });
    }, STATE_POLL_MS);
    return () => clearInterval(id);
  }, [hasTracked, datenState, command, now]);

  /** The tracked job whose progress dialog shows, with its live row (null until the robot reports it). */
  const dialogMeta = Object.values(tracked).find((m) => m.dialog) || null;
  const dialogRow = dialogMeta && effective && Array.isArray(effective.jobs)
    ? effective.jobs.find((j) => j.job_id === dialogMeta.jobId) || null
    : null;

  return { track, untrack, tracked, dialog: dialogMeta ? { meta: dialogMeta, row: dialogRow } : null, payload: effective };
}
