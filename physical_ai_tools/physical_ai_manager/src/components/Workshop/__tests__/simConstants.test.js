/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The simulator matches the rig (owner decision Q6, N6/N11 of the 2026-10-18
// semantics round): the reach shape a profile advertises — its inner and outer
// radius and, for an arm that reaches only part of the circle, its SECTOR — is
// what both panes draw and what a placement is snapped into. The Edu:1 reaches
// ±90° (its back half is out of reach); the OMX ring is a full circle, its
// outer radius the rig's (0.25 m).

import fs from 'fs';
import path from 'path';
import { describe, expect, it } from 'vitest';
import {
  REACH_INNER_M, REACH_OUTER_M, clampIntoReach, reachAnnulus, reachContains,
} from '../simConstants';

const OMX = { reach_inner_m: 0.10, reach_outer_m: 0.25 };
const EDU1 = { reach_inner_m: 0.07, reach_outer_m: 0.35, reach_sector_deg: [-90, 90] };

describe('reachAnnulus — the advertised shape', () => {
  it('edu1_sector_and_omx_radii_from_caps', () => {
    expect(reachAnnulus(OMX)).toEqual({ inner: 0.10, outer: 0.25 });
    expect('sector' in reachAnnulus(OMX)).toBe(false);
    expect(reachAnnulus(EDU1)).toEqual({ inner: 0.07, outer: 0.35, sector: { minDeg: -90, maxDeg: 90 } });
    // No caps (an older server, cloud mode): the OMX constants, no sector.
    expect(reachAnnulus(null)).toEqual({ inner: REACH_INNER_M, outer: REACH_OUTER_M });
  });

  it('ignores a sector it cannot draw', () => {
    for (const bad of [[90, -90], [0, 0], [Number.NaN, 90], [-200, 90], [-90, 200], 'x', [-90], null]) {
      expect('sector' in reachAnnulus({ ...EDU1, reach_sector_deg: bad })).toBe(false);
    }
  });
});

describe('reachContains / clampIntoReach', () => {
  const omx = reachAnnulus(OMX);
  const edu1 = reachAnnulus(EDU1);

  it('a point is inside when its radius AND its bearing are', () => {
    expect(reachContains(omx, 0.15, 0)).toBe(true);
    expect(reachContains(omx, -0.15, 0)).toBe(true); // a full ring: behind counts
    expect(reachContains(omx, 0.26, 0)).toBe(false);
    expect(reachContains(edu1, 0.15, 0)).toBe(true);
    expect(reachContains(edu1, 0, 0.15)).toBe(true); // exactly on the +90° edge
    expect(reachContains(edu1, -0.03, 0.15)).toBe(false); // behind the Edu:1
    expect(reachContains(edu1, 0.05, 0)).toBe(false); // inside the inner radius
  });

  it('snaps the radius onto the ring and the bearing onto the nearest sector edge', () => {
    // Radius only (inside the sector): as before.
    const out = clampIntoReach(0.40, 0, edu1);
    expect(out.x).toBeCloseTo(0.35, 9);
    expect(out.y).toBeCloseTo(0, 9);
    // Behind, nearer the +90° edge: onto that edge, radius kept.
    const r = Math.hypot(-0.03, 0.15);
    const left = clampIntoReach(-0.03, 0.15, edu1);
    expect(left.x).toBeCloseTo(0, 9);
    expect(left.y).toBeCloseTo(r, 9);
    // Behind, nearer the −90° edge.
    const right = clampIntoReach(-0.03, -0.15, edu1);
    expect(right.x).toBeCloseTo(0, 9);
    expect(right.y).toBeCloseTo(-r, 9);
    // Straight behind and too close: the edge AND the inner radius.
    const back = clampIntoReach(-0.02, 0.001, edu1);
    expect(Math.hypot(back.x, back.y)).toBeCloseTo(0.07, 9);
    expect(reachContains(edu1, back.x, back.y)).toBe(true);
    // The base itself lands on the inner radius in the middle of the sector.
    expect(clampIntoReach(0, 0, edu1)).toEqual({ x: 0.07, y: 0 });
    // A point inside is returned unchanged.
    expect(clampIntoReach(0.15, 0.05, edu1)).toEqual({ x: 0.15, y: 0.05 });
    // The OMX keeps the full ring: behind stays behind.
    expect(clampIntoReach(-0.15, 0, omx)).toEqual({ x: -0.15, y: 0 });
  });

  it('stays free of three.js (it is in the entry bundle)', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'simConstants.js'), 'utf8');
    expect(src).not.toMatch(/from ['"]three['"]/);
  });
});
