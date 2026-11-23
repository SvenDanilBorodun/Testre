/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Ziel by touch: is the captured TCP plausibly ON the mat? A Ziel is a point
// on the table whose height the run takes from the fixture, so a Z pressed
// with the gripper held in the air would silently drop that height.
// Pure; no Blockly, no React.

import { armGeometry } from '../../../utils/armProfile';
import fixture from '../fixture/sceneFixture.json';

// The expected FK TCP height when the fingertips touch the mat (spec 40 S5):
// z = 0 is the table, the mat's top lies MAT_THICKNESS_M above it, and the
// solver's EE point sits FINGERTIP_BELOW_EE_M above the mat when the fingertips
// touch it (the OMX with the gripper at the cube-holding opening — a
// provisional value until the owner's measurement, D86; the V1's TCP IS the
// closed fingertip, 0). Read from the fixture contract, never a literal here.
const C = fixture.constants;
export const ZIEL_TOUCH_TCP_Z_M = Object.freeze({
  omx_f: C.MAT_THICKNESS_M + C.FINGERTIP_BELOW_EE_M.omx,
  v1: C.MAT_THICKNESS_M + C.FINGERTIP_BELOW_EE_M.v1,
});
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
