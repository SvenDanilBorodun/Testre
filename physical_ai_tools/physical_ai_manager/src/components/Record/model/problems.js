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

// features/hfToken/syncDecision::START_BLOCK spells one kind in snake_case.
const HF_TOKEN_REASON_KEY = Object.freeze({
  none: 'none',
  unusable: 'unusable',
  transfer: 'transfer',
  busy: 'busy',
  failed: 'failed',
  taken_over: 'takenOver',
  takenOver: 'takenOver',
});

// The two reasons that resolve by themselves: nothing on the Startseite helps.
const HF_TOKEN_NO_LINK = new Set(['transfer', 'busy']);

/**
 * Why Start is refused for the student's own Hugging-Face token (the robot does
 * not hold it yet): 'none' | 'unusable' | 'transfer' | 'busy' | 'failed' |
 * 'taken_over'. Every reason but `transfer` and `busy` sends the student to the
 * Startseite — those two resolve by themselves — so only the others get the link.
 * Returns null for a reason it does not know.
 */
export function hfTokenProblem(reason) {
  const key = HF_TOKEN_REASON_KEY[reason];
  if (!key) return null;
  return { kind: 'bad', textDe: P.hfToken[key], linkToHome: !HF_TOKEN_NO_LINK.has(key) };
}

const HF_TOKEN_HINT_KEYS = new Set(['offline', 'error', 'unavailable', 'unsupported']);

/**
 * The NON-blocking token hint (owner decision S2): 'offline' | 'error' |
 * 'unavailable' | 'unsupported' (features/hfToken/hfTokenSelectors::
 * selectHfRecordHint). A `warn`, never `bad`: it refuses nothing, it explains
 * the empty Benutzer-ID list and links to the Startseite card. Null otherwise.
 */
export function hfTokenHintProblem(reason) {
  if (!HF_TOKEN_HINT_KEYS.has(reason)) return null;
  return { kind: 'warn', textDe: P.hfTokenHint[reason], linkToHome: true };
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
 *   5b. any other reason Start is refused (the same dataset still uploading) —
 *       where Start is offered; disk and source blocks are rows 4 and 5 already
 *   5c. the non-blocking Hugging-Face-Token hint (S2), where Start is offered
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
  startBlock = null,
  hfTokenHint = null,
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
  // Start is off for a reason the rows above do not name (V2-R2-2): say it.
  if (view === 'READY' && startBlock?.problem && startBlock.kind !== 'disk' && startBlock.kind !== 'source') {
    out.push(startBlock.problem);
  }
  // Start is NOT off for this one: it only says why the Benutzer-ID list is empty.
  const hintP = view === 'READY' && startBlock?.kind !== 'hftoken' ? hfTokenHintProblem(hfTokenHint) : null;
  if (hintP) out.push(hintP);
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
