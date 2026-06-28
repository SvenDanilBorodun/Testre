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

const EVENT_COLOR = '#ec4899';

// Hat blocks ("when X happens") have no `previousStatement` so they
// only appear at the top of the workspace. The runtime interpreter
// (overlays/workflow/interpreter.py) collects them as parallel handler
// stacks; a motion_lock in WorkflowContext keeps motion serialized so
// two handlers can't trigger arm motion simultaneously.
const HAT_SHAPE = {
  // No previousStatement — this makes the block a hat (top-only).
  nextStatement: null,
};

const NAME_MAX_LEN = 40;
function eventNameValidator(newValue) {
  if (typeof newValue !== 'string') return null;
  const trimmed = newValue.trim();
  if (trimmed === '') return null;
  // Forbid characters that would make audit log scraping awkward.
  if (/[\r\n\0]/.test(trimmed)) return null;
  return trimmed.slice(0, NAME_MAX_LEN);
}

export const EVENT_BLOCKS = [
  {
    type: 'edubotics_broadcast',
    message0: DE.BROADCAST,
    args0: [{ type: 'field_input', name: 'EVENT_NAME', text: 'start' }],
    previousStatement: null,
    nextStatement: null,
    colour: EVENT_COLOR,
    tooltip:
      'Sendet ein Ereignis an alle "wenn …" Hat-Blöcke mit dem '
      + 'gleichen Namen.',
    extensions: ['edubotics_validate_event_name'],
  },
  {
    type: 'edubotics_when_broadcast',
    message0: DE.WHEN_BROADCAST,
    args0: [{ type: 'field_input', name: 'EVENT_NAME', text: 'start' }],
    ...HAT_SHAPE,
    colour: EVENT_COLOR,
    tooltip:
      'Hat-Block: läuft jedes Mal, wenn ein Ereignis mit diesem Namen '
      + 'gesendet wird.',
    extensions: ['edubotics_validate_event_name'],
  },
];

function registerExtensionOnce(name, fn) {
  if (!Blockly.Extensions.isRegistered(name)) {
    Blockly.Extensions.register(name, fn);
  }
}

export function registerEventBlocks() {
  registerExtensionOnce('edubotics_validate_event_name', function () {
    const f = this.getField('EVENT_NAME');
    if (f && typeof f.setValidator === 'function') {
      f.setValidator(eventNameValidator);
    }
  });
  // Audit round-3 §A — guard against re-definition on hot-reload or
  // Jest re-import. Blockly.defineBlocksWithJsonArray throws "Block
  // type X is already defined" the second time a definition lands.
  // Skip entries whose type is already registered so HMR doesn't crash.
  const toDefine = EVENT_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}
