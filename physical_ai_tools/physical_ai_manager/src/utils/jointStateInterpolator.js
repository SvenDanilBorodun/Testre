/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// ── JointState stream → a smooth pose for the 3D twin ─────────────────────────
// ONE implementation for every UrdfTwin mount: the Startseite hero, Aufnahme,
// the Roboter-Studio dock (all the real /joint_states) and the simulator stage
// (/sim/joint_states).
//
// WHY. The twin used to call setJointValue with each message as it landed, so it
// could only ever move in steps: 10 per second on the real arm (throttled from
// 100 Hz), and on the simulator — which sent each second of motion as ONE 30-pose
// burst until 2026-09-11 — about two per second, each up to 48° at once. A
// renderer that draws at 60 Hz needs a pose for EVERY frame, not for every
// message.
//
// HOW — snapshot interpolation, the netcode technique:
//   * every sample is placed on the PUBLISHER's timeline (header.stamp), not on
//     its arrival time. Arrival is distorted by the websocket and by whatever
//     the browser's main thread was busy with (Blockly, a React render, GC); the
//     stamp says when the pose was true. The sim stamps each waypoint with the
//     time it was scheduled for, the real arm with its controller cycle.
//   * the render clock runs a fixed INTERP_DELAY_MS behind the newest data, so
//     it almost always sits BETWEEN two received samples and the pose is a plain
//     linear blend of them — joint space, which is also what the real
//     JointTrajectoryController interpolates in.
//   * the server→browser clock offset is the sliding-window MINIMUM of
//     (arrival − stamp): the fastest delivery seen recently is the best estimate
//     of "clock difference + pure transport". This absorbs any constant offset —
//     a WSL2 container clock, a Pi or a Jetson on the LAN — without NTP.
//
// Deliberate behaviours, each of which a "simplification" would break:
//   * NO EXTRAPOLATION. Past the newest sample the pose HOLDS. Predicting where
//     an arm will be is how a twin shows motion that never happened.
//   * STEP, never glide, across a gap longer than INTERP_MAX_GAP_MS, and per
//     joint across a jump that would need more than INTERP_MAX_JOINT_SPEED_RAD_S.
//     A resumed stream, or a full-circle encoder wrapping from +π to −π, is a
//     discontinuity in the data; blending it would draw an arm sweeping through
//     poses it never took (the wrap: a full turn the wrong way). A TELEPORT that
//     is neither — the simulator's reset to its Grundstellung, which can land
//     only ~100 ms after the last pose and under the speed limit — is announced
//     by the PUBLISHER instead: physical_ai_server::_publish_sim_teleport re-sends
//     the current pose 3 ms before the jump, so the gap in front of the jump is
//     one no joint speed can fill. Inferring it here from the size of the jump
//     alone would also break a genuinely fast move across a dropped sample.
//   * the render clock NEVER RUNS BACKWARDS. When the offset estimate rises
//     (the old minimum aged out) the render time freezes briefly instead of
//     rewinding, so a pose is never shown going back in time.
//   * a joint missing from a message KEEPS its last value (carry-forward), as
//     setJointValue did; non-finite values are ignored, as before.
//   * a stamp that jumps BACKWARDS by more than BACKWARD_RESET_MS starts a new
//     timeline (the container clock stepped back); a slightly out-of-order one
//     is DROPPED — its neighbours already bracket it, and resetting on it would
//     throw away the whole buffer for one late sample.
//   * a stream without stamps (header zero or absent) falls back to arrival
//     time, i.e. exactly the old snap-per-message behaviour plus the delay.
//
// Pure: no three.js, no React, no timers — `frame(nowMs)` is driven by the
// caller's animation loop, so this module stays in unit tests' reach and out of
// the entry bundle's weight class (simConstants' rule applies: keep it
// three-free).

// How far the render clock trails the newest data. Must exceed the sample
// spacing (~33 ms at the twins' ~30 Hz) plus the delivery jitter a busy browser
// adds, or the clock catches up with the data and the pose stalls between
// samples. 100 ms is three samples of margin; it is also the whole visual
// latency this adds, which a monitor view does not notice.
export const INTERP_DELAY_MS = 100;
// A gap wider than this between two samples is a paused / resumed stream, not
// motion to be blended (the sim heartbeat's 500 ms repeats, a run pausing
// between moves).
export const INTERP_MAX_GAP_MS = 250;
// A per-joint change between two consecutive samples that would need a joint
// speed above this is a discontinuity (a wrap, a teleport), not motion. 2.5× the
// fastest joint limit in the three URDF assets (edu1 joint2/joint3, 7.87 rad/s;
// the OMX is 4.8, edu6 5.45). A 2π encoder wrap needs ≥ 25 rad/s even across the
// widest gap that is still blended (INTERP_MAX_GAP_MS), so it always steps.
export const INTERP_MAX_JOINT_SPEED_RAD_S = 20;

const OFFSET_WINDOW_MS = 5000;
// Two samples this close together are one (a burst from an older server image).
const DUPLICATE_STAMP_MS = 0.5;
// A stamp this far before the newest one is a new timeline, not a late sample.
const BACKWARD_RESET_MS = 500;
// Hard cap on buffered samples. The render loop spends them; this bounds a
// background tab, whose animation frames stop while its messages keep coming.
const MAX_SAMPLES = 64;

// header.stamp → epoch milliseconds, or null for "no stamp". ROS 2 sends
// {sec, nanosec}; a ROS 1-style {secs, nsecs} is accepted too. An unset stamp
// (0, 0) counts as absent.
export function stampToMs(msg) {
  const stamp = msg && msg.header && msg.header.stamp;
  if (!stamp || typeof stamp !== 'object') return null;
  let sec = null;
  if (Number.isFinite(stamp.sec)) sec = stamp.sec;
  else if (Number.isFinite(stamp.secs)) sec = stamp.secs;
  if (sec === null) return null;
  let nsec = 0;
  if (Number.isFinite(stamp.nanosec)) nsec = stamp.nanosec;
  else if (Number.isFinite(stamp.nsecs)) nsec = stamp.nsecs;
  const ms = sec * 1000 + nsec / 1e6;
  return ms > 0 ? ms : null;
}

export function createJointStateInterpolator({
  delayMs = INTERP_DELAY_MS,
  maxGapMs = INTERP_MAX_GAP_MS,
  maxJointSpeedRadS = INTERP_MAX_JOINT_SPEED_RAD_S,
  offsetWindowMs = OFFSET_WINDOW_MS,
} = {}) {
  let samples = []; // [{ t, pose: Map<name, rad> }], ascending t (publisher clock)
  let offsets = []; // monotonic deque [{ at, off }] for the sliding-window minimum
  let offset = null; // arrival − stamp, ms; null until a stamped sample arrives
  let lastRenderT = -Infinity;
  let version = 0; // bumped by every accepted push
  // Reused output: frame() hands back THIS map, so a caller must not keep it
  // past the call (UrdfTwin copies what it applies).
  const out = new Map();

  function reset() {
    samples = [];
    offsets = [];
    offset = null;
    lastRenderT = -Infinity;
  }

  function trackOffset(arrivalMs, off) {
    while (offsets.length && offsets[offsets.length - 1].off >= off) offsets.pop();
    offsets.push({ at: arrivalMs, off });
    while (offsets.length > 1 && offsets[0].at < arrivalMs - offsetWindowMs) {
      offsets.shift();
    }
    offset = offsets[0].off;
  }

  // Accept one sensor_msgs/JointState. Returns true when it carried at least one
  // finite joint value (anything else leaves the buffer untouched).
  function push(msg, arrivalMs) {
    if (!msg || !Array.isArray(msg.name) || !Array.isArray(msg.position)) return false;
    const n = Math.min(msg.name.length, msg.position.length);
    const values = [];
    for (let i = 0; i < n; i += 1) {
      const name = msg.name[i];
      const v = msg.position[i];
      if (typeof name === 'string' && typeof v === 'number' && Number.isFinite(v)) {
        values.push([name, v]);
      }
    }
    if (values.length === 0) return false;

    const stamp = stampToMs(msg);
    let t;
    if (stamp !== null) {
      const newest = samples.length ? samples[samples.length - 1] : null;
      if (newest && stamp < newest.t - BACKWARD_RESET_MS) reset();
      else if (newest && stamp < newest.t - DUPLICATE_STAMP_MS) return false;
      trackOffset(arrivalMs, arrivalMs - stamp);
      t = stamp;
    } else {
      t = arrivalMs - (offset === null ? 0 : offset);
      const newest = samples.length ? samples[samples.length - 1] : null;
      if (newest && t < newest.t) t = newest.t;
    }

    const newest = samples.length ? samples[samples.length - 1] : null;
    const pose = new Map(newest ? newest.pose : undefined);
    values.forEach(([name, v]) => pose.set(name, v));
    if (newest && t - newest.t <= DUPLICATE_STAMP_MS) {
      newest.pose = pose;
    } else {
      samples.push({ t, pose });
      if (samples.length > MAX_SAMPLES) samples = samples.slice(-MAX_SAMPLES);
    }
    version += 1;
    return true;
  }

  function copyInto(pose) {
    out.clear();
    pose.forEach((v, name) => out.set(name, v));
    return out;
  }

  // The pose to draw at local time `nowMs` (the caller's performance.now()).
  //   * `pending` — newer data is still waiting to be shown, so the caller has to
  //     keep asking every frame;
  //   * `moving`  — the returned pose is a BLEND that changes from frame to frame.
  //     False whenever it is a sample verbatim (the newest one, a hold before the
  //     first, a hold across a gap) or a blend of two samples that agree — i.e.
  //     the DATA has stopped moving, which is the moment a caller that skips
  //     sub-threshold changes must land exactly. That is not the same as
  //     `pending`: a live stream from a resting arm is pending forever.
  function frame(nowMs) {
    const n = samples.length;
    if (n === 0) return { pose: null, pending: false, moving: false };
    let rt = nowMs - (offset === null ? 0 : offset) - delayMs;
    if (rt < lastRenderT) rt = lastRenderT;
    lastRenderT = rt;

    const newest = samples[n - 1];
    if (rt >= newest.t) {
      if (n > 1) samples = [newest]; // everything older is spent
      return { pose: copyInto(newest.pose), pending: false, moving: false };
    }
    if (rt <= samples[0].t) {
      return { pose: copyInto(samples[0].pose), pending: true, moving: false };
    }
    let i = 0;
    while (i + 1 < n && samples[i + 1].t <= rt) i += 1;
    if (i > 0) samples = samples.slice(i);
    const a = samples[0];
    const b = samples[1];
    const gap = b.t - a.t;
    if (gap > maxGapMs) {
      return { pose: copyInto(a.pose), pending: true, moving: false };
    }
    const alpha = gap > 0 ? (rt - a.t) / gap : 1;
    // The widest change this gap can hold as MOTION; beyond it the joint steps
    // at b's time. For a gap near zero the limit is near zero too — harmless,
    // because stepping and blending across a fraction of a millisecond draw the
    // same frame.
    const maxStep = (maxJointSpeedRadS * gap) / 1000;
    let moving = false;
    out.clear();
    b.pose.forEach((vb, name) => {
      const va = a.pose.get(name);
      if (va === undefined) out.set(name, vb);
      else if (Math.abs(vb - va) > maxStep) out.set(name, va);
      else {
        if (vb !== va) moving = true;
        out.set(name, va + (vb - va) * alpha);
      }
    });
    return { pose: out, pending: true, moving };
  }

  return {
    push,
    frame,
    reset,
    get size() { return samples.length; },
    get version() { return version; },
    get offsetMs() { return offset; },
  };
}
