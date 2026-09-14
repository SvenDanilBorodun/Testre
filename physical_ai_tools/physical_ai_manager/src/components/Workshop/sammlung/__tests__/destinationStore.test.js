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
 * The `edubotics-destinations` document serializer and its per-workspace store.
 *
 * Real Blockly 12.5.1 (headless workspaces, real serialization, real events and
 * a real undo stack) — the contract is exactly the part a hand-written fake
 * would get wrong: load order by priority, event grouping across a store rename
 * and a block field edit, and the silence of `workspaces.load`.
 */
import { describe, it, expect, beforeAll, afterEach } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import { registerDestinationBlocks, destinationNameErrorDe } from '../../blocks/destinations';
import { DE, formatDe } from '../../blocks/messages_de';
import {
  DESTINATIONS_SERIALIZER_NAME,
  DESTINATIONS_SERIALIZER_PRIORITY,
  DESTINATIONS_CHANGE_EVENT,
  MAX_DESTINATION_ENTRIES,
  getDestinationStore,
  registerDestinationSerializer,
  readDestinationEntries,
  entriesForRunPayload,
  nextAutoName,
  takenDestinationNames,
  sanitizeDestinationNameInput,
} from '../destinationStore';

// Blockly dispatches events via requestAnimationFrame(() => setTimeout(fireNow,
// 0)); a bare setTimeout(0) delivers nothing (measured: 0 events). Same helper
// shape as blocks/__tests__/foreverDeadCode.test.js.
const flushEvents = async () => {
  await new Promise((resolve) => {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => { setTimeout(resolve, 0); });
    } else {
      setTimeout(resolve, 0);
    }
  });
  await new Promise((resolve) => { setTimeout(resolve, 0); });
};

const { save, load } = Blockly.serialization.workspaces;

const GOOD = {
  id: 'd_4f1c9a2e',
  name: 'Ablage',
  kind: 'pin',
  x: 0.182,
  y: -0.064,
  z: 0.012,
  source: 'camera',
  robot_type: 'omx_f',
  created_at: '2026-09-13T10:12:04.000Z',
};

const POSE = {
  id: 'd_9b07e311',
  name: 'Über der Kiste',
  kind: 'pose',
  x: 0.141,
  y: 0.102,
  z: 0.118,
  source: 'capture',
  robot_type: 'omx_f',
  created_at: '2026-09-13T10:20:51.000Z',
  joints: [0, -0.9123, 1.1047, 0.3, 0, 0.8],
  joint_names: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'],
};

const PIN_INPUT = { name: 'A', kind: 'pin', source: 'camera', x: 0.1, y: 0.2, z: 0.01 };

const workspaces = [];
function newWorkspace() {
  const ws = new Blockly.Workspace();
  workspaces.push(ws);
  return ws;
}

function collectChangeEvents(ws) {
  const seen = [];
  ws.addChangeListener((e) => {
    if (e.type === DESTINATIONS_CHANGE_EVENT) seen.push(e);
  });
  return seen;
}

beforeAll(() => {
  registerDestinationBlocks();
  registerDestinationSerializer();
});

afterEach(() => {
  while (workspaces.length) workspaces.pop().dispose();
});

describe('registration', () => {
  it('registers the serializer under the exact name and priority', () => {
    const item = Blockly.registry.getObject(
      Blockly.registry.Type.SERIALIZER, DESTINATIONS_SERIALIZER_NAME);
    expect(item).toBeTruthy();
    expect(item.priority).toBe(90);
    expect(DESTINATIONS_SERIALIZER_PRIORITY).toBe(90);
    const { priorities } = Blockly.serialization;
    expect(priorities.VARIABLES).toBeGreaterThan(90);
    expect(90).toBeGreaterThan(priorities.BLOCKS);
  });

  it('is idempotent', () => {
    expect(() => registerDestinationSerializer()).not.toThrow();
    expect(() => registerDestinationSerializer()).not.toThrow();
    expect(Blockly.registry.hasItem(Blockly.registry.Type.EVENT, DESTINATIONS_CHANGE_EVENT))
      .toBe(true);
  });
});

describe('save / load', () => {
  it('round-trips a canonical state byte-identically', () => {
    const canonical = { version: 1, entries: [GOOD, POSE] };
    const ws = newWorkspace();
    load({ [DESTINATIONS_SERIALIZER_NAME]: canonical }, ws);
    expect(JSON.stringify(save(ws)[DESTINATIONS_SERIALIZER_NAME]))
      .toBe(JSON.stringify(canonical));
  });

  it('an empty workspace carries no key at all', () => {
    const ws = newWorkspace();
    expect(DESTINATIONS_SERIALIZER_NAME in save(ws)).toBe(false);
  });

  it('load is total and keeps only the valid entry', () => {
    const bad = [
      null, 'x', 7, [],
      { version: 2, entries: [] },
      { version: 1, entries: 'x' },
      {
        version: 1,
        entries: [
          null,
          { name: 'A!' },
          { name: 'B', kind: 'pin', x: NaN, y: 0, z: 0 },
          { name: 'C', kind: 'cam', x: 0, y: 0, z: 0 },
          GOOD,
        ],
      },
    ];
    const kept = bad.map((state) => {
      const ws = newWorkspace();
      expect(() => load({ [DESTINATIONS_SERIALIZER_NAME]: state }, ws)).not.toThrow();
      return getDestinationStore(ws).getEntries().map((e) => e.name);
    });
    // Only the last state carries a valid entry; every other one loads empty.
    expect(kept).toEqual([[], [], [], [], [], [], ['Ablage']]);
  });

  it('normalises coordinates, ids, duplicates, the cap, robot types and joints', () => {
    const entries = [
      { ...GOOD, id: 'nope', x: 0.123456789, y: -0.00001 },
      { ...GOOD, id: 'd_00000001', x: 1 },            // duplicate NAME → dropped
      { ...GOOD, name: 'Zwei', robot_type: 'r2d2' },  // duplicate id → regenerated
      { ...POSE, joint_names: ['joint1'] },          // joints without matching names
    ];
    for (let i = 0; i < 70; i += 1) {
      entries.push({ name: `P${i}`, kind: 'pin', x: 0, y: 0, z: 0 });
    }
    const got = readDestinationEntries({
      [DESTINATIONS_SERIALIZER_NAME]: { version: 1, entries },
    });
    expect(got).toHaveLength(MAX_DESTINATION_ENTRIES);
    const [first, second, third] = got;
    expect(first.id).toMatch(/^d_[0-9a-f]{8}$/);
    expect(first.id).not.toBe('nope');
    expect(first.x).toBe(0.1235);
    expect(Object.is(first.y, -0)).toBe(false);
    expect(first.y).toBe(0);
    expect(second.name).toBe('Zwei');
    expect(second.id).toMatch(/^d_[0-9a-f]{8}$/);
    expect(second.id).not.toBe(first.id);
    expect('robot_type' in second).toBe(false);
    expect(third.name).toBe('Über der Kiste');
    expect('joints' in third).toBe(false);
    expect('joint_names' in third).toBe(false);
    expect(got.filter((e) => e.name === 'Ablage')).toHaveLength(1);
  });

  it('two headless workspaces keep isolated stores', () => {
    const a = newWorkspace();
    const b = newWorkspace();
    expect(getDestinationStore(a).add(PIN_INPUT).ok).toBe(true);
    expect(getDestinationStore(a)).toBe(getDestinationStore(a));
    expect(getDestinationStore(b).getEntries()).toEqual([]);
    expect(DESTINATIONS_SERIALIZER_NAME in save(b)).toBe(false);
  });

  it('loads before the blocks: variables + destinations + blocks in one state', () => {
    const ws = newWorkspace();
    const state = {
      variables: [{ name: 'x', id: 'v1' }],
      [DESTINATIONS_SERIALIZER_NAME]: { version: 1, entries: [GOOD] },
      blocks: {
        languageVersion: 0,
        blocks: [{ type: 'edubotics_destination_ref', id: 'r1', fields: { NAME: 'Ablage' } }],
      },
    };
    expect(() => load(state, ws)).not.toThrow();
    expect(ws.getBlockById('r1')).toBeTruthy();
    expect(getDestinationStore(ws).getByName('Ablage')).toBeTruthy();
  });

  it('readDestinationEntries of a document without the key is empty', () => {
    expect(readDestinationEntries({})).toEqual([]);
    expect(readDestinationEntries(null)).toEqual([]);
  });
});

describe('store mutations', () => {
  it('add refuses a bad name, a taken name, a bad coordinate and the 65th entry', () => {
    const ws = newWorkspace();
    const store = getDestinationStore(ws);
    expect(store.add({ ...PIN_INPUT, name: 'A!' }))
      .toEqual({ ok: false, error: destinationNameErrorDe('A!') });
    const first = store.add(PIN_INPUT);
    expect(first.ok).toBe(true);
    expect(first.entry.created_at).toMatch(/^\d{4}-\d\d-\d\dT/);
    expect(Number.isNaN(Date.parse(first.entry.created_at))).toBe(false);
    expect(store.add({ ...PIN_INPUT, x: 0.3 }))
      .toEqual({ ok: false, error: formatDe(DE.ERR_NAME_TAKEN, 'A') });
    expect(store.add({ ...PIN_INPUT, name: 'B', y: NaN }))
      .toEqual({ ok: false, error: DE.ERR_COORDINATES });
    for (let i = 1; i < MAX_DESTINATION_ENTRIES; i += 1) {
      expect(store.add({ ...PIN_INPUT, name: `P${i}` }).ok).toBe(true);
    }
    expect(store.add({ ...PIN_INPUT, name: 'Zu viel' }))
      .toEqual({ ok: false, error: DE.ERR_STORE_FULL });
    expect(store.getEntries()).toHaveLength(MAX_DESTINATION_ENTRIES);
  });

  it('rename, remove and restore keep names unique and order stable', () => {
    const ws = newWorkspace();
    const store = getDestinationStore(ws);
    const a = store.add(PIN_INPUT).entry;
    const b = store.add({ ...PIN_INPUT, name: 'B', kind: 'pose', source: 'capture' }).entry;
    expect(store.rename(a.id, 'B').error).toBe(formatDe(DE.ERR_NAME_TAKEN, 'B'));
    expect(store.rename(a.id, '  Ablage  ')).toMatchObject({ ok: true, oldName: 'A' });
    expect(store.getById(a.id).name).toBe('Ablage');
    expect(store.rename('d_ffffffff', 'X').error).toBe(DE.ERR_DESTINATION_MISSING);
    const removed = store.remove(a.id);
    expect(removed).toMatchObject({ ok: true, index: 0 });
    expect(store.getEntries().map((e) => e.name)).toEqual(['B']);
    expect(store.restore(removed.entry, removed.index)).toEqual({ ok: true });
    expect(store.getEntries().map((e) => e.id)).toEqual([a.id, b.id]);
  });
});

describe('events and undo', () => {
  it('add fires exactly one change event; workspaces.load fires none', async () => {
    const ws = newWorkspace();
    const seen = collectChangeEvents(ws);
    load({ [DESTINATIONS_SERIALIZER_NAME]: { version: 1, entries: [GOOD] } }, ws);
    await flushEvents();
    expect(seen).toHaveLength(0);
    getDestinationStore(ws).add(PIN_INPUT);
    await flushEvents();
    expect(seen).toHaveLength(1);
    expect(seen[0].newEntries.map((e) => e.name)).toEqual(['Ablage', 'A']);
  });

  it('undo removes an added entry and redo brings it back, notifying subscribers', async () => {
    const ws = newWorkspace();
    const store = getDestinationStore(ws);
    const notified = [];
    store.subscribe((entries) => notified.push(entries.map((e) => e.name)));
    store.add(PIN_INPUT);
    await flushEvents();
    ws.undo(false);
    await flushEvents();
    expect(store.getEntries()).toEqual([]);
    expect(notified[notified.length - 1]).toEqual([]);
    ws.undo(true);
    await flushEvents();
    expect(store.getEntries().map((e) => e.name)).toEqual(['A']);
    expect(notified[notified.length - 1]).toEqual(['A']);
  });

  it('a grouped store rename + block field edit undoes as ONE step', async () => {
    const ws = newWorkspace();
    const store = getDestinationStore(ws);
    const { entry } = store.add(PIN_INPUT);
    const block = ws.newBlock('edubotics_destination_ref');
    block.setFieldValue('A', 'NAME');
    await flushEvents();
    Blockly.Events.setGroup(true);
    store.rename(entry.id, 'B');
    block.setFieldValue('B', 'NAME');
    Blockly.Events.setGroup(false);
    await flushEvents();
    ws.undo(false);
    await flushEvents();
    expect(store.getById(entry.id).name).toBe('A');
    expect(block.getFieldValue('NAME')).toBe('A');
  });
});

describe('helpers', () => {
  it('nextAutoName picks the smallest free number', () => {
    expect(nextAutoName('Ziel %1', ['Ziel 1', 'Ziel 3'])).toBe('Ziel 2');
    expect(nextAutoName('Ziel %1', [])).toBe('Ziel 1');
    expect(nextAutoName('Ziel', ['Ziel'])).toBe('Ziel');   // terminates without %1
  });

  it('takenDestinationNames unions store names and pin/current block names', () => {
    const ws = newWorkspace();
    getDestinationStore(ws).add({ ...PIN_INPUT, name: 'Im Speicher' });
    ws.newBlock('edubotics_destination_pin').setFieldValue('Pin', 'NAME');
    ws.newBlock('edubotics_destination_current').setFieldValue('Hier', 'NAME');
    ws.newBlock('edubotics_destination_ref').setFieldValue('Nur Verweis', 'NAME');
    const taken = takenDestinationNames(ws);
    expect(taken).toContain('Im Speicher');
    expect(taken).toContain('Pin');
    expect(taken).toContain('Hier');
    expect(taken).not.toContain('Nur Verweis');
  });

  it('entriesForRunPayload sends exactly {name, kind, x, y, z}', () => {
    const [entry] = readDestinationEntries({
      [DESTINATIONS_SERIALIZER_NAME]: { version: 1, entries: [POSE] },
    });
    expect(entriesForRunPayload([entry])).toEqual([
      { name: 'Über der Kiste', kind: 'pose', x: 0.141, y: 0.102, z: 0.118 },
    ]);
    expect(Object.keys(entriesForRunPayload([entry])[0])).toEqual(['name', 'kind', 'x', 'y', 'z']);
  });

  it('sanitizeDestinationNameInput strips and caps without trimming', () => {
    expect(sanitizeDestinationNameInput(' A!b ')).toBe(' Ab ');
    expect(sanitizeDestinationNameInput('x'.repeat(30))).toHaveLength(24);
  });
});
