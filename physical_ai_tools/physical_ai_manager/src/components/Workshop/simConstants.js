/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Shared sim-mode constants used by BOTH the eagerly-loaded SimScene chunk (the
// 2D top-down table editor) AND the lazy UrdfTwin chunk (the 3D twin). It is the
// single source of truth for the reach-annulus radii (the 2D SVG ring and the 3D
// table ring must agree) and the fallback object appearance (so a type the
// catalog does not describe renders identically in both panes).
//
// DEPENDENCY-FREE BY DESIGN: this module must NEVER import `three`. It is pulled
// into the entry bundle by SimScene (statically imported from the eager
// WorkshopPage); a `three` import here would drag three.js (~600 KB) into the
// entry chunk the white-screen CI greps watch — three/urdf-loader must stay
// exclusive to the lazy UrdfTwin chunk. Keep the values plain numbers/strings.

import sceneFixture from './fixture/sceneFixture.json';
import { armSpecFor, baseAxisXFor, zoneOutline } from './fixture/inZone';

// Strict-vertical reach annulus SHOWN TO THE STUDENT (the graspable ring on the
// table). Deliberately NOT the IK solver's wrist-centre span (0.0415/0.2825) —
// this is the practical projected table-top radius both panes draw. The SINGLE
// definition; UrdfTwin's 3D ring imports these so a change never desyncs the panes.
export const REACH_INNER_M = 0.10;
export const REACH_OUTER_M = 0.28;

// Fallback sim-object appearance when the catalog has no dims for a type (a
// missing/failed catalog fetch, or a type absent from the catalogDims map). Both
// renderers fall back to these so the 2D SVG square and the 3D box always match.
export const SIM_OBJECT_FALLBACK_SIZE_M = 0.03; // amber cube edge (m)
export const SIM_OBJECT_COLOR_HEX = '#f59e0b'; // amber — resting
export const SIM_OBJECT_HELD_COLOR_HEX = '#22c55e'; // green — grasped

// #rrggbb validator for a catalog color_hex value (case-insensitive).
const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;

// buildCatalogDims — reduce a GetObjectCatalog service response to the per-type
// render map { [type]: {height_m, width_m, color, max_instances} } that SimScene
// (2D editor) and UrdfTwin (3D twin) consume.
//
// The srv exposes PARALLEL arrays: `type_names[]` is the anchor and
// `object_height_m[]`/`object_width_m[]`/`color_hex[]`/`max_instances[]` run in
// the same order. Any render array may be ABSENT or SHORT when the frontend talks
// to an OLD server that predates the sim-stage seam, so every field is read
// defensively per index and OMITTED from an entry when its array is missing/short
// or the value is invalid. Consumers read a per-type entry as
// `dims && Number.isFinite(dims.width_m)` (etc.), so an omitted key transparently
// falls back to that consumer's own default (SIM_OBJECT_FALLBACK_SIZE_M / amber /
// no placement cap).
//
// CONTRACT: an entry is emitted ONLY if it carries at least one valid key. A type
// whose every render field is absent/invalid is skipped entirely — so an old
// server (no render arrays at all) yields an empty map `{}` and both renderers
// fall back to the fixed defaults for every type (today's behaviour, unbroken).
// Dependency-free (pure function, no `three`) — safe to call from the eager
// WorkshopPage. Values are copied by value; the returned map is a fresh object.
export function buildCatalogDims(res) {
  const out = {};
  if (!res || !Array.isArray(res.type_names)) return out;
  const heights = Array.isArray(res.object_height_m) ? res.object_height_m : null;
  const widths = Array.isArray(res.object_width_m) ? res.object_width_m : null;
  const colors = Array.isArray(res.color_hex) ? res.color_hex : null;
  const maxes = Array.isArray(res.max_instances) ? res.max_instances : null;
  res.type_names.forEach((type, i) => {
    if (typeof type !== 'string' || !type) return;
    const entry = {};
    if (heights && Number.isFinite(heights[i]) && heights[i] > 0) {
      entry.height_m = heights[i];
    }
    if (widths && Number.isFinite(widths[i]) && widths[i] > 0) {
      entry.width_m = widths[i];
    }
    if (colors && typeof colors[i] === 'string' && HEX_COLOR_RE.test(colors[i])) {
      entry.color = colors[i];
    }
    // A count: non-negative integer only (a fractional/negative cap is dropped so
    // the consumer's Number.isFinite(max_instances) check falls back to no cap).
    if (maxes && Number.isInteger(maxes[i]) && maxes[i] >= 0) {
      entry.max_instances = maxes[i];
    }
    // Skip a type that resolved to nothing so the map matches the old-server {}.
    // (The catalog is unique-keyed by construction; a later duplicate wins.)
    if (Object.keys(entry).length > 0) {
      out[type] = entry;
    }
  });
  return out;
}

// reachAnnulus — the profile-driven reach shape (N6/N11 of the 2026-10-18
// semantics round). The server manifest carries reach_inner_m / reach_outer_m
// (OMX 0.10/0.25 — the rig's reach at its grasp height —, V1 0.10/0.34);
// absent keys (old server, cloud mode) keep the exported OMX constants. A
// profile that reaches only part of the circle also carries
// `reach_sector_deg: [min, max]` (V1: [-90, 90], the bearing
// atan2(y, x) in the base frame, x forward, y left), and only then does the
// result carry `sector: {minDeg, maxDeg}` — the key is ABSENT otherwise, so a
// full ring compares equal to `{inner, outer}`.
export function reachAnnulus(caps) {
  const inner = caps && typeof caps.reach_inner_m === 'number'
    && Number.isFinite(caps.reach_inner_m) && caps.reach_inner_m > 0
    ? caps.reach_inner_m : REACH_INNER_M;
  const outer = caps && typeof caps.reach_outer_m === 'number'
    && Number.isFinite(caps.reach_outer_m) && caps.reach_outer_m > inner
    ? caps.reach_outer_m : REACH_OUTER_M;
  const out = { inner, outer };
  const sector = caps && caps.reach_sector_deg;
  if (Array.isArray(sector) && sector.length === 2
      && sector.every((v) => typeof v === 'number' && Number.isFinite(v) && v >= -180 && v <= 180)
      && sector[0] < sector[1]) {
    out.sector = { minDeg: sector[0], maxDeg: sector[1] };
  }
  return out;
}

const DEG = Math.PI / 180;

// The smallest angle between two bearings (radians), in [0, π].
function angleBetween(a, b) {
  const d = Math.abs(((a - b) % (2 * Math.PI) + 3 * Math.PI) % (2 * Math.PI) - Math.PI);
  return d;
}

// The bearing `theta` (radians) moved onto the sector of `annulus` — unchanged
// when it is inside, else onto the nearer of the two edges.
function bearingIntoSector(theta, annulus) {
  const sector = annulus && annulus.sector;
  if (!sector) return theta;
  const lo = sector.minDeg * DEG;
  const hi = sector.maxDeg * DEG;
  if (theta >= lo - 1e-12 && theta <= hi + 1e-12) return theta;
  return angleBetween(theta, lo) <= angleBetween(theta, hi) ? lo : hi;
}

/**
 * Is the base-frame point (x, y) inside the reach shape: its radius between
 * the inner and the outer radius, and its bearing inside the sector (when the
 * shape has one)?
 */
export function reachContains(annulus, x, y) {
  const inner = annulus && annulus.inner ? annulus.inner : REACH_INNER_M;
  const outer = annulus && annulus.outer ? annulus.outer : REACH_OUTER_M;
  const r = Math.hypot(x, y);
  if (!(r >= inner - 1e-12 && r <= outer + 1e-12)) return false;
  const theta = Math.atan2(y, x);
  return bearingIntoSector(theta, annulus) === theta;
}

/**
 * The base-frame point (x, y) SNAPPED into the reach shape: its radius onto
 * the ring, its bearing onto the nearest sector edge. A point already inside
 * is returned unchanged; the base itself lands on the inner radius in the
 * middle of the sector (bearing 0 for a full ring).
 */
export function clampIntoReach(x, y, annulus) {
  const inner = annulus && annulus.inner ? annulus.inner : REACH_INNER_M;
  const outer = annulus && annulus.outer ? annulus.outer : REACH_OUTER_M;
  const r = Math.hypot(x, y);
  if (r === 0) {
    const sector = annulus && annulus.sector;
    const mid = sector ? ((sector.minDeg + sector.maxDeg) / 2) * DEG : 0;
    return mid === 0 ? { x: inner, y: 0 } : { x: inner * Math.cos(mid), y: inner * Math.sin(mid) };
  }
  const rr = Math.min(outer, Math.max(inner, r));
  const theta = Math.atan2(y, x);
  const tt = bearingIntoSector(theta, annulus);
  if (rr === r && tt === theta) return { x, y };
  if (tt === theta) {
    const k = rr / r;
    return { x: x * k, y: y * k };
  }
  return { x: rr * Math.cos(tt), y: rr * Math.sin(tt) };
}

// Fallback per-type placement cap when the GetObjectCatalog map is unavailable.
//
// The cap normally comes from catalogDims[type].max_instances (= len(tag_ids) in
// the server's FIXED catalog). WorkshopPage installs `{}` on a failed or empty
// catalog fetch, and the cap then evaluated to `null` -- meaning NO CAP -- so a
// student could place a third cube that the server silently never detects
// (SimPerception skips everything past the type's tag count). The guard that
// exists to prevent an invisible object disappeared exactly when the information
// it needed was missing.
//
// 2 is the shipped „Würfel" tag count (object_catalog.py::_FIXED_CATALOG,
// tag_ids=[20, 21]). It is a FLOOR for the degraded path only -- a real
// max_instances from the service always wins.
export const DEFAULT_MAX_INSTANCES = 2;

// Resolve the placement cap for one type. Pure + exported so the degraded path
// is actually testable — the live call site is a useCallback inside SimScene,
// and this fallback shipped DEAD (the constant was imported and never read)
// precisely because nothing could see it.
//
// A real, finite max_instances from the service always wins; anything missing,
// short, non-numeric or NaN degrades to DEFAULT_MAX_INSTANCES rather than to
// "no cap".
export function resolveMaxInstances(catalogDims, type) {
  const dims = catalogDims && catalogDims[type];
  const max = dims && dims.max_instances;
  return Number.isFinite(max) ? max : DEFAULT_MAX_INSTANCES;
}

// ── The tower kit (spec 40 S8, D66) ─────────────────────────────────────────
// What the simulator and the 3D twin draw of the tower kit, in Roboter Studio's
// world W (the arm's base frame; x_W = x_F + base_axis_x, inZone.js): the base
// plate the arm stands on (raised by LIFT_M), the mat with its notch around the
// plate, the tower's nine oriented boxes at their contract place (the bridge
// and both head boxes included) and the printed work zone with its cut. All of
// it from sceneFixture.json, rendered from the server's contract; nothing here
// is a measurement. Built once per arm, so the twin's effects see one identity.
const C_FIX = sceneFixture.constants;
const LAYOUTS = new Map();

function buildLayout(spec, baseAxisX) {
  const toW = ([x, y]) => [x + baseAxisX, y];
  const [[px0, py0], [px1, py1]] = C_FIX.PLATE_OUTLINE_F;
  const [[mx0, my0], [mx1, my1]] = spec.mat_sheet_f;
  const { x_max: nx, y_abs: ny } = C_FIX.MAT_NOTCH_F;
  // The mat's outline, its notch open to the rear edge around the plate.
  const mat = [[mx0, my0], [mx1, my0], [mx1, my1], [mx0, my1], [mx0, ny], [nx, ny], [nx, -ny], [mx0, -ny]];
  return Object.freeze({
    id: C_FIX.FIXTURE_ID,
    liftM: C_FIX.LIFT_M,
    baseAxisX,
    spec,
    plate: Object.freeze([toW([px0, py0]), toW([px1, py1])]),
    mat: Object.freeze(mat.map(toW)),
    // centre in W; `rows` = R row by row (its COLUMNS are the box's axes); half extents.
    boxes: Object.freeze(C_FIX.TOWER_OBB_F.map(([name, centre, rows, half]) => Object.freeze({
      name, centre: [centre[0] + baseAxisX, centre[1], centre[2]], rows, half,
    }))),
    zone: Object.freeze(zoneOutline(spec).map(toW)),
  });
}

/** The tower kit of the arm a capability manifest names, or null for an arm the contract does not know. */
export function fixtureLayout(caps) {
  const spec = armSpecFor(caps);
  const baseAxisX = baseAxisXFor(caps);
  if (!spec || baseAxisX === null) return null;
  if (!LAYOUTS.has(spec)) LAYOUTS.set(spec, buildLayout(spec, baseAxisX));
  return LAYOUTS.get(spec);
}

/** The table footprint (x, y) of an oriented box: the convex hull of its eight corners, counter-clockwise. */
export function boxFootprint(box) {
  const { centre, rows, half } = box;
  const pts = [];
  for (const sx of [-1, 1]) {
    for (const sy of [-1, 1]) {
      for (const sz of [-1, 1]) {
        const l = [sx * half[0], sy * half[1], sz * half[2]];
        pts.push([
          centre[0] + rows[0][0] * l[0] + rows[0][1] * l[1] + rows[0][2] * l[2],
          centre[1] + rows[1][0] * l[0] + rows[1][1] * l[1] + rows[1][2] * l[2],
        ]);
      }
    }
  }
  // Andrew's monotone chain.
  pts.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 1e-15) lower.pop();
    lower.push(p);
  }
  const upper = [];
  for (let i = pts.length - 1; i >= 0; i -= 1) {
    const p = pts[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 1e-15) upper.pop();
    upper.push(p);
  }
  return lower.slice(0, -1).concat(upper.slice(0, -1));
}

/**
 * The 2D simulator table's window (metres of W) for the tower kit: everything
 * the kit draws — the mat, the plate, the tower's footprint, the zone — with a
 * margin, symmetric left and right of the arm (+x forward is up on the table).
 */
export function towerView(layout, margin = 0.02) {
  const pts = [...layout.mat, ...layout.plate, ...layout.zone, ...layout.boxes.flatMap(boxFootprint)];
  const xs = pts.map((p) => p[0]);
  const yAbs = Math.max(...pts.map((p) => Math.abs(p[1])));
  return { minX: Math.min(...xs) - margin, maxX: Math.max(...xs) + margin, minY: -yAbs - margin, maxY: yAbs + margin };
}
