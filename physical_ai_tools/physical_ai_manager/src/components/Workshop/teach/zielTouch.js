/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Ziel by touch: is the captured TCP plausibly ON the table? A Ziel is a point
// on the table whose height the run re-reads from the table plane, so a Z
// pressed with the gripper held in the air would silently drop that height.
// Pure; no Blockly, no React. Rig gate S-R4 owns the per-arm numbers.

import { armGeometry } from '../../../utils/armProfile';

// The expected FK TCP height when the fingertips touch the table (z = 0 is the table surface
// on every shipped arm: CLAUDE.md „z = 0 IS the table"). OMX: the FK TCP is end_effector_link and the
// fingers reach ik_solver._TOOL_TIP_EXT_M = 0.04 m further. Edu:6 / Edu:1: the TCP IS the fingertip.
export const ZIEL_TOUCH_TCP_Z_M = Object.freeze({ omx_f: 0.04, edu6: 0.0, edu1: 0.0 });
export const ZIEL_TOUCH_TOLERANCE_MM = 30;

const EXPECTED = new Map(Object.entries(ZIEL_TOUCH_TCP_Z_M));

/** Height of the captured TCP above its expected touch height, in whole millimetres. */
export function zielTouchHeightAboveTableMm(worldZ, caps) {
  const expected = EXPECTED.get(armGeometry(caps).urdfAssetId) ?? 0;
  return Math.round((worldZ - expected) * 1000);
}

/** True when the touch is more than the tolerance above the table (NaN → false). */
export function isZielTouchTooHigh(worldZ, caps) {
  return zielTouchHeightAboveTableMm(worldZ, caps) > ZIEL_TOUCH_TOLERANCE_MM;
}

/** Millimetres → centimetres with one decimal and a German comma („12,0"). */
export function formatCmDe(heightMm) {
  return (heightMm / 10).toLocaleString('de-DE', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}
