/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Toolbox-consistency guard.
//
// A toast block (`edubotics_toast`) once shipped DEFINED + backend-registered
// but MISSING from the toolbox, so students could never see or use it. This
// suite registers every Workshop block exactly as BlocklyWorkspace.jsx's
// registerAllBlocksOnce() does, then asserts the built toolbox surfaces EVERY
// defined `edubotics_*` block. It MUST fail if a future block is defined but
// not added to buildToolbox().

import * as Blockly from 'blockly/core';
import { buildToolbox, TOOLBOX } from '../blocks/toolbox';
import { registerMotionBlocks } from '../blocks/motion';
import { registerPerceptionBlocks } from '../blocks/perception';
import { registerDestinationBlocks } from '../blocks/destinations';
import { registerOutputBlocks } from '../blocks/output';
import { registerEventBlocks } from '../blocks/events';
import { registerControlBlocks } from '../blocks/control';
import { registerCounterBlocks } from '../blocks/counters';

// Mirror BlocklyWorkspace.jsx::registerAllBlocksOnce() — same functions, same
// order. Each register* call is idempotent (HMR/re-import guarded), so calling
// them here is safe even though the editor may also register at import time.
function registerAllWorkshopBlocks() {
  registerMotionBlocks();
  registerPerceptionBlocks();
  registerDestinationBlocks();
  registerOutputBlocks();
  registerEventBlocks();
  registerControlBlocks();
  registerCounterBlocks();
}

// Recursively collect every `{ kind: 'block', type }` entry from a toolbox JSON
// tree. Walks nested category `contents` arrays and any nested objects; shadow
// blocks (`{ shadow: { type } }`) carry no `kind` and are intentionally NOT
// collected (they are never `edubotics_*` and aren't palette entries).
function collectToolboxBlockTypes(node, acc = new Set()) {
  if (Array.isArray(node)) {
    node.forEach((n) => collectToolboxBlockTypes(n, acc));
  } else if (node && typeof node === 'object') {
    if (node.kind === 'block' && typeof node.type === 'string') {
      acc.add(node.type);
    }
    Object.values(node).forEach((v) => {
      if (v && typeof v === 'object') collectToolboxBlockTypes(v, acc);
    });
  }
  return acc;
}

// Allowlist of `edubotics_*` blocks that are DELIBERATELY defined but withheld
// from the toolbox palette. Currently empty — every defined block belongs in
// the toolbox. If you ever need to exclude one, add it here WITH a one-line
// reason; otherwise this guard fails (a defined-but-invisible block is exactly
// the shipped toast-block blocker this suite exists to catch).
const INTENTIONALLY_EXCLUDED_FROM_TOOLBOX = new Set([]);

describe('Workshop toolbox consistency', () => {
  let definedTypes;
  let toolboxTypes;

  beforeAll(() => {
    registerAllWorkshopBlocks();
    definedTypes = new Set(
      Object.keys(Blockly.Blocks).filter((t) => t.startsWith('edubotics_'))
    );
    toolboxTypes = collectToolboxBlockTypes(buildToolbox());
  });

  test('Blockly imports + registers every Workshop block cleanly under jsdom', () => {
    // A registration regression (a register* function silently no-op'ing) would
    // collapse this count. The repo currently defines well over 30 edubotics_*
    // blocks across the 7 register groups.
    expect(definedTypes.size).toBeGreaterThanOrEqual(30);
    // Spot-check one block from each register group is actually registered.
    for (const type of [
      'edubotics_home', // motion
      'edubotics_grasp_object', // perception (custom init dropdown)
      'edubotics_object_position', // perception (static JSON)
      'edubotics_destination_pin', // destinations
      'edubotics_toast', // output (the shipped blocker)
      'edubotics_broadcast', // events
      'edubotics_forever', // control
      'edubotics_counter_reset', // counters
    ]) {
      expect(Blockly.Blocks[type]).toBeTruthy();
    }
  });

  test('every defined edubotics_* block appears in the built toolbox', () => {
    const missing = [...definedTypes].filter(
      (t) => !toolboxTypes.has(t) && !INTENTIONALLY_EXCLUDED_FROM_TOOLBOX.has(t)
    );
    // Helpful diff: lists any defined-but-unreachable block by name.
    expect(missing).toEqual([]);
  });

  test('the edubotics_toast block is present in the toolbox (regression anchor)', () => {
    // The exact bug this suite guards against: a defined + backend-registered
    // toast block that never made it into the palette.
    expect(toolboxTypes.has('edubotics_toast')).toBe(true);
  });

  test('every edubotics_* toolbox entry references a real registered block', () => {
    // Reverse direction: a toolbox typo / a block renamed-or-deleted but still
    // listed in buildToolbox() would point students at a non-existent block.
    const dangling = [...toolboxTypes].filter(
      (t) => t.startsWith('edubotics_') && !definedTypes.has(t)
    );
    expect(dangling).toEqual([]);
  });

  test('the named TOOLBOX export and a fresh buildToolbox() agree on edubotics_* blocks', () => {
    // TOOLBOX is `buildToolbox()` captured at module load; assert the runtime
    // default the editor injects carries the same edubotics_* set.
    const exportTypes = collectToolboxBlockTypes(TOOLBOX);
    const exportEdubotics = [...exportTypes].filter((t) => t.startsWith('edubotics_')).sort();
    const freshEdubotics = [...toolboxTypes].filter((t) => t.startsWith('edubotics_')).sort();
    expect(exportEdubotics).toEqual(freshEdubotics);
  });
});
