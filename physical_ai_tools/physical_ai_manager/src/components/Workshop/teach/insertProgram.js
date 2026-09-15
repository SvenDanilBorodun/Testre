/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Als Programm einfügen": the round's items become ONE new top-level stack,
// in capture order, created in one event group (one undo step). It is never
// connected to an existing stack — top-level stacks run in CREATION order, so
// a join would silently change what an existing program does.
//
// No top-level access to Blockly members (page tests mock blockly/core
// minimally); everything Blockly happens inside insertProgram().

import * as Blockly from 'blockly/core';

import { placeGripperState, recordingGripperStates } from '../../../utils/armProfile';

export { placeGripperState, recordingGripperStates };

const ORIGIN = 20;
const GAP_X = 60;

// „Greifer merken": the gripper state each item was captured with, read from
// data the capture already carries — a recording's own gripper column, a
// place's S3 joint snapshot (WorkshopCapturePose.joint_positions). Nothing is
// subscribed and nothing is guessed: every unreadable case is null (unknown).
// The two pure classifiers live in utils/armProfile.js (dependency-free, so the
// Sammlung drawer can show a Position's gripper without pulling in Blockly).

/**
 * The overlay's `gripperStateOf(item)`: recordings → `{ start, end }` from the
 * rows the keep stored (`item.upload.rows`); a Position → `{ state }` from the
 * store entry `entryOf(item)` returns (null when it left the store). A Ziel is
 * always `{ state: null }`: the list and the drawer name a gripper state only
 * for Positionen (design §S3), and a Ziel touched with the claw closed for the
 * measurement must not insert an unexplained „schließe Greifer".
 */
export function makeGripperStateOf({ caps = null, entryOf = () => null } = {}) {
  return (item) => {
    if (!item) return null;
    if (item.kind === 'recording') {
      return recordingGripperStates(item.upload && item.upload.rows, caps);
    }
    if (item.kind !== 'pose') return { state: null };
    let entry = null;
    try {
      entry = entryOf(item);
    } catch (_) {
      entry = null;
    }
    return { state: placeGripperState(entry, caps) };
  };
}

function safeGripperState(gripperStateOf, item) {
  try {
    const s = gripperStateOf(item);
    return s && typeof s === 'object' ? s : null;
  } catch (_) {
    return null;
  }
}

const knownState = (v) => (v === 'open' || v === 'closed' ? v : null);

function recordingStatement(item) {
  if (item.status !== 'saved' || typeof item.name !== 'string' || !item.name) return null;
  return { type: 'edubotics_replay_trajectory', fields: { NAME: item.name } };
}

function placeStatement(item, placeNameOf) {
  let name = null;
  try {
    name = placeNameOf(item);
  } catch (_) {
    name = null;
  }
  if (typeof name !== 'string' || !name) return null;
  return {
    type: 'edubotics_move_to',
    inputs: { DESTINATION: { block: { type: 'edubotics_destination_ref', fields: { NAME: name } } } },
  };
}

/**
 * Pure. `items` are the overlay's „In dieser Runde" rows: recordings
 * (`{kind:'recording', name, status}`) and places (`{kind:'pose'|'pin'|'ziel',
 * name, entryId}`).
 *
 * opts.placeNameOf(item) → the place's CURRENT store name, or null when it is
 *   no longer in the store (skipped). Default: the row's own name.
 * opts.gripperStateOf(item) → recordings `{ start, end }`, places `{ state }`
 *   ('open' | 'closed' | null each), or null. Default `() => null`: no gripper
 *   block. A place whose known state differs from the last known state gets
 *   „schließe/öffne Greifer" AFTER its move; the first known state never emits
 *   (nothing to compare it with), and an unknown state never emits nor resets.
 *   Only EMITTED items advance the state — a skipped item moves no arm.
 *
 * @returns {{ json: object|null, count: number }} `count` = top-level statements.
 */
export function buildProgramBlocks(items, opts = {}) {
  const placeNameOf = typeof opts.placeNameOf === 'function' ? opts.placeNameOf : (item) => item.name;
  const gripperStateOf = typeof opts.gripperStateOf === 'function' ? opts.gripperStateOf : () => null;
  const statements = [];
  let prev = null;
  for (const item of Array.isArray(items) ? items : []) {
    if (item && item.kind === 'recording') {
      const s = recordingStatement(item);
      if (s) {
        statements.push(s);
        const g = safeGripperState(gripperStateOf, item);
        prev = (g && knownState(g.end)) || prev;
      }
    } else if (item && (item.kind === 'pose' || item.kind === 'pin' || item.kind === 'ziel')) {
      const s = placeStatement(item, placeNameOf);
      if (s) {
        statements.push(s);
        const g = safeGripperState(gripperStateOf, item);
        const state = g && knownState(g.state);
        if (state && prev && state !== prev) {
          statements.push({ type: state === 'closed' ? 'edubotics_close_gripper' : 'edubotics_open_gripper' });
        }
        prev = state || prev;
      }
    }
  }
  let json = null;
  for (let i = statements.length - 1; i >= 0; i -= 1) {
    json = json ? { ...statements[i], next: { block: json } } : { ...statements[i] };
  }
  return { json, count: statements.length };
}

// Right of everything already on the canvas, level with the topmost block.
function insertionOrigin(workspace) {
  const tops = workspace.getTopBlocks(false);
  if (!tops.length) return { x: ORIGIN, y: ORIGIN };
  const rects = tops.map((b) => b.getBoundingRectangle());
  return {
    x: Math.max(...rects.map((r) => r.right)) + GAP_X,
    y: Math.min(...rects.map((r) => r.top)),
  };
}

/** Append the program as a new stack; returns `{ blockId, count }`. */
export function insertProgram(workspace, items, opts = {}) {
  const { json, count } = buildProgramBlocks(items, opts);
  if (!workspace || !json || count === 0) return { blockId: null, count: 0 };
  let block = null;
  Blockly.Events.setGroup(true);
  try {
    const { x, y } = insertionOrigin(workspace);
    block = Blockly.serialization.blocks.append({ ...json, x, y }, workspace, { recordUndo: true });
  } finally {
    Blockly.Events.setGroup(false);
  }
  try {
    workspace.centerOnBlock(block.id);
  } catch (_) { /* a headless workspace has no scrollbars */ }
  try {
    Blockly.getFocusManager().focusNode(block);
  } catch (_) { /* focus is a convenience */ }
  return { blockId: block.id, count };
}
