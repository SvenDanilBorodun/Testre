/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
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
