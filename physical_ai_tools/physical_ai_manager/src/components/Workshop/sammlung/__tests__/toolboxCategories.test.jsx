/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The four Sammlung toolbox groups, on a real BlocklyWorkspace mount: the
// toolbox shape, each callback's flyout JSON (cards and Plan B), tutorial
// restriction, the teacher/empty provider, and the refresh rules.

import React from 'react';
import { render, waitFor } from '@testing-library/react';
import * as Blockly from 'blockly/core';
import BlocklyWorkspace from '../../BlocklyWorkspace';
import {
  SAMMLUNG_BASE_BLOCKS,
  SAMMLUNG_CATEGORY_KEYS,
  SAMMLUNG_TOOLBOX_IDS,
} from '../../blocks/toolbox';
import { DE } from '../../blocks/messages_de';
import { createSammlungProvider } from '../provider';
import { getDestinationStore } from '../destinationStore';
import { __setFlyoutModeForTests, refreshIfOpen } from '../toolboxCategories';

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

// Same offset stubs as BlocklyWorkspace.test.jsx: jsdom lays nothing out.
const realOffset = {
  width: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth'),
  height: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight'),
};
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true,
    get() { return this.classList && this.classList.contains('injectionDiv') ? 800 : 0; },
  });
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get() { return this.classList && this.classList.contains('injectionDiv') ? 600 : 0; },
  });
  if (!SVGElement.prototype.getBBox) {
    SVGElement.prototype.getBBox = () => ({ x: 0, y: 0, width: 40, height: 16 });
  }
  if (!SVGElement.prototype.getComputedTextLength) {
    SVGElement.prototype.getComputedTextLength = () => 40;
  }
});
afterEach(() => {
  __setFlyoutModeForTests('cards');
  vi.restoreAllMocks();
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', realOffset.width);
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', realOffset.height);
});

async function mountEditor(props = {}) {
  const onWorkspaceReady = vi.fn();
  const utils = render(<BlocklyWorkspace onWorkspaceReady={onWorkspaceReady} {...props} />);
  await waitFor(() => expect(onWorkspaceReady).toHaveBeenCalled());
  return { ...utils, ws: onWorkspaceReady.mock.calls[0][0] };
}

const TANZ_NEW = {
  id: 'traj-2', name: 'Tanz', point_count: 105, duration_s: 4.2, fps: 25,
  robot_profile: 'omx_f', created_at: '2026-09-13T12:00:00Z',
};
const TANZ_OLD = { ...TANZ_NEW, id: 'traj-1', created_at: '2026-09-12T12:00:00Z' };

const FULL_CAPS = {
  hardware: true, simMode: false, teach: true, drawer: true, preview: true,
  previewVariables: false, pinCamera: true, pinSim: false,
};

function fixtureProvider(overrides = {}) {
  return createSammlungProvider({
    capabilities: FULL_CAPS,
    robotType: 'omx_f',
    trajectories: { status: 'ready', items: [TANZ_NEW, TANZ_OLD] },
    ...overrides,
  });
}

// One recording group (2 versions), one missing name, one store pin, one pose,
// one program pin, one variable.
function seed(ws) {
  const block = (type, name) => {
    const b = ws.newBlock(type);
    if (name !== undefined) b.setFieldValue(name, 'NAME');
    b.initSvg();
    b.render();
    return b;
  };
  block('edubotics_replay_trajectory', 'Tanz');
  block('edubotics_replay_trajectory', 'Fehlt');
  const programPin = block('edubotics_destination_pin', 'P1');
  ws.createVariable('Zahl', '', 'v1');
  const store = getDestinationStore(ws);
  const pin = store.add({ name: 'Ablage', kind: 'pin', x: 0.182, y: -0.064, z: 0.012, source: 'camera' }).entry;
  const pose = store.add({
    name: 'Oben', kind: 'pose', x: 0.1, y: 0, z: 0.15, source: 'capture', robot_type: 'omx_f',
  }).entry;
  return { programPin, pin, pose };
}

const flyoutOf = (ws, key) => ws.getToolboxCategoryCallback(key)(ws);
const label = (text) => ({ kind: 'label', text });
const button = (text, callbackkey) => ({ kind: 'button', text, callbackkey });
const card = (fields) => ({ kind: 'edubotics_asset_card', gap: 4, ...fields });

describe('Sammlung toolbox groups', () => {
  it('sit below a separator with stable ids, in order', async () => {
    const { ws, unmount } = await mountEditor({ sammlungProvider: fixtureProvider() });
    const items = ws.getToolbox().getToolboxItems();
    const sepIndex = items.findIndex((it) => it instanceof Blockly.ToolboxSeparator);
    expect(sepIndex).toBeGreaterThan(0);
    expect(items.slice(sepIndex + 1).map((it) => it.getId())).toEqual([
      SAMMLUNG_TOOLBOX_IDS.VARIABLEN,
      SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN,
      SAMMLUNG_TOOLBOX_IDS.ZIELE,
      SAMMLUNG_TOOLBOX_IDS.POSITIONEN,
    ]);
    unmount();
  });

  it('build the fixture flyouts as cards', async () => {
    const provider = fixtureProvider();
    const { ws, unmount } = await mountEditor({ sammlungProvider: provider });
    const { programPin, pin, pose } = seed(ws);

    expect(flyoutOf(ws, SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN)).toEqual([
      label('2 von 16'),
      button(DE.FLY_TEACH_RECORDING, 'EDU_SAMMLUNG_TEACH_RECORDING'),
      button(DE.FLY_MANAGE, 'EDU_SAMMLUNG_MANAGE_AUFNAHMEN'),
      card({
        assetKind: 'recording', assetId: 'traj-2', assetName: 'Tanz', title: 'Tanz',
        meta: '4,2 s · 105 Punkte',
        chips: [{ text: '1× benutzt', level: 'ok' }, { text: '2 Versionen', level: 'warn' }],
        canPreview: true, colour: '#3b82f6',
      }),
      { kind: 'block', type: 'edubotics_replay_trajectory', fields: { NAME: 'Tanz' } },
      label(DE.FLY_SECTION_MISSING),
      card({
        assetKind: 'missingRecording', assetId: '', assetName: 'Fehlt', title: 'Fehlt', meta: '',
        chips: [{ text: 'fehlt', level: 'bad' }, { text: '1 Block sucht diese Aufnahme', level: 'bad' }],
        canPreview: false, colour: '#3b82f6',
      }),
    ]);

    expect(flyoutOf(ws, SAMMLUNG_CATEGORY_KEYS.ZIELE)).toEqual([
      label('1 Ziel'),
      button(DE.FLY_TEACH_ZIEL, 'EDU_SAMMLUNG_TEACH_ZIEL'),
      button(DE.FLY_PIN_CAMERA, 'EDU_SAMMLUNG_PIN_CAMERA'),
      button(DE.FLY_MANAGE, 'EDU_SAMMLUNG_MANAGE_ZIELE'),
      { kind: 'block', type: 'edubotics_destination_pin' },
      { kind: 'block', type: 'edubotics_destination_ref' },
      { kind: 'block', type: 'edubotics_destination_current' },
      label(DE.FLY_SECTION_YOURS),
      card({
        assetKind: 'pin', assetId: pin.id, assetName: 'Ablage', title: 'Ablage',
        meta: 'x 182 · y −64 mm · Kamera', chips: [{ text: 'nicht benutzt', level: 'ok' }],
        canPreview: true, colour: '#f59e0b',
      }),
      { kind: 'block', type: 'edubotics_destination_ref', fields: { NAME: 'Ablage' } },
      label(DE.FLY_SECTION_PROGRAM),
      card({
        assetKind: 'programPin', assetId: programPin.id, assetName: 'P1', title: 'P1',
        meta: 'im Programm · x — · y — mm', chips: [], canPreview: false, colour: '#f59e0b',
      }),
    ]);

    expect(flyoutOf(ws, SAMMLUNG_CATEGORY_KEYS.POSITIONEN)).toEqual([
      label('1 Position'),
      button(DE.FLY_TEACH_POSE, 'EDU_SAMMLUNG_TEACH_POSE'),
      button(DE.FLY_MANAGE, 'EDU_SAMMLUNG_MANAGE_POSITIONEN'),
      card({
        assetKind: 'pose', assetId: pose.id, assetName: 'Oben', title: 'Oben',
        meta: 'z 150 mm · OMX', chips: [{ text: 'nicht benutzt', level: 'ok' }],
        canPreview: true, colour: '#14b8a6',
      }),
      { kind: 'block', type: 'edubotics_destination_ref', fields: { NAME: 'Oben' } },
    ]);

    const core = Blockly.Variables.flyoutCategory(ws, false);
    expect(core.map((it) => it.type || it.callbackkey))
      .toEqual(['CREATE_VARIABLE', 'variables_set', 'math_change', 'variables_get']);
    expect(flyoutOf(ws, SAMMLUNG_CATEGORY_KEYS.VARIABLEN)).toEqual([
      label('1 Variable'),
      core[0],
      button(DE.FLY_MANAGE, 'EDU_SAMMLUNG_MANAGE_VARIABLEN'),
      core[1],
      core[2],
      card({
        assetKind: 'variable', assetId: 'v1', assetName: 'Zahl', title: 'Zahl',
        meta: 'noch kein Wert', chips: [{ text: 'nicht benutzt', level: 'ok' }],
        canPreview: false, colour: '#a78bfa',
      }),
      core[3],
    ]);
    expect(ws.getButtonCallback('CREATE_VARIABLE')).toEqual(expect.any(Function));
    unmount();
  });

  it('build the same view models as Plan B labels and buttons', async () => {
    __setFlyoutModeForTests('plan-b');
    const { ws, unmount } = await mountEditor({ sammlungProvider: fixtureProvider() });
    seed(ws);
    const asset = {
      'web-class': 'eduSammlungCardButton',
      'edu-asset-kind': 'recording', 'edu-asset-id': 'traj-2', 'edu-asset-name': 'Tanz',
    };
    expect(flyoutOf(ws, SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN).slice(3)).toEqual([
      { kind: 'label', text: 'Tanz · 4,2 s · 105 Punkte · 1× benutzt · 2 Versionen', 'web-class': 'eduSammlungCardLabel' },
      { kind: 'button', text: '▶ Im Simulator ansehen', callbackkey: 'EDU_SAMMLUNG_CARD_PREVIEW', ...asset },
      { kind: 'button', text: '⋯ Verwalten', callbackkey: 'EDU_SAMMLUNG_CARD_MANAGE', ...asset },
      { kind: 'block', type: 'edubotics_replay_trajectory', fields: { NAME: 'Tanz' } },
      label(DE.FLY_SECTION_MISSING),
      { kind: 'label', text: 'Fehlt · fehlt · 1 Block sucht diese Aufnahme', 'web-class': 'eduSammlungCardLabel' },
      {
        kind: 'button', text: '⋯ Verwalten', callbackkey: 'EDU_SAMMLUNG_CARD_MANAGE',
        'web-class': 'eduSammlungCardButton',
        'edu-asset-kind': 'missingRecording', 'edu-asset-id': '', 'edu-asset-name': 'Fehlt',
      },
    ]);
    // The Plan B buttons dispatch through the provider like the cards do.
    const dispatched = [];
    const provider = fixtureProvider();
    provider.setActionHandler((a) => dispatched.push(a));
    unmount();
    const second = await mountEditor({ sammlungProvider: provider });
    second.ws.getButtonCallback('EDU_SAMMLUNG_CARD_PREVIEW')({ info: asset });
    second.ws.getButtonCallback('EDU_SAMMLUNG_CARD_MANAGE')({ info: asset });
    expect(dispatched).toEqual([
      { type: 'preview', asset: { kind: 'recording', id: 'traj-2', name: 'Tanz', robotProfile: 'omx_f' } },
      { type: 'manage', tab: 'aufnahmen', focusId: 'Tanz' },
    ]);
    second.unmount();
  });
});

describe('Sammlung groups — restriction, counts, teacher page', () => {
  it('a tutorial restriction keeps ref blocks, cards and labels, drops pin/current blocks', async () => {
    const { ws, unmount } = await mountEditor({
      sammlungProvider: fixtureProvider({
        restrictedBlocks: ['edubotics_destination_ref', 'edubotics_move_to', 'edubotics_home'],
      }),
    });
    seed(ws);
    const ziele = flyoutOf(ws, SAMMLUNG_CATEGORY_KEYS.ZIELE);
    const blockTypes = ziele.filter((it) => it.kind === 'block').map((it) => it.type);
    expect(blockTypes).toEqual(['edubotics_destination_ref', 'edubotics_destination_ref']);
    expect(ziele.filter((it) => it.kind === 'edubotics_asset_card')).toHaveLength(2);
    expect(ziele.filter((it) => it.kind === 'label').map((it) => it.text))
      .toEqual(['1 Ziel', DE.FLY_SECTION_YOURS, DE.FLY_SECTION_PROGRAM]);
    // No replay block survives the restriction in „Aufnahmen" — not even the generic one.
    expect(flyoutOf(ws, SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN).some((it) => it.kind === 'block')).toBe(false);
    unmount();
  });

  it('an EMPTY restriction list is unrestricted', async () => {
    const provider = fixtureProvider({ restrictedBlocks: null });
    const { ws, unmount } = await mountEditor({ sammlungProvider: provider });
    seed(ws);
    const keys = Object.values(SAMMLUNG_CATEGORY_KEYS);
    const unrestricted = keys.map((k) => flyoutOf(ws, k));
    provider.setSnapshot({ restrictedBlocks: [] });
    expect(keys.map((k) => flyoutOf(ws, k))).toEqual(unrestricted);
    unmount();
  });

  it('count labels: singular, plural, and none at zero; the recording status line comes first', async () => {
    const provider = fixtureProvider({ trajectories: { status: 'loading', items: [] } });
    const { ws, unmount } = await mountEditor({ sammlungProvider: provider });
    const first = (key) => flyoutOf(ws, key)[0];
    expect(first(SAMMLUNG_CATEGORY_KEYS.ZIELE).kind).not.toBe('label');
    expect(first(SAMMLUNG_CATEGORY_KEYS.POSITIONEN).kind).not.toBe('label');
    expect(first(SAMMLUNG_CATEGORY_KEYS.VARIABLEN).kind).not.toBe('label');
    expect(first(SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN)).toEqual(label(DE.FLY_RECORDINGS_LOADING));
    const store = getDestinationStore(ws);
    store.add({ name: 'A', kind: 'pin', x: 0, y: 0, z: 0, source: 'sim' });
    expect(first(SAMMLUNG_CATEGORY_KEYS.ZIELE)).toEqual(label('1 Ziel'));
    store.add({ name: 'B', kind: 'pin', x: 0, y: 0, z: 0, source: 'sim' });
    expect(first(SAMMLUNG_CATEGORY_KEYS.ZIELE)).toEqual(label('2 Ziele'));
    ws.createVariable('eins');
    ws.createVariable('zwei');
    expect(first(SAMMLUNG_CATEGORY_KEYS.VARIABLEN)).toEqual(label('2 Variablen'));
    provider.setSnapshot({ trajectories: { status: 'error' } });
    expect(first(SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN)).toEqual(label(DE.FLY_RECORDINGS_ERROR));
    provider.setSnapshot({ trajectories: { status: 'ready', items: [] } });
    expect(first(SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN)).toEqual(label(DE.FLY_RECORDINGS_EMPTY));
    unmount();
  });

  it('the teacher page (no provider) offers no teach, camera or manage buttons', async () => {
    const { ws, unmount } = await mountEditor();
    seed(ws);
    const all = Object.values(SAMMLUNG_CATEGORY_KEYS).flatMap((k) => flyoutOf(ws, k));
    expect(all.filter((it) => it.kind === 'button').map((it) => it.callbackkey))
      .toEqual(['CREATE_VARIABLE']);
    const aufnahmen = flyoutOf(ws, SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN);
    expect(aufnahmen[0]).toEqual(label(DE.FLY_RECORDINGS_TEACHER));
    expect(aufnahmen[aufnahmen.length - 1]).toEqual({ kind: 'block', type: 'edubotics_replay_trajectory' });
    // Every Sammlung category opens in the real flyout without an error.
    const toolbox = ws.getToolbox();
    for (const id of Object.values(SAMMLUNG_TOOLBOX_IDS)) {
      expect(() => toolbox.setSelectedItem(toolbox.getToolboxItemById(id))).not.toThrow();
      expect(ws.getFlyout().getContents().length).toBeGreaterThan(0);
    }
    unmount();
  });

  it('every SAMMLUNG_BASE_BLOCKS type is emitted by a Sammlung flyout', async () => {
    const { ws, unmount } = await mountEditor({ sammlungProvider: fixtureProvider() });
    const emitted = new Set(Object.values(SAMMLUNG_CATEGORY_KEYS)
      .flatMap((k) => flyoutOf(ws, k))
      .filter((it) => it.kind === 'block')
      .map((it) => it.type));
    for (const { type } of SAMMLUNG_BASE_BLOCKS) expect(emitted.has(type)).toBe(true);
    unmount();
  });

  it('the four callbacks exist on a read-only workspace too', async () => {
    const { ws, unmount } = await mountEditor({ readOnly: true });
    for (const key of Object.values(SAMMLUNG_CATEGORY_KEYS)) {
      expect(ws.getToolboxCategoryCallback(key)).toEqual(expect.any(Function));
    }
    unmount();
  });
});

describe('refreshIfOpen', () => {
  it('refreshes only while a Sammlung group is selected, and defers during a drag', async () => {
    const provider = fixtureProvider();
    const { ws, unmount } = await mountEditor({ sammlungProvider: provider });
    const toolbox = ws.getToolbox();
    const refresh = vi.spyOn(ws, 'refreshToolboxSelection');

    expect(refreshIfOpen(ws)).toBe(false);
    toolbox.setSelectedItem(toolbox.getToolboxItems()[1]); // „Bewegung"
    expect(refreshIfOpen(ws)).toBe(false);
    expect(refresh).not.toHaveBeenCalled();

    toolbox.setSelectedItem(toolbox.getToolboxItemById(SAMMLUNG_TOOLBOX_IDS.ZIELE));
    expect(refreshIfOpen(ws)).toBe(true);
    expect(refresh).toHaveBeenCalledTimes(1);
    // Provider changes refresh the open group, coalesced into one rebuild.
    provider.setSnapshot({ robotType: 'edu6_studio' });
    provider.setSnapshot({ robotType: 'omx_f' });
    expect(refresh).toHaveBeenCalledTimes(1);
    await new Promise((resolve) => { setTimeout(resolve, 200); });
    expect(refresh).toHaveBeenCalledTimes(2);

    const dragging = vi.spyOn(ws, 'isDragging').mockReturnValue(true);
    expect(refreshIfOpen(ws)).toBe(false);
    expect(refresh).toHaveBeenCalledTimes(2);
    dragging.mockReturnValue(false);
    const block = ws.newBlock('edubotics_home');
    const BlockDrag = Blockly.Events.get(Blockly.Events.BLOCK_DRAG);
    Blockly.Events.fire(new BlockDrag(block, false, []));
    await flushEvents();
    expect(refresh).toHaveBeenCalledTimes(3);
    unmount();
  });
});
