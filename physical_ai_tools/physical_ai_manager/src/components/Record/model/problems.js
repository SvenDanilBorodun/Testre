// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Aufnahme page's problem banner (spec §3.9): which ONE problem the
// student sees, if any. The banner appears only for a real problem; the list
// is ordered by what matters first and the page shows its head.
//
// Problem: `{kind: 'warn'|'bad'|'info', textDe, linkToHome?}` — `bad` stops
// something (the recording, or Start), `warn` is worth knowing, `info` is a
// short note. Pure.

import RECORD_COPY from './recordCopy';

const P = RECORD_COPY.problem;

export const WARN_NOTICE_MS = 12000;
const SOURCE_ORDER = Object.freeze({ camera: 0, follower: 1, leader: 2 });

const bySourceOrder = (a, b) => (SOURCE_ORDER[a.kind] ?? 9) - (SOURCE_ORDER[b.kind] ?? 9);

/**
 * The disk problem, or null. Not recording: below the start floor Start is
 * refused (`bad`). Recording: between the floors a warning, below the
 * critical floor the robot ends the session itself (`bad`).
 */
export function diskProblem(disk, { running = false } = {}) {
  if (!disk || (disk.verdict !== 'low' && disk.verdict !== 'critical')) return null;
  if (running) {
    return disk.verdict === 'critical'
      ? { kind: 'bad', textDe: P.diskCritical(disk.free) }
      : { kind: 'warn', textDe: P.diskLowRecording(disk.free, disk.criticalFloor) };
  }
  return { kind: 'bad', textDe: P.diskLow(disk.free, disk.startFloor) };
}

/** The sentence for one stalled source (the leader's has three causes). */
export function stalledSourceProblem(source, { bridge = null, activation = null } = {}) {
  if (!source) return null;
  if (source.kind === 'camera') return { kind: 'bad', textDe: P.cameraStalled(source.name) };
  if (source.kind === 'follower') return { kind: 'bad', textDe: P.followerStalled };
  if (bridge && bridge.available === true && bridge.followerOnly === true) {
    return { kind: 'bad', textDe: P.leaderOff };
  }
  if (activation && activation.state && activation.state !== 'active') {
    return { kind: 'bad', textDe: P.leaderNotActivated, linkToHome: true };
  }
  return { kind: 'bad', textDe: P.leaderStalled };
}

/** The sentence for one slow source. */
export function slowSourceProblem(source, fps) {
  if (!source) return null;
  if (source.kind === 'camera') return { kind: 'warn', textDe: P.cameraSlow(source.name, source.hz, fps) };
  return { kind: 'warn', textDe: P.armSlow(source.kind, source.hz, fps) };
}

/** Sources with `verdict`, in the order camera → follower → leader. */
export function sourcesWith(verdicts, verdict) {
  if (!Array.isArray(verdicts)) return [];
  return verdicts.filter((v) => v.verdict === verdict).sort(bySourceOrder);
}

/**
 * Every problem, most important first:
 *   1. the session's hard error notice
 *   2. a transient command failure / validation message (the page keeps it 8 s)
 *   3. the link lost while running
 *   4. disk low / critical
 *   5. stalled sources
 *   6. a record [WARNUNG] notice, ≤ 12 s old
 *   7. slow sources
 *   8. a transient info note
 * A notice the finish card already prints is skipped.
 */
export function deriveProblems({
  view = '',
  running = false,
  heartbeat = 'connected',
  notice = null,
  transient = null,
  session = null,
  disk = null,
  verdicts = null,
  bridge = null,
  activation = null,
  fps = 0,
  nowWallMs = Date.now(),
} = {}) {
  const out = [];
  const finish = session?.finish;
  const cardShows = view === 'FINISHING'
    ? [finish?.message, finish?.endNote, session?.errorText].filter(Boolean)
    : [finish?.endNote].filter(Boolean);
  const liveTransient = transient && Number.isFinite(transient.until) && nowWallMs < transient.until
    ? transient : null;

  if (notice?.kind === 'error' && notice.text && !cardShows.includes(notice.text)) {
    out.push({ kind: 'bad', textDe: notice.text });
  }
  if (liveTransient && liveTransient.kind !== 'info') {
    out.push({ kind: liveTransient.kind || 'bad', textDe: liveTransient.textDe });
  }
  if (running && heartbeat !== 'connected') {
    out.push({ kind: 'bad', textDe: P.linkLost });
  }
  const diskP = diskProblem(disk, { running });
  if (diskP) out.push(diskP);
  for (const s of sourcesWith(verdicts, 'stalled')) {
    out.push(stalledSourceProblem(s, { bridge, activation }));
  }
  if (notice?.kind === 'warn' && notice.text && Number.isFinite(notice.at)
      && nowWallMs - notice.at <= WARN_NOTICE_MS && !cardShows.includes(notice.text)) {
    out.push({ kind: 'warn', textDe: notice.text });
  }
  for (const s of sourcesWith(verdicts, 'slow')) out.push(slowSourceProblem(s, fps));
  if (liveTransient && liveTransient.kind === 'info') {
    out.push({ kind: 'info', textDe: liveTransient.textDe });
  }
  return out;
}

/** The problem the banner shows, or null. */
export function firstProblem(input) {
  return deriveProblems(input)[0] || null;
}
