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

// Sentinel value shown in the X/Y/Z labels of a fresh destination_pin
// block. Updated to the world coordinates returned by /workshop/mark_destination
// when the student clicks the scene camera while this block is selected.
// The runtime handler (workflow/handlers/destinations.py) parses these
// fields directly — see audit §1.4 for why a ctx.z_table fallback is
// dangerous (silently maps a never-pinned block to z = z_table on the
// table plane and the gripper crashes into the table).
const UNPINNED = '—';

// Validator for the destination's NAME input. Names appear in
// log lines and as keys in the runtime destinations dict; keep them
// short and printable. We strip leading/trailing whitespace and reject
// the sentinel string so a student can't name a block "—".
const NAME_MAX_LEN = 24;
function nameValidator(newValue) {
  if (typeof newValue !== 'string') return null;
  const trimmed = newValue.trim();
  if (trimmed === '' || trimmed === UNPINNED) return null;
  return trimmed.slice(0, NAME_MAX_LEN);
}

export const DESTINATION_BLOCKS = [
  {
    type: 'edubotics_destination_pin',
    // Two-line layout: name on top, "x= y= z=" read-only labels below
    // so the teacher and student can see whether the block has been
    // pinned yet. message1 + args1 is Blockly's idiom for a second
    // row in the same block.
    message0: DE.DESTINATION_PIN,
    args0: [{ type: 'field_input', name: 'NAME', text: 'A' }],
    message1: 'x %1  y %2  z %3',
    args1: [
      { type: 'field_label_serializable', name: 'X', text: UNPINNED },
      { type: 'field_label_serializable', name: 'Y', text: UNPINNED },
      { type: 'field_label_serializable', name: 'Z', text: UNPINNED },
    ],
    previousStatement: null,
    nextStatement: null,
    colour: DEST_COLOR,
    tooltip:
      'Wähle diesen Block aus und klicke dann in die Szenen-Kamera, um '
      + 'das Ziel zu setzen.',
    extensions: ['edubotics_validate_destination_name'],
  },
  {
    type: 'edubotics_destination_current',
    message0: DE.DESTINATION_CURRENT,
    args0: [{ type: 'field_input', name: 'NAME', text: 'B' }],
    previousStatement: null,
    nextStatement: null,
    colour: DEST_COLOR,
    extensions: ['edubotics_validate_destination_name'],
  },
];

/**
 * Update the X/Y/Z labels of a destination_pin block in-place. Called
 * from WorkshopPage's onMark handler after /workshop/mark_destination
 * resolves. Returns true if the block was a destination_pin and the
 * fields were updated, false otherwise (so the caller can warn that
 * the click had no target).
 */
export function applyPinnedCoordinates(block, world_x, world_y, world_z) {
  if (!block || block.type !== 'edubotics_destination_pin') return false;
  const fmt = (v) => Number(v).toFixed(3);
  block.setFieldValue(fmt(world_x), 'X');
  block.setFieldValue(fmt(world_y), 'Y');
  block.setFieldValue(fmt(world_z), 'Z');
  return true;
}

function registerExtensionOnce(name, fn) {
  if (!Blockly.Extensions.isRegistered(name)) {
    Blockly.Extensions.register(name, fn);
  }
}

export function registerDestinationBlocks() {
  registerExtensionOnce('edubotics_validate_destination_name', function () {
    const field = this.getField('NAME');
    if (field && typeof field.setValidator === 'function') {
      field.setValidator(nameValidator);
    }
  });
  // Skip re-definition on HMR / Jest re-import. Audit round-3 §A.
  const toDefine = DESTINATION_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}
