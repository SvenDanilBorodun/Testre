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
 * „wiederhole fortlaufend" — the EDITOR half of the dead-code warning.
 *
 * The block carries a `nextStatement` connector, so a student can snap blocks
 * underneath it, where they can never run: the loop's only exit is Stopp.
 * `interpreter.py::_exec_forever` says so at RUN time and its docstring claims
 * `blocks/control.js` says so while the program is being written — this is the
 * half that makes that claim true.
 *
 * Removing the connector is NOT an option and this test exists partly to keep
 * that on the record: `Blockly.serialization.workspaces.load` throws
 * MissingConnection on any saved workspace that already has a block there,
 * `BlocklyWorkspace.jsx` swallows the throw to console.error, and the next edit
 * autosaves the truncated program — the exact failure that forced the
 * `check: 'String'` revert documented in `blocks/motion.js`.
 *
 * Real Blockly 12.5.1, real events, real `getNextBlock()` — the listener is
 * driven by the workspace, never called directly.
 */
import { describe, it, expect, beforeAll, vi } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerMotionBlocks } from '../motion';
import {
  registerControlBlocks,
  attachControlWorkspaceValidators,
  FOREVER_DEAD_CODE_WARNING_ID,
} from '../control';
import { DE } from '../messages_de';

// Blockly does NOT dispatch events synchronously: `events/utils.ts::
// fireInternal` schedules `requestAnimationFrame(() => setTimeout(fireNow, 0))`
// for the first queued event, so nothing has reached the change listener when
// `connect()` returns — and a plain `setTimeout(…, 0)` is still too early
// (measured: 0 of 4 events delivered after 10 ms, all 4 after the next frame).
// Register our own rAF AFTER Blockly's, then step one macrotask past its
// `setTimeout(fireNow, 0)`.
const flushEvents = async () => {
  await new Promise((resolve) => {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => { setTimeout(resolve, 0); });
    } else {
      setTimeout(resolve, 0);
    }
  });
  await new Promise((resolve) => { setTimeout(resolve, 0); });
};

describe('„wiederhole fortlaufend" dead-code warning', () => {
  beforeAll(() => {
    Blockly.setLocale(De);
    registerMotionBlocks();
    registerControlBlocks();
  });

  const build = () => {
    const workspace = new Blockly.Workspace();
    const dispose = attachControlWorkspaceValidators(workspace);
    const forever = workspace.newBlock('edubotics_forever');
    const spy = vi.spyOn(forever, 'setWarningText');
    return { workspace, dispose, forever, spy };
  };

  it('warns in German when a block is snapped underneath', async () => {
    const { workspace, dispose, forever, spy } = build();
    const below = workspace.newBlock('edubotics_home');

    forever.nextConnection.connect(below.previousConnection);
    await flushEvents();

    expect(forever.getNextBlock()).toBe(below);
    expect(spy).toHaveBeenCalledWith(
      DE.FOREVER_DEAD_CODE_WARNING,
      FOREVER_DEAD_CODE_WARNING_ID,
    );
    dispose();
    workspace.dispose();
  });

  it('clears the warning again when the block is pulled off', async () => {
    const { workspace, dispose, forever, spy } = build();
    const below = workspace.newBlock('edubotics_home');

    forever.nextConnection.connect(below.previousConnection);
    await flushEvents();
    spy.mockClear();

    forever.nextConnection.disconnect();
    await flushEvents();

    expect(forever.getNextBlock()).toBeNull();
    expect(spy).toHaveBeenCalledWith(null, FOREVER_DEAD_CODE_WARNING_ID);
    // and never with the warning text on this pass
    expect(spy).not.toHaveBeenCalledWith(
      DE.FOREVER_DEAD_CODE_WARNING,
      FOREVER_DEAD_CODE_WARNING_ID,
    );
    dispose();
    workspace.dispose();
  });

  it('never warns about blocks INSIDE the loop — those run every pass', async () => {
    const { workspace, dispose, forever, spy } = build();
    const inside = workspace.newBlock('edubotics_home');

    forever.getInput('DO').connection.connect(inside.previousConnection);
    await flushEvents();

    expect(forever.getNextBlock()).toBeNull();
    expect(spy).not.toHaveBeenCalledWith(
      DE.FOREVER_DEAD_CODE_WARNING,
      FOREVER_DEAD_CODE_WARNING_ID,
    );
    dispose();
    workspace.dispose();
  });

  it('is KEYED, so it cannot collide with RunControls’ unkeyed warnings',
    async () => {
      // BlockSvg.setWarningText keeps one message per id. RunControls'
      // debuggerWarnings effect writes with NO id; this writer must always
      // pass its own, or the two would overwrite each other on any block that
      // happened to carry both.
      const { workspace, dispose, forever, spy } = build();
      const below = workspace.newBlock('edubotics_home');
      forever.nextConnection.connect(below.previousConnection);
      await flushEvents();

      expect(spy).toHaveBeenCalled();
      spy.mock.calls.forEach((call) => {
        expect(call[1]).toBe(FOREVER_DEAD_CODE_WARNING_ID);
      });
      dispose();
      workspace.dispose();
    });

  it('the German text carries literal umlauts (Rule §1)', () => {
    expect(DE.FOREVER_DEAD_CODE_WARNING).toContain('Blöcke');
    expect(DE.FOREVER_DEAD_CODE_WARNING).not.toMatch(/ae|oe|ue/);
  });

  it('the disposer detaches the listener', async () => {
    const { workspace, dispose, forever, spy } = build();
    dispose();
    const below = workspace.newBlock('edubotics_home');
    forever.nextConnection.connect(below.previousConnection);
    await flushEvents();
    expect(spy).not.toHaveBeenCalled();
    workspace.dispose();
  });

  it('tolerates a workspace that cannot take listeners', () => {
    expect(typeof attachControlWorkspaceValidators(null)).toBe('function');
    expect(() => attachControlWorkspaceValidators(null)()).not.toThrow();
  });
});
