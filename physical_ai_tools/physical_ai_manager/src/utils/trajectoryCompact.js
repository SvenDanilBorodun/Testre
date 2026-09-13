/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Contract-B trajectory rows ([j1..jn, grip, t_s]) rounded for transport.
//
// A full-precision JS/Python double serialises to ~19 characters, so a hand-
// guided recording outgrew BOTH 256 KiB caps it has to pass long before the
// recorder's own 120 s limit: the cloud trajectory validator (a save failed with
// a 413 after ~76 s of continuous 7-wide motion, ~66 s 8-wide) and the server's
// /workflow/start payload cap (the recording rides in the run payload).
//
// Joint values → 1e-4 rad, the time column → 1 ms. 1e-4 rad is ~15× finer than
// either servo family's encoder step (2π/4096 ≈ 1.5e-3 rad), so nothing the arm
// could reproduce is lost. Rounding is monotone, so the NON-DECREASING time
// column the server's extract_points requires stays non-decreasing.
//
// Mirrors physical_ai_server.py::_MANUAL_RECORD_JOINT_DECIMALS / _TIME_DECIMALS,
// which round at the sampler; this copy also shrinks recordings saved before
// that existed, and anything an older server image sends.

export const TRAJECTORY_JOINT_DECIMALS = 4;
export const TRAJECTORY_TIME_DECIMALS = 3;

function roundTo(value, decimals) {
  // Anything that is not a finite number passes through UNCHANGED, so the
  // validators downstream still see (and refuse) exactly what was recorded.
  if (typeof value !== 'number' || !Number.isFinite(value)) return value;
  const factor = 10 ** decimals;
  const rounded = Math.round(value * factor) / factor;
  // A magnitude so large that `value * factor` overflows must not turn a finite
  // (if absurd) value into Infinity — leave it for the validators to refuse.
  if (!Number.isFinite(rounded)) return value;
  // Normalise -0 so it serialises as "0", not "-0".
  return rounded === 0 ? 0 : rounded;
}

/**
 * @param {Array} points - Contract-B rows; the LAST element of each row is t_s.
 * @returns {Array} a new array of new rows (the input is never mutated);
 *   non-array input and non-array rows are returned as they are.
 */
export function compactTrajectoryPoints(points) {
  if (!Array.isArray(points)) return points;
  return points.map((row) => {
    if (!Array.isArray(row)) return row;
    const last = row.length - 1;
    return row.map((v, i) => roundTo(
      v, i === last ? TRAJECTORY_TIME_DECIMALS : TRAJECTORY_JOINT_DECIMALS,
    ));
  });
}
