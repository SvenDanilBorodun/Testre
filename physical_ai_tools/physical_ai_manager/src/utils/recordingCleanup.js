/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Recording clean-up for a Vormachen take (pure; no React, no Blockly).
//
// Rows are Contract B (`[j1..jn, grip, t_s]`, time LAST). Clean-up only ever
// REMOVES samples (the start/end handles) or SHORTENS a still gap (the wait
// before the first move, optionally long pauses). Joint values are copied
// untouched. All time arithmetic is in integer milliseconds, so the time column
// stays non-decreasing and every originally positive pair step stays ≥ 1 ms —
// a shift only ever shortens ONE gap to 300 / 500 ms. The server's replay
// velocity floor stays authoritative; nothing here makes a replay faster than
// the recording allowed.
//
// Runs BEFORE compactTrajectoryPoints (utils/trajectoryCompact.js).

import { armGeometry } from './armProfile';

export const LEAD_GAP_MAX_MS = 300;
export const FALL_WINDOW_MS = 800;
export const FALL_SPEED_RAD_S = 2.5;
// A fall is a release at the END of a real take: it is suggested only when at
// least this much of the take lies before its onset. Without it a short take,
// or one that is a single fast deliberate move, was trimmed to its first two
// samples by default (rig gate S-R6 owns the number).
export const FALL_MIN_BEFORE_MS = 800;
export const PAUSE_GAP_MS = 1000;
export const PAUSE_TARGET_MS = 500;
export const ACTIVITY_BIN_MS = 200;
export const MIN_POINTS = 2;

/**
 * The arm joints that sag under gravity when a student lets go (indices into
 * the row). Rig gate S-R6 owns the physical check of the heuristic.
 */
export function fallJointIndices(caps) {
  const geo = armGeometry(caps);
  if (geo.urdfAssetId === 'edu6') return [1, 2, 4];
  if (geo.urdfAssetId === 'omx_f' || geo.urdfAssetId === 'edu1') return [1, 2, 3];
  return geo.armJoints >= 4 ? [1, 2, 3] : [1, 2];
}

function isRows(points) {
  return Array.isArray(points) && points.every((row) => Array.isArray(row) && row.length >= 2);
}

function timeMs(row) {
  return Math.round(Number(row[row.length - 1]) * 1000);
}

function clampIndex(value, lo, hi, fallback) {
  const v = Number.isInteger(value) ? value : fallback;
  return Math.min(Math.max(v, lo), hi);
}

// Earliest pair k (rows k → k+1) inside the last FALL_WINDOW_MS where a fall
// joint moves faster than FALL_SPEED_RAD_S and keeps that direction to the end.
// A zero-Δt pair carries no direction and is skipped; a still pair (Δq = 0)
// breaks the run, so a fall that has already come to rest is not suggested.
function keepsDirection(points, ms, j, from, sign) {
  for (let m = from; m < points.length - 1; m += 1) {
    if (ms[m + 1] - ms[m] >= 1
        && Math.sign(Number(points[m + 1][j]) - Number(points[m][j])) !== sign) return false;
  }
  return true;
}

function fallOnset(points, ms, joints) {
  const last = points.length - 1;
  const windowStart = ms[last] - FALL_WINDOW_MS;
  for (let k = 0; k < last; k += 1) {
    const dt = ms[k + 1] - ms[k];
    if (ms[k] >= windowStart && dt >= 1) {
      const fast = joints.find((j) => {
        const dq = Number(points[k + 1][j]) - Number(points[k][j]);
        return Math.abs(dq) / (dt / 1000) > FALL_SPEED_RAD_S
          && keepsDirection(points, ms, j, k + 1, Math.sign(dq));
      });
      if (fast !== undefined) return k;
    }
  }
  return null;
}

/**
 * @returns {{ width, durationMs, leadGapMs, leadTrimMs, suggestedEndIndex: number|null,
 *   pauses: Array<{index, gapMs}>, activity: number[], timesMs: number[] }}
 *   `timesMs` are the raw times relative to the first row (the strip's x axis).
 *   `pauses` excludes the leading gap, which `leadGapMs` reports on its own.
 */
export function analyzeTake(points, caps) {
  const empty = {
    width: 0, durationMs: 0, leadGapMs: 0, leadTrimMs: 0, suggestedEndIndex: null,
    pauses: [], activity: [], timesMs: [],
  };
  if (!isRows(points) || points.length === 0) return empty;
  const width = points[0].length;
  const ms = points.map(timeMs);
  const timesMs = ms.map((t) => t - ms[0]);
  if (points.length < MIN_POINTS) return { ...empty, width, timesMs };
  const last = points.length - 1;
  const durationMs = ms[last] - ms[0];
  const leadGapMs = ms[1] - ms[0];
  const found = fallOnset(points, ms, fallJointIndices(caps));
  const onset = found !== null && ms[found] - ms[0] >= FALL_MIN_BEFORE_MS ? found : null;
  const suggested = onset === null ? null : Math.max(onset, 1);
  const pauses = [];
  for (let k = 2; k <= last; k += 1) {
    const gapMs = ms[k] - ms[k - 1];
    if (gapMs > PAUSE_GAP_MS) pauses.push({ index: k, gapMs });
  }
  // At least one bin whenever there are two rows, so motion is never dropped.
  const bins = Math.max(1, Math.ceil(durationMs / ACTIVITY_BIN_MS));
  const activity = new Array(bins).fill(0);
  for (let k = 0; k < last; k += 1) {
    const b = Math.min(bins - 1, Math.max(0, Math.floor(timesMs[k + 1] / ACTIVITY_BIN_MS)));
    let sum = 0;
    for (let j = 0; j < width - 1; j += 1) sum += Math.abs(Number(points[k + 1][j]) - Number(points[k][j]));
    if (Number.isFinite(sum)) activity[b] += sum;
  }
  return {
    width,
    durationMs,
    leadGapMs,
    leadTrimMs: Math.max(0, leadGapMs - LEAD_GAP_MAX_MS),
    suggestedEndIndex: suggested !== null && suggested < last ? suggested : null,
    pauses,
    activity,
    timesMs,
  };
}

/**
 * @returns a NEW array of new rows (the input is never mutated). Fewer than
 *   MIN_POINTS rows (or non-Contract-B input) come back unchanged.
 */
export function applyCleanup(points, {
  trimLead = true,
  startIndex = 0,
  endIndex = Array.isArray(points) ? points.length - 1 : 0,
  compressPauses = false,
} = {}) {
  if (!isRows(points) || points.length < MIN_POINTS) {
    return Array.isArray(points) ? points.map((row) => (Array.isArray(row) ? row.slice() : row)) : points;
  }
  const last = points.length - 1;
  const ms = points.map(timeMs);
  if (trimLead) {
    const gap = ms[1] - ms[0];
    if (gap > LEAD_GAP_MAX_MS) {
      const shift = gap - LEAD_GAP_MAX_MS;
      for (let i = 1; i <= last; i += 1) ms[i] -= shift;
    }
  }
  const start = clampIndex(startIndex, 0, last - 1, 0);
  const end = clampIndex(endIndex, start + 1, last, last);
  const kept = ms.slice(start, end + 1);
  const base = kept[0];
  for (let i = 0; i < kept.length; i += 1) kept[i] -= base;
  if (compressPauses) {
    for (let k = 1; k < kept.length; k += 1) {
      const gap = kept[k] - kept[k - 1];
      if (gap > PAUSE_GAP_MS) {
        const shift = gap - PAUSE_TARGET_MS;
        for (let m = k; m < kept.length; m += 1) kept[m] -= shift;
      }
    }
  }
  return points.slice(start, end + 1).map((row, i) => [...row.slice(0, -1), kept[i] / 1000]);
}
