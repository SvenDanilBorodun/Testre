/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * Which Sammlung assets the program on the MAIN workspace uses: replay and
 * reference blocks by name, the „Ziel setzen"/„Ziel hier merken" statements,
 * and variable uses. Read by the asset index (cards, chips) and the drawer.
 *
 * „Enabled" means the block will run: its own flag AND no disabled ancestor.
 * Counts keep enabled and disabled apart, because a card's usage chip counts
 * both while „missing" only counts blocks that would actually run.
 */

import * as Blockly from 'blockly/core';
import { REPLAY_BLOCK_TYPE } from '../blocks/trajectories';

const REF_TYPE = 'edubotics_destination_ref';
const PIN_TYPE = 'edubotics_destination_pin';
const CURRENT_TYPE = 'edubotics_destination_current';

function fieldName(block) {
  try {
    const raw = block.getFieldValue('NAME');
    return typeof raw === 'string' ? raw.trim() : '';
  } catch (_) {
    return '';
  }
}

function isRunnable(block) {
  try {
    return block.isEnabled() && !block.getInheritedDisabled();
  } catch (_) {
    return false;
  }
}

// X/Y/Z are read-only label fields holding a formatted metre value or the
// „—" sentinel of an unpinned block; Number('—') is NaN, which is the answer.
function labelNumber(block, name) {
  try {
    const raw = block.getFieldValue(name);
    if (typeof raw !== 'string' || raw.trim() === '') return NaN;
    return Number(raw);
  } catch (_) {
    return NaN;
  }
}

function countInto(map, name, block, enabled) {
  let row = map.get(name);
  if (!row) {
    row = { enabled: 0, disabled: 0, blockIds: [] };
    map.set(name, row);
  }
  if (enabled) row.enabled += 1;
  else row.disabled += 1;
  row.blockIds.push(block.id);
}

// First entry per name, upgraded once from a disabled block to an enabled one.
function shouldRecord(map, name, enabled) {
  const prev = map.get(name);
  return !prev || (!prev.enabled && enabled);
}

/**
 * @returns {{
 *   replay: Map<string,{enabled:number,disabled:number,blockIds:string[]}>,
 *   refs: Map<string,{enabled:number,disabled:number,blockIds:string[]}>,
 *   pinStatements: Map<string,{blockId:string,enabled:boolean,x:number,y:number,z:number}>,
 *   currentStatements: Map<string,{blockId:string,enabled:boolean}>,
 *   variableUses: Map<string,number>,
 * }}
 */
export function collectBlockUsage(workspace) {
  const usage = {
    replay: new Map(),
    refs: new Map(),
    pinStatements: new Map(),
    currentStatements: new Map(),
    variableUses: new Map(),
  };
  if (!workspace || typeof workspace.getAllBlocks !== 'function') return usage;
  // A flyout's blocks are offers, not uses.
  if (workspace.isFlyout) return usage;

  let blocks = [];
  try {
    blocks = workspace.getAllBlocks(false);
  } catch (_) {
    return usage;
  }
  for (const block of blocks) {
    if (!block || block.isInFlyout) continue;
    const { type } = block;
    if (type !== REPLAY_BLOCK_TYPE && type !== REF_TYPE
      && type !== PIN_TYPE && type !== CURRENT_TYPE) continue;
    const name = fieldName(block);
    if (!name) continue;
    const enabled = isRunnable(block);
    if (type === REPLAY_BLOCK_TYPE) {
      countInto(usage.replay, name, block, enabled);
    } else if (type === REF_TYPE) {
      countInto(usage.refs, name, block, enabled);
    } else if (type === PIN_TYPE) {
      // The first ENABLED statement in creation order wins; a disabled one is
      // kept only until an enabled one of the same name turns up. A disabled
      // block never runs, so letting it shadow a later enabled pin reported a
      // „missing Ziel" for a name the run resolves fine.
      if (shouldRecord(usage.pinStatements, name, enabled)) {
        usage.pinStatements.set(name, {
          blockId: block.id,
          enabled,
          x: labelNumber(block, 'X'),
          y: labelNumber(block, 'Y'),
          z: labelNumber(block, 'Z'),
        });
      }
    } else if (shouldRecord(usage.currentStatements, name, enabled)) {
      usage.currentStatements.set(name, { blockId: block.id, enabled });
    }
  }

  let variables = [];
  try {
    variables = workspace.getVariableMap().getAllVariables();
  } catch (_) {
    variables = [];
  }
  for (const variable of variables) {
    const id = typeof variable.getId === 'function' ? variable.getId() : variable.id;
    let uses = [];
    try {
      uses = Blockly.Variables.getVariableUsesById(workspace, id) || [];
    } catch (_) {
      uses = [];
    }
    usage.variableUses.set(id, uses.length);
  }
  return usage;
}

/** Scroll the editor to a block and focus it; never throws. */
export function jumpToBlock(workspace, blockId) {
  if (!workspace || !blockId) return;
  try {
    workspace.centerOnBlock(blockId, true);
    const block = workspace.getBlockById(blockId);
    if (block) Blockly.getFocusManager().focusNode(block);
  } catch (_) {
    // A block deleted in between, or a workspace without rendering.
  }
}
