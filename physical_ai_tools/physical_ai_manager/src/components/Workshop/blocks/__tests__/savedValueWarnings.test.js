/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The editor half of „a saved value is kept, not repaired": what the server
// will refuse is marked ON THE BLOCK while the program is being written.
//
// Both warnings are keyed, so they cannot clobber RunControls' IK pre-check
// warnings, and neither may change what the document saves.

import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerDestinationBlocks } from '../destinations';
import { registerCounterBlocks } from '../counters';
import { registerEventBlocks } from '../events';
import { registerTrajectoryBlocks } from '../trajectories';
import {
  attachSavedValueWarnings,
  MAX_LIST_CREATE_ITEMS,
  SAVED_NAME_WARNING_ID,
  LIST_SIZE_WARNING_ID,
} from '../savedValueWarnings';
import { DE } from '../messages_de';

const flush = () => new Promise((resolve) => {
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => { setTimeout(resolve, 0); });
  } else {
    setTimeout(resolve, 0);
  }
});

let workspace;
let dispose;

beforeAll(() => {
  Blockly.setLocale(De);
  registerDestinationBlocks();
  registerCounterBlocks();
  registerEventBlocks();
  registerTrajectoryBlocks();
});

beforeEach(() => {
  const host = document.createElement('div');
  document.body.appendChild(host);
  workspace = Blockly.inject(host, {});
  dispose = attachSavedValueWarnings(workspace);
});

afterEach(() => {
  dispose();
  workspace.dispose();
});

// The text Blockly is actually showing on the block, or null when the block
// carries no warning icon at all.
const warningOf = (block) => {
  const icon = block.getIcon && block.getIcon(Blockly.icons.WarningIcon.TYPE);
  return icon ? icon.getText() : null;
};

test('the two warning ids are distinct, so neither can clobber the other', () => {
  expect(SAVED_NAME_WARNING_ID).not.toBe(LIST_SIZE_WARNING_ID);
});

test('the list limit is pinned to the literal the server refuses at', () => {
  // The other half of this pin is `test_constant_pins.py`
  // (`workflow/interpreter.py`, `MAX_LIST_CREATE_ITEMS`, `20`). Two literals,
  // one on each side of the wire: moving the limit has to touch both files, so
  // the warning a student sees cannot drift away from the actual refusal.
  expect(MAX_LIST_CREATE_ITEMS).toBe(20);
});

test('a name the robot refuses is marked on the block, and the bytes are untouched', async () => {
  const block = Blockly.serialization.blocks.append(
    { type: 'edubotics_destination_pin', fields: { NAME: 'Ablage.1' } }, workspace,
  );
  await flush();

  expect(warningOf(block)).toContain(DE.SAVED_NAME_WARNING);
  // A warning is not serializable: the document still says exactly „Ablage.1".
  expect(Blockly.serialization.blocks.save(block).fields.NAME).toBe('Ablage.1');
});

test('a name the robot accepts carries no warning', async () => {
  const block = Blockly.serialization.blocks.append(
    { type: 'edubotics_destination_pin', fields: { NAME: 'Ablage 1' } }, workspace,
  );
  await flush();
  expect(warningOf(block)).toBeNull();
});

test('„erzeuge Liste mit" is flagged past the limit the server enforces', async () => {
  const block = Blockly.serialization.blocks.append(
    { type: 'lists_create_with', extraState: { itemCount: MAX_LIST_CREATE_ITEMS + 1 } }, workspace,
  );
  // Fill every socket — the server counts FILLED sockets, and so must we.
  for (let i = 0; i <= MAX_LIST_CREATE_ITEMS; i += 1) {
    const item = Blockly.serialization.blocks.append(
      { type: 'math_number', fields: { NUM: i } }, workspace,
    );
    block.getInput(`ADD${i}`).connection.connect(item.outputConnection);
  }
  await flush();
  expect(warningOf(block)).toContain(DE.LIST_TOO_LONG_WARNING);
});

test('a list at the limit is fine, and empty sockets do not count', async () => {
  const block = Blockly.serialization.blocks.append(
    { type: 'lists_create_with', extraState: { itemCount: MAX_LIST_CREATE_ITEMS + 5 } }, workspace,
  );
  for (let i = 0; i < MAX_LIST_CREATE_ITEMS; i += 1) {
    const item = Blockly.serialization.blocks.append(
      { type: 'math_number', fields: { NUM: i } }, workspace,
    );
    block.getInput(`ADD${i}`).connection.connect(item.outputConnection);
  }
  await flush();
  expect(warningOf(block)).toBeNull();
});
