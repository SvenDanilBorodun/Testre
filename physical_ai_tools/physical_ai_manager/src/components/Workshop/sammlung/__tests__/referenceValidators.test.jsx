/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Keyed „missing name" warnings on the real, rendered Blockly canvas.

import { describe, it, expect, beforeAll, beforeEach, afterEach } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { createSammlungProvider } from '../provider';
import { getDestinationStore } from '../destinationStore';
import {
  ASSET_MISSING_WARNING_ID,
  attachAssetReferenceValidators,
  refreshAssetReferenceWarnings,
} from '../referenceValidators';

// Blockly dispatches events via requestAnimationFrame(() => setTimeout(fireNow, 0)).
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
// Blockly's event queue first, then the validator's own setTimeout(0).
const settle = async () => {
  await flushEvents();
  await new Promise((resolve) => { setTimeout(resolve, 0); });
};

const MISSING_RECORDING = 'Eine Aufnahme „Tanz" gibt es in diesem Workflow nicht. Nimm sie auf oder wähle eine vorhandene.';
const MISSING_DESTINATION = '„Ablage" ist nicht in deiner Sammlung — lege das Ziel neu an oder wähle ein vorhandenes.';

let host;
let ws;
let provider;
let dispose;

beforeAll(() => {
  Blockly.setLocale(De);
  registerTrajectoryBlocks();
  registerDestinationBlocks();
});

beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  ws = Blockly.inject(host, {});
  provider = createSammlungProvider({ trajectories: { status: 'ready', items: [] } });
  dispose = attachAssetReferenceValidators(ws, { current: provider });
});

afterEach(() => {
  dispose();
  ws.dispose();
  host.remove();
});

function add(type, name) {
  const block = ws.newBlock(type);
  block.setFieldValue(name, 'NAME');
  block.initSvg();
  block.render();
  return block;
}

function warningOf(block) {
  const icon = block.getIcon(Blockly.icons.WarningIcon.TYPE);
  return icon ? icon.getText() : '';
}

describe('Sammlung reference warnings', () => {
  it('warns a replay block whose recording is not in the ready list, and clears it', async () => {
    const block = add('edubotics_replay_trajectory', 'Tanz');
    await settle();
    expect(warningOf(block)).toBe(MISSING_RECORDING);
    provider.setSnapshot({ trajectories: { status: 'ready', items: [{ id: 't1', name: 'Tanz' }] } });
    await settle();
    expect(warningOf(block)).toBe('');
  });

  it('warns no replay block while the list is loading (fail open)', async () => {
    provider.setSnapshot({ trajectories: { status: 'loading', items: [] } });
    const block = add('edubotics_replay_trajectory', 'Tanz');
    await settle();
    expect(warningOf(block)).toBe('');
    provider.setSnapshot({ trajectories: { status: 'ready' } });
    await settle();
    expect(warningOf(block)).toBe(MISSING_RECORDING);
    provider.setSnapshot({ trajectories: { status: 'error' } });
    await settle();
    expect(warningOf(block)).toBe('');
  });

  it('a store add clears a reference warning', async () => {
    const block = add('edubotics_destination_ref', 'Ablage');
    await settle();
    expect(warningOf(block)).toBe(MISSING_DESTINATION);
    getDestinationStore(ws).add({ name: 'Ablage', kind: 'pin', x: 0.1, y: 0.1, z: 0.01, source: 'camera' });
    await settle();
    expect(warningOf(block)).toBe('');
  });

  it('an enabled „Ziel setzen" statement with the name clears it; a disabled one does not', async () => {
    const ref = add('edubotics_destination_ref', 'Ablage');
    const pin = add('edubotics_destination_pin', 'Ablage');
    await settle();
    expect(warningOf(ref)).toBe('');
    pin.setDisabledReason(true, 'test');
    await settle();
    expect(warningOf(ref)).toBe(MISSING_DESTINATION);
  });

  it('never warns a disabled block', async () => {
    const replay = add('edubotics_replay_trajectory', 'Tanz');
    const ref = add('edubotics_destination_ref', 'Ablage');
    replay.setDisabledReason(true, 'test');
    ref.setDisabledReason(true, 'test');
    await settle();
    expect(warningOf(replay)).toBe('');
    expect(warningOf(ref)).toBe('');
  });

  it('an unkeyed clear removes the icon; a forced refresh restores it', async () => {
    const block = add('edubotics_replay_trajectory', 'Tanz');
    await settle();
    expect(warningOf(block)).toBe(MISSING_RECORDING);
    block.setWarningText(null);
    expect(warningOf(block)).toBe('');
    refreshAssetReferenceWarnings(ws);
    await settle();
    expect(warningOf(block)).toBe('');
    refreshAssetReferenceWarnings(ws, { force: true });
    await settle();
    expect(warningOf(block)).toBe(MISSING_RECORDING);
  });

  it('leaves other warnings alone', async () => {
    const other = add('edubotics_destination_pin', 'A');
    other.setWarningText('Ziel ist nicht erreichbar.');
    const block = add('edubotics_replay_trajectory', 'Tanz');
    block.setWarningText('Anderer Hinweis', 'another_key');
    await settle();
    expect(warningOf(block)).toContain(MISSING_RECORDING);
    provider.setSnapshot({ trajectories: { status: 'ready', items: [{ id: 't', name: 'Tanz' }] } });
    await settle();
    expect(warningOf(block)).toBe('Anderer Hinweis');
    expect(warningOf(other)).toBe('Ziel ist nicht erreichbar.');
  });

  it('uses its own key', () => {
    expect(ASSET_MISSING_WARNING_ID).toBe('edubotics_asset_missing');
  });

  it('dispose removes every listener', async () => {
    dispose();
    const block = add('edubotics_replay_trajectory', 'Tanz');
    provider.setSnapshot({ trajectories: { status: 'ready', items: [] } });
    getDestinationStore(ws).add({ name: 'X', kind: 'pin', x: 0, y: 0, z: 0, source: 'camera' });
    refreshAssetReferenceWarnings(ws, { force: true });
    await settle();
    expect(warningOf(block)).toBe('');
  });
});
