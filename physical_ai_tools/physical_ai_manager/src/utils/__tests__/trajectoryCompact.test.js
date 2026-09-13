/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import {
  compactTrajectoryPoints,
  TRAJECTORY_JOINT_DECIMALS,
  TRAJECTORY_TIME_DECIMALS,
} from '../trajectoryCompact';

describe('compactTrajectoryPoints', () => {
  test('rounds joints to 1e-4 rad and the LAST column (t_s) to 1 ms', () => {
    expect(TRAJECTORY_JOINT_DECIMALS).toBe(4);
    expect(TRAJECTORY_TIME_DECIMALS).toBe(3);
    const out = compactTrajectoryPoints([
      [0.123456789, -1.570796327, 1.570796327, 0.00004, -0.99996, 0.8, 12.3456789],
    ]);
    expect(out).toEqual([[0.1235, -1.5708, 1.5708, 0, -1, 0.8, 12.346]]);
  });

  test('serialises short — the whole point', () => {
    const row = [0.12345678901234, -1.2345678901234, 2.3456789012345,
      -0.0000001, 0.5, 0.80000000001, 42.12345678901];
    expect(JSON.stringify(compactTrajectoryPoints([row])))
      .toBe('[[0.1235,-1.2346,2.3457,0,0.5,0.8,42.123]]');
  });

  test('the width follows each row (8-wide edu6 rows keep their layout)', () => {
    const out = compactTrajectoryPoints([[1, 2, 3, 4, 5, 6, 1.23456, 9.87654]]);
    expect(out[0]).toHaveLength(8);
    expect(out[0][6]).toBe(1.2346);
    expect(out[0][7]).toBe(9.877);
  });

  test('keeps a non-decreasing time column non-decreasing', () => {
    const rows = [0.0404, 0.0405, 0.0809, 0.08094, 0.1204].map((t) => [0, 0, t]);
    const times = compactTrajectoryPoints(rows).map((r) => r[2]);
    for (let i = 1; i < times.length; i += 1) {
      expect(times[i]).toBeGreaterThanOrEqual(times[i - 1]);
    }
  });

  test('passes anything that is not a finite number through for the validators to refuse', () => {
    const odd = [[NaN, Infinity, 'x', null, 1e308, 0.5]];
    expect(compactTrajectoryPoints(odd)).toEqual([[NaN, Infinity, 'x', null, 1e308, 0.5]]);
    expect(compactTrajectoryPoints(null)).toBeNull();
    expect(compactTrajectoryPoints([5, [1.23456, 2]])).toEqual([5, [1.2346, 2]]);
  });

  test('never mutates its input', () => {
    const rows = [[0.123456, 1.0000001]];
    const snapshot = JSON.stringify(rows);
    compactTrajectoryPoints(rows);
    expect(JSON.stringify(rows)).toBe(snapshot);
  });
});
