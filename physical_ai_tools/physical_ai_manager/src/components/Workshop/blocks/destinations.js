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

const DEST_COLOR = '#f59e0b';

export const DESTINATION_BLOCKS = [
  {
    type: 'edubotics_destination_pin',
    message0: DE.DESTINATION_PIN,
    args0: [{ type: 'field_input', name: 'NAME', text: 'A' }],
    previousStatement: null,
    nextStatement: null,
    colour: DEST_COLOR,
  },
  {
    type: 'edubotics_destination_current',
    message0: DE.DESTINATION_CURRENT,
    args0: [{ type: 'field_input', name: 'NAME', text: 'B' }],
    previousStatement: null,
    nextStatement: null,
    colour: DEST_COLOR,
  },
];

export function registerDestinationBlocks() {
  Blockly.defineBlocksWithJsonArray(DESTINATION_BLOCKS);
}
