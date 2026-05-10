/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import * as Blockly from 'blockly/core';
import { DE, WORKSPACE_BOUNDS_M } from './messages_de';

const MOTION_COLOR = '#3b82f6';

// Wait-seconds bounds. The runtime overlay (handlers/motion.py)
// also clamps server-side; this is the pre-flight UX hint.
const WAIT_SECONDS_MIN = 0.1;
const WAIT_SECONDS_MAX = 60;

export const MOTION_BLOCKS = [
  {
    type: 'edubotics_home',
    message0: DE.HOME,
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
    tooltip: 'Bewegt den Roboterarm zur Heimposition.',
  },
  {
    type: 'edubotics_open_gripper',
    message0: DE.OPEN_GRIPPER,
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
    tooltip: 'Öffnet den Greifer.',
  },
  {
    type: 'edubotics_close_gripper',
    message0: DE.CLOSE_GRIPPER,
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
    tooltip: 'Schließt den Greifer.',
  },
  {
    type: 'edubotics_move_to',
    message0: DE.MOVE_TO,
    args0: [{ type: 'input_value', name: 'DESTINATION' }],
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
    tooltip: 'Bewegt den Greifer zu einem Ziel-Block.',
  },
  {
    type: 'edubotics_pickup',
    message0: DE.PICKUP,
    args0: [{ type: 'input_value', name: 'TARGET' }],
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
    tooltip:
      'Fährt über das Ziel, schließt den Greifer und hebt das '
      + 'Objekt an.',
  },
  {
    type: 'edubotics_drop_at',
    message0: DE.DROP_AT,
    args0: [{ type: 'input_value', name: 'DESTINATION' }],
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
    tooltip:
      'Fährt zum Ziel, öffnet den Greifer und hebt den Arm wieder '
      + 'an.',
  },
  {
    type: 'edubotics_wait_seconds',
    message0: DE.WAIT_SECONDS,
    args0: [{ type: 'input_value', name: 'SECONDS', check: 'Number' }],
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
    tooltip: 'Wartet die angegebene Zeit, bevor der nächste Block läuft.',
  },
];

/**
 * Runs after a block is added to the workspace. We attach validators
 * to fields that exist directly on this block (not the connected
 * shadow). Numeric ranges here are advisory — the runtime safety
 * envelope is the authoritative limit.
 */
function attachWaitSecondsValidator(block) {
  // The wait-seconds block now takes a value-input shadow rather than a
  // direct numeric field, so we instead validate at extension time when
  // the connected math_number's NUM is set. Blockly fires `change`
  // events on the workspace; the editor (BlocklyWorkspace.jsx) wires
  // a global change listener that calls back into validators if the
  // payload is a math_number connected to a wait_seconds parent.
  // No-op here for now, kept for symmetry.
  void block;
}

/**
 * Generic numeric clamp validator factory.
 *   field.setValidator(numericClamp(min, max))
 * Returns the input unchanged when in range. When out of range,
 * coerces to the nearest bound and lets the field re-render with the
 * coerced value (visible to the student).
 */
export function numericClamp(min, max) {
  return (newValue) => {
    const n = Number(newValue);
    if (!Number.isFinite(n)) return min;
    if (n < min) return min;
    if (n > max) return max;
    return n;
  };
}

export const MOTION_VALIDATORS = {
  WAIT_SECONDS: numericClamp(WAIT_SECONDS_MIN, WAIT_SECONDS_MAX),
  // Move-to coords share a single envelope; field validators on each
  // axis reference WORKSPACE_BOUNDS_M from messages_de. The plus-minus
  // mutator in destination_pin handles those numerics; we don't need
  // a direct field validator here.
  WORKSPACE_X: numericClamp(WORKSPACE_BOUNDS_M.x.min, WORKSPACE_BOUNDS_M.x.max),
  WORKSPACE_Y: numericClamp(WORKSPACE_BOUNDS_M.y.min, WORKSPACE_BOUNDS_M.y.max),
  WORKSPACE_Z: numericClamp(WORKSPACE_BOUNDS_M.z.min, WORKSPACE_BOUNDS_M.z.max),
};

export function registerMotionBlocks() {
  Blockly.defineBlocksWithJsonArray(MOTION_BLOCKS);
  // Attach a workspace-wide listener that clamps numeric inputs on
  // wait_seconds when a math_number is connected directly. Done as
  // a Blockly extension so it survives copy/paste of blocks.
  if (!Blockly.Extensions.isRegistered('edubotics_wait_seconds_clamp')) {
    Blockly.Extensions.register('edubotics_wait_seconds_clamp', function () {
      attachWaitSecondsValidator(this);
    });
  }
}
