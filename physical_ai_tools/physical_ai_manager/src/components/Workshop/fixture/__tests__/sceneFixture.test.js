/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// The React copy of the fixture contract (spec 40 §3, WP-02). sceneFixture.json and
// sceneFixture.cases.json are RENDERED from the server's scene_fixture.py by
// tools/export_scene_fixture.py (the server suite byte-fences both against a fresh
// render); this suite holds the shape the page will read, and proves the work-zone case
// table is reproducible in the browser's double arithmetic from the JSON's own knots —
// the table WP-17's sceneFixture.inZone twin is held equal by.

import { describe, expect, it } from 'vitest';
import fixture from '../sceneFixture.json';
import cases from '../sceneFixture.cases.json';

const C = fixture.constants;
const ARMS = { v1: C.ARM_V1, omx: C.ARM_OMX };
const TOL = 1e-9;

function cutAz(knots, r) {
  if (!knots.length || r > knots[knots.length - 1][0] + TOL) return Infinity;
  if (r <= knots[0][0]) return knots[0][1];
  for (let i = 0; i + 1 < knots.length; i += 1) {
    const [r0, a0] = knots[i];
    const [r1, a1] = knots[i + 1];
    if (r0 - TOL <= r && r <= r1 + TOL) {
      if (r1 - r0 <= 1e-12) return Math.min(a0, a1);
      const t = Math.min(1, Math.max(0, (r - r0) / (r1 - r0)));
      return a0 + (a1 - a0) * t;
    }
  }
  return Infinity;
}

const azHi = (spec, r) => Math.min(spec.zone_sector_deg[1], cutAz(spec.zone_cut_knots, r));

function inZone(spec, x, y) {
  const r = Math.hypot(x, y);
  if (!(spec.zone_inner_m - TOL <= r && r <= spec.zone_outer_m + TOL)) return false;
  const az = (Math.atan2(y, x) * 180) / Math.PI;
  return spec.zone_sector_deg[0] - TOL <= az && az <= azHi(spec, r) + TOL;
}

describe('sceneFixture.json', () => {
  it('carries the contract hash and the fixture id', () => {
    expect(fixture.contract_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(C.FIXTURE_ID).toBe('tower_v1');
    expect(C.RESERVED_TAG_IDS).toEqual([400, 586]);
  });

  it('carries the nine oriented tower boxes with their axes and half extents', () => {
    expect(C.TOWER_OBB_F.map((b) => b[0])).toEqual([
      'pad', 'socket', 'rib_a', 'rib_b', 'column', 'block', 'head', 'lens', 'bridge',
    ]);
    for (const [, centre, rows, half] of C.TOWER_OBB_F) {
      expect(centre).toHaveLength(3);
      expect(rows).toHaveLength(3);
      rows.forEach((row) => expect(row).toHaveLength(3));
      expect(half.every((h) => h > 0)).toBe(true);
    }
  });

  it('names each arm by its profiles and cuts its zone with ascending knots', () => {
    expect(C.ARM_V1.profiles).toEqual(['v1_studio']);
    expect(C.ARM_OMX.profiles).toEqual(['omx_full', 'omx_follower']);
    for (const spec of Object.values(ARMS)) {
      const radii = spec.zone_cut_knots.map((k) => k[0]);
      expect([...radii].sort((a, b) => a - b)).toEqual(radii);
      expect(radii[0]).toBe(spec.zone_inner_m);
    }
  });

  it('keeps every calibration tag id inside the reserved range', () => {
    const [lo, hi] = C.RESERVED_TAG_IDS;
    const ids = [...Object.keys(C.FIXTURE_TAGS_F).map(Number)];
    for (const spec of Object.values(ARMS)) {
      ids.push(...Object.keys(spec.mat_tags_f).map(Number), ...spec.target_tag_ids);
    }
    expect(ids.length).toBe(48);
    expect(ids.every((id) => id >= lo && id <= hi)).toBe(true);
  });
});

describe('sceneFixture.cases.json (the work-zone predicate)', () => {
  it('holds for both arms in the browser arithmetic', () => {
    for (const [key, spec] of Object.entries(ARMS)) {
      const rows = cases.in_zone[key];
      expect(rows.length).toBeGreaterThan(20);
      for (const row of rows) {
        expect(inZone(spec, row.x_f, row.y_f)).toBe(row.in_zone);
      }
      for (const row of cases.zone_az_hi[key]) {
        expect(Math.abs(azHi(spec, row.r_m) - row.az_hi_deg)).toBeLessThanOrEqual(1e-9);
      }
    }
  });

  it('has cases on both sides of every edge', () => {
    for (const rows of Object.values(cases.in_zone)) {
      expect(rows.some((r) => r.in_zone)).toBe(true);
      expect(rows.some((r) => !r.in_zone)).toBe(true);
    }
  });
});
