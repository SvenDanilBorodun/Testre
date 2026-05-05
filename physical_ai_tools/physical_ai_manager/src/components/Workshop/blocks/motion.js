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
import { DE } from './messages_de';

const MOTION_COLOR = '#3b82f6';

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
  },
  {
    type: 'edubotics_pickup',
    message0: DE.PICKUP,
    args0: [{ type: 'input_value', name: 'TARGET' }],
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
  },
  {
    type: 'edubotics_drop_at',
    message0: DE.DROP_AT,
    args0: [{ type: 'input_value', name: 'DESTINATION' }],
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
  },
  {
    type: 'edubotics_wait_seconds',
    message0: DE.WAIT_SECONDS,
    args0: [{ type: 'field_number', name: 'SECONDS', value: 1, min: 0, precision: 0.1 }],
    previousStatement: null,
    nextStatement: null,
    colour: MOTION_COLOR,
  },
];

export function registerMotionBlocks() {
  Blockly.defineBlocksWithJsonArray(MOTION_BLOCKS);
}
