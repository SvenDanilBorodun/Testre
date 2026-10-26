/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The „Code"-Vorschau of a robot block is a call of the REAL text API
// (Principle 2 of the 2026-10-18 semantics round: robot_api.py is the only
// source for robot blocks). This test renders every block that has a
// robot_api row — once with every input filled by a valid value of its
// parameter's kind, once with every input EMPTY — plus the hats, a broadcast,
// the Tempo dropdown and an un-pinned pin, through the editor's own preview
// path (pythonCodeGen.generatePython), and compares the result with
// fixtures/preview-api-cases.json. That fixture is what
// physical_ai_server/test/test_preview_matches_api.py judges with the server's
// own table: every `robot.X(…)` names a public row with an arity and keywords
// it accepts, every literal argument passes code_rpc.validate_value, the whole
// preview parses, and `robot` is imported. A change to the generator or to
// the table fails here until the fixture is regenerated:
//
//   EDUBOTICS_REGEN_PREVIEW_CASES=1 npx vitest run src/components/Workshop/__tests__/pythonCodeGen.api.cases.test.js
//
// It replaces pythonCodeGen.robotApi.test.js, whose name-set comparison the
// row-driven generator made meaningless (every row is emitted from its row).

import fs from 'fs';
import path from 'path';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { beforeAll, describe, expect, it } from 'vitest';
import api from '../code/robot_api.json';
import { registerMotionBlocks } from '../blocks/motion';
import { registerPerceptionBlocks, setObjectCatalogOptions } from '../blocks/perception';
import { registerDestinationBlocks } from '../blocks/destinations';
import { registerOutputBlocks } from '../blocks/output';
import { registerEventBlocks } from '../blocks/events';
import { registerControlBlocks } from '../blocks/control';
import { registerCounterBlocks } from '../blocks/counters';
import { registerTrajectoryBlocks } from '../blocks/trajectories';
import { BLOCK_ONLY_FIELDS, generatePython } from '../pythonCodeGen';

const FIXTURE = path.join(__dirname, 'fixtures', 'preview-api-cases.json');
const REGEN = !['', '0', undefined].includes(process.env.EDUBOTICS_REGEN_PREVIEW_CASES);
const ROWS = api.methods.filter((m) => typeof m.block_type === 'string');

function registerAll() {
  Blockly.setLocale(De);
  registerMotionBlocks();
  registerPerceptionBlocks();
  registerDestinationBlocks();
  registerOutputBlocks();
  registerEventBlocks();
  registerControlBlocks();
  registerCounterBlocks();
  registerTrajectoryBlocks();
  setObjectCatalogOptions([['Würfel', 'wuerfel']]);
}

// A valid VALUE for a parameter, as the block the student would plug in.
function inputFor(param) {
  switch (param.kind) {
    case 'target':
      return { block: { type: 'edubotics_destination_ref', fields: { NAME: 'Ablage' } } };
    case 'ziel':
      return { block: { type: 'edubotics_find_object', fields: { OBJECT_TYPE: 'wuerfel' } } };
    case 'float':
    case 'int': {
      const lo = typeof param.lo === 'number' ? param.lo : 0;
      const hi = typeof param.hi === 'number' ? param.hi : lo + 2;
      const v = param.kind === 'int' ? Math.round((lo + hi) / 2) : Math.round(((lo + hi) / 2) * 10) / 10;
      return { block: { type: 'math_number', fields: { NUM: v } } };
    }
    default:
      return { block: { type: 'text', fields: { TEXT: 'Hallo' } } };
  }
}

// A valid FIELD value where the block's default is not one already.
const FIELD_VALUES = {
  edubotics_destination_pin: { NAME: 'Ablage', X: '0.182', Y: '-0.050', Z: '0.030' },
  edubotics_destination_ref: { NAME: 'Ablage' },
  edubotics_destination_current: { NAME: 'Hier' },
  edubotics_replay_trajectory: { NAME: 'Winken' },
  edubotics_toast: { LEVEL: 'success' },
};

function inputNamesOf(type) {
  const ws = new Blockly.Workspace();
  try {
    const b = ws.newBlock(type);
    return b.inputList.filter((i) => i.connection && i.name).map((i) => i.name);
  } finally {
    ws.dispose();
  }
}

function rowBlock(row, filled) {
  const inputs = {};
  const names = inputNamesOf(row.block_type);
  if (filled) {
    for (const p of row.params) {
      const key = p.arg_key.toUpperCase();
      if (names.includes(key)) inputs[key] = inputFor(p);
    }
  }
  const state = { type: row.block_type, fields: { ...(FIELD_VALUES[row.block_type] || {}) } };
  if (Object.keys(inputs).length) state.inputs = inputs;
  return state;
}

function statement(row, block) {
  if (row.table !== 'value') return block;
  return { type: 'variables_set', fields: { VAR: { id: 'v_r' } }, inputs: { VALUE: { block } } };
}

function program(name, blocks) {
  return {
    name,
    ws: { blocks: { languageVersion: 0, blocks }, variables: [{ name: 'r', id: 'v_r' }] },
  };
}

function buildCases() {
  const cases = [];
  for (const row of ROWS) {
    cases.push({ ...program(`${row.block_type}:filled`, [statement(row, rowBlock(row, true))]), row: row.name, kind: 'filled' });
    cases.push({ ...program(`${row.block_type}:empty`, [statement(row, rowBlock(row, false))]), row: row.name, kind: 'empty' });
  }
  const home = { type: 'edubotics_home' };
  cases.push({
    ...program('hats_and_broadcast', [
      { type: 'edubotics_broadcast', fields: { EVENT_NAME: 'start' } },
      { type: 'edubotics_when_broadcast', fields: { EVENT_NAME: 'start' }, next: { block: home } },
      { type: 'edubotics_when_broadcast', fields: { EVENT_NAME: 'start' }, next: { block: { type: 'edubotics_open_gripper' } } },
      { type: 'edubotics_when_object_seen', fields: { OBJECT_TYPE: 'wuerfel' }, next: { block: { type: 'edubotics_grasp_object', fields: { OBJECT_TYPE: 'wuerfel' } } } },
      { type: 'edubotics_when_counter_gt', fields: { NAME: 'Punkte', N: 3 } },
    ]),
    kind: 'hats',
  });
  cases.push({
    ...program('broadcast_without_hat', [
      { type: 'controls_if', inputs: { IF0: { block: { type: 'logic_boolean', fields: { BOOL: 'TRUE' } } }, DO0: { block: { type: 'edubotics_broadcast', fields: { EVENT_NAME: 'niemand' } } } } },
    ]),
    kind: 'hats',
  });
  cases.push({
    ...program('tempo', [
      { type: 'edubotics_move_to', fields: { GESCHWINDIGKEIT: 'schnell' }, inputs: { DESTINATION: inputFor({ kind: 'target' }) } },
      { type: 'edubotics_lift', fields: { GESCHWINDIGKEIT: 'langsam' } },
    ]),
    kind: 'filled',
  });
  cases.push({
    ...program('pin_unpinned', [{ type: 'edubotics_destination_pin', fields: { NAME: 'B' } }]),
    kind: 'empty',
    row: 'pin',
  });
  cases.push({
    ...program('control_blocks', [
      { type: 'edubotics_forever', inputs: { DO: { block: { type: 'edubotics_wait_until', inputs: { BOOL: { block: { type: 'edubotics_grasp_held' } } } } } } },
      { type: 'edubotics_while_visible', fields: { OBJECT_TYPE: 'wuerfel', MAX_REPS: 0 }, inputs: { DO: { block: { type: 'edubotics_grasp_object', fields: { OBJECT_TYPE: 'wuerfel' } } } } },
    ]),
    kind: 'filled',
  });
  // E2: „höchstens 1 Mal" renders a counted loop (MAX_REPS 0 above keeps the
  // `while`).
  cases.push({
    ...program('while_visible_max_reps', [
      { type: 'edubotics_while_visible', fields: { OBJECT_TYPE: 'wuerfel', MAX_REPS: 1 }, inputs: { DO: { block: { type: 'edubotics_grasp_object', fields: { OBJECT_TYPE: 'wuerfel' } } } } },
    ]),
    kind: 'filled',
  });
  cases.push({ ...program('empty_program', []), kind: 'empty' });
  return cases;
}

async function compute() {
  const out = [];
  for (const c of buildCases()) {
    const ws = new Blockly.Workspace();
    try {
      Blockly.serialization.workspaces.load(c.ws, ws);
      // eslint-disable-next-line no-await-in-loop
      const py = await generatePython(ws);
      out.push({ name: c.name, kind: c.kind, ...(c.row ? { row: c.row } : {}), py });
    } finally {
      ws.dispose();
    }
  }
  return {
    _generated_by: 'physical_ai_manager/src/components/Workshop/__tests__/pythonCodeGen.api.cases.test.js '
      + '(EDUBOTICS_REGEN_PREVIEW_CASES=1 to regenerate)',
    _checked_by: 'physical_ai_server/test/test_preview_matches_api.py (every robot call against robot_api.py)',
    cases: out,
  };
}

function serialize(now) {
  const head = Object.entries(now)
    .filter(([k]) => k !== 'cases')
    .map(([k, v]) => ` ${JSON.stringify(k)}: ${JSON.stringify(v)},`);
  const cases = now.cases.map((c, k) => `  ${JSON.stringify(c)}${k + 1 < now.cases.length ? ',' : ''}`);
  return `{\n${head.join('\n')}\n "cases": [\n${cases.join('\n')}\n ]\n}\n`;
}

beforeAll(() => {
  registerAll();
});

describe('the preview of every robot block is a call of the real text API', () => {
  it('matches the fixture the Python API check judges', async () => {
    const now = await compute();
    if (REGEN) {
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, serialize(now));
    }
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(now).toEqual(saved);
  });

  it('maps every parameter of every row onto its block by arg_key', () => {
    const ws = new Blockly.Workspace();
    try {
      const unmapped = [];
      const blockOnly = new Set();
      for (const row of ROWS) {
        const b = ws.newBlock(row.block_type);
        const inputs = b.inputList.filter((i) => i.name).map((i) => i.name);
        const fields = b.inputList.flatMap((i) => i.fieldRow.filter((f) => f.name).map((f) => f.name));
        for (const p of row.params) {
          const key = p.arg_key.toUpperCase();
          if (p.required && !inputs.includes(key) && !fields.includes(key)) unmapped.push(`${row.name}.${key}`);
        }
        for (const n of [...inputs, ...fields]) {
          if (!row.params.some((p) => p.arg_key.toUpperCase() === n)) blockOnly.add(n);
        }
      }
      expect(unmapped).toEqual([]);
      // Exactly these fields carry no parameter (the Tempo dropdown, the pin's
      // drive-to row and button, the reference block's „Ziel"/„Position" label).
      expect([...blockOnly].sort()).toEqual([...BLOCK_ONLY_FIELDS].sort());
      expect([...BLOCK_ONLY_FIELDS].sort()).toEqual(['DRIVE_BTN', 'DRIVE_TO_ROW', 'GESCHWINDIGKEIT', 'KIND']);
    } finally {
      ws.dispose();
    }
  });

  it('robot_api.json carries 32 handler rows with a block type and one code-only row', () => {
    expect(api.methods).toHaveLength(33);
    const handlerRows = api.methods.filter((m) => m.table !== 'code');
    const codeOnly = api.methods.filter((m) => m.table === 'code');
    expect(handlerRows).toHaveLength(32);
    expect(codeOnly.map((m) => m.name)).toEqual(['zeige']);
    for (const m of api.methods) {
      expect(typeof m.doc_de).toBe('string');
      expect(m.doc_de.length).toBeGreaterThan(0);
      expect(Array.isArray(m.params)).toBe(true);
      for (const p of m.params) expect(typeof p.arg_key).toBe('string');
    }
    for (const m of handlerRows) expect(m.block_type).toMatch(/^edubotics_/);
    for (const m of codeOnly) expect(m.block_type).toBeNull();
  });

  it('robot_api.json carries both frame bounds and the three project caps', () => {
    const { limits } = api;
    expect(limits.MAX_FRAME_BYTES).toBe(65536);
    expect(limits.CONTROL_MAX_FRAME_BYTES).toBe(196608);
    expect(limits.MAX_CODE_FILES).toBe(32);
    expect(limits.MAX_CODE_FILE_BYTES).toBe(65536);
    expect(limits.MAX_CODE_PROJECT_BYTES).toBe(131072);
    expect(limits.CONTROL_MAX_FRAME_BYTES).toBeGreaterThanOrEqual(limits.MAX_CODE_PROJECT_BYTES + 8192);
    expect(typeof limits.CODE_PATH_RE).toBe('string');
  });
});
