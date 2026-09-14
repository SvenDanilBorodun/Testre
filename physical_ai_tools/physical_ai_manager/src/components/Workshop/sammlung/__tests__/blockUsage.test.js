/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { describe, it, expect, beforeAll, afterEach } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { collectBlockUsage } from '../blockUsage';

let ws;

beforeAll(() => {
  Blockly.setLocale(De);
  registerTrajectoryBlocks();
  registerDestinationBlocks();
});

afterEach(() => {
  if (ws) ws.dispose();
  ws = null;
});

const replay = (name) => ({ type: 'edubotics_replay_trajectory', fields: { NAME: name } });
const pinBlock = (name, x = '—', y = '—', z = '—', id) => ({
  type: 'edubotics_destination_pin', ...(id ? { id } : {}), fields: { NAME: name, X: x, Y: y, Z: z },
});

function load(blocks, variables) {
  ws = new Blockly.Workspace();
  Blockly.serialization.workspaces.load({
    blocks: { languageVersion: 0, blocks },
    ...(variables ? { variables } : {}),
  }, ws);
  return ws;
}

describe('collectBlockUsage', () => {
  it('counts replay and reference blocks per name, enabled and disabled apart', () => {
    load([
      { ...replay('Tanz'), x: 0, y: 0 },
      { ...replay('Tanz'), x: 0, y: 100 },
      { ...replay('  Winken '), x: 0, y: 200 },
      { type: 'edubotics_destination_ref', id: 'r1', fields: { NAME: 'Ablage' }, x: 0, y: 300 },
    ]);
    const second = ws.getTopBlocks(false)[1];
    second.setDisabledReason(true, 'test');
    const usage = collectBlockUsage(ws);
    expect(usage.replay.get('Tanz')).toMatchObject({ enabled: 1, disabled: 1 });
    expect(usage.replay.get('Tanz').blockIds).toHaveLength(2);
    expect(usage.replay.get('Winken')).toMatchObject({ enabled: 1, disabled: 0 });
    expect(usage.refs.get('Ablage')).toEqual({ enabled: 1, disabled: 0, blockIds: ['r1'] });
  });

  it('a block under a disabled parent counts as disabled', () => {
    load([{
      type: 'controls_repeat_ext',
      inputs: { DO: { block: replay('Tanz') } },
    }]);
    ws.getTopBlocks(false)[0].setDisabledReason(true, 'test');
    const usage = collectBlockUsage(ws);
    expect(usage.replay.get('Tanz')).toMatchObject({ enabled: 0, disabled: 1 });
  });

  it('the first pin statement in creation order wins, with X/Y/Z parsed and „—" as NaN', () => {
    load([
      { ...pinBlock('A', '0.182', '-0.064', '0.012', 'p1'), x: 0, y: 0 },
      { ...pinBlock('A', '0.5', '0.5', '0.5', 'p2'), x: 0, y: 100 },
      { ...pinBlock('B', undefined, undefined, undefined, 'p3'), x: 0, y: 200 },
      { type: 'edubotics_destination_current', id: 'c1', fields: { NAME: 'Hier' }, x: 0, y: 300 },
    ]);
    const usage = collectBlockUsage(ws);
    expect(usage.pinStatements.get('A')).toEqual({
      blockId: 'p1', enabled: true, x: 0.182, y: -0.064, z: 0.012,
    });
    const b = usage.pinStatements.get('B');
    expect(b.blockId).toBe('p3');
    expect([b.x, b.y, b.z].every(Number.isNaN)).toBe(true);
    expect(usage.currentStatements.get('Hier')).toEqual({ blockId: 'c1', enabled: true });
  });

  it('counts variable uses by id', () => {
    load([
      { type: 'variables_set', fields: { VAR: { id: 'v1' } }, x: 0, y: 0 },
      { type: 'variables_get', fields: { VAR: { id: 'v1' } }, x: 0, y: 100 },
    ], [{ name: 'Zahl', id: 'v1' }, { name: 'Leer', id: 'v2' }]);
    const usage = collectBlockUsage(ws);
    expect(usage.variableUses.get('v1')).toBe(2);
    expect(usage.variableUses.get('v2')).toBe(0);
  });

  it('ignores a flyout workspace and tolerates no workspace', () => {
    load([replay('Tanz')]);
    expect(collectBlockUsage(ws).replay.size).toBe(1);
    const flyoutLike = { isFlyout: true, getAllBlocks: (o) => ws.getAllBlocks(o) };
    expect(collectBlockUsage(flyoutLike).replay.size).toBe(0);
    expect(collectBlockUsage(null).replay.size).toBe(0);
  });
});
