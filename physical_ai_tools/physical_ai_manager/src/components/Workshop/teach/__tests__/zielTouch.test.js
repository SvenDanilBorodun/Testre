/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import fs from 'fs';
import path from 'path';
import { describe, it, expect } from 'vitest';
import {
  ZIEL_TOUCH_TCP_Z_M,
  ZIEL_TOUCH_TOLERANCE_MM,
  formatCmDe,
  isZielTouchTooHigh,
  zielTouchHeightAboveTableMm,
} from '../zielTouch';

const OMX = { urdf_asset_id: 'omx_f', arm_joints: 5 };
const EDU6 = { urdf_asset_id: 'edu6', arm_joints: 6 };
const EDU1 = { urdf_asset_id: 'edu1', arm_joints: 5 };

describe('zielTouch — per-profile plausibility', () => {
  it('OMX: 0.070 m is exactly 30 mm (not too high); 0.071 m is too high', () => {
    expect(ZIEL_TOUCH_TOLERANCE_MM).toBe(30);
    expect(zielTouchHeightAboveTableMm(0.07, OMX)).toBe(30);
    expect(isZielTouchTooHigh(0.07, OMX)).toBe(false);
    expect(isZielTouchTooHigh(0.071, OMX)).toBe(true);
    // A touch on the table itself.
    expect(isZielTouchTooHigh(0.04, OMX)).toBe(false);
  });

  it('Edu:6 and Edu:1: the TCP is the fingertip — 0.030 m not, 0.031 m too high', () => {
    for (const caps of [EDU6, EDU1]) {
      expect(isZielTouchTooHigh(0.03, caps)).toBe(false);
      expect(isZielTouchTooHigh(0.031, caps)).toBe(true);
    }
  });

  it('no manifest falls back to the OMX (armGeometry\'s default asset)', () => {
    expect(isZielTouchTooHigh(0.07, null)).toBe(false);
    expect(isZielTouchTooHigh(0.071, null)).toBe(true);
  });

  it('a non-finite height is never „too high" (the store refuses it instead)', () => {
    expect(isZielTouchTooHigh(Number.NaN, OMX)).toBe(false);
  });

  it('German centimetres with one decimal', () => {
    expect(formatCmDe(120)).toBe('12,0');
    expect(formatCmDe(31)).toBe('3,1');
  });
});

describe('zielTouch — lockstep with the server solver', () => {
  it('ZIEL_TOUCH_TCP_Z_M.omx_f equals ik_solver._TOOL_TIP_EXT_M', () => {
    const file = path.resolve(process.cwd(), '../physical_ai_server/physical_ai_server/workflow/ik_solver.py');
    // A missing file FAILS here (readFileSync throws) — never a silent pass.
    const source = fs.readFileSync(file, 'utf8');
    const match = source.match(/^_TOOL_TIP_EXT_M = ([0-9.]+)$/m);
    expect(match).not.toBeNull();
    expect(Number(match[1])).toBe(ZIEL_TOUCH_TCP_Z_M.omx_f);
  });
});
