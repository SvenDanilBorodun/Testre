/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Funktionen": removing a parameter must move exactly the block that was
// plugged into THAT parameter, and leave every other argument on the parameter
// it was written for.
//
// Measured before the fix (Blockly 12.5.1 + @blockly/block-plus-minus 9.0.10):
// `tu(x=11, y=22, z=33)` with ⊖ on the FIRST parameter became `tu(y=11, z=22)`
// and dropped 33 on the canvas — every value one parameter to the left, the
// program silently different, and nothing downstream able to notice (the server
// binds positionally against the definition's own names, which agree). The two
// causes and the event discipline that keeps Rückgängig working are documented
// in blocks/procedures.js.
//
// Real Blockly, the real plugin from node_modules, and the real ⊖ FieldImage —
// calling `minus()` directly fires no mutation event and would make the undo
// assertions vacuous.

import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerProcedureCallArgumentFix } from '../procedures';

// Blockly queues events onto `requestAnimationFrame(() => setTimeout(…, 0))`;
// undo/redo only sees a group once it has been delivered.
const tick = () => new Promise((resolve) => {
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => { setTimeout(resolve, 0); });
  } else {
    setTimeout(resolve, 0);
  }
});
// Two full rAF→timeout turns: one delivers the group, the next lets anything it
// triggered settle. A single turn left `undo(true)` looking at a redo stack the
// previous undo had not finished filling.
const flush = async () => {
  await tick();
  await tick();
  await new Promise((resolve) => { setTimeout(resolve, 0); });
};

let workspace;

beforeAll(async () => {
  Blockly.setLocale(De);
  await import('@blockly/block-plus-minus');
  registerProcedureCallArgumentFix();
});

beforeEach(() => {
  const host = document.createElement('div');
  document.body.appendChild(host);
  workspace = Blockly.inject(host, {});
});

afterEach(() => {
  workspace.dispose();
});

// „parameter=value" for every ARG socket, in socket order.
const args = (call) => call.inputList
  .filter((input) => /^ARG\d+$/.test(input.name))
  .map((input) => {
    const label = input.fieldRow.map((field) => field.getText()).join('');
    const plugged = input.connection.targetBlock();
    return `${label}=${plugged ? plugged.getFieldValue('NUM') : '∅'}`;
  });

const looseNumbers = (ws) => ws.getTopBlocks(false)
  .filter((block) => block.type === 'math_number')
  .map((block) => Number(block.getFieldValue('NUM')));

/** tu(x, y, z), called once with 11 / 22 / 33 plugged in. */
function buildProcedureAndCall(ws, name, callType = 'procedures_callnoreturn') {
  const def = Blockly.serialization.blocks.append(
    { type: 'procedures_defnoreturn', fields: { NAME: name } }, ws,
  );
  def.plus();
  def.plus();
  def.plus();
  const call = Blockly.serialization.blocks.append(
    { type: callType, extraState: { name, params: ['x', 'y', 'z'] } }, ws,
  );
  [11, 22, 33].forEach((value, index) => {
    const number = Blockly.serialization.blocks.append(
      { type: 'math_number', fields: { NUM: value } }, ws,
    );
    call.getInput(`ARG${index}`).connection.connect(number.outputConnection);
  });
  return { def, call };
}

/** Press the ⊖ of the parameter at `index` the way a student does. */
function clickMinus(def, index) {
  const row = def.getInput(def.argData_[index].argId);
  const minus = row.fieldRow.find((field) => field instanceof Blockly.FieldImage);
  minus.clickHandler(minus);
}

describe('removing a „Funktion" parameter', () => {
  test.each([
    [0, ['y=22', 'z=33'], 11],
    [1, ['x=11', 'z=33'], 22],
    [2, ['x=11', 'y=22'], 33],
  ])('⊖ on parameter %i keeps every other value on its own parameter', async (
    index, expected, orphan,
  ) => {
    const { def, call } = buildProcedureAndCall(workspace, 'tu');
    await flush();
    workspace.clearUndo();

    clickMinus(def, index);
    await flush();

    expect(args(call)).toEqual(expected);
    // The removed parameter's own value is the one left on the canvas.
    expect(looseNumbers(workspace)).toEqual([orphan]);
  });

  test('Rückgängig restores the call, and Wiederholen re-applies it', async () => {
    const { def, call } = buildProcedureAndCall(workspace, 'tu');
    await flush();
    workspace.clearUndo();

    clickMinus(def, 0);
    await flush();
    expect(args(call)).toEqual(['y=22', 'z=33']);

    workspace.undo(false);
    await flush();
    expect(args(call)).toEqual(['x=11', 'y=22', 'z=33']);
    expect(looseNumbers(workspace)).toEqual([]);

    workspace.undo(true);
    await flush();
    expect(args(call)).toEqual(['y=22', 'z=33']);
  });

  test('a document just reopened is fixed too, before any other edit', async () => {
    // The call block learns the parameter IDS from a definition change. Right
    // after a load no such change has happened, so identity has to fall back to
    // the parameter NAMES — otherwise the very first ⊖ on a reopened program
    // takes the old positional path.
    const source = new Blockly.Workspace();
    buildProcedureAndCall(source, 'tu');
    const saved = Blockly.serialization.workspaces.save(source);
    source.dispose();

    Blockly.serialization.workspaces.load(saved, workspace);
    await flush();
    const def = workspace.getAllBlocks(false)
      .find((block) => block.type === 'procedures_defnoreturn');
    const call = workspace.getAllBlocks(false)
      .find((block) => block.type === 'procedures_callnoreturn');

    clickMinus(def, 0);
    await flush();

    expect(args(call)).toEqual(['y=22', 'z=33']);
    expect(looseNumbers(workspace)).toEqual([11]);
  });

  test('„Funktion mit Rückgabe" behaves the same', async () => {
    const { def, call } = buildProcedureAndCall(workspace, 'rechne', 'procedures_callreturn');
    await flush();

    clickMinus(def, 0);
    await flush();

    expect(args(call)).toEqual(['y=22', 'z=33']);
  });

  test('renaming a parameter moves no value, and the saved bytes are Blockly’s own', async () => {
    const { def, call } = buildProcedureAndCall(workspace, 'tu');
    await flush();

    def.getField(def.argData_[1].argId).setValue('zahl');
    await flush();

    expect(args(call)).toEqual(['x=11', 'zahl=22', 'z=33']);
    // The id map lives in memory; the caller keeps core's {name, params} shape,
    // so a document written here still loads in a Blockly without this fix.
    expect(Blockly.serialization.blocks.save(call).extraState)
      .toEqual({ name: 'tu', params: ['x', 'zahl', 'z'] });
  });
});
