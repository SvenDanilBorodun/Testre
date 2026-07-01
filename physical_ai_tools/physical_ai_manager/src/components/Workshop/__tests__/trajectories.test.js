/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Batch 2b — the „spiele Bewegung ab" replay block + the collectReplayNames
// walker RunControls uses to build the CONTRACT-C `trajectories` sibling.

import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import {
  registerTrajectoryBlocks,
  collectReplayNames,
  REPLAY_BLOCK_TYPE,
} from '../blocks/trajectories';
import { buildToolbox } from '../blocks/toolbox';

describe('replay-trajectory block', () => {
  beforeAll(() => {
    registerTrajectoryBlocks();
  });

  test('the replay block registers under the contract type id', () => {
    expect(REPLAY_BLOCK_TYPE).toBe('edubotics_replay_trajectory');
    expect(Blockly.Blocks[REPLAY_BLOCK_TYPE]).toBeTruthy();
  });

  test('a placed replay block carries a NAME field', () => {
    const ws = new Blockly.Workspace();
    try {
      const b = ws.newBlock(REPLAY_BLOCK_TYPE);
      expect(b.getField('NAME')).toBeTruthy();
    } finally {
      ws.dispose();
    }
  });

  test('the NAME validator rejects bracket / control chars (sentinel spoofing)', () => {
    const ws = new Blockly.Workspace();
    try {
      const b = ws.newBlock(REPLAY_BLOCK_TYPE);
      const field = b.getField('NAME');
      field.setValue('Bewegung 2');
      expect(field.getValue()).toBe('Bewegung 2');
      // A name that would spoof a [VAR:..]/[TOAST:..] log sentinel is rejected,
      // so the field keeps its previous value.
      field.setValue('Spoof [VAR:x]');
      expect(field.getValue()).toBe('Bewegung 2');
    } finally {
      ws.dispose();
    }
  });

  test('the replay block is present in the built toolbox (Bewegung)', () => {
    const json = JSON.stringify(buildToolbox());
    expect(json).toContain(REPLAY_BLOCK_TYPE);
  });
});

describe('collectReplayNames', () => {
  test('returns [] for empty / null input', () => {
    expect(collectReplayNames(null)).toEqual([]);
    expect(collectReplayNames({})).toEqual([]);
    expect(collectReplayNames({ blocks: { blocks: [] } })).toEqual([]);
  });

  test('collects unique trimmed names across nesting + next chains, first-seen order', () => {
    const tree = {
      blocks: {
        blocks: [
          { type: 'edubotics_home' },
          { type: REPLAY_BLOCK_TYPE, fields: { NAME: 'A' } },
          {
            type: 'edubotics_forever',
            inputs: {
              DO: {
                block: {
                  type: REPLAY_BLOCK_TYPE,
                  fields: { NAME: 'B' },
                  // duplicate A deeper in a next-chain → de-duplicated.
                  next: { block: { type: REPLAY_BLOCK_TYPE, fields: { NAME: 'A' } } },
                },
              },
            },
          },
          {
            type: 'controls_if',
            inputs: {
              IF0: { block: { type: 'edubotics_see_object', fields: { OBJECT_TYPE: 'cube' } } },
              // whitespace is trimmed.
              DO0: { block: { type: REPLAY_BLOCK_TYPE, fields: { NAME: '  C  ' } } },
            },
          },
        ],
      },
    };
    expect(collectReplayNames(tree)).toEqual(['A', 'B', 'C']);
  });

  test('skips replay blocks with an empty NAME', () => {
    const tree = {
      blocks: {
        blocks: [
          { type: REPLAY_BLOCK_TYPE, fields: { NAME: '   ' } },
          { type: REPLAY_BLOCK_TYPE, fields: {} },
          { type: REPLAY_BLOCK_TYPE },
        ],
      },
    };
    expect(collectReplayNames(tree)).toEqual([]);
  });

  test('skips replay blocks the student disabled (Blockly-12 disabledReasons shape)', () => {
    // Blockly 12.5's serialization.workspaces.save() emits a disabled block as
    // `{ disabledReasons: ['manual'] }` with NO `enabled` key — the REAL shape.
    // A disabled block's name must NOT be collected (else the run 404s on a name
    // whose trajectory was deleted, even though the block never runs).
    const tree = {
      blocks: {
        blocks: [
          { type: REPLAY_BLOCK_TYPE, fields: { NAME: 'Aktiv' } },
          { type: REPLAY_BLOCK_TYPE, disabledReasons: ['manual'], fields: { NAME: 'Deaktiviert' } },
        ],
      },
    };
    const names = collectReplayNames(tree);
    expect(names).toEqual(['Aktiv']);
    expect(names).not.toContain('Deaktiviert');
  });

  test('skips replay blocks disabled via the legacy `enabled: false` shape', () => {
    // Back-compat: older serializations wrote `enabled: false` — still honoured.
    const tree = {
      blocks: {
        blocks: [
          { type: REPLAY_BLOCK_TYPE, fields: { NAME: 'Aktiv' } },
          { type: REPLAY_BLOCK_TYPE, enabled: false, fields: { NAME: 'Deaktiviert' } },
        ],
      },
    };
    const names = collectReplayNames(tree);
    expect(names).toEqual(['Aktiv']);
    expect(names).not.toContain('Deaktiviert');
  });

  test('a disabled replay block still recurses into its enabled next chain', () => {
    // Real Blockly-12 shape: the disabled parent is skipped but its enabled
    // next-chain child is still collected.
    const tree = {
      blocks: {
        blocks: [
          {
            type: REPLAY_BLOCK_TYPE,
            disabledReasons: ['manual'],
            fields: { NAME: 'Aus' },
            next: { block: { type: REPLAY_BLOCK_TYPE, fields: { NAME: 'An' } } },
          },
        ],
      },
    };
    const names = collectReplayNames(tree);
    expect(names).toEqual(['An']);
    expect(names).not.toContain('Aus');
  });
});
