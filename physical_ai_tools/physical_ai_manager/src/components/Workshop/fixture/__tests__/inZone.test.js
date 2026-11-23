/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// inZone.js is the browser's copy of the ONE work-zone predicate
// (scene_fixture.zone_az_hi / in_zone, spec 40 §3, D66): the printed zone with
// the corner next to the tower base cut out. It is held equal to the Python
// predicate by the case table the server suite renders
// (sceneFixture.cases.json, tools/export_scene_fixture.py), never by a second
// hand-written table.

import fs from 'fs';
import path from 'path';
import { describe, expect, it } from 'vitest';
import fixture from '../sceneFixture.json';
import cases from '../sceneFixture.cases.json';
import {
  ARM_KEY_BY_ASSET,
  BASE_AXIS_X_M,
  armSpecFor,
  clampIntoZone,
  inZone,
  zoneAzHi,
  zoneOutline,
} from '../inZone';

const C = fixture.constants;
const SPECS = { omx: C.ARM_OMX, v1: C.ARM_V1 };
const DEG = Math.PI / 180;
const polar = (r, azDeg) => [r * Math.cos(azDeg * DEG), r * Math.sin(azDeg * DEG)];
const azOf = (x, y) => Math.atan2(y, x) / DEG;

describe('inZone — the case table of the Python predicate', () => {
  it('agrees with every in_zone case of both arms', () => {
    for (const [key, spec] of Object.entries(SPECS)) {
      const rows = cases.in_zone[key];
      expect(rows.length).toBeGreaterThan(20);
      for (const row of rows) {
        expect([key, row.what, inZone(spec, row.x_f, row.y_f)]).toEqual([key, row.what, row.in_zone]);
      }
    }
  });

  it('agrees with every zone_az_hi case to 1e-9', () => {
    for (const [key, spec] of Object.entries(SPECS)) {
      for (const row of cases.zone_az_hi[key]) {
        expect(Math.abs(zoneAzHi(spec, row.r_m) - row.az_hi_deg)).toBeLessThanOrEqual(1e-9);
      }
    }
  });

  it('refuses what the Python predicate refuses: a non-finite or a non-numeric point', () => {
    expect(inZone(SPECS.omx, Number.NaN, 0.1)).toBe(false);
    expect(inZone(SPECS.omx, 0.15, Number.POSITIVE_INFINITY)).toBe(false);
    expect(inZone(SPECS.omx, 'x', 0.1)).toBe(false);
    expect(inZone(SPECS.omx, null, 0.1)).toBe(false);
  });
});

describe('inZone — the snap into the zone', () => {
  it('OMX: a tap at r 0.15 m, az 84° lands on zone_az_hi(0.15) at the same radius', () => {
    const spec = SPECS.omx;
    const [x, y] = polar(0.15, 84);
    expect(inZone(spec, x, y)).toBe(false);
    const p = clampIntoZone(spec, x, y);
    expect(Math.hypot(p.x, p.y)).toBeCloseTo(0.15, 12);
    expect(azOf(p.x, p.y)).toBeCloseTo(zoneAzHi(spec, 0.15), 9);
    expect(inZone(spec, p.x, p.y)).toBe(true);
  });

  it('a point inside the zone is returned unchanged', () => {
    for (const spec of Object.values(SPECS)) {
      const [x, y] = polar(0.2, 10);
      expect(clampIntoZone(spec, x, y)).toEqual({ x, y });
    }
  });

  it('the radius snaps onto the ring and the bearing onto the nearer zone edge', () => {
    const v1 = SPECS.v1;
    // Behind the arm on the tower's side: nearer the cut than the −85° edge.
    let p = clampIntoZone(v1, ...polar(0.2, 120));
    expect(azOf(p.x, p.y)).toBeCloseTo(zoneAzHi(v1, 0.2), 9);
    // Behind the arm on the other side: onto the −85° edge.
    p = clampIntoZone(v1, ...polar(0.2, -120));
    expect(azOf(p.x, p.y)).toBeCloseTo(v1.zone_sector_deg[0], 9);
    // Too far out on the tower's side: the radius first, then that radius's cut
    // (OMX: 80° lies beyond the cut at the outer radius, 74°).
    const omx = SPECS.omx;
    p = clampIntoZone(omx, ...polar(0.5, 80));
    expect(Math.hypot(p.x, p.y)).toBeCloseTo(omx.zone_outer_m, 12);
    expect(azOf(p.x, p.y)).toBeCloseTo(zoneAzHi(omx, omx.zone_outer_m), 9);
    expect(zoneAzHi(omx, omx.zone_outer_m)).toBe(74);
    // The joint-1 axis itself: the inner radius, the middle of the zone's bearings.
    p = clampIntoZone(v1, 0, 0);
    expect(Math.hypot(p.x, p.y)).toBeCloseTo(v1.zone_inner_m, 12);
    expect(inZone(v1, p.x, p.y)).toBe(true);
  });

  it('every snapped point of a dense grid lies in the zone', () => {
    for (const spec of Object.values(SPECS)) {
      for (let x = -0.4; x <= 0.4; x += 0.0123) {
        for (let y = -0.4; y <= 0.4; y += 0.0117) {
          const p = clampIntoZone(spec, x, y);
          expect(inZone(spec, p.x, p.y)).toBe(true);
        }
      }
    }
  });
});

describe('inZone — the zone outline the simulator draws', () => {
  it('every outline point lies on the zone (the cut included), and it follows the cut', () => {
    for (const spec of Object.values(SPECS)) {
      const outline = zoneOutline(spec);
      expect(outline.length).toBeGreaterThan(20);
      for (const [x, y] of outline) expect(inZone(spec, x, y)).toBe(true);
      // The tower-side edge: the outline reaches every knot's azimuth at its radius.
      for (const [r, az] of spec.zone_cut_knots) {
        if (r > spec.zone_outer_m || r < spec.zone_inner_m) continue;
        const hit = outline.some(([x, y]) => Math.abs(Math.hypot(x, y) - r) < 1e-9
          && Math.abs(azOf(x, y) - Math.min(az, spec.zone_sector_deg[1])) < 1e-6);
        expect(hit).toBe(true);
      }
      // The corner next to the tower is NOT inside the drawn outline's sector:
      // no outline point lies beyond the cut.
      for (const [x, y] of outline) {
        const r = Math.hypot(x, y);
        expect(azOf(x, y)).toBeLessThanOrEqual(zoneAzHi(spec, r) + 1e-6);
      }
    }
  });
});

describe('inZone — which arm, and where its joint-1 axis lies in Roboter Studio\'s world', () => {
  it('keys the arms by the URDF asset id of the capability manifest', () => {
    expect(ARM_KEY_BY_ASSET).toEqual({ omx_f: 'omx', v1: 'v1' });
    expect(armSpecFor({ urdf_asset_id: 'v1' })).toBe(C.ARM_V1);
    expect(armSpecFor({ urdf_asset_id: 'omx_f' })).toBe(C.ARM_OMX);
    // No manifest is the OMX (armGeometry's default asset), an unknown asset no arm.
    expect(armSpecFor(null)).toBe(C.ARM_OMX);
    expect(armSpecFor({ urdf_asset_id: 'edu6' })).toBeNull();
  });

  it('BASE_AXIS_X_M.omx_f equals ik_solver._J1_XYZ[0] (the server\'s base_axis_x)', () => {
    const file = path.resolve(process.cwd(), '../physical_ai_server/physical_ai_server/workflow/ik_solver.py');
    // A missing file FAILS here (readFileSync throws) — never a silent pass.
    const source = fs.readFileSync(file, 'utf8');
    const match = source.match(/^_J1_XYZ = \((-?[0-9.]+), /m);
    expect(match).not.toBeNull();
    expect(Number(match[1])).toBe(BASE_AXIS_X_M.omx_f);
  });

  it('BASE_AXIS_X_M.v1 equals v1_ik.BASE_AXIS_X_WORLD', () => {
    const file = path.resolve(process.cwd(), '../physical_ai_server/physical_ai_server/workflow/v1_ik.py');
    const source = fs.readFileSync(file, 'utf8');
    const match = source.match(/^BASE_AXIS_X_WORLD = (-?[0-9.]+)$/m);
    expect(match).not.toBeNull();
    expect(Number(match[1])).toBe(BASE_AXIS_X_M.v1);
  });
});
