/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Vormachen (teaching on the real arm) — the pure entry gate and the timing
// constants useTeachSession runs on. Every number that mirrors a server value
// names its source; the ones a server change could silently invalidate are
// pinned by robotis_ai_setup/tests/test_teach_preview_velocity_floor.py.

import { DE } from '../blocks/messages_de';

/**
 * Why Vormachen cannot open right now, or null. ORDER IS THE CONTRACT: the
 * first matching reason is the one the student reads.
 *
 * @returns {'offline'|'running'|'preview'|'sim'|'handguide'|'leader'|null}
 */
export function teachEntryBlockReason({
  heartbeatStatus, runState, paused, simMode, jogHandGuideOn, previewActive, rsLeaderOn,
} = {}) {
  if (heartbeatStatus !== 'connected') return 'offline';
  if (runState === 'running' || paused === true) return previewActive ? 'preview' : 'running';
  if (simMode) return 'sim';
  if (jogHandGuideOn) return 'handguide';
  // Hand mode only: until leader-arm Vormachen lands, a live leader refuses the
  // overlay instead of letting the server refuse every key press.
  if (rsLeaderOn) return 'leader';
  return null;
}

export const TEACH_BLOCK_TITLES_DE = Object.freeze({
  offline: DE.TEACH_BLOCK_OFFLINE,
  running: DE.TEACH_BLOCK_RUNNING,
  preview: DE.TEACH_BLOCK_PREVIEW,
  sim: DE.TEACH_BLOCK_SIM,
  handguide: DE.TEACH_BLOCK_JOG,
  glide: DE.TEACH_BLOCK_GLIDE,
  leader: DE.TEACH_BLOCK_LEADER,
});

export const TEACH_COUNTDOWN_S = 3;
export const TEACH_SPACE_DEBOUNCE_MS = 400;
// Well inside the server's 30 s idle watchdog (_MANUAL_IDLE_RETORQUE_S).
export const TEACH_KEEPALIVE_MS = 15000;
export const TEACH_RECORD_MAX_S = 120; // mirror of physical_ai_server.py::RECORD_MAX_S
// mirror of handlers/trajectory.py::extract_points (`len(points) < 2` refuses)
export const TEACH_MIN_POINTS = 2;
// A /workshop/replay drive = lead-in + the take. The lead-in is build_segment
// stretched to maxΔq × 15/8 / (0.6 × v_limit) over a 1.5 s floor:
// ≤ 2π × 1.875 / (0.6 × 4.72) = 4.16 s on every shipped profile
// (v_limit 4.8 OMX, 5.45 edu6, 4.72 edu1).
export const TEACH_ROBOT_PREVIEW_LEAD_IN_MAX_MS = 4500;
// The SLOWEST shipped v_limit. A lower number only makes the estimate longer
// (the floor only ever extends), so it must be ≤ every profile's
// velocity_limit_rad_s and the 4.8 default.
export const TEACH_REPLAY_VELOCITY_FLOOR_RAD_S = 4.72;
// HINT ONLY, never an exit. _run_replay waits ≤ 30 s for _manual_lock, then
// _retorque_follower_or_keep_locked tries _set_follower_torque 3× — each ≤ 2 s
// wait_for_service + (OMX rail, torque not confirmed ON) 2 × ≤ 1 s rail-match
// wait + 0.2 s settle + 0.07 s + a 3 s call deadline ≈ 7.3 s — so the drive can
// start ≈ 52 s after the answer, and the whole sequence runs inside
// `with self._dxl_torque_lock:` (no timeout), so a concurrent torque switch
// makes the start UNBOUNDED. No timer can prove „never moved"; past this the
// banner adds TEACH_ROBOT_PREVIEW_NO_MOTION and „Stopp" is the way out.
export const TEACH_ROBOT_PREVIEW_NO_MOTION_HINT_MS = 52000;
export const TEACH_ROBOT_PREVIEW_SETTLE_DELTA_RAD = 0.01; // LeaderToggle PREP_SETTLE_DELTA_RAD
export const TEACH_ROBOT_PREVIEW_SETTLE_STEP_MS = 150; // samples judged on this spacing, by arrival
export const TEACH_ROBOT_PREVIEW_SETTLE_SAMPLES = 4;
// No sample for this long = the feed is UNKNOWN, never „still".
export const TEACH_ROBOT_PREVIEW_FEED_STALE_MS = 1000;
// After a CONFIRMED Stopp the chunk already published
// (DEFAULT_CHUNK_DURATION_S 1.0) plays out.
export const TEACH_ROBOT_PREVIEW_STOP_TAIL_MAX_MS = 2000;
// WP12b: detector debounce 150 ms + status + rosbridge latency, ~6× margin.
export const TEACH_LEADER_COLLISION_GRACE_MS = 1000;

// handlers/trajectory.py: DEFAULT_FPS (1/30 for a non-positive step) and
// _MIN_PAIR_DT_S; trajectory_builder._VELOCITY_SAFETY_FRACTION.
const REPLAY_FALLBACK_DT_S = 1 / 30;
const REPLAY_MIN_PAIR_DT_S = 0.001;
const REPLAY_VELOCITY_SAFETY_FRACTION = 0.6;

/**
 * Upper bound (ms) of a /workshop/replay drive: the lead-in bound plus the
 * resegmented take, mirroring handlers/trajectory.py::resegment_trajectory pair
 * by pair — every recorded pair is STRETCHED by build_linear_segment until no
 * column (gripper included, time excluded) moves faster than 0.6 × v_limit, so
 * a fast take replays longer than it was recorded.
 *
 * @param {Array<Array<number>>} rows Contract-B rows `[q…, grip, t_s]`.
 * @param {number} speed the replay speed multiplier (divides dt, never the floor).
 * @returns {number} whole milliseconds.
 */
export function replayDriveEstimateMs(rows, speed = 1.0) {
  if (!Array.isArray(rows) || rows.length < 2) return TEACH_ROBOT_PREVIEW_LEAD_IN_MAX_MS;
  const s = Number(speed) > 0 && Number.isFinite(Number(speed)) ? Number(speed) : 1.0;
  const vSafe = REPLAY_VELOCITY_SAFETY_FRACTION * TEACH_REPLAY_VELOCITY_FLOOR_RAD_S;
  let totalS = 0;
  for (let i = 0; i < rows.length - 1; i += 1) {
    const r0 = rows[i];
    const r1 = rows[i + 1];
    if (!Array.isArray(r0) || !Array.isArray(r1) || r0.length < 2 || r1.length < 2) continue;
    let dt = (Number(r1[r1.length - 1]) - Number(r0[r0.length - 1])) / s;
    if (!Number.isFinite(dt) || dt <= 0) dt = REPLAY_FALLBACK_DT_S;
    else if (dt < REPLAY_MIN_PAIR_DT_S) dt = REPLAY_MIN_PAIR_DT_S;
    const cols = Math.min(r0.length, r1.length) - 1;
    let maxDelta = 0;
    for (let c = 0; c < cols; c += 1) {
      const d = Math.abs(Number(r1[c]) - Number(r0[c]));
      if (Number.isFinite(d) && d > maxDelta) maxDelta = d;
    }
    totalS += Math.max(dt, maxDelta / vSafe);
  }
  const ms = TEACH_ROBOT_PREVIEW_LEAD_IN_MAX_MS + totalS * 1000;
  // Round to a micro-millisecond first so float noise (0.04 × 1000 =
  // 40.000000000000004) cannot add a whole millisecond through the ceil.
  return Math.ceil(Math.round(ms * 1000) / 1000);
}
