/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Blockly → real-Python generator contract. Registers every Workshop block
// exactly as the editor does, builds a HEADLESS workspace from serialized
// state, runs pythonCodeGen.generatePython, and asserts the block → robot.*
// map plus hat-wrapping, C-block bodies, and built-in-block passthrough.

import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerMotionBlocks } from '../blocks/motion';
import {
  registerPerceptionBlocks,
  setObjectCatalogOptions,
} from '../blocks/perception';
import { registerDestinationBlocks } from '../blocks/destinations';
import { registerOutputBlocks } from '../blocks/output';
import { registerEventBlocks } from '../blocks/events';
import { registerControlBlocks } from '../blocks/control';
import { registerCounterBlocks } from '../blocks/counters';
import { registerTrajectoryBlocks } from '../blocks/trajectories';
import { generatePython } from '../pythonCodeGen';

function registerAll() {
  registerMotionBlocks();
  registerPerceptionBlocks();
  registerDestinationBlocks();
  registerOutputBlocks();
  registerEventBlocks();
  registerControlBlocks();
  registerCounterBlocks();
  registerTrajectoryBlocks();
}

// Build a headless workspace from a list of top-level serialized blocks, then
// generate Python from it.
async function gen(blocks) {
  const ws = new Blockly.Workspace();
  try {
    Blockly.serialization.workspaces.load({ blocks: { blocks } }, ws);
    return await generatePython(ws);
  } finally {
    ws.dispose();
  }
}

beforeAll(() => {
  // Populate Blockly.Msg so built-in blocks (controls_*, text, math_*)
  // interpolate their messages on init — mirrors BlocklyWorkspace.jsx.
  Blockly.setLocale(De);
  registerAll();
  // Make 'cube' a valid OBJECT_TYPE dropdown value so the serialized blocks
  // deserialize cleanly.
  setObjectCatalogOptions([['Würfel', 'cube']]);
});

describe('pythonCodeGen — motion', () => {
  test('home / gripper / lift map to robot.* calls', async () => {
    const code = await gen([
      { type: 'edubotics_home' },
      { type: 'edubotics_open_gripper' },
      { type: 'edubotics_close_gripper' },
      { type: 'edubotics_lift' },
    ]);
    expect(code).toContain('robot.home()');
    expect(code).toContain('robot.open_gripper()');
    expect(code).toContain('robot.close_gripper()');
    expect(code).toContain('robot.lift()');
  });

  test('wait_seconds reads its SECONDS value input', async () => {
    const code = await gen([
      {
        type: 'edubotics_wait_seconds',
        inputs: { SECONDS: { shadow: { type: 'math_number', fields: { NUM: 2 } } } },
      },
    ]);
    expect(code).toContain('robot.wait(2)');
  });

  test('move_to consumes a destination_ref value as a string', async () => {
    const code = await gen([
      {
        type: 'edubotics_move_to',
        inputs: {
          DESTINATION: { block: { type: 'edubotics_destination_ref', fields: { NAME: 'A' } } },
        },
      },
    ]);
    expect(code).toContain("robot.move_to('A')");
  });

  test('per-move „Tempo" (GESCHWINDIGKEIT) renders as a tempo kwarg', async () => {
    const code = await gen([
      {
        type: 'edubotics_move_to',
        fields: { GESCHWINDIGKEIT: 'schnell' },
        inputs: {
          DESTINATION: { block: { type: 'edubotics_destination_ref', fields: { NAME: 'A' } } },
        },
      },
      { type: 'edubotics_lift', fields: { GESCHWINDIGKEIT: 'langsam' } },
    ]);
    expect(code).toContain("robot.move_to('A', tempo='schnell')");
    expect(code).toContain("robot.lift(tempo='langsam')");
  });

  test('default „Tempo" (global) renders no tempo kwarg', async () => {
    const code = await gen([
      {
        type: 'edubotics_move_to',
        fields: { GESCHWINDIGKEIT: 'global' },
        inputs: {
          DESTINATION: { block: { type: 'edubotics_destination_ref', fields: { NAME: 'B' } } },
        },
      },
    ]);
    expect(code).toContain("robot.move_to('B')");
    expect(code).not.toContain('tempo=');
  });

  test('replay_trajectory maps to robot.replay(<name>)', async () => {
    const code = await gen([
      { type: 'edubotics_replay_trajectory', fields: { NAME: 'Bewegung 1' } },
    ]);
    expect(code).toContain("robot.replay('Bewegung 1')");
  });
});

describe('pythonCodeGen — perception', () => {
  test('grasp_object statement uses the OBJECT_TYPE field', async () => {
    const code = await gen([
      { type: 'edubotics_grasp_object', fields: { OBJECT_TYPE: 'cube' } },
    ]);
    expect(code).toContain("robot.grasp('cube')");
  });

  test('sees() value block plugs into a built-in controls_if', async () => {
    const code = await gen([
      {
        type: 'controls_if',
        inputs: {
          IF0: { block: { type: 'edubotics_see_object', fields: { OBJECT_TYPE: 'cube' } } },
          DO0: { block: { type: 'edubotics_home' } },
        },
      },
    ]);
    expect(code).toContain("if robot.sees('cube'):");
    expect(code).toMatch(/if robot\.sees\('cube'\):\n {2}robot\.home\(\)/);
  });

  test('find_object → descend_to nests as a value', async () => {
    const code = await gen([
      {
        type: 'edubotics_descend_to',
        inputs: {
          ZIEL: { block: { type: 'edubotics_find_object', fields: { OBJECT_TYPE: 'cube' } } },
        },
      },
    ]);
    expect(code).toContain("robot.descend_to(robot.find('cube'))");
  });

  test('grasp_held / wait_until_held map to is_holding / wait_until_held', async () => {
    const code = await gen([
      {
        type: 'edubotics_wait_until',
        inputs: { BOOL: { block: { type: 'edubotics_grasp_held' } } },
      },
      { type: 'edubotics_wait_until_held' },
    ]);
    expect(code).toContain('robot.is_holding()');
    // wait_until_held is a value block; a top-level naked value becomes a stmt.
    expect(code).toContain('robot.wait_until_held(10)');
  });

  test('while_visible becomes a while-loop over robot.sees(...)', async () => {
    const code = await gen([
      {
        type: 'edubotics_while_visible',
        fields: { OBJECT_TYPE: 'cube', MAX_REPS: 0 },
        inputs: { DO: { block: { type: 'edubotics_home' } } },
      },
    ]);
    expect(code).toMatch(/while robot\.sees\('cube'\):\n {2}robot\.home\(\)/);
  });
});

describe('pythonCodeGen — destinations', () => {
  test('destination_pin emits coordinates (None when un-pinned)', async () => {
    const code = await gen([
      { type: 'edubotics_destination_pin', fields: { NAME: 'A', X: '0.1', Y: '-0.2', Z: '0.05' } },
      { type: 'edubotics_destination_pin', fields: { NAME: 'B' } },
    ]);
    expect(code).toContain("robot.pin('A', 0.1, -0.2, 0.05)");
    expect(code).toContain("robot.pin('B', None, None, None)");
  });

  test('destination_current → pin_current', async () => {
    const code = await gen([
      { type: 'edubotics_destination_current', fields: { NAME: 'Heim' } },
    ]);
    expect(code).toContain("robot.pin_current('Heim')");
  });
});

describe('pythonCodeGen — output + counters', () => {
  test('output blocks map to log/beep/speak/tone/toast', async () => {
    const code = await gen([
      {
        type: 'edubotics_log',
        inputs: { MESSAGE: { shadow: { type: 'text', fields: { TEXT: 'Hallo' } } } },
      },
      { type: 'edubotics_play_sound' },
      { type: 'edubotics_play_tone', fields: { FREQ: 880, SECONDS: 0.25 } },
      {
        type: 'edubotics_toast',
        fields: { LEVEL: 'success', SECONDS: 3 },
        inputs: { TEXT: { shadow: { type: 'text', fields: { TEXT: 'Fertig' } } } },
      },
    ]);
    expect(code).toContain("robot.log('Hallo')");
    expect(code).toContain('robot.beep()');
    expect(code).toContain('robot.tone(880, 0.25)');
    expect(code).toContain("robot.toast('Fertig', level='success', seconds=3)");
  });

  test('counter blocks map to counter_reset/add/get', async () => {
    const code = await gen([
      { type: 'edubotics_counter_reset', fields: { NAME: 'Punkte' } },
      { type: 'edubotics_counter_add', fields: { NAME: 'Punkte' } },
      {
        type: 'edubotics_log',
        inputs: { MESSAGE: { block: { type: 'edubotics_counter_get', fields: { NAME: 'Punkte' } } } },
      },
    ]);
    expect(code).toContain("robot.counter_reset('Punkte')");
    expect(code).toContain("robot.counter_add('Punkte')");
    expect(code).toContain("robot.log(robot.counter_get('Punkte'))");
  });
});

describe('pythonCodeGen — events + control', () => {
  test('broadcast statement + when_broadcast hat wrapper', async () => {
    const code = await gen([
      { type: 'edubotics_broadcast', fields: { EVENT_NAME: 'start' } },
      {
        type: 'edubotics_when_broadcast',
        fields: { EVENT_NAME: 'start' },
        next: { block: { type: 'edubotics_home' } },
      },
    ]);
    expect(code).toContain("robot.broadcast('start')");
    expect(code).toMatch(/def when_start\(\):\n {2}robot\.home\(\)/);
  });

  test('when_counter_gt hat with empty body emits pass', async () => {
    const code = await gen([
      { type: 'edubotics_when_counter_gt', fields: { NAME: 'Punkte', N: 3 } },
    ]);
    expect(code).toMatch(/def when_punkte_over_3\(\):\n {2}pass/);
  });

  test('forever → while True with indented body', async () => {
    const code = await gen([
      {
        type: 'edubotics_forever',
        inputs: { DO: { block: { type: 'edubotics_home' } } },
      },
    ]);
    expect(code).toMatch(/while True:\n {2}robot\.home\(\)/);
  });
});

describe('pythonCodeGen — built-in blocks + edge cases', () => {
  test('built-in controls_repeat_ext is handled by the Python generator', async () => {
    const code = await gen([
      {
        type: 'controls_repeat_ext',
        inputs: {
          TIMES: { shadow: { type: 'math_number', fields: { NUM: 3 } } },
          DO: { block: { type: 'edubotics_home' } },
        },
      },
    ]);
    expect(code).toMatch(/for \w+ in range\(3\):/);
    expect(code).toContain('robot.home()');
  });

  test('an empty workspace produces empty code', async () => {
    expect((await gen([])).trim()).toBe('');
  });

  test('a null workspace resolves to an empty string', async () => {
    expect(await generatePython(null)).toBe('');
  });
});
