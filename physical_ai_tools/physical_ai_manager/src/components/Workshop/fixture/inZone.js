/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The printed work zone of the tower kit (spec 40 §3, decision D66) — the JS
// twin of scene_fixture.zone_az_hi / in_zone. ONE predicate: the mat
// generator, the simulator and the zone tests all ask it, and this copy is held
// equal to the Python one by the case table the server suite renders
// (sceneFixture.cases.json). The knots, radii and sector come from
// sceneFixture.json, never from a literal here.
//
// Coordinates are the fixture frame F: origin on the joint-1 axis at the table
// surface, x forward, y left, metres. Roboter Studio's world W is the arm's
// URDF base frame, x_W = x_F + base_axis_x (BASE_AXIS_X_M below; the OMX's
// joint-1 axis sits 11.25 mm behind its base origin, the V1's on it).
//
// Pure, no React, no three: the eager SimScene chunk imports it.

import fixture from './sceneFixture.json';

const C = fixture.constants;
const DEG = Math.PI / 180;

// Radius and azimuth tolerance of the predicate (§3: "both 1e-9 tolerant").
const ZONE_TOL = 1e-9;

// The contract's per-arm key for a capability manifest's urdf_asset_id
// (omx_full and omx_follower share the OMX's asset).
export const ARM_KEY_BY_ASSET = Object.freeze({ omx_f: 'omx', v1: 'v1' });

// The joint-1 axis's x in W per URDF asset: the server's ik.base_axis_x
// (ik_solver._J1_XYZ[0], v1_ik.BASE_AXIS_X_WORLD; inZone.test.js reads both).
export const BASE_AXIS_X_M = Object.freeze({ omx_f: -0.01125, v1: 0.0 });

const SPECS = Object.freeze({ omx: C.ARM_OMX, v1: C.ARM_V1 });

function assetOf(caps) {
  const id = caps && typeof caps === 'object' ? caps.urdf_asset_id : undefined;
  // No manifest (or none naming an asset) is the OMX, like armGeometry.
  return typeof id === 'string' && id ? id : 'omx_f';
}

/** The contract's arm table (ARM_OMX / ARM_V1) for a capability manifest, or null. */
export function armSpecFor(caps) {
  const key = ARM_KEY_BY_ASSET[assetOf(caps)];
  return key ? SPECS[key] : null;
}

/** W x of the arm's joint-1 axis, or null for an arm the contract does not know. */
export function baseAxisXFor(caps) {
  const asset = assetOf(caps);
  return Object.prototype.hasOwnProperty.call(BASE_AXIS_X_M, asset) ? BASE_AXIS_X_M[asset] : null;
}

/**
 * The cut's azimuth (deg) at radius r: linear between consecutive knots (radii
 * ascending), a knot's own value within the tolerance of its radius, the first
 * knot's value below it, +Infinity beyond the last knot (no cut there).
 */
function cutAz(knots, r) {
  if (!knots.length || r > knots[knots.length - 1][0] + ZONE_TOL) return Infinity;
  if (r <= knots[0][0]) return knots[0][1];
  for (let i = 0; i + 1 < knots.length; i += 1) {
    const [r0, a0] = knots[i];
    const [r1, a1] = knots[i + 1];
    if (r0 - ZONE_TOL <= r && r <= r1 + ZONE_TOL) {
      if (r1 - r0 <= 1e-12) return Math.min(a0, a1);
      const t = Math.min(1, Math.max(0, (r - r0) / (r1 - r0)));
      return a0 + (a1 - a0) * t;
    }
  }
  return Infinity;
}

/** The zone's tower-side edge at radius r (deg): the sector edge or the cut, whichever is smaller. */
export function zoneAzHi(spec, r) {
  return Math.min(spec.zone_sector_deg[1], cutAz(spec.zone_cut_knots, r));
}

/** True when (xF, yF) lies in the printed zone (radius and azimuth, both 1e-9 tolerant). */
export function inZone(spec, xF, yF) {
  if (typeof xF !== 'number' || typeof yF !== 'number') return false;
  if (!Number.isFinite(xF) || !Number.isFinite(yF)) return false;
  const r = Math.hypot(xF, yF);
  if (!(spec.zone_inner_m - ZONE_TOL <= r && r <= spec.zone_outer_m + ZONE_TOL)) return false;
  const az = Math.atan2(yF, xF) / DEG;
  return spec.zone_sector_deg[0] - ZONE_TOL <= az && az <= zoneAzHi(spec, r) + ZONE_TOL;
}

// The smallest angle between two bearings (deg), in [0, 180].
function degBetween(a, b) {
  return Math.abs((((a - b) % 360) + 540) % 360 - 180);
}

/**
 * (xF, yF) SNAPPED into the zone the way the simulator's reach shape always
 * snapped: the radius onto the ring, then the bearing onto the nearer edge the
 * zone has at that radius (the sector's low edge or the cut). A point already
 * inside is returned unchanged; the joint-1 axis itself lands on the inner
 * radius in the middle of the zone's bearings.
 */
export function clampIntoZone(spec, xF, yF) {
  if (inZone(spec, xF, yF)) return { x: xF, y: yF };
  const r = Math.hypot(xF, yF);
  const rr = Math.min(spec.zone_outer_m, Math.max(spec.zone_inner_m, Number.isFinite(r) ? r : 0));
  const lo = spec.zone_sector_deg[0];
  const hi = zoneAzHi(spec, rr);
  let az;
  if (!(r > 0) || !Number.isFinite(r)) {
    az = (lo + hi) / 2;
  } else {
    const t = Math.atan2(yF, xF) / DEG;
    if (t >= lo && t <= hi) az = t;
    else az = degBetween(t, lo) <= degBetween(t, hi) ? lo : hi;
  }
  return { x: rr * Math.cos(az * DEG), y: rr * Math.sin(az * DEG) };
}

/**
 * The zone's outline in F as [[x, y], …] (an open polygon; the drawer closes
 * it): the outer arc from the low sector edge to the tower-side edge, that edge
 * inward (the cut, sampled every `stepM` of radius with every knot radius
 * included), the inner arc back, and the low edge outward.
 */
export function zoneOutline(spec, stepM = 0.002) {
  const inner = spec.zone_inner_m;
  const outer = spec.zone_outer_m;
  const lo = spec.zone_sector_deg[0];
  const at = (r, az) => [r * Math.cos(az * DEG), r * Math.sin(az * DEG)];
  const arc = (r, a0, a1) => {
    const n = Math.max(2, Math.ceil(Math.abs(a1 - a0) / 2));
    const pts = [];
    for (let i = 0; i <= n; i += 1) pts.push(at(r, a0 + ((a1 - a0) * i) / n));
    return pts;
  };
  // The radii the tower-side edge passes, outer to inner, every knot included.
  const radii = new Set([outer, inner]);
  for (let r = outer; r > inner; r -= stepM) radii.add(r);
  spec.zone_cut_knots.forEach(([r]) => { if (r > inner && r < outer) radii.add(r); });
  const edge = [...radii].sort((a, b) => b - a).map((r) => at(r, zoneAzHi(spec, r)));
  return [
    ...arc(outer, lo, zoneAzHi(spec, outer)),
    ...edge.slice(1, -1),
    ...arc(inner, zoneAzHi(spec, inner), lo),
  ];
}
