/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Shared setup of the robot_api ↔ block tests: every Workshop block registered
// exactly as BlocklyWorkspace.jsx does, and the robot_api.json rows that carry
// a block type.

import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import api from '../../code/robot_api.json';
import { registerMotionBlocks } from '../motion';
import { registerPerceptionBlocks, setObjectCatalogOptions } from '../perception';
import { registerDestinationBlocks } from '../destinations';
import { registerOutputBlocks } from '../output';
import { registerEventBlocks } from '../events';
import { registerControlBlocks } from '../control';
import { registerCounterBlocks } from '../counters';
import { registerTrajectoryBlocks } from '../trajectories';

export const ROW_BLOCKS = api.methods.filter((m) => typeof m.block_type === 'string');

export function registerEverything() {
  Blockly.setLocale(De);
  registerMotionBlocks();
  registerPerceptionBlocks();
  registerDestinationBlocks();
  registerOutputBlocks();
  registerEventBlocks();
  registerControlBlocks();
  registerCounterBlocks();
  registerTrajectoryBlocks();
  setObjectCatalogOptions([['Würfel', 'wuerfel']]);
}

export function fieldsOf(block) {
  const out = [];
  for (const input of block.inputList) for (const f of input.fieldRow) if (f.name) out.push(f);
  return out;
}

export function tooltipOf(block) {
  let t = block.tooltip;
  while (typeof t === 'function') t = t.call(block);
  return String(t || '');
}
