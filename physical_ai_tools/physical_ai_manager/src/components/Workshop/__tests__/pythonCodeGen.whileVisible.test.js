/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// E2 (Roboter Studio review fixes): the „Code"-Vorschau of „Solange <Typ>
// sichtbar" says what the block does. Its „höchstens N Mal" renders as a
// counted loop (0 keeps the `while`), and one German comment above the loop
// states what a text program cannot show: the observation pose before every
// look, the skip of an object that cannot be grasped, the end without
// progress. The preview must still be a Python module CPython compiles.

import { spawnSync } from 'child_process';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { beforeAll, describe, expect, it } from 'vitest';
import { registerMotionBlocks } from '../blocks/motion';
import { registerPerceptionBlocks, setObjectCatalogOptions } from '../blocks/perception';
import { registerControlBlocks } from '../blocks/control';
import { generatePython } from '../pythonCodeGen';

const COMMENT = '# „Solange Würfel sichtbar": vor jedem Blick fährt der Arm in die Beobachtungsstellung; '
  + 'ein Objekt, das nicht gegriffen werden kann, wird übersprungen; ohne Fortschritt endet die Schleife von selbst.';

async function gen(blocks) {
  const ws = new Blockly.Workspace();
  try {
    Blockly.serialization.workspaces.load({ blocks: { blocks } }, ws);
    return await generatePython(ws);
  } finally {
    ws.dispose();
  }
}

// CPython's own compiler (not only its parser: `break` outside a loop is a
// compile error).
function compiles(src) {
  const r = spawnSync('python3', ['-c', 'import sys; compile(sys.stdin.read(), "<vorschau>", "exec")'], {
    input: src, encoding: 'utf8',
  });
  if (r.error) throw r.error;
  return { ok: r.status === 0, stderr: r.stderr };
}

const loop = (maxReps, body = { type: 'edubotics_home' }) => ({
  type: 'edubotics_while_visible',
  fields: { OBJECT_TYPE: 'cube', MAX_REPS: maxReps },
  ...(body ? { inputs: { DO: { block: body } } } : {}),
});

beforeAll(() => {
  Blockly.setLocale(De);
  registerMotionBlocks();
  registerPerceptionBlocks();
  registerControlBlocks();
  setObjectCatalogOptions([['Würfel', 'cube']]);
});

describe('E2: the preview of „Solange … sichtbar"', () => {
  it('MAX_REPS 0 keeps the while loop, under the comment', async () => {
    const code = await gen([loop(0)]);
    expect(code).toContain(`${COMMENT}\nwhile robot.sees('cube'):\n  robot.home()\n`);
    expect(code).not.toContain('_durchgang');
  });

  it('MAX_REPS 1 renders a counted loop that ends when nothing is seen', async () => {
    const code = await gen([loop(1)]);
    expect(code).toContain(
      `${COMMENT}\nfor _durchgang in range(1):\n  if not robot.sees('cube'):\n    break\n  robot.home()\n`,
    );
    expect(code).not.toMatch(/^while /m);
    expect(await gen([loop(3)])).toContain('for _durchgang in range(3):');
  });

  it('0 and 1 render differently, and the comment is always there', async () => {
    const zero = await gen([loop(0)]);
    const one = await gen([loop(1)]);
    expect(zero).not.toBe(one);
    for (const code of [zero, one]) expect(code.split(COMMENT).length - 1).toBe(1);
  });

  it('an empty body is `pass`, and a nested loop keeps the comment at its level', async () => {
    expect(await gen([loop(2, null)])).toContain("if not robot.sees('cube'):\n    break\n  pass\n");
    const nested = await gen([{ type: 'edubotics_forever', inputs: { DO: { block: loop(2) } } }]);
    expect(nested).toContain(`while True:\n  ${COMMENT}\n  for _durchgang in range(2):\n    if not robot.sees('cube'):\n      break\n    robot.home()\n`);
  });

  it('runs like the block: at most N passes, ending early when nothing is seen', async () => {
    // A stub `robot` that sees `visible` objects and removes one per
    // robot.home() — run by CPython, which prints how many passes ran.
    const passes = async (maxReps, visible) => {
      const stub = [
        'import sys, types',
        'robot = types.ModuleType("robot")',
        `state = {"left": ${visible}, "passes": 0}`,
        'robot.sees = lambda t: state["left"] > 0',
        'def _home():',
        '    state["passes"] += 1',
        '    state["left"] -= 1',
        'robot.home = _home',
        'sys.modules["robot"] = robot',
        'exec(compile(sys.stdin.read(), "<vorschau>", "exec"), {})',
        'print(state["passes"])',
      ].join('\n');
      const r = spawnSync('python3', ['-c', stub], { input: await gen([loop(maxReps)]), encoding: 'utf8' });
      if (r.error) throw r.error;
      expect({ status: r.status, stderr: r.stderr }).toMatchObject({ status: 0 });
      return Number(r.stdout.trim());
    };
    expect(await passes(3, 10)).toBe(3);
    expect(await passes(3, 2)).toBe(2);
    expect(await passes(1, 5)).toBe(1);
    expect(await passes(0, 4)).toBe(4);
  });

  it('the module compiles, in every shape', async () => {
    const programs = [
      [loop(0)], [loop(1)], [loop(5, null)],
      [{ type: 'edubotics_forever', inputs: { DO: { block: loop(2, loop(1)) } } }],
    ];
    for (const blocks of programs) {
      // eslint-disable-next-line no-await-in-loop
      const code = await gen(blocks);
      const r = compiles(code);
      // The code and CPython's message ride along, so a failure shows both.
      expect({ ...r, code }).toMatchObject({ ok: true });
    }
  });
});
