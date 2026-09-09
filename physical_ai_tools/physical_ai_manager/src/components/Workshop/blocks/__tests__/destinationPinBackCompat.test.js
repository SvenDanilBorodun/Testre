// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Backward safety for the destination pin, proved against real Blockly 12.5.1.
//
// A pinned destination's HEIGHT is now re-asked from the table plane at run time
// (`motion.resolve_destination_z`) instead of being trusted from storage — the
// server side of audit §9.2(a). The tempting follow-up is to drop the now-
// advisory `Z` label field from the block. It must NOT be dropped: every
// already-saved workflow carries a `Z` in `fields`, and CLAUDE.md records the
// same trap three times over (`workspaces.load` throws MissingConnection or
// drops state, `BlocklyWorkspace.jsx` swallows it to `console.error`, and the
// next edit autosaves the truncated program). The field is also still the
// FALLBACK on an uncalibrated rig, where no plane exists to ask.

import { describe, it, expect, beforeAll } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import { registerDestinationBlocks } from '../destinations';

// The bytes a pre-change editor saved: a pin carrying all three label fields.
const LEGACY_PIN = {
  blocks: {
    languageVersion: 0,
    blocks: [{
      type: 'edubotics_destination_pin',
      id: 'pin1',
      fields: { NAME: 'Ablage', X: '0.220', Y: '0.000', Z: '0.045' },
    }],
  },
};

beforeAll(() => {
  registerDestinationBlocks();
});

describe('edubotics_destination_pin — a saved workflow still loads whole', () => {
  it('loads the legacy X/Y/Z fields without dropping the block', () => {
    const workspace = new Blockly.Workspace();
    expect(() => Blockly.serialization.workspaces.load(LEGACY_PIN, workspace))
      .not.toThrow();
    // A MissingConnection aborts the REST of a load, so counting is what rules
    // out a silently truncated workspace.
    expect(workspace.getAllBlocks(false).length).toBe(1);
    const block = workspace.getBlockById('pin1');
    expect(block.getFieldValue('NAME')).toBe('Ablage');
    expect(block.getFieldValue('X')).toBe('0.220');
    expect(block.getFieldValue('Y')).toBe('0.000');
    expect(block.getFieldValue('Z')).toBe('0.045');
  });

  it('re-saves it byte-identically, so an autosave cannot silently truncate it', () => {
    const workspace = new Blockly.Workspace();
    Blockly.serialization.workspaces.load(LEGACY_PIN, workspace);
    const again = Blockly.serialization.workspaces.save(workspace);
    expect(again.blocks.blocks[0].fields).toEqual({
      NAME: 'Ablage', X: '0.220', Y: '0.000', Z: '0.045',
    });
  });

  it('still serialises a Z the server can fall back to on an uncalibrated rig', () => {
    const workspace = new Blockly.Workspace();
    const block = workspace.newBlock('edubotics_destination_pin');
    block.setFieldValue('0.045', 'Z');
    const saved = Blockly.serialization.workspaces.save(workspace);
    expect(saved.blocks.blocks[0].fields.Z).toBe('0.045');
  });
});
