/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// INVARIANT: deserialization never discards or rewrites a value a student
// saved. Field validators guard EDITS, not LOADS (blocks/fieldLoad.js).
//
// Measured before the fix, on a plain load: a pin named „Ablage.1" came back
// „Ablage1"; a name of only punctuation collapsed to the block DEFAULT „A", so
// two pins could silently become one; a 38-character name was cut to 24;
// `Bewegung [2]` became `Bewegung 1`, which can address a DIFFERENT real
// recording; and an object type the catalog had not delivered yet („kugel")
// became the placeholder and then the FIRST catalog entry. Every one of those
// rewrites was then written back to the student's file, because Blockly
// delivers load events asynchronously and the autosave cannot tell them from an
// edit.

import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerDestinationBlocks } from '../destinations';
import { registerCounterBlocks } from '../counters';
import { registerEventBlocks } from '../events';
import { registerTrajectoryBlocks } from '../trajectories';
import { registerPerceptionBlocks, setObjectCatalogOptions, setWorkspaceAccessor } from '../perception';
import { DE } from '../messages_de';

let workspace;

beforeAll(() => {
  Blockly.setLocale(De);
  registerDestinationBlocks();
  registerCounterBlocks();
  registerEventBlocks();
  registerTrajectoryBlocks();
  registerPerceptionBlocks();
});

beforeEach(() => {
  workspace = new Blockly.Workspace();
});

afterEach(() => {
  setWorkspaceAccessor(null);
  workspace.dispose();
});

/** Load one block, re-serialize it, and hand back the round-tripped fields. */
function roundTrip(state) {
  const block = Blockly.serialization.blocks.append(state, workspace);
  return { block, fields: Blockly.serialization.blocks.save(block).fields };
}

describe('a saved name survives being loaded', () => {
  test.each([
    ['edubotics_destination_pin', 'NAME', 'Ablage.1'],
    ['edubotics_destination_pin', 'NAME', '!!!'],
    ['edubotics_destination_pin', 'NAME', 'Ein sehr sehr sehr langer Zielname hier'],
    ['edubotics_destination_ref', 'NAME', 'Ablage/2'],
    ['edubotics_counter_reset', 'NAME', 'Tore[rot]'],
    ['edubotics_broadcast', 'EVENT_NAME', 'los\njetzt'],
    ['edubotics_replay_trajectory', 'NAME', 'Bewegung [2]'],
  ])('%s.%s keeps %p', (type, field, value) => {
    const { fields } = roundTrip({ type, fields: { [field]: value } });
    expect(fields[field]).toBe(value);
  });

  test('a student EDIT is still validated', () => {
    const { block } = roundTrip({ type: 'edubotics_destination_pin', fields: { NAME: 'Ablage.1' } });
    block.setFieldValue('Neu.Name', 'NAME');
    expect(block.getFieldValue('NAME')).toBe('NeuName');
  });
});

describe('a saved object type survives the catalog arriving late', () => {
  test('it is kept, offered as „(unbekannt)" and never snapped away', () => {
    const { block, fields } = roundTrip({
      type: 'edubotics_grasp_object', fields: { OBJECT_TYPE: 'kugel' },
    });
    // Loaded before any catalog: kept verbatim, and the round trip is lossless.
    expect(fields.OBJECT_TYPE).toBe('kugel');
    const field = block.getField('OBJECT_TYPE');
    expect(field.getOptions(false)).toContainEqual([`kugel ${DE.OBJECT_TYPE_UNKNOWN}`, 'kugel']);

    // A catalog that does not contain it must not overwrite the student's block.
    setWorkspaceAccessor(() => workspace);
    setObjectCatalogOptions([['Würfel', 'wuerfel']]);
    expect(block.getFieldValue('OBJECT_TYPE')).toBe('kugel');
    expect(Blockly.serialization.blocks.save(block).fields.OBJECT_TYPE).toBe('kugel');

    // And when it finally arrives, the block is simply correct.
    setObjectCatalogOptions([['Würfel', 'wuerfel'], ['Kugel', 'kugel']]);
    expect(block.getFieldValue('OBJECT_TYPE')).toBe('kugel');
    expect(block.getField('OBJECT_TYPE').getOptions(false))
      .toEqual([['Würfel', 'wuerfel'], ['Kugel', 'kugel']]);
  });

  test('a catalog that is already loaded does not eat the saved type either', () => {
    // The guard used to infer "not yet set" from the value the field HELD
    // during the load. That is the „(lädt …)" placeholder only while the
    // catalog is EMPTY — once one exists the default is its FIRST entry, so a
    // saved „kugel" loaded as „wuerfel" and was saved back that way. Measured
    // 2026-09-16; the flag replaced the inference.
    setObjectCatalogOptions([['Würfel', 'wuerfel']]);
    const { block, fields } = roundTrip({
      type: 'edubotics_grasp_object', fields: { OBJECT_TYPE: 'kugel' },
    });
    expect(block.getFieldValue('OBJECT_TYPE')).toBe('kugel');
    expect(fields.OBJECT_TYPE).toBe('kugel');
  });

  test('the „(lädt …)" placeholder IS replaced — it is ours, not the student’s', () => {
    // The catalog is module state that outlives one test, exactly as it
    // outlives one workspace in the page: start from "nothing delivered".
    setObjectCatalogOptions([]);
    const block = Blockly.serialization.blocks.append({ type: 'edubotics_grasp_object' }, workspace);
    expect(block.getFieldValue('OBJECT_TYPE')).toBe('__none__');
    setWorkspaceAccessor(() => workspace);
    setObjectCatalogOptions([['Würfel', 'wuerfel']]);
    expect(block.getFieldValue('OBJECT_TYPE')).toBe('wuerfel');
  });
});
