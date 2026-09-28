/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// buildProgramSteps: the ONE reading of a Vormachen round („Als Programm
// einfügen") that both the Blockly stack and the code lines are built from.
// buildProgramBlocks is its block rendering and must stay byte-identical to
// what it produced before the split (insertProgram.test.jsx pins that).

import { describe, it, expect } from 'vitest';
import { buildProgramBlocks, buildProgramSteps, makeGripperStateOf } from '../insertProgram';

const OMX_NAMES = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'];
const poseEntry = (grip) => ({ joints: [0, -0.9, 1.1, 0.3, 0, grip], joint_names: OMX_NAMES });
const omxRow = (grip, t) => [0, 0, 0, 0, 0, grip, t];

const ENTRIES = { a: poseEntry(0.8), b: poseEntry(-0.3), z: poseEntry(0.8) };
const OPTS = {
  placeNameOf: (item) => (ENTRIES[item.entryId] ? `N-${item.entryId}` : null),
  gripperStateOf: makeGripperStateOf({ caps: null, entryOf: (item) => ENTRIES[item.entryId] || null }),
};
const ITEMS = [
  { kind: 'recording', name: 'B1', status: 'saved', upload: { rows: [omxRow(0.8, 0), omxRow(-0.4, 1)] } },
  { kind: 'recording', name: 'B2', status: 'failed' },
  { kind: 'pose', name: 'P1', entryId: 'a' },
  { kind: 'ziel', name: 'Z1', entryId: 'gone' },
  { kind: 'pose', name: 'P2', entryId: 'b' },
  { kind: 'pin', name: 'Z2', entryId: 'z' },
];

describe('buildProgramSteps', () => {
  it('is the round as steps, gripper steps included', () => {
    expect(buildProgramSteps(ITEMS, OPTS)).toEqual([
      { type: 'replay', name: 'B1' },
      { type: 'move_to', name: 'N-a' },
      { type: 'open_gripper' },
      { type: 'move_to', name: 'N-b' },
      { type: 'close_gripper' },
      { type: 'move_to', name: 'N-z' },
    ]);
  });

  it('is what buildProgramBlocks renders, one block per step, in order', () => {
    const { json, count } = buildProgramBlocks(ITEMS, OPTS);
    const types = [];
    for (let b = json; b; b = b.next && b.next.block) types.push(b.type);
    expect(types).toEqual([
      'edubotics_replay_trajectory', 'edubotics_move_to', 'edubotics_open_gripper',
      'edubotics_move_to', 'edubotics_close_gripper', 'edubotics_move_to',
    ]);
    expect(count).toBe(buildProgramSteps(ITEMS, OPTS).length);
  });

  it('is empty on nothing insertable and total on garbage', () => {
    expect(buildProgramSteps([])).toEqual([]);
    expect(buildProgramSteps(null)).toEqual([]);
    expect(buildProgramSteps([{ kind: 'recording', name: 'X', status: 'saving' }, null, 5])).toEqual([]);
  });
});
