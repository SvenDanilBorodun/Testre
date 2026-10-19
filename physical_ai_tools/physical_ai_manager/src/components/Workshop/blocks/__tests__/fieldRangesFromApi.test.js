/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// A number field on a robot block allows exactly the range the robot accepts
// (robot_api.py's lo/hi for that parameter, Principle 2 of the 2026-10-18
// semantics round). F15: the TIMEOUT fields allowed 1–120 while the robot
// takes 0–300. Widening never alters a saved value — field validators guard
// edits, not loads (blocks/fieldLoad.js).

import * as Blockly from 'blockly/core';
import { describe, expect, it } from 'vitest';
import { ROW_BLOCKS, registerEverything, fieldsOf } from './_apiBlocks';

// Registered at module scope: `it.each` below is collected before any hook.
registerEverything();
const ws = new Blockly.Workspace();

function numberFieldsWithRows() {
  const out = [];
  for (const row of ROW_BLOCKS) {
    const block = ws.newBlock(row.block_type);
    for (const f of fieldsOf(block)) {
      if (!(f instanceof Blockly.FieldNumber)) continue;
      const param = row.params.find((p) => p.arg_key.toUpperCase() === f.name);
      out.push([`${row.block_type}.${f.name}`, f, param]);
    }
  }
  return out;
}

describe('number field ranges come from robot_api.json', () => {
  it('every number field of a row block belongs to a parameter of its row', () => {
    const fields = numberFieldsWithRows();
    expect(fields.length).toBeGreaterThanOrEqual(5);
    const orphans = fields.filter(([, , param]) => !param).map(([name]) => name);
    expect(orphans).toEqual([]);
  });

  it.each(numberFieldsWithRows())('%s allows exactly the row’s lo..hi', (_name, field, param) => {
    expect(field.getMin()).toBe(param.lo);
    expect(field.getMax()).toBe(param.hi);
  });

  it('F15: both TIMEOUT fields allow 0 to 300 seconds', () => {
    for (const type of ['edubotics_wait_until_object_seen', 'edubotics_wait_until_held']) {
      const f = ws.newBlock(type).getField('TIMEOUT');
      expect([f.getMin(), f.getMax()]).toEqual([0, 300]);
    }
  });

  it('a saved TIMEOUT outside the old range loads unchanged', () => {
    const block = Blockly.serialization.blocks.append(
      { type: 'edubotics_wait_until_held', fields: { TIMEOUT: 250 } }, ws,
    );
    expect(block.getFieldValue('TIMEOUT')).toBe(250);
  });
});
