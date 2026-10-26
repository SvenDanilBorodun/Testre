/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// E3 (Roboter Studio review fixes): on the robot every „wenn"-Block runs in
// its own thread and a „sende Nachricht" goes on at once. The „Code"-Vorschau
// says the same: a send STARTS each listening hat's function in a thread
// (`threading.Thread(target=when_…).start()`), `import threading` follows
// `import robot` (which stays the first import), and the module compiles.

import { spawnSync } from 'child_process';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { beforeAll, describe, expect, it } from 'vitest';
import { registerMotionBlocks } from '../blocks/motion';
import { registerEventBlocks } from '../blocks/events';
import { registerControlBlocks } from '../blocks/control';
import { generatePython } from '../pythonCodeGen';

async function gen(blocks) {
  const ws = new Blockly.Workspace();
  try {
    Blockly.serialization.workspaces.load({ blocks: { blocks } }, ws);
    return await generatePython(ws);
  } finally {
    ws.dispose();
  }
}

function compiles(src) {
  const r = spawnSync('python3', ['-c', 'import sys; compile(sys.stdin.read(), "<vorschau>", "exec")'], {
    input: src, encoding: 'utf8',
  });
  if (r.error) throw r.error;
  return { ok: r.status === 0, stderr: r.stderr };
}

const send = (name) => ({ type: 'edubotics_broadcast', fields: { EVENT_NAME: name } });
const hat = (name, body = { type: 'edubotics_home' }) => ({
  type: 'edubotics_when_broadcast', fields: { EVENT_NAME: name }, ...(body ? { next: { block: body } } : {}),
});

beforeAll(() => {
  Blockly.setLocale(De);
  registerMotionBlocks();
  registerEventBlocks();
  registerControlBlocks();
});

describe('E3: the preview of „sende Nachricht"', () => {
  it('starts every listening hat in a thread, one line each', async () => {
    const code = await gen([send('start'), hat('start'), hat('start', { type: 'edubotics_open_gripper' })]);
    expect(code).toContain(
      'threading.Thread(target=when_start).start()  # Nachricht „start“ — der „wenn“-Block läuft nebenher\n'
      + 'threading.Thread(target=when_start_2).start()  # Nachricht „start“ — der „wenn“-Block läuft nebenher\n',
    );
    // The hat is never CALLED directly any more.
    expect(code).not.toMatch(/^\s*when_start(?:_2)?\(\)/m);
  });

  it('imports threading right after robot, which stays the first import', async () => {
    const code = await gen([
      { type: 'controls_repeat_ext', inputs: { TIMES: { block: { type: 'math_number', fields: { NUM: 2 } } }, DO: { block: send('los') } } },
      { type: 'math_change', fields: { VAR: { id: 'v' } }, inputs: { DELTA: { block: { type: 'math_number', fields: { NUM: 1 } } } } },
      hat('los'),
    ].map((b, k) => ({ ...b, x: 0, y: 100 * k })));
    const imports = code.split('\n').filter((l) => /^(?:from\s+\S+\s+)?import\s/.test(l));
    expect(imports[0]).toBe('import robot');
    expect(imports[1]).toBe('import threading');
  });

  it('a send nobody waits for imports nothing and is unchanged', async () => {
    const code = await gen([send('niemand'), hat('anders')]);
    expect(code).toContain('pass  # Nachricht „niemand“ — kein „wenn“-Block wartet darauf\n');
    expect(code).not.toContain('threading');
  });

  it('the module compiles', async () => {
    const programs = [
      [send('start'), hat('start')],
      [{ type: 'edubotics_forever', inputs: { DO: { block: send('a') } } }, hat('a', null), hat('a')],
      [send('niemand')],
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
