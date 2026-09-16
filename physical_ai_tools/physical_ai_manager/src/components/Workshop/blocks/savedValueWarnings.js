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
import { nameValidator as destinationNameValidator } from './destinations';
import { counterNameValidator } from './counters';
import { eventNameValidator } from './events';
import { trajectoryNameValidator } from './trajectories';

// ---------------------------------------------------------------------------
// The editor half of „deserialization never rewrites a saved value"
// ---------------------------------------------------------------------------
//
// `blocks/fieldLoad.js` stops the field validators from repairing a saved name
// behind the student's back. The name is then still one the SERVER will refuse
// („Ungültiger Ziel-Name", `_DESTINATION_NAME_RE`) — so the editor says so, on
// the block, while the program is being written, instead of the student finding
// out mid-lesson. That is the same division of labour
// `attachControlWorkspaceValidators` uses for „wiederhole fortlaufend": the run
// time owns the refusal, the editor owns the early warning.
//
// It also carries the one limit the editor could always have known about and
// never said: `interpreter.py::MAX_LIST_CREATE_ITEMS` refuses a „erzeuge Liste
// mit" of more than 20 FILLED sockets, while ⊕ goes on forever (measured to 43).
//
// Both are KEYED warnings — `BlockSvg.setWarningText(text, id)` keeps one
// message per id, so this writer can never clobber RunControls' IK pre-check
// warnings — and neither touches the saved bytes: WarningIcon is not
// serializable, and a keyed warning fires no change event.

/** Warning id for a name the server will refuse. */
export const SAVED_NAME_WARNING_ID = 'edubotics_saved_name';
/** Warning id for a „erzeuge Liste mit" the server will refuse. */
export const LIST_SIZE_WARNING_ID = 'edubotics_list_too_long';

// Mirror of `interpreter.py::MAX_LIST_CREATE_ITEMS`. The server counts FILLED
// sockets, not the block's item count, so an empty socket is free — exactly
// what this check counts, or the warning would appear on a block that runs.
export const MAX_LIST_CREATE_ITEMS = 20;

// [block type, field name, the validator that judges an EDIT of that field].
// A saved value that the validator would have changed is a value the server
// refuses; the validator is the single source of the rule, so this table can
// never drift from what the editor enforces while typing.
const NAME_FIELDS = [
  ['edubotics_destination_pin', 'NAME', destinationNameValidator],
  ['edubotics_destination_ref', 'NAME', destinationNameValidator],
  ['edubotics_counter_reset', 'NAME', counterNameValidator],
  ['edubotics_counter_add', 'NAME', counterNameValidator],
  ['edubotics_counter_get', 'NAME', counterNameValidator],
  ['edubotics_when_counter_gt', 'NAME', counterNameValidator],
  ['edubotics_broadcast', 'EVENT_NAME', eventNameValidator],
  ['edubotics_when_broadcast', 'EVENT_NAME', eventNameValidator],
  ['edubotics_replay_trajectory', 'NAME', trajectoryNameValidator],
];

function filledListSockets(block) {
  return block.inputList.filter((input) => (
    /^ADD\d+$/.test(input.name)
    && input.connection
    && input.connection.targetBlock()
  )).length;
}

function checkNames(workspace) {
  NAME_FIELDS.forEach(([type, fieldName, validator]) => {
    if (typeof workspace.getBlocksByType !== 'function') return;
    workspace.getBlocksByType(type, false).forEach((block) => {
      if (!block || typeof block.setWarningText !== 'function') return;
      const value = block.getFieldValue(fieldName);
      if (typeof value !== 'string') return;
      // `null` means the validator refuses the value outright; any other
      // return that differs from the value means it would have rewritten it.
      const verdict = validator(value);
      const refused = verdict === null || verdict !== value;
      block.setWarningText(refused ? DE.SAVED_NAME_WARNING : null, SAVED_NAME_WARNING_ID);
    });
  });
}

function checkListLengths(workspace) {
  if (typeof workspace.getBlocksByType !== 'function') return;
  workspace.getBlocksByType('lists_create_with', false).forEach((block) => {
    if (!block || typeof block.setWarningText !== 'function') return;
    const tooLong = filledListSockets(block) > MAX_LIST_CREATE_ITEMS;
    block.setWarningText(tooLong ? DE.LIST_TOO_LONG_WARNING : null, LIST_SIZE_WARNING_ID);
  });
}

/**
 * Mark blocks whose saved value the robot will refuse.
 *
 * Shaped like `attachMotionWorkspaceValidators` /
 * `attachControlWorkspaceValidators`: takes the workspace, returns the disposer
 * the injection effect calls on teardown.
 *
 * @param {Blockly.Workspace} workspace The injected workspace.
 * @returns {() => void} Disposer that removes the change listener.
 */
export function attachSavedValueWarnings(workspace) {
  if (!workspace || typeof workspace.addChangeListener !== 'function') {
    return () => {};
  }
  const listener = (event) => {
    if (!event) return;
    // A MOVE changes which list sockets are FILLED, and nothing else here — so
    // it re-checks the lists only. Names cannot change by dragging, and a drag
    // is the one event that arrives in bursts; scanning nine block types per
    // move would put that work in the middle of every drag frame.
    const movesOnly = event.type === Blockly.Events.BLOCK_MOVE;
    if (
      !movesOnly
      && event.type !== Blockly.Events.FINISHED_LOADING
      && event.type !== Blockly.Events.BLOCK_CHANGE
      && event.type !== Blockly.Events.BLOCK_CREATE
      && event.type !== Blockly.Events.BLOCK_DELETE
    ) {
      return;
    }
    try {
      if (!movesOnly) checkNames(workspace);
      checkListLengths(workspace);
    } catch (_) { /* a diagnostic must never break the editor */ }
  };
  workspace.addChangeListener(listener);
  return () => {
    try {
      workspace.removeChangeListener(listener);
    } catch (_) { /* workspace disposed */ }
  };
}

export default attachSavedValueWarnings;
