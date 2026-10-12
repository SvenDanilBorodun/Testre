/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// R0-6: a destination reference names its kind — „Position <name>" for a
// Position of the document's store, „Ziel <name>" otherwise — on EVERY way a
// reference block comes to exist or change (real Blockly 12 + the real store).
// The label is a non-serializable field_label set without an event.

import * as Blockly from 'blockly/core';
import {
  DESTINATIONS_CHANGE_EVENT_TYPE,
  registerDestinationBlocks,
  setDestinationKindResolver,
} from '../destinations';
import {
  DESTINATIONS_CHANGE_EVENT,
  getDestinationStore,
  registerDestinationSerializer,
} from '../../sammlung/destinationStore';
import { DE } from '../messages_de';

const REF = 'edubotics_destination_ref';
const POSE = {
  id: 'p1', name: 'Über Kiste', kind: 'pose', x: 0.1, y: 0, z: 0.1, source: 'capture',
};
const PIN = {
  id: 'z1', name: 'Ablage', kind: 'pin', x: 0.2, y: 0, z: 0.0, source: 'camera',
};

// Blockly delivers events via requestAnimationFrame(() => setTimeout(fireNow, 0)).
// The same chain registered after it runs after fireNow, however loaded the
// machine is (a fixed 40 ms wait flaked under the full suite); twice, so an
// event fired while the first batch is delivered is delivered too.
async function flush() {
  for (let i = 0; i < 2; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => { requestAnimationFrame(() => { setTimeout(resolve, 0); }); });
  }
}

const storeResolver = (ws, name) => (getDestinationStore(ws).getByName(name.trim()) || {}).kind || null;
const kindOf = (block) => block.getFieldValue('KIND');

let host;
let ws;

beforeAll(() => {
  registerDestinationBlocks();
  registerDestinationSerializer();
});

afterEach(() => {
  setDestinationKindResolver(null);
  if (ws) ws.dispose();
  ws = null;
  if (host) host.remove();
  host = null;
});

function inject(toolboxContents = null) {
  host = document.createElement('div');
  document.body.appendChild(host);
  ws = Blockly.inject(host, toolboxContents
    ? { toolbox: { kind: 'categoryToolbox', contents: toolboxContents } }
    : {});
  return ws;
}

describe('the destination reference names its kind (R0-6)', () => {
  it('spells the event type the store fires', () => {
    expect(DESTINATIONS_CHANGE_EVENT_TYPE).toBe(DESTINATIONS_CHANGE_EVENT);
  });

  it('without a resolver every label reads „Ziel"', () => {
    setDestinationKindResolver(null);
    ws = new Blockly.Workspace();
    getDestinationStore(ws).replaceSilently([POSE]);
    const b = Blockly.serialization.blocks.append({ type: REF, fields: { NAME: 'Über Kiste' } }, ws);
    expect(kindOf(b)).toBe(DE.DESTINATION_REF_ZIEL);
  });

  it('a throwing resolver reads „Ziel" too', () => {
    setDestinationKindResolver(() => { throw new Error('boom'); });
    ws = new Blockly.Workspace();
    const b = Blockly.serialization.blocks.append({ type: REF, fields: { NAME: 'Ablage' } }, ws);
    expect(kindOf(b)).toBe(DE.DESTINATION_REF_ZIEL);
  });

  it('workspace load, blocks.append and paste: a Position reads „Position", a Ziel „Ziel"', async () => {
    setDestinationKindResolver(storeResolver);
    inject();
    Blockly.serialization.workspaces.load({
      'edubotics-destinations': { version: 1, entries: [POSE, PIN] },
      blocks: {
        languageVersion: 0,
        blocks: [
          { type: REF, id: 'L', fields: { NAME: 'Über Kiste' } },
          { type: REF, id: 'Z', fields: { NAME: 'Ablage' } },
        ],
      },
    }, ws);
    expect(kindOf(ws.getBlockById('L'))).toBe(DE.DESTINATION_REF_POSE);
    expect(kindOf(ws.getBlockById('Z'))).toBe(DE.DESTINATION_REF_ZIEL);
    // blocks.append loads fields with events DISABLED: the label still follows.
    const appended = Blockly.serialization.blocks.append({ type: REF, fields: { NAME: 'Über Kiste' } }, ws);
    expect(kindOf(appended)).toBe(DE.DESTINATION_REF_POSE);
    const pasted = Blockly.clipboard.paste(appended.toCopyData(), ws);
    expect(kindOf(pasted)).toBe(DE.DESTINATION_REF_POSE);
  });

  it('a block in an opened FLYOUT asks its target workspace', async () => {
    setDestinationKindResolver(storeResolver);
    inject([{
      kind: 'category',
      name: 'Positionen',
      contents: [{ kind: 'block', type: REF, fields: { NAME: 'Über Kiste' } }],
    }]);
    getDestinationStore(ws).replaceSilently([POSE]);
    ws.getToolbox().selectItemByPosition(0);
    await flush();
    const flyoutBlock = ws.getToolbox().getFlyout().getWorkspace().getTopBlocks(false)[0];
    expect(flyoutBlock.workspace.isFlyout).toBe(true);
    expect(kindOf(flyoutBlock)).toBe(DE.DESTINATION_REF_POSE);
  });

  it('a NAME edit follows, and so does its undo; a store change re-labels; undo of a delete keeps it', async () => {
    setDestinationKindResolver(storeResolver);
    inject();
    getDestinationStore(ws).replaceSilently([POSE]);
    const a = Blockly.serialization.blocks.append({ type: REF, fields: { NAME: 'Über Kiste' } }, ws);
    await flush();
    a.getField('NAME').setValue('Neu');
    await flush();
    expect(kindOf(a)).toBe(DE.DESTINATION_REF_ZIEL);
    ws.undo(false);
    await flush();
    expect(a.getFieldValue('NAME')).toBe('Über Kiste');
    expect(kindOf(a)).toBe(DE.DESTINATION_REF_POSE);

    // A Position added under an existing Ziel block's name: no field changed.
    const z = Blockly.serialization.blocks.append({ type: REF, fields: { NAME: 'Hoch' } }, ws);
    expect(kindOf(z)).toBe(DE.DESTINATION_REF_ZIEL);
    getDestinationStore(ws).add({
      name: 'Hoch', kind: 'pose', x: 0, y: 0, z: 0.2, source: 'capture',
    });
    await flush();
    expect(kindOf(z)).toBe(DE.DESTINATION_REF_POSE);

    const { id } = a;
    a.dispose(true);
    await flush();
    ws.undo(false);
    await flush();
    expect(kindOf(ws.getBlockById(id))).toBe(DE.DESTINATION_REF_POSE);
  });

  // Verification round 1, F5a: the store-change relabel runs inside Blockly's
  // event delivery, where a change event WOULD be recorded and counted.
  it('a store change re-labels without a KIND change event and without an undo entry', async () => {
    setDestinationKindResolver(storeResolver);
    inject();
    const z = Blockly.serialization.blocks.append({ type: REF, fields: { NAME: 'Hoch' } }, ws);
    await flush();
    const seen = [];
    ws.addChangeListener((e) => seen.push(e));
    ws.clearUndo();
    getDestinationStore(ws).add({
      name: 'Hoch', kind: 'pose', x: 0, y: 0, z: 0.2, source: 'capture',
    });
    await flush();
    expect(kindOf(z)).toBe(DE.DESTINATION_REF_POSE);
    const kindChange = (e) => e.type === Blockly.Events.BLOCK_CHANGE && e.name === 'KIND';
    expect(seen.filter(kindChange)).toEqual([]);
    expect(ws.getUndoStack().filter(kindChange)).toEqual([]);
    // The store's own change is the one undo step.
    expect(ws.getUndoStack().map((e) => e.type)).toEqual([DESTINATIONS_CHANGE_EVENT_TYPE]);
  });

  it('the label fires no event and makes no undo entry; the saved JSON keeps only NAME', async () => {
    setDestinationKindResolver(storeResolver);
    inject();
    const seen = [];
    ws.addChangeListener((e) => seen.push(e.type));
    Blockly.serialization.workspaces.load({
      'edubotics-destinations': { version: 1, entries: [POSE] },
      blocks: { languageVersion: 0, blocks: [{ type: REF, id: 'L', fields: { NAME: 'Über Kiste' } }] },
    }, ws);
    const appended = Blockly.serialization.blocks.append({ type: REF, fields: { NAME: 'Über Kiste' } }, ws);
    await flush();
    expect(ws.getUndoStack()).toHaveLength(0);
    expect(seen.filter((t) => t === Blockly.Events.BLOCK_CHANGE)).toEqual([]);
    const saved = Blockly.serialization.blocks.save(appended);
    expect(saved.fields).toEqual({ NAME: 'Über Kiste' });
    expect(JSON.stringify(Blockly.serialization.workspaces.save(ws))).not.toContain('KIND');
  });
});
