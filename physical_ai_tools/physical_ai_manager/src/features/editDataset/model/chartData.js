// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The joint charts' data (spec §F5), pure: radians → degrees (the gripper too),
// a min/max down-sampling to at most 600 points per series that keeps every
// spike, and a y-range padded to at least 10°.

export const MAX_CHART_POINTS = 600;
export const MIN_Y_SPAN_DEG = 10;

const RAD_TO_DEG = 180 / Math.PI;

/** Radians → degrees. */
export function toDegrees(rad) {
  return Number(rad) * RAD_TO_DEG;
}

/** Column `k` of a `n × J` row array, in degrees. */
export function columnDegrees(rows, k) {
  const out = new Array(rows ? rows.length : 0);
  for (let i = 0; i < out.length; i += 1) {
    const row = rows[i];
    const v = row ? Number(row[k]) : NaN;
    out[i] = Number.isFinite(v) ? v * RAD_TO_DEG : null;
  }
  return out;
}

/**
 * At most `maxPoints` points `{i, v}` (i = frame index) that keep the shape:
 * the values are split into buckets of equal width and each bucket keeps its
 * minimum and its maximum, in frame order. Short series are returned whole.
 */
export function downsampleMinMax(values, maxPoints = MAX_CHART_POINTS) {
  const n = values ? values.length : 0;
  const pts = [];
  if (n === 0) return pts;
  if (n <= maxPoints) {
    for (let i = 0; i < n; i += 1) if (values[i] !== null && values[i] !== undefined) pts.push({ i, v: values[i] });
    return pts;
  }
  const buckets = Math.max(1, Math.floor(maxPoints / 2));
  const width = n / buckets;
  for (let b = 0; b < buckets; b += 1) {
    const start = Math.floor(b * width);
    const end = b === buckets - 1 ? n : Math.floor((b + 1) * width);
    let lo = -1;
    let hi = -1;
    for (let i = start; i < end; i += 1) {
      const v = values[i];
      if (v === null || v === undefined) continue;
      if (lo < 0 || v < values[lo]) lo = i;
      if (hi < 0 || v > values[hi]) hi = i;
    }
    if (lo < 0) continue;
    if (lo === hi) pts.push({ i: lo, v: values[lo] });
    else if (lo < hi) pts.push({ i: lo, v: values[lo] }, { i: hi, v: values[hi] });
    else pts.push({ i: hi, v: values[hi] }, { i: lo, v: values[lo] });
  }
  return pts;
}

/** `[lo, hi]` over every finite value of the series, at least 10° wide (centred). */
export function yDomain(...series) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const s of series) {
    for (const v of s || []) {
      if (v === null || v === undefined || !Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (!Number.isFinite(lo)) return [-MIN_Y_SPAN_DEG / 2, MIN_Y_SPAN_DEG / 2];
  if (hi - lo < MIN_Y_SPAN_DEG) {
    const c = (hi + lo) / 2;
    lo = c - MIN_Y_SPAN_DEG / 2;
    hi = c + MIN_Y_SPAN_DEG / 2;
  }
  const pad = (hi - lo) * 0.06;
  return [lo - pad, hi + pad];
}

/** True when the zero line lies inside the range (the chart draws it then). */
export function spansZero([lo, hi]) {
  return lo < 0 && hi > 0;
}

/** The follower (`state`) and leader (`action`) series of joint `k`, down-sampled, in degrees. */
export function jointChart(data, k) {
  const follower = columnDegrees(data && data.state, k);
  const leader = columnDegrees(data && data.action, k);
  const domain = yDomain(follower, leader);
  return {
    follower: downsampleMinMax(follower),
    leader: downsampleMinMax(leader),
    domain,
    zero: spansZero(domain),
    followerAll: follower,
    leaderAll: leader,
  };
}
