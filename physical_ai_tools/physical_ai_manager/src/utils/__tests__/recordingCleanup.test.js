/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { describe, it, expect } from 'vitest';
import {
  ACTIVITY_BIN_MS,
  analyzeTake,
  applyCleanup,
  fallJointIndices,
} from '../recordingCleanup';

const OMX = { urdf_asset_id: 'omx_f', arm_joints: 5 };
const EDU6 = { urdf_asset_id: 'edu6', arm_joints: 6 };

// A still OMX row (7 wide) at time t seconds.
const row7 = (t, q = [0, 0.5, -0.5, 0, 0, 0.8]) => [...q, t];

// Deterministic PRNG (mulberry32).
function prng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const timeCol = (rows) => rows.map((r) => r[r.length - 1]);
const msCol = (rows) => timeCol(rows).map((t) => Math.round(t * 1000));

describe('recordingCleanup — the lead gap', () => {
  it('compresses a 2.346 s lead gap to exactly 0.300 s and shifts every later row by 2.046 s', () => {
    const rows = [row7(0), row7(2.346, [0.1, 0.5, -0.5, 0, 0, 0.8]), row7(2.4, [0.2, 0.5, -0.5, 0, 0, 0.8]),
      row7(3.1, [0.3, 0.5, -0.5, 0, 0, 0.8])];
    const out = applyCleanup(rows);
    expect(timeCol(out)).toEqual([0, 0.3, 0.354, 1.054]);
    expect(msCol(out).slice(1).map((m, i) => msCol(rows)[i + 1] - m)).toEqual([2046, 2046, 2046]);
    expect(analyzeTake(rows, OMX).leadTrimMs).toBe(2046);
    expect(analyzeTake(rows, OMX).leadGapMs).toBe(2346);
  });

  it('does not shift at a lead gap of exactly 300 ms, nor below', () => {
    for (const gap of [0.3, 0.12]) {
      const rows = [row7(0), row7(gap, [0.1, 0.5, -0.5, 0, 0, 0.8]), row7(gap + 0.5)];
      expect(timeCol(applyCleanup(rows))).toEqual(timeCol(rows));
      expect(analyzeTake(rows, OMX).leadTrimMs).toBe(0);
    }
  });

  it('trimLead:false keeps the gap', () => {
    const rows = [row7(0), row7(2), row7(2.5)];
    expect(timeCol(applyCleanup(rows, { trimLead: false }))).toEqual([0, 2, 2.5]);
  });

  it('re-bases the kept slice to start at 0', () => {
    const rows = [row7(10.0), row7(10.1), row7(10.25), row7(10.5)];
    expect(timeCol(applyCleanup(rows))).toEqual([0, 0.1, 0.25, 0.5]);
    expect(timeCol(applyCleanup(rows, { startIndex: 2 }))).toEqual([0, 0.25]);
  });
});

describe('recordingCleanup — invariants on randomized takes', () => {
  it('the time column stays non-decreasing and every positive step stays ≥ 1 ms (200 takes)', () => {
    const rand = prng(20260915);
    for (let n = 0; n < 200; n += 1) {
      const count = 2 + Math.floor(rand() * 60);
      const width = rand() < 0.5 ? 7 : 8;
      let ms = Math.floor(rand() * 5000);
      const rows = [];
      for (let i = 0; i < count; i += 1) {
        const r = rand();
        // Mostly short steps, some zero steps, some long gaps.
        ms += r < 0.1 ? 0 : r < 0.8 ? 1 + Math.floor(rand() * 60) : 300 + Math.floor(rand() * 4000);
        const q = Array.from({ length: width - 1 }, () => rand() * 2 - 1);
        rows.push([...q, ms / 1000]);
      }
      const start = Math.floor(rand() * count);
      const end = Math.floor(rand() * count);
      const out = applyCleanup(rows, {
        trimLead: rand() < 0.8, startIndex: start, endIndex: end, compressPauses: rand() < 0.5,
      });
      expect(out.length).toBeGreaterThanOrEqual(2);
      const outMs = msCol(out);
      expect(outMs[0]).toBe(0);
      const inMs = msCol(rows);
      const offset = Math.min(Math.max(start, 0), count - 2);
      for (let i = 1; i < out.length; i += 1) {
        const step = outMs[i] - outMs[i - 1];
        const wasPositive = inMs[offset + i] - inMs[offset + i - 1] > 0;
        expect(step).toBeGreaterThanOrEqual(wasPositive ? 1 : 0);
        // Joint columns are the source row's, bit-identical.
        expect(out[i].slice(0, -1)).toEqual(rows[offset + i].slice(0, -1));
      }
    }
  });

  it('widths 7 and 8 keep every joint value bit-identical and the row width', () => {
    for (const width of [7, 8]) {
      const rows = [0, 0.5, 2.9, 3.0].map((t, i) => [
        ...Array.from({ length: width - 1 }, (_, j) => 0.123456789 * (i + 1) + j / 7), t]);
      const out = applyCleanup(rows, { compressPauses: true });
      expect(out.every((r) => r.length === width)).toBe(true);
      out.forEach((r, i) => {
        for (let j = 0; j < width - 1; j += 1) expect(Object.is(r[j], rows[i][j])).toBe(true);
      });
    }
  });

  it('never mutates the input', () => {
    const rows = [row7(0), row7(2), row7(2.5)];
    const copy = JSON.parse(JSON.stringify(rows));
    applyCleanup(rows, { compressPauses: true, startIndex: 1 });
    expect(rows).toEqual(copy);
  });
});

describe('recordingCleanup — the end handle and the fall onset', () => {
  // 3 s still-ish take at 40 ms, then the last 400 ms joint2 falls at −4 rad/s.
  function fallTake(speed) {
    const rows = [];
    let q2 = 0.5;
    for (let ms = 0; ms <= 3000; ms += 40) rows.push(row7(ms / 1000, [0.001 * ms / 40, q2, -0.5, 0, 0, 0.8]));
    const onset = rows.length - 1;
    for (let ms = 3040; ms <= 3400; ms += 40) {
      q2 -= speed * 0.04;
      rows.push(row7(ms / 1000, [0, q2, -0.5, 0, 0, 0.8]));
    }
    return { rows, onset };
  }

  it('a monotone fall faster than 2.5 rad/s suggests ending at the row before it', () => {
    const { rows, onset } = fallTake(4);
    expect(analyzeTake(rows, OMX).suggestedEndIndex).toBe(onset);
  });

  it('slow lowering (1 rad/s) suggests nothing', () => {
    expect(analyzeTake(fallTake(1).rows, OMX).suggestedEndIndex).toBeNull();
  });

  it('a fast move that reverses before the end suggests nothing', () => {
    const rows = [row7(0), row7(0.04)];
    let q2 = 0.5;
    for (let ms = 80; ms <= 400; ms += 40) { q2 -= 0.2; rows.push(row7(ms / 1000, [0, q2, -0.5, 0, 0, 0.8])); }
    for (let ms = 440; ms <= 800; ms += 40) { q2 += 0.01; rows.push(row7(ms / 1000, [0, q2, -0.5, 0, 0, 0.8])); }
    expect(analyzeTake(rows, OMX).suggestedEndIndex).toBeNull();
  });

  it('a fast move outside the last 800 ms is not a fall', () => {
    const rows = [row7(0), row7(0.04, [0, 0.3, -0.5, 0, 0, 0.8]), row7(0.08, [0, 0.1, -0.5, 0, 0, 0.8])];
    for (let ms = 1000; ms <= 3000; ms += 500) rows.push(row7(ms / 1000, [0, 0.1 - ms / 1e5, -0.5, 0, 0, 0.8]));
    expect(analyzeTake(rows, OMX).suggestedEndIndex).toBeNull();
  });

  it('only the profile\'s fall joints count (edu6 watches joint5, not joint4)', () => {
    const fall = (idx) => {
      const rows = [row7(0, [0, 0, 0, 0, 0, 0, 1]), row7(0.5, [0, 0, 0, 0, 0, 0, 1]),
        row7(1.0, [0, 0, 0, 0, 0, 0, 1])];
      for (let i = 1; i <= 5; i += 1) {
        const q = [0, 0, 0, 0, 0, 0, 1];
        q[idx] = -0.2 * i;
        rows.push([...q, 1.0 + 0.04 * i]);
      }
      return rows;
    };
    expect(analyzeTake(fall(4), EDU6).suggestedEndIndex).toBe(2);
    expect(analyzeTake(fall(3), EDU6).suggestedEndIndex).toBeNull();
    expect(analyzeTake(fall(3), OMX).suggestedEndIndex).toBe(2);
  });

  it('a short take, or a single fast move, is never trimmed down to its start', () => {
    const fastOnly = [row7(0, [0, 0.5, 0, 0, 0, 0.8]), row7(0.04, [0, 0.3, 0, 0, 0, 0.8]),
      row7(0.08, [0, 0.1, 0, 0, 0, 0.8])];
    expect(analyzeTake(fastOnly, OMX).suggestedEndIndex).toBeNull();
    // 600 ms of take, then a real fall: still too little before the onset.
    const rows = [row7(0), row7(0.3), row7(0.6)];
    let q2 = 0.5;
    for (let ms = 640; ms <= 800; ms += 40) { q2 -= 0.2; rows.push(row7(ms / 1000, [0, q2, -0.5, 0, 0, 0.8])); }
    expect(analyzeTake(rows, OMX).suggestedEndIndex).toBeNull();
    // The same fall after 800 ms of take is suggested.
    const longer = [row7(0), row7(0.4), row7(0.8)];
    q2 = 0.5;
    for (let ms = 840; ms <= 1000; ms += 40) { q2 -= 0.2; longer.push(row7(ms / 1000, [0, q2, -0.5, 0, 0, 0.8])); }
    expect(analyzeTake(longer, OMX).suggestedEndIndex).toBe(2);
  });

  it('endIndex slices the tail; the handles clamp to at least 2 rows', () => {
    const rows = [0, 0.1, 0.2, 0.3, 0.4].map((t) => row7(t));
    expect(applyCleanup(rows, { endIndex: 2 })).toHaveLength(3);
    expect(applyCleanup(rows, { startIndex: 4 })).toHaveLength(2);
    expect(applyCleanup(rows, { startIndex: 3, endIndex: 1 })).toHaveLength(2);
    expect(applyCleanup(rows, { startIndex: 0, endIndex: 0 })).toHaveLength(2);
    expect(applyCleanup(rows, { startIndex: -5, endIndex: 99 })).toHaveLength(5);
  });
});

describe('recordingCleanup — pauses', () => {
  const rows = [row7(0), row7(0.2, [0.1, 0.5, -0.5, 0, 0, 0.8]), row7(1.7, [0.2, 0.5, -0.5, 0, 0, 0.8]),
    row7(1.8, [0.3, 0.5, -0.5, 0, 0, 0.8]), row7(4.3, [0.4, 0.5, -0.5, 0, 0, 0.8])];

  it('are off by default', () => {
    expect(timeCol(applyCleanup(rows))).toEqual([0, 0.2, 1.7, 1.8, 4.3]);
  });

  it('compressPauses turns every gap over 1 s into 0.5 s', () => {
    expect(timeCol(applyCleanup(rows, { compressPauses: true }))).toEqual([0, 0.2, 0.7, 0.8, 1.3]);
    expect(analyzeTake(rows, OMX).pauses).toEqual([{ index: 2, gapMs: 1500 }, { index: 4, gapMs: 2500 }]);
  });

  it('a gap of exactly 1 s is not a pause', () => {
    const r = [row7(0), row7(0.1), row7(1.1)];
    expect(timeCol(applyCleanup(r, { compressPauses: true }))).toEqual([0, 0.1, 1.1]);
  });
});

describe('recordingCleanup — analysis details', () => {
  it('fallJointIndices per asset id', () => {
    expect(fallJointIndices(OMX)).toEqual([1, 2, 3]);
    expect(fallJointIndices(null)).toEqual([1, 2, 3]);
    expect(fallJointIndices(EDU6)).toEqual([1, 2, 4]);
    expect(fallJointIndices({ urdf_asset_id: 'edu1', arm_joints: 5 })).toEqual([1, 2, 3]);
    expect(fallJointIndices({ urdf_asset_id: 'future', arm_joints: 4 })).toEqual([1, 2, 3]);
    expect(fallJointIndices({ urdf_asset_id: 'future', arm_joints: 3 })).toEqual([1, 2]);
  });

  it('activity sums |Δq| over every joint column into the bin of each pair\'s END time', () => {
    const r = [
      [0, 0, 0, 0, 0, 0, 0],
      [0.1, 0, 0, 0, 0, -0.2, 0.05], // ends at 50 ms → bin 0: 0.3
      [0.1, 0.3, 0, 0, 0, -0.2, 0.25], // ends at 250 ms → bin 1: 0.3
      [0.1, 0.3, 0, 0, 0.5, -0.2, 0.3], // ends at 300 ms → bin 1: +0.5
      [0.1, 0.3, 0, 0, 0.5, 0.2, 0.5], // ends at 500 ms → bin 2: 0.4
    ];
    const a = analyzeTake(r, OMX);
    expect(a.durationMs).toBe(500);
    expect(a.activity).toHaveLength(Math.ceil(500 / ACTIVITY_BIN_MS));
    expect(a.activity.map((v) => Math.round(v * 1000) / 1000)).toEqual([0.3, 0.8, 0.4]);
    expect(a.width).toBe(7);
    expect(a.timesMs).toEqual([0, 50, 250, 300, 500]);
  });

  it('exactly 2 rows stay 2 rows; a 1-row input comes back unchanged', () => {
    const two = [row7(0), row7(0.5, [0.2, 0.5, -0.5, 0, 0, 0.8])];
    expect(applyCleanup(two)).toHaveLength(2);
    expect(analyzeTake(two, OMX).suggestedEndIndex).toBeNull();
    const one = [row7(4.2)];
    expect(applyCleanup(one)).toEqual(one);
    expect(() => analyzeTake(one, OMX)).not.toThrow();
    expect(analyzeTake(one, OMX).suggestedEndIndex).toBeNull();
  });
});
