/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Moved out of `components/Workshop/RunControls.jsx` so the run start, the
// simulator previews and the Sammlung share ONE reading of a recording row and
// ONE arm-family comparison.

// CONTRACT C — a fetched trajectory must reduce to { fps, points } for the run
// payload. The cloud row is expected to expose `points` (array) + `fps`
// directly (CONTRACT B), but tolerate a stringified `points_json` fallback so a
// small cloud-shape difference doesn't wedge a replay run. `robot_profile` (the
// migration-035 arm-family tag, widened by 039) is carried through so the caller
// can refuse a cross-profile replay BEFORE it reaches the runtime. Two different
// failures, and only the first is about width: an OMX recording on an Edu:1
// (5 arm joints -> the SAME 7-wide points) passes every width check there is and
// simply drives the wrong arm, while an OMX 7-wide recording on an
// edu6 rig otherwise dies later with the misleading „Aufnahme ist beschädigt.").
//
// A third shape: `services/workflowApi.js::getTrajectory` returns the row with
// the Contract-B object under `samples` ({ fps, points }). Its `points` win when
// `samples.points` is an array, with `samples.fps` preferred over a row-level
// `fps`; the tag is still the row's `robot_profile`.
export function normalizeTrajectory(t) {
  if (!t || typeof t !== 'object') return null;
  let points = Array.isArray(t.points) ? t.points : null;
  let fps = Number(t.fps) || 0;
  if (t.samples && typeof t.samples === 'object' && Array.isArray(t.samples.points)) {
    points = t.samples.points;
    fps = Number(t.samples.fps) || Number(t.fps) || 0;
  }
  if (!points && typeof t.points_json === 'string') {
    try {
      const parsed = JSON.parse(t.points_json);
      if (parsed && Array.isArray(parsed.points)) {
        points = parsed.points;
        if (!fps) fps = Number(parsed.fps) || 0;
      }
    } catch (_) { /* leave points null → caller errors */ }
  }
  if (!points || points.length === 0) return null;
  const robotProfile = typeof t.robot_profile === 'string' ? t.robot_profile : null;
  return { fps, points, robotProfile };
}

// Canonical arm-family id for a recording tag / rig identity. An absent/NULL tag
// is a LEGACY (pre-035) recording — always OMX by construction — so it maps to
// 'omx_f', and an unknown rig identity likewise falls back to 'omx_f' (the
// pre-edu6 default). A recording is replayable here only when its family matches
// the current rig's.
//
// Compare IDS, never widths. Since the Edu:1 (migration 039) two genuinely
// different arms share a Contract-B point width of 7, so a length-based shortcut
// here would silently re-open cross-arm replay for exactly that pair.
export function trajectoryMatchesRig(trajProfile, rigRobotType) {
  const traj = (typeof trajProfile === 'string' && trajProfile.trim()) || 'omx_f';
  const rig = (typeof rigRobotType === 'string' && rigRobotType.trim()) || 'omx_f';
  return traj === rig;
}
