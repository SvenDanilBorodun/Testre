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

const OUTPUT_COLOR = '#a855f7';

export const OUTPUT_BLOCKS = [
  {
    type: 'edubotics_log',
    message0: DE.LOG,
    args0: [{ type: 'input_value', name: 'MESSAGE' }],
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
  },
  {
    type: 'edubotics_play_sound',
    message0: DE.PLAY_SOUND,
    previousStatement: null,
    nextStatement: null,
    colour: OUTPUT_COLOR,
  },
];

export function registerOutputBlocks() {
  Blockly.defineBlocksWithJsonArray(OUTPUT_BLOCKS);
}
