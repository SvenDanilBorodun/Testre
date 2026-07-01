/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import * as Blockly from 'blockly/core';
import { DE } from './messages_de';

// Batch 2b — recorded-motion replay. A student records a hand-guided motion in
// the RecordPanel (saved as a named trajectory on the cloud workflow), then
// drops this block to replay it inside a program. The block carries only the
// trajectory NAME (a free-text field); the run bar (RunControls) collects every
// referenced name, fetches each trajectory's {fps, points} from the cloud, and
// injects them as a top-level `trajectories` sibling into the /workflow/start
// payload. The server resolves the name against ctx.trajectories at runtime.
//
// HARD CONTRACT with the Python interpreter (workflow/interpreter._build_args
// lowercases field names): the block type id `edubotics_replay_trajectory` and
// the field NAME → args['name'] are read verbatim server-side. Do not rename.
export const REPLAY_BLOCK_TYPE = 'edubotics_replay_trajectory';

// Reuse the Bewegung (motion) hue so a replayed motion visually groups with the
// other motion blocks in the palette + workspace.
const TRAJECTORY_COLOR = '#3b82f6';

// Keep names short + printable — they are dictionary keys in the runtime
// ctx.trajectories map and appear in log lines. Mirrors the STRONGER counter
// NAME validator (blocks/counters.js): the name is echoed into log lines, so
// reject the control + bracket chars that would break audit-log scraping or
// spoof a [VAR:..]/[TOAST:..] sentinel.
const NAME_MAX_LEN = 40;
function nameValidator(newValue) {
  if (typeof newValue !== 'string') return null;
  const trimmed = newValue.trim();
  if (trimmed === '') return null;
  // eslint-disable-next-line no-control-regex
  if (/[\r\n\0[\]]/.test(trimmed)) return null;
  return trimmed.slice(0, NAME_MAX_LEN);
}

export const TRAJECTORY_BLOCKS = [
  {
    type: REPLAY_BLOCK_TYPE,
    message0: DE.REPLAY_TRAJECTORY,
    args0: [{ type: 'field_input', name: 'NAME', text: 'Bewegung 1' }],
    previousStatement: null,
    nextStatement: null,
    colour: TRAJECTORY_COLOR,
    tooltip:
      'Spielt eine zuvor aufgenommene Bewegung ab (gleicher Name wie im '
      + 'Aufnahme-Feld). Nimm die Bewegung zuerst mit „Bewegung aufnehmen" auf '
      + 'und speichere sie unter genau diesem Namen.',
    extensions: ['edubotics_validate_trajectory_name'],
  },
];

function registerExtensionOnce(name, fn) {
  if (!Blockly.Extensions.isRegistered(name)) {
    Blockly.Extensions.register(name, fn);
  }
}

export function registerTrajectoryBlocks() {
  registerExtensionOnce('edubotics_validate_trajectory_name', function () {
    const field = this.getField('NAME');
    if (field && typeof field.setValidator === 'function') {
      field.setValidator(nameValidator);
    }
  });
  // HMR / Jest re-import guard — defineBlocksWithJsonArray throws on a
  // second definition of the same type (audit round-3 §A idiom).
  const toDefine = TRAJECTORY_BLOCKS.filter(
    (def) => !(def && def.type && Blockly.Blocks[def.type])
  );
  if (toDefine.length > 0) {
    Blockly.defineBlocksWithJsonArray(toDefine);
  }
}

/**
 * Walk a serialized Blockly workspace JSON tree and collect the trimmed,
 * de-duplicated NAME of every `edubotics_replay_trajectory` block, in
 * first-seen order. Used by RunControls to know which trajectories the program
 * references so it can fetch + inject them (CONTRACT C). Pure + side-effect
 * free so it is unit-testable without a live workspace.
 *
 * @param {object|null} blocklyJson - `Blockly.serialization.workspaces.save()` shape.
 * @returns {string[]} unique trajectory names referenced by replay blocks.
 */
export function collectReplayNames(blocklyJson) {
  const seen = new Set();
  const order = [];
  const visit = (node) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      node.forEach(visit);
      return;
    }
    // Skip replay blocks the student DISABLED. Blockly 12.5's
    // `serialization.workspaces.save()` serializes a disabled block as
    // `{ disabledReasons: ["manual"] }` with NO `enabled` key (it only writes
    // `disabledReasons` from `getDisabledReasons()` when `!isEnabled()`); older
    // serializations used `enabled: false`. Handle BOTH shapes — the old
    // `node.enabled !== false` guard let a disabled block slip through on real
    // Blockly-12 output (undefined !== false), so a disabled „spiele Bewegung ab"
    // whose trajectory was deleted STILL aborted the run at start (404 on the
    // name) even though the block never runs. Recursion into the block's `next`
    // chain continues below regardless (a disabled block can carry enabled ones).
    const isDisabled =
      node.enabled === false
      || (Array.isArray(node.disabledReasons) && node.disabledReasons.length > 0);
    if (node.type === REPLAY_BLOCK_TYPE && !isDisabled) {
      const raw = node.fields && node.fields.NAME;
      const name = typeof raw === 'string' ? raw.trim() : '';
      if (name && !seen.has(name)) {
        seen.add(name);
        order.push(name);
      }
    }
    // Recurse into nested block structures: inputs.{X}.{block|shadow},
    // next.block, and the top-level `blocks.blocks[]` array.
    if (node.blocks) visit(node.blocks);
    if (node.inputs) {
      Object.values(node.inputs).forEach((slot) => {
        if (slot && typeof slot === 'object') {
          visit(slot.block);
          visit(slot.shadow);
        }
      });
    }
    if (node.next && typeof node.next === 'object') {
      visit(node.next.block);
    }
  };
  visit(blocklyJson);
  return order;
}
