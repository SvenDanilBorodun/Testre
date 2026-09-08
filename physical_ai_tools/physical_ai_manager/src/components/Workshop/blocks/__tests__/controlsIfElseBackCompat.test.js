/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * Proof 3 for the `controls_if` else clause: bytes written by the NEW mutator
 * still load in the OLD code.
 *
 * This file deliberately NEVER calls `registerControlsIfElseMutator()`. vitest
 * isolates module state per file, so `controls_if` here is built by whichever
 * mutator was registered at construction time — Blockly core 12.5.1's in the
 * first block, @blockly/block-plus-minus 9.0.10's in the second, i.e. exactly
 * the two "old" readers a student's file can meet: an un-updated image, and the
 * synchronous `workspaces.load` that runs before `initPlugins` resolves.
 *
 * The fixtures are shared with `controlsIfElse.test.js`, which asserts that our
 * mutator writes precisely those bytes. Same bytes in, same bytes out, either
 * way round — that pairing is the cross-version proof.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerMotionBlocks } from '../motion';
import { registerControlBlocks } from '../control';
import { ALL_FIXTURES, IF_WITH_ELSE, IF_ELSEIF_ELSE } from './controlsIfFixtures';

// Every extraState shape the NEW mutator can emit. Keep in lockstep with the
// `saveExtraState` assertions in controlsIfElse.test.js.
const STATES_THE_NEW_CODE_WRITES = [
  null,
  { hasElse: true },
  { elseIfCount: 1 },
  { elseIfCount: 1, hasElse: true },
  { elseIfCount: 2, hasElse: true },
];

const expectedBlockCounts = {
  'if / dann': 2,
  'if / dann / sonst': 3,
  'if / sonst wenn x2 / sonst': 5,
  'if / sonst wenn x1, kein sonst': 3,
};

function runOldReaderProofs(readerName) {
  it.each(ALL_FIXTURES)(
    `${readerName} loads %s whole and re-saves it byte-identically`,
    (label, fixture) => {
      const workspace = new Blockly.Workspace();
      expect(() => Blockly.serialization.workspaces.load(fixture, workspace))
        .not.toThrow();
      // A MissingConnection aborts the REST of the load, so counting blocks is
      // what actually rules out a partial workspace.
      expect(workspace.getAllBlocks(false).length)
        .toBe(expectedBlockCounts[label]);
      expect(JSON.stringify(Blockly.serialization.workspaces.save(workspace)))
        .toBe(JSON.stringify(fixture));
      workspace.dispose();
    },
  );

  it(`${readerName} materialises the ELSE input and its body`, () => {
    const workspace = new Blockly.Workspace();
    Blockly.serialization.workspaces.load(IF_WITH_ELSE, workspace);
    const block = workspace.getBlockById('if-else-00000000000');
    expect(block.getInput('ELSE')).not.toBeNull();
    expect(block.getInputTargetBlock('ELSE').id).toBe('else-body-000000000');
    workspace.dispose();
  });

  it(`${readerName} accepts every extraState the new mutator writes`, () => {
    STATES_THE_NEW_CODE_WRITES.forEach((state) => {
      const workspace = new Blockly.Workspace();
      const block = workspace.newBlock('controls_if');
      expect(() => {
        if (state) block.loadExtraState(state);
      }).not.toThrow();
      expect(!!block.getInput('ELSE')).toBe(!!(state && state.hasElse));
      // and it writes the same thing straight back
      expect(JSON.stringify(block.saveExtraState() || null))
        .toBe(JSON.stringify(state));
      workspace.dispose();
    });
  });

  it(`${readerName} keeps every else-if body of the both-keys fixture`, () => {
    const workspace = new Blockly.Workspace();
    Blockly.serialization.workspaces.load(IF_ELSEIF_ELSE, workspace);
    const block = workspace.getBlockById('if-both-00000000000');
    ['DO0', 'DO1', 'DO2', 'ELSE'].forEach((name) => {
      expect(block.getInputTargetBlock(name)).not.toBeNull();
    });
    workspace.dispose();
  });
}

describe('proof 3a — Blockly core 12.5.1 mutator reads the new bytes', () => {
  beforeAll(() => {
    Blockly.setLocale(De);
    registerMotionBlocks();
    registerControlBlocks();
  });

  it('is really running core’s mutator (no plugin imported yet)', () => {
    // Core tracks `elseifCount_`/`elseCount_`; the plugin uses
    // `elseIfCount_`/`hasElse_`. If this ever flips, the describe below stops
    // testing a second reader and this file silently proves half as much.
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    expect(block.elseifCount_).toBe(0);
    expect(block.elseCount_).toBe(0);
    expect(block.hasElse_).toBeUndefined();
    workspace.dispose();
  });

  runOldReaderProofs('Blockly core');
});

describe('proof 3b — the block-plus-minus mutator reads the new bytes', () => {
  beforeAll(async () => {
    await import('@blockly/block-plus-minus');
  });

  it('is really running the plugin’s mutator', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('controls_if');
    expect(block.hasElse_).toBe(false);
    expect(block.elseIfCount_).toBe(0);
    // The plugin has no way to ADD an else — that is the defect our mutator
    // fixes, pinned here so the two files cannot drift into agreeing.
    block.plus();
    block.plus();
    expect(block.getInput('ELSE')).toBeNull();
    expect(block.inputList.map((i) => i.name))
      .toEqual(['IF0', 'DO0', 'IF1', 'DO1', 'IF2', 'DO2']);
    workspace.dispose();
  });

  runOldReaderProofs('block-plus-minus');
});
