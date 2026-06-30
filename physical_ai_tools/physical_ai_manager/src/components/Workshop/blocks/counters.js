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

// Cyan hue, distinct from the existing category colours; mirrored by the
// „Zähler" toolbox category in toolbox.js.
const COUNTER_COLOR = '#0891b2';

// Hat blocks ("wenn …") have no `previousStatement`, so they only appear at
// the top of the workspace (mirrors events.js HAT_SHAPE). The interpreter
// collects them via HAT_BLOCK_TYPES + WorkflowManager runs each as its own
// edge-triggered handler stack.
const HAT_SHAPE = {
  nextStatement: null,
};

// The counter NAME is used verbatim as the server-side dict key (interpreter
// lowercases the field KEY → arg `name`, never the VALUE). Trim, forbid the
// control + bracket chars that would break audit-log scraping or spoof a
// [TOAST:..]/[VAR:..] sentinel, and cap the length. Mirrors events.js.
const NAME_MAX_LEN = 40;
function counterNameValidator(newValue) {
  if (typeof newValue !== 'string') return null;
  const trimmed = newValue.trim();
  if (trimmed === '') return null;
  // eslint-disable-next-line no-control-regex
  if (/[\r\n\0[\]]/.test(trimmed)) return null;
  return trimmed.slice(0, NAME_MAX_LEN);
}

// HARD CONTRACT with the Python runtime:
//   edubotics_counter_reset    → STATEMENT_HANDLERS (counters.reset),  field NAME
//   edubotics_counter_add      → STATEMENT_HANDLERS (counters.add),    field NAME
//   edubotics_counter_get      → VALUE_EVALUATORS  (counters.get),     field NAME
//   edubotics_when_counter_gt  → HAT_BLOCK_TYPES (interpreter) +
//                                WorkflowManager._wait_for_hat_trigger, fields
//                                NAME + N (threshold)
// Do not rename the type ids or field NAMEs.
export const COUNTER_BLOCKS = [
  {
    type: 'edubotics_counter_reset',
    message0: DE.COUNTER_RESET,
    args0: [{ type: 'field_input', name: 'NAME', text: 'Punkte' }],
    previousStatement: null,
    nextStatement: null,
    colour: COUNTER_COLOR,
    tooltip: 'Setzt den genannten Zähler auf 0 zurück.',
    extensions: ['edubotics_validate_counter_name'],
  },
  {
    type: 'edubotics_counter_add',
    message0: DE.COUNTER_ADD,
    args0: [{ type: 'field_input', name: 'NAME', text: 'Punkte' }],
    previousStatement: null,
    nextStatement: null,
    colour: COUNTER_COLOR,
    tooltip: 'Zählt den genannten Zähler um 1 hoch.',
    extensions: ['edubotics_validate_counter_name'],
  },
  {
    type: 'edubotics_counter_get',
    message0: DE.COUNTER_GET,
    args0: [{ type: 'field_input', name: 'NAME', text: 'Punkte' }],
    output: 'Number',
    colour: COUNTER_COLOR,
    tooltip: 'Liefert den aktuellen Wert des genannten Zählers.',
    extensions: ['edubotics_validate_counter_name'],
  },
  {
    type: 'edubotics_when_counter_gt',
    message0: DE.WHEN_COUNTER_GT,
    args0: [
      { type: 'field_input', name: 'NAME', text: 'Punkte' },
      { type: 'field_number', name: 'N', value: 3, min: 0, precision: 1 },
    ],
    ...HAT_SHAPE,
    colour: COUNTER_COLOR,
    tooltip:
      'Hat-Block: läuft, sobald der genannte Zähler größer als die Zahl wird.',
    extensions: ['edubotics_validate_counter_name'],
  },
];

function registerExtensionOnce(name, fn) {
  if (!Blockly.Extensions.isRegistered(name)) {
    Blockly.Extensions.register(name, fn);
  }
}

export function registerCounterBlocks() {
  registerExtensionOnce('edubotics_validate_counter_name', function () {
    const f = this.getField('NAME');
    if (f && typeof f.setValidator === 'function') {
      f.setValidator(counterNameValidator);
    }
  });
  // Skip re-definition on HMR / Jest re-import (defineBlocksWithJsonArray throws
  // "Block type X is already defined" the second time). Mirrors events.js §A.
  const toDefine = COUNTER_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}
