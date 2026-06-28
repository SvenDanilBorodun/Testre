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

// Phase-2 control blocks live in the Logik category, so they take the
// Logik hue (#eab308 in toolbox.js) for visual consistency.
const CONTROL_COLOR = '#eab308';

// HARD CONTRACT with the Python interpreter (workflow/interpreter.py btype
// ladder): the type ids and input NAMEs below are read verbatim server-side.
//   edubotics_forever      → statement input `DO`
//   edubotics_wait_until   → value input `BOOL` (Boolean)
// Do not rename either id or input.
export const CONTROL_BLOCKS = [
  {
    // „wiederhole fortlaufend" — Scratch-style forever / NEPO
    // „wiederhole unendlich". A C-block whose body (DO) repeats until the
    // student presses Stopp; the interpreter caps iterations as a backstop.
    type: 'edubotics_forever',
    message0: DE.FOREVER,
    message1: '%1',
    args1: [{ type: 'input_statement', name: 'DO' }],
    previousStatement: null,
    nextStatement: null,
    colour: CONTROL_COLOR,
    tooltip:
      'Wiederholt die enthaltenen Blöcke fortlaufend, bis du auf „Stopp" '
      + 'drückst.',
  },
  {
    // „warte bis <Bedingung>" — NEPO „warte bis" / Scratch „wait until".
    // Polls the Boolean condition until it is true (or the run stops).
    type: 'edubotics_wait_until',
    message0: DE.WAIT_UNTIL,
    args0: [{ type: 'input_value', name: 'BOOL', check: 'Boolean' }],
    previousStatement: null,
    nextStatement: null,
    colour: CONTROL_COLOR,
    tooltip:
      'Hält an, bis die Bedingung erfüllt ist, und läuft dann mit dem '
      + 'nächsten Block weiter.',
  },
];

export function registerControlBlocks() {
  // Skip re-definition on HMR / Jest re-import — Blockly.defineBlocksWithJsonArray
  // throws "Block type X is already defined" on the second landing. Audit round-3 §A.
  const toDefine = CONTROL_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}
