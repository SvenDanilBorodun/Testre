/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

import { describe, it, expect } from 'vitest';
import fixture from '../../fixture/sceneFixture.json';
import {
  ZIEL_TOUCH_TCP_Z_M,
  ZIEL_TOUCH_TOLERANCE_MM,
  formatCmDe,
  isZielTouchTooHigh,
  zielTouchHeightAboveTableMm,
} from '../zielTouch';

const OMX = { urdf_asset_id: 'omx_f', arm_joints: 5 };
const V1 = { urdf_asset_id: 'v1', arm_joints: 5 };
const C = fixture.constants;

// The touch heights (spec 40 S5): the fingertips on the MAT, i.e. the mat's top
// (MAT_THICKNESS_M) plus the solver's EE point above the table when the
// fingertips touch it (FINGERTIP_BELOW_EE_M). OMX 0.00305 m (the provisional
// fingertip offset, D86), V1 0.00045 m.
const OMX_TOUCH_M = C.MAT_THICKNESS_M + C.FINGERTIP_BELOW_EE_M.omx;
const V1_TOUCH_M = C.MAT_THICKNESS_M + C.FINGERTIP_BELOW_EE_M.v1;

describe('zielTouch — per-profile plausibility', () => {
  it('OMX: 30 mm above the touch height is not too high, 31 mm is', () => {
    expect(ZIEL_TOUCH_TOLERANCE_MM).toBe(30);
    expect(zielTouchHeightAboveTableMm(0.03305, OMX)).toBe(30);
    expect(isZielTouchTooHigh(0.03305, OMX)).toBe(false);
    expect(isZielTouchTooHigh(0.03405, OMX)).toBe(true);
    // A touch on the mat itself.
    expect(isZielTouchTooHigh(OMX_TOUCH_M, OMX)).toBe(false);
    expect(zielTouchHeightAboveTableMm(OMX_TOUCH_M, OMX)).toBe(0);
  });

  it('V1: the closed claw on the mat — 0.03045 m not, 0.03145 m too high', () => {
    expect(zielTouchHeightAboveTableMm(0.03045, V1)).toBe(30);
    expect(isZielTouchTooHigh(0.03045, V1)).toBe(false);
    expect(isZielTouchTooHigh(0.03145, V1)).toBe(true);
    expect(isZielTouchTooHigh(V1_TOUCH_M, V1)).toBe(false);
  });

  it('no manifest falls back to the OMX (armGeometry\'s default asset)', () => {
    expect(isZielTouchTooHigh(0.03305, null)).toBe(false);
    expect(isZielTouchTooHigh(0.03405, null)).toBe(true);
  });

  it('a non-finite height is never „too high" (the store refuses it instead)', () => {
    expect(isZielTouchTooHigh(Number.NaN, OMX)).toBe(false);
  });

  it('German centimetres with one decimal', () => {
    expect(formatCmDe(120)).toBe('12,0');
    expect(formatCmDe(31)).toBe('3,1');
  });
});

describe('zielTouch — lockstep with the fixture contract (sceneFixture.json)', () => {
  it('keeps its two keys, omx_f and v1', () => {
    expect(Object.keys(ZIEL_TOUCH_TCP_Z_M).sort()).toEqual(['omx_f', 'v1']);
  });

  it('ZIEL_TOUCH_TCP_Z_M.omx_f equals the JSON\'s mat thickness + the OMX fingertip offset', () => {
    expect(ZIEL_TOUCH_TCP_Z_M.omx_f).toBe(C.MAT_THICKNESS_M + C.FINGERTIP_BELOW_EE_M.omx);
  });

  it('ZIEL_TOUCH_TCP_Z_M.v1 equals the JSON\'s mat thickness + the V1 fingertip offset', () => {
    expect(ZIEL_TOUCH_TCP_Z_M.v1).toBe(C.MAT_THICKNESS_M + C.FINGERTIP_BELOW_EE_M.v1);
  });
});
