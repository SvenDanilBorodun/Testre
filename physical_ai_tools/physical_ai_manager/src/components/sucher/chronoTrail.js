// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The chronophotography trail of the 3D twin (M6, Sucher §6.6), as a pure
// state machine that knows nothing of three.js — UrdfTwin owns the clones and
// feeds this the live joint values once per animation frame.
//
// Every `stepMs` at most, when the live pose moved more than CHRONO_MOVE_RAD
// on any joint since the last snapshot, the OLDEST ghost takes the live pose
// and starts to fade: opacity CHRONO_MAX_OPACITY · max(0, 1 − age/fadeMs).
// The first look at a pose only records it (a resting arm leaves no ghost).
// No motion → the ghosts fade out → `needsRender` turns false one frame after
// the last ghost reached 0, so the twin's paint-on-demand loop idles again.
// Reduced motion: no snapshots, every ghost at 0.

export const CHRONO_DEFAULTS = Object.freeze({ count: 8, stepMs: 120, fadeMs: 1200 });
export const CHRONO_MAX_OPACITY = 0.35;
export const CHRONO_MOVE_RAD = 0.01;

/** The `chronoTrail` prop as a config, or null when the trail is off. */
export function resolveChronoTrail(prop) {
  if (!prop) return null;
  const o = typeof prop === 'object' ? prop : {};
  const pick = (key, lo, hi) => {
    const v = Number(o[key]);
    return Number.isFinite(v) && v >= lo ? Math.min(hi, v) : CHRONO_DEFAULTS[key];
  };
  return {
    count: Math.floor(pick('count', 1, 32)),
    stepMs: pick('stepMs', 1, 60000),
    fadeMs: pick('fadeMs', 1, 60000),
  };
}

/** A fresh trail state for `config` (see resolveChronoTrail). */
export function createChronoTrail(config) {
  const cfg = config || CHRONO_DEFAULTS;
  return {
    cfg,
    slots: Array.from({ length: cfg.count }, () => ({ bornAt: null, pose: null })),
    next: 0,
    lastCheckAt: null,
    lastPose: null,
    opacities: new Array(cfg.count).fill(0),
    wasActive: false,
  };
}

function maxDelta(a, b) {
  let d = 0;
  a.forEach((value, name) => {
    const prev = b.get(name);
    const diff = prev === undefined ? Infinity : Math.abs(value - prev);
    if (diff > d) d = diff;
  });
  return d;
}

/**
 * Advance the trail to `now` (ms). `livePose` is a Map joint name → value, or a
 * function returning one (asked only when a snapshot is due, so an idle frame
 * builds nothing), or null when the robot has no pose yet; `reduced` stops
 * every snapshot.
 * Returns `{ snap, opacities, needsRender }`: `snap` is the slot index that
 * just took the live pose (the caller copies `state.slots[snap].pose` onto
 * that ghost) or -1; `opacities` one value per ghost; `needsRender` whether
 * the frame must be painted (a ghost visible, or one just hidden).
 */
export function chronoTick(state, now, livePose, { reduced = false } = {}) {
  const { cfg } = state;
  let snap = -1;
  // The pose is looked at once per `stepMs` at most, moving or not, so a
  // resting arm costs one comparison per step and nothing per frame.
  const due = !reduced && (state.lastCheckAt === null || now - state.lastCheckAt >= cfg.stepMs);
  const pose = due ? (typeof livePose === 'function' ? livePose() : livePose) : null;
  if (pose && pose.size > 0) {
    state.lastCheckAt = now;
    if (state.lastPose === null) {
      state.lastPose = new Map(pose);
    } else if (maxDelta(pose, state.lastPose) > CHRONO_MOVE_RAD) {
      snap = state.next;
      const slot = state.slots[snap];
      slot.pose = new Map(pose);
      slot.bornAt = now;
      state.next = (state.next + 1) % cfg.count;
      state.lastPose = new Map(pose);
    }
  }
  let active = false;
  for (let i = 0; i < cfg.count; i += 1) {
    const slot = state.slots[i];
    let op = 0;
    if (!reduced && slot.bornAt !== null) {
      const age = now - slot.bornAt;
      op = CHRONO_MAX_OPACITY * Math.max(0, 1 - age / cfg.fadeMs);
      if (op <= 0) slot.bornAt = null;
    } else if (reduced) {
      slot.bornAt = null;
    }
    state.opacities[i] = op;
    if (op > 0) active = true;
  }
  const needsRender = active || state.wasActive || snap >= 0;
  state.wasActive = active;
  return { snap, opacities: state.opacities, needsRender };
}

// ── D30: no trail on a software WebGL renderer ──────────────────────────────
// On a software rasteriser (SwiftShader, llvmpipe, Windows' „Microsoft Basic
// Render Driver") every ghost is a full extra draw of the arm on the CPU: the
// verifier measured a frame p95 of 308 ms with the trail against 65 ms
// without. UrdfTwin asks this ONCE, when it creates its renderer, and then
// builds no ghosts; a real GPU keeps the trail.

const SOFTWARE_RENDERER_RE = /swiftshader|llvmpipe|softpipe|basic render driver|software/i;

/** The WebGL renderer's name (the unmasked one where the browser gives it), or null. */
export function webglRendererName(gl) {
  if (!gl || typeof gl.getParameter !== 'function') return null;
  try {
    const ext = typeof gl.getExtension === 'function' ? gl.getExtension('WEBGL_debug_renderer_info') : null;
    const unmasked = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
    if (typeof unmasked === 'string' && unmasked) return unmasked;
  } catch {
    // fall back to the masked name
  }
  try {
    const name = gl.getParameter(gl.RENDERER);
    return typeof name === 'string' && name ? name : null;
  } catch {
    return null;
  }
}

/** Whether a renderer name is a software rasteriser. An unknown name is not. */
export function isSoftwareRenderer(name) {
  return typeof name === 'string' && SOFTWARE_RENDERER_RE.test(name);
}
