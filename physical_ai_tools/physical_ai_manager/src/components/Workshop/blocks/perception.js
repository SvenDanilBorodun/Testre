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
import { COLORS, OBJECT_CLASSES, DE } from './messages_de';

const PERCEPTION_COLOR = '#22c55e';

const COLOR_DROPDOWN = COLORS.map(([label, value]) => [label, value]);
const OBJECT_DROPDOWN = OBJECT_CLASSES.map((c) => [c, c]);

export const PERCEPTION_BLOCKS = [
  {
    type: 'edubotics_detect_color',
    message0: DE.DETECT_COLOR,
    args0: [{ type: 'field_dropdown', name: 'COLOR', options: COLOR_DROPDOWN }],
    output: 'Array',
    colour: PERCEPTION_COLOR,
  },
  {
    type: 'edubotics_wait_until_color',
    message0: DE.WAIT_UNTIL_COLOR,
    args0: [
      { type: 'field_dropdown', name: 'COLOR', options: COLOR_DROPDOWN },
      { type: 'field_number', name: 'TIMEOUT', value: 10, min: 1 },
    ],
    output: 'Boolean',
    colour: PERCEPTION_COLOR,
  },
  {
    type: 'edubotics_count_color',
    message0: DE.COUNT_COLOR,
    args0: [{ type: 'field_dropdown', name: 'COLOR', options: COLOR_DROPDOWN }],
    output: 'Number',
    colour: PERCEPTION_COLOR,
  },
  {
    type: 'edubotics_detect_marker',
    message0: DE.DETECT_MARKER,
    args0: [{ type: 'field_number', name: 'MARKER_ID', value: 0, min: 0, precision: 1 }],
    output: 'Array',
    colour: PERCEPTION_COLOR,
  },
  {
    type: 'edubotics_wait_until_marker',
    message0: DE.WAIT_UNTIL_MARKER,
    args0: [
      { type: 'field_number', name: 'MARKER_ID', value: 0, min: 0, precision: 1 },
      { type: 'field_number', name: 'TIMEOUT', value: 10, min: 1 },
    ],
    output: 'Boolean',
    colour: PERCEPTION_COLOR,
  },
  {
    type: 'edubotics_detect_object',
    message0: DE.DETECT_OBJECT,
    args0: [{ type: 'field_dropdown', name: 'CLASS', options: OBJECT_DROPDOWN }],
    output: 'Array',
    colour: PERCEPTION_COLOR,
  },
  {
    type: 'edubotics_wait_until_object',
    message0: DE.WAIT_UNTIL_OBJECT,
    args0: [
      { type: 'field_dropdown', name: 'CLASS', options: OBJECT_DROPDOWN },
      { type: 'field_number', name: 'TIMEOUT', value: 10, min: 1 },
    ],
    output: 'Boolean',
    colour: PERCEPTION_COLOR,
  },
  {
    type: 'edubotics_count_objects_class',
    message0: DE.COUNT_OBJECT,
    args0: [{ type: 'field_dropdown', name: 'CLASS', options: OBJECT_DROPDOWN }],
    output: 'Number',
    colour: PERCEPTION_COLOR,
  },
];

export function registerPerceptionBlocks() {
  Blockly.defineBlocksWithJsonArray(PERCEPTION_BLOCKS);
}
