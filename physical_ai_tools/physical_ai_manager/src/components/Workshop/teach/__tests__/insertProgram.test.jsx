/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Als Programm einfügen" on a real, injected Blockly workspace.

import { describe, it, expect, beforeAll, beforeEach, afterEach } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { registerMotionBlocks } from '../../blocks/motion';
import {
  buildProgramBlocks, insertProgram, makeGripperStateOf, placeGripperState, recordingGripperStates,
} from '../insertProgram';

const flushEvents = async () => {
  await new Promise((resolve) => {
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => { setTimeout(resolve, 0); });
    else setTimeout(resolve, 0);
  });
  await new Promise((resolve) => { setTimeout(resolve, 0); });
};

// jsdom lays nothing out; Blockly sizes from the injection div and measures
// text with getBBox / getComputedTextLength, which jsdom lacks.
const realOffset = {
  width: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth'),
  height: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight'),
};

const ITEMS = [
  { kind: 'recording', name: 'Bewegung 1', status: 'saved' },
  { kind: 'recording', name: 'Bewegung 2', status: 'failed' },
  { kind: 'pose', name: 'Position 1', entryId: 'd_pose' },
  { kind: 'ziel', name: 'Ziel 1', entryId: 'd_gone' },
  { kind: 'recording', name: 'Bewegung 3', status: 'saving' },
  { kind: 'ziel', name: 'Ziel 2', entryId: 'd_pin' },
];
const STORE = { d_pose: 'Position 1', d_pin: 'Ablage' };
const placeNameOf = (item) => STORE[item.entryId] || null;

const moveTo = (name) => ({
  type: 'edubotics_move_to',
  inputs: { DESTINATION: { block: { type: 'edubotics_destination_ref', fields: { NAME: name } } } },
});

describe('buildProgramBlocks (pure)', () => {
  it('chains saved recordings and places still in the store, in capture order', () => {
    const { json, count } = buildProgramBlocks(ITEMS, { placeNameOf, gripperStateOf: () => null });
    expect(count).toBe(3);
    expect(json).toEqual({
      type: 'edubotics_replay_trajectory',
      fields: { NAME: 'Bewegung 1' },
      next: { block: { ...moveTo('Position 1'), next: { block: moveTo('Ablage') } } },
    });
  });

  it('nothing insertable → count 0 and no json', () => {
    expect(buildProgramBlocks([{ kind: 'recording', name: 'B', status: 'failed' }])).toEqual({ json: null, count: 0 });
    expect(buildProgramBlocks([])).toEqual({ json: null, count: 0 });
  });

  it('a throwing store lookup skips the place instead of aborting', () => {
    const r = buildProgramBlocks([{ kind: 'pose', name: 'P', entryId: 'x' }], {
      placeNameOf: () => { throw new Error('gone'); },
    });
    expect(r.count).toBe(0);
  });
});

// „Greifer merken" — gripper blocks exactly where the captured state changed.
const chainTypes = (json) => {
  const out = [];
  for (let b = json; b; b = b.next && b.next.block) out.push(b.type);
  return out;
};
const OMX_NAMES = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'];
const poseEntry = (grip, names = OMX_NAMES) => ({
  joints: [0, -0.9, 1.1, 0.3, 0, grip], joint_names: names,
});
const omxRow = (grip, t) => [0, 0, 0, 0, 0, grip, t];

describe('buildProgramBlocks — „Greifer merken"', () => {
  const places = (entries) => {
    const entryOf = (item) => entries[item.entryId] || null;
    return {
      placeNameOf: (item) => (entries[item.entryId] ? item.name : null),
      gripperStateOf: makeGripperStateOf({ caps: null, entryOf }),
    };
  };

  it('pose open → pose closed emits „schließe Greifer" after the second move', () => {
    const opts = places({ a: poseEntry(0.8), b: poseEntry(-0.3) });
    const { json, count } = buildProgramBlocks([
      { kind: 'pose', name: 'P1', entryId: 'a' },
      { kind: 'pose', name: 'P2', entryId: 'b' },
    ], opts);
    expect(chainTypes(json)).toEqual([
      'edubotics_move_to', 'edubotics_move_to', 'edubotics_close_gripper',
    ]);
    expect(json.next.block.inputs.DESTINATION.block.fields.NAME).toBe('P2');
    expect(count).toBe(3);
  });

  it('a recording ending closed → pose open emits „öffne Greifer" after the pose', () => {
    const opts = places({ a: poseEntry(0.8) });
    const { json } = buildProgramBlocks([
      { kind: 'recording', name: 'B1', status: 'saved', upload: { rows: [omxRow(0.8, 0), omxRow(-0.4, 1)] } },
      { kind: 'pose', name: 'P1', entryId: 'a' },
    ], opts);
    expect(chainTypes(json)).toEqual([
      'edubotics_replay_trajectory', 'edubotics_move_to', 'edubotics_open_gripper',
    ]);
  });

  it('the same state twice emits nothing, and the first known state never emits', () => {
    const opts = places({ a: poseEntry(-0.4), b: poseEntry(-0.2) });
    const { json } = buildProgramBlocks([
      { kind: 'pose', name: 'P1', entryId: 'a' },
      { kind: 'ziel', name: 'Z1', entryId: 'b' },
    ], opts);
    expect(chainTypes(json)).toEqual(['edubotics_move_to', 'edubotics_move_to']);
  });

  it('unknown states (in the band, no joints, old server) emit nothing and do not reset', () => {
    const opts = places({
      a: poseEntry(0.8), b: poseEntry(0.35), c: { }, d: poseEntry(0.8),
    });
    const { json } = buildProgramBlocks([
      { kind: 'pose', name: 'P1', entryId: 'a' },
      { kind: 'pose', name: 'P2', entryId: 'b' },
      { kind: 'pose', name: 'P3', entryId: 'c' },
      { kind: 'pose', name: 'P4', entryId: 'd' },
    ], opts);
    expect(chainTypes(json)).toEqual(Array(4).fill('edubotics_move_to'));
  });

  it('a mismatched gripper joint name is unknown', () => {
    const edu6Names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'end_gear_joint'];
    expect(placeGripperState(poseEntry(-0.3, edu6Names), null)).toBeNull();
    const opts = places({ a: poseEntry(0.8), b: poseEntry(-0.3, edu6Names) });
    const { json } = buildProgramBlocks([
      { kind: 'pose', name: 'P1', entryId: 'a' },
      { kind: 'pose', name: 'P2', entryId: 'b' },
    ], opts);
    expect(chainTypes(json)).toEqual(['edubotics_move_to', 'edubotics_move_to']);
  });

  it('a skipped item (unsaved recording, place gone) never advances the state', () => {
    const opts = places({ b: poseEntry(0.8) });
    const { json } = buildProgramBlocks([
      { kind: 'recording', name: 'B1', status: 'failed', upload: { rows: [omxRow(-0.4, 0), omxRow(-0.4, 1)] } },
      { kind: 'pose', name: 'gone', entryId: 'a' },
      { kind: 'pose', name: 'P2', entryId: 'b' },
    ], opts);
    expect(chainTypes(json)).toEqual(['edubotics_move_to']);
  });

  it('a throwing gripperStateOf degrades to no gripper block', () => {
    const { json } = buildProgramBlocks([
      { kind: 'pose', name: 'P1' }, { kind: 'pose', name: 'P2' },
    ], { gripperStateOf: () => { throw new Error('boom'); } });
    expect(chainTypes(json)).toEqual(['edubotics_move_to', 'edubotics_move_to']);
  });

  it('recordingGripperStates reads the profile gripper column of exact-width rows only', () => {
    expect(recordingGripperStates([omxRow(0.8, 0), omxRow(-0.4, 1)], null))
      .toEqual({ start: 'open', end: 'closed' });
    const edu6Caps = {
      arm_joints: 6,
      joint_names: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6', 'end_gear_joint'],
      sim_close_threshold_rad: 1.5,
      gripper_open_rad: 1.75,
    };
    // A 7-wide OMX row under edu6 caps is not an edu6 take → unknown.
    expect(recordingGripperStates([omxRow(0.8, 0)], edu6Caps)).toEqual({ start: null, end: null });
    expect(recordingGripperStates([[0, 0, 0, 0, 0, 0, 1.0, 0], [0, 0, 0, 0, 0, 0, 1.75, 1]], edu6Caps))
      .toEqual({ start: 'closed', end: 'open' });
    expect(recordingGripperStates([], null)).toEqual({ start: null, end: null });
    expect(recordingGripperStates(undefined, null)).toEqual({ start: null, end: null });
  });
});

describe('insertProgram (real Blockly)', () => {
  let host;
  let ws;

  beforeAll(() => {
    Blockly.setLocale(De);
    registerTrajectoryBlocks();
    registerDestinationBlocks();
    registerMotionBlocks();
  });

  beforeEach(() => {
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get() { return 800; } });
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get() { return 600; } });
    if (!SVGElement.prototype.getBBox) {
      SVGElement.prototype.getBBox = () => ({ x: 0, y: 0, width: 40, height: 16 });
    }
    if (!SVGElement.prototype.getComputedTextLength) {
      SVGElement.prototype.getComputedTextLength = () => 40;
    }
    host = document.createElement('div');
    document.body.appendChild(host);
    ws = Blockly.inject(host, {});
  });

  afterEach(() => {
    ws.dispose();
    host.remove();
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', realOffset.width);
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', realOffset.height);
  });

  function addStack(x, y) {
    const block = Blockly.serialization.blocks.append({
      type: 'edubotics_replay_trajectory', x, y, fields: { NAME: 'Alt' },
      next: { block: { type: 'edubotics_replay_trajectory', fields: { NAME: 'Alt 2' } } },
    }, ws);
    return block;
  }

  const intersects = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

  it('on an empty canvas the stack lands at (20, 20) and counts its statements', () => {
    const r = insertProgram(ws, ITEMS, { placeNameOf });
    expect(r.count).toBe(3);
    const block = ws.getBlockById(r.blockId);
    expect(block.type).toBe('edubotics_replay_trajectory');
    expect(block.getRelativeToSurfaceXY()).toMatchObject({ x: 20, y: 20 });
    expect(ws.getTopBlocks(false)).toHaveLength(1);
    const types = [];
    for (let b = block; b; b = b.getNextBlock()) types.push(b.type);
    expect(types).toEqual(['edubotics_replay_trajectory', 'edubotics_move_to', 'edubotics_move_to']);
  });

  it('never joins an existing stack and never overlaps an existing top block', () => {
    const a = addStack(10, 40);
    const b = addStack(300, -30);
    const r = insertProgram(ws, ITEMS, { placeNameOf });
    const inserted = ws.getBlockById(r.blockId);
    expect(inserted.getParent()).toBeNull();
    expect(inserted.previousConnection.isConnected()).toBe(false);
    expect(ws.getTopBlocks(false)).toHaveLength(3);
    const box = inserted.getBoundingRectangle();
    expect(box.right - box.left).toBeGreaterThan(0);
    expect(box.bottom - box.top).toBeGreaterThan(0);
    for (const other of [a, b]) {
      expect(intersects(box, other.getBoundingRectangle())).toBe(false);
    }
    expect(inserted.getRelativeToSurfaceXY().y).toBe(-30);
  });

  it('one undo removes the whole inserted stack', async () => {
    addStack(10, 40);
    await flushEvents();
    ws.clearUndo();
    const undoable = [];
    ws.addChangeListener((e) => { if (e.recordUndo) undoable.push(e); });
    const r = insertProgram(ws, ITEMS, { placeNameOf });
    await flushEvents();
    expect(ws.getAllBlocks(false).length).toBe(2 + 3 + 2);
    // Every undoable event of the insertion shares ONE named event group.
    expect(undoable.length).toBeGreaterThan(0);
    expect(undoable[0].group).not.toBe('');
    expect(new Set(undoable.map((e) => e.group)).size).toBe(1);
    ws.undo(false);
    await flushEvents();
    expect(ws.getBlockById(r.blockId)).toBeNull();
    expect(ws.getAllBlocks(false).length).toBe(2);
  });

  it('gripper blocks load into the real workspace inside the one stack', () => {
    const entries = { a: poseEntry(0.8), b: poseEntry(-0.3) };
    const r = insertProgram(ws, [
      { kind: 'pose', name: 'P1', entryId: 'a' },
      { kind: 'pose', name: 'P2', entryId: 'b' },
    ], { gripperStateOf: makeGripperStateOf({ entryOf: (item) => entries[item.entryId] }) });
    expect(r.count).toBe(3);
    const types = [];
    for (let b = ws.getBlockById(r.blockId); b; b = b.getNextBlock()) types.push(b.type);
    expect(types).toEqual(['edubotics_move_to', 'edubotics_move_to', 'edubotics_close_gripper']);
    expect(ws.getTopBlocks(false)).toHaveLength(1);
  });

  it('nothing to insert touches nothing', () => {
    const r = insertProgram(ws, [{ kind: 'recording', name: 'X', status: 'failed' }], {});
    expect(r).toEqual({ blockId: null, count: 0 });
    expect(ws.getTopBlocks(false)).toHaveLength(0);
  });
});
