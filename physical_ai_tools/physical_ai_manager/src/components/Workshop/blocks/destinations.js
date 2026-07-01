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

// Batch 2b — the per-block „fahre dorthin" button. A small clickable play glyph
// on every destination_pin block. Clicking it drives the REAL follower to that
// pinned point via /workshop/jog (mode 'drive_to'). Blockly blocks live outside
// React, so the field's click handler bridges to WorkshopPage through a
// module-level accessor (the same idiom perception.js uses for the object
// catalog): WorkshopPage registers a handler (jogArm + German confirm/toast) via
// setDriveToHandler; the field click reads the block's stored coords and calls
// it. `_driveToHandler` is a MODULE-level singleton — WorkshopPage registers ONE
// handler for the whole page (not per-block), so a flyout-block click is NOT a
// no-op via "no handler registered". It is safe because: (a) driveToFromBlock
// early-returns while no handler is registered (before WorkshopPage mounts /
// after it unmounts), and (b) an un-pinned block's X/Y/Z read as the „—" sentinel
// → NaN, which the registered handler REFUSES in German (the „— / NaN" guard in
// WorkshopPage). The icon is an inline ASCII-only SVG (base64-safe, offline).
const DRIVE_ICON =
  'data:image/svg+xml;base64,'
  + btoa(
    "<svg xmlns='http://www.w3.org/2000/svg' width='22' height='16'>"
    + "<rect x='0' y='0' width='22' height='16' rx='3' fill='#0f766e'/>"
    + "<path d='M8 4 L15 8 L8 12 Z' fill='#ffffff'/></svg>",
  );

let _driveToHandler = null;

// WorkshopPage wires this to a jogArm-backed handler (with German confirm +
// toast). Pass a function taking { name, x, y, z }; pass null to unregister.
export function setDriveToHandler(fn) {
  _driveToHandler = typeof fn === 'function' ? fn : null;
}

// The logic the „fahre dorthin" field click runs. Reads the block's NAME/X/Y/Z
// (X/Y/Z are the read-only labels updated by the camera click; un-pinned reads
// as the „—" sentinel → NaN, which the registered handler rejects in German).
// Exported so it can be unit-tested without simulating a real Blockly click.
export function driveToFromBlock(block) {
  if (!block || typeof block.getFieldValue !== 'function') return;
  if (typeof _driveToHandler !== 'function') return;
  const name = block.getFieldValue('NAME') || '';
  const x = Number(block.getFieldValue('X'));
  const y = Number(block.getFieldValue('Y'));
  const z = Number(block.getFieldValue('Z'));
  _driveToHandler({ name, x, y, z });
}

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
      + 'das Ziel zu setzen. Mit „fahre dorthin" fährt der Roboter direkt '
      + 'zu diesem Punkt.',
    extensions: [
      'edubotics_validate_destination_name',
      'edubotics_destination_drive_button',
    ],
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
  {
    // WS3: a VALUE reference to a pinned destination. destination_pin is a
    // STATEMENT (it can't plug into a value socket), so move_to / drop_at had
    // no producible value to fill their DESTINATION input. This block outputs
    // the pinned NAME string; the runtime resolves it against ctx.destinations.
    type: 'edubotics_destination_ref',
    message0: DE.DESTINATION_REF,
    args0: [{ type: 'field_input', name: 'NAME', text: 'A' }],
    output: 'String',
    colour: DEST_COLOR,
    tooltip:
      'Verweist auf ein gepinntes Ziel (gleicher Name wie ein „Pin"-Block). '
      + 'Ziehe diesen Block in „bewege zu" oder „lege ab bei".',
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
  // Batch 2b — append the clickable „fahre dorthin" button as a third row on the
  // pin block. FieldImage with an onClick is Blockly's button-in-block idiom;
  // the field is non-serializable so it adds nothing to the saved workflow. The
  // click bridges to WorkshopPage via driveToFromBlock → the registered handler.
  registerExtensionOnce('edubotics_destination_drive_button', function () {
    const btn = new Blockly.FieldImage(
      DRIVE_ICON,
      22,
      16,
      'fahre dorthin',
      (field) => driveToFromBlock(field && field.getSourceBlock && field.getSourceBlock()),
    );
    this.appendDummyInput('DRIVE_TO_ROW')
      .appendField('fahre dorthin')
      .appendField(btn, 'DRIVE_BTN');
  });
  // Skip re-definition on HMR / Jest re-import. Audit round-3 §A.
  const toDefine = DESTINATION_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}
