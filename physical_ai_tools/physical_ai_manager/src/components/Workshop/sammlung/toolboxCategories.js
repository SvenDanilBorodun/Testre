/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * The four Sammlung toolbox groups „Variablen / Aufnahmen / Ziele /
 * Positionen": dynamic category callbacks that build each flyout from the
 * workspace (block usage, variables, the destination store) and the page's
 * provider snapshot (recordings, capabilities, tutorial restriction).
 *
 * Registered on EVERY workspace — the teacher page and read-only previews too:
 * a toolbox that names a custom key with no registered callback throws when
 * the category is opened.
 *
 * `restrictedBlocks` is applied HERE: dynamic callbacks never pass through
 * `toolbox.js::filterContents`, so each block item is checked against the
 * snapshot's list with exactly that function's rule — an EMPTY list is
 * unrestricted. Cards, labels and buttons are never restricted.
 */

import * as Blockly from 'blockly/core';
import { DE, formatDe } from '../blocks/messages_de';
import { SAMMLUNG_CATEGORY_KEYS } from '../blocks/toolbox';
import { REPLAY_BLOCK_TYPE } from '../blocks/trajectories';
import { getDestinationStore } from './destinationStore';
import { collectBlockUsage } from './blockUsage';
import { buildAssetIndex } from './assetIndex';
import { DEFAULT_SAMMLUNG_SNAPSHOT } from './provider';
import {
  ASSET_CARD_FLYOUT_TYPE,
  manageActionFor,
  previewAssetFor,
} from './AssetCardInflater';

// 'cards' (custom flyout items) or 'plan-b' (labels + buttons). The spike
// outcome is recorded in docs/KNOWN-ISSUES.md („Sammlung flyout: cards vs Plan B").
export let SAMMLUNG_FLYOUT_MODE = 'cards';

/** Test seam: render the same view models as cards or as Plan B items. */
export function __setFlyoutModeForTests(mode) {
  SAMMLUNG_FLYOUT_MODE = mode === 'plan-b' ? 'plan-b' : 'cards';
}

export const SAMMLUNG_BUTTON_KEYS = Object.freeze({
  TEACH_RECORDING: 'EDU_SAMMLUNG_TEACH_RECORDING',
  TEACH_POSE: 'EDU_SAMMLUNG_TEACH_POSE',
  TEACH_ZIEL: 'EDU_SAMMLUNG_TEACH_ZIEL',
  PIN_CAMERA: 'EDU_SAMMLUNG_PIN_CAMERA',
  PIN_SIM: 'EDU_SAMMLUNG_PIN_SIM',
  MANAGE_VARIABLEN: 'EDU_SAMMLUNG_MANAGE_VARIABLEN',
  MANAGE_AUFNAHMEN: 'EDU_SAMMLUNG_MANAGE_AUFNAHMEN',
  MANAGE_ZIELE: 'EDU_SAMMLUNG_MANAGE_ZIELE',
  MANAGE_POSITIONEN: 'EDU_SAMMLUNG_MANAGE_POSITIONEN',
  CARD_PREVIEW: 'EDU_SAMMLUNG_CARD_PREVIEW',
  CARD_MANAGE: 'EDU_SAMMLUNG_CARD_MANAGE',
});

const REFRESH_DEBOUNCE_MS = 150;
// Every SAMMLUNG_TOOLBOX_IDS value starts with this (blocks/toolbox.js).
const SAMMLUNG_ID_PREFIX = 'sammlung-';

// Main workspace → the BlocklyWorkspace's provider ref (read at event time, so
// a card never holds a provider of its own).
const providerRefs = new WeakMap();
// Main workspace → a refresh was requested while a drag was in progress.
const dirtyWorkspaces = new WeakSet();

export function getProviderForWorkspace(workspace) {
  if (!workspace) return null;
  const ref = providerRefs.get(workspace);
  return (ref && ref.current) || null;
}

function snapshotOf(workspace) {
  const provider = getProviderForWorkspace(workspace);
  let snap = null;
  try {
    snap = provider ? provider.getSnapshot() : null;
  } catch (_) {
    snap = null;
  }
  return snap && typeof snap === 'object' ? snap : DEFAULT_SAMMLUNG_SNAPSHOT;
}

function dispatch(workspace, action) {
  const provider = getProviderForWorkspace(workspace);
  if (!provider) return;
  try {
    provider.dispatchAction(action);
  } catch (err) {
    console.error('Sammlung action failed:', err);
  }
}

const label = (text) => ({ kind: 'label', text });
const button = (text, callbackkey) => ({ kind: 'button', text, callbackkey });

function restrictionOf(snapshot) {
  const list = snapshot.restrictedBlocks;
  return Array.isArray(list) && list.length > 0 ? new Set(list) : null;
}

// Card flyout item (§ the card contract) or its Plan B labels + buttons.
// `c` (the snapshot capabilities) is passed only by the groups whose ▶ is a SIM
// RUN (recordings, Ziele, Positionen): while `previewPending` that ▶ is drawn
// disabled. A variable's ▶ only highlights a marker and never waits. The key is
// added only when true, so a card that is not pending is byte-unchanged.
function cardItems(vm, c = null) {
  const previewPending = vm.canPreview === true && vm.assetKind !== 'variable'
    && !!c && c.previewPending === true;
  if (SAMMLUNG_FLYOUT_MODE === 'plan-b') {
    // `title · meta · chip · chip`; an empty meta (a missing recording) adds
    // no empty segment.
    const text = [vm.title, vm.meta, ...vm.chips.map((c) => c.text)].filter(Boolean).join(' · ');
    const assetAttrs = {
      'web-class': 'eduSammlungCardButton',
      'edu-asset-kind': vm.assetKind,
      'edu-asset-id': vm.assetId,
      'edu-asset-name': vm.assetName,
    };
    const items = [{
      kind: 'label',
      text,
      'web-class': 'eduSammlungCardLabel',
    }];
    // Plan B (a test seam, never shipped) draws no disabled state: a press while
    // the bridge is pending is refused by useSimPreview with the same title.
    if (vm.canPreview) {
      items.push({
        kind: 'button',
        text: `▶ ${DE.PREVIEW_START}`,
        callbackkey: SAMMLUNG_BUTTON_KEYS.CARD_PREVIEW,
        ...assetAttrs,
      });
    }
    items.push({
      kind: 'button',
      text: `⋯ ${DE.CARD_MANAGE}`,
      callbackkey: SAMMLUNG_BUTTON_KEYS.CARD_MANAGE,
      ...assetAttrs,
    });
    return items;
  }
  return [{
    kind: ASSET_CARD_FLYOUT_TYPE,
    assetKind: vm.assetKind,
    assetId: vm.assetId,
    assetName: vm.assetName,
    title: vm.title,
    meta: vm.meta,
    chips: vm.chips.map((c) => ({ text: c.text, level: c.level })),
    canPreview: vm.canPreview,
    ...(previewPending ? { previewPending: true } : {}),
    colour: vm.colour,
    gap: 4,
  }];
}

function countLabel(n, oneKey, manyKey) {
  if (!(n > 0)) return [];
  return [label(n === 1 ? DE[oneKey] : formatDe(DE[manyKey], n))];
}

function workspaceVariables(workspace) {
  try {
    return workspace.getVariableMap().getAllVariables().map((v) => ({
      id: v.getId(),
      name: v.getName(),
    }));
  } catch (_) {
    return [];
  }
}

/** The asset index of `workspace` for the current snapshot. */
export function buildWorkspaceAssetIndex(workspace, snapshot = snapshotOf(workspace)) {
  return buildAssetIndex({
    usage: collectBlockUsage(workspace),
    variables: workspaceVariables(workspace),
    destinations: getDestinationStore(workspace).getEntries(),
    trajectories: snapshot.trajectories,
    robotType: snapshot.robotType,
    lastPreviewResult: snapshot.lastPreviewResult,
    variableValues: snapshot.variableValues,
    capabilities: snapshot.capabilities,
    now: Date.now(),
  });
}

function caps(snapshot) {
  return snapshot.capabilities && typeof snapshot.capabilities === 'object'
    ? snapshot.capabilities : DEFAULT_SAMMLUNG_SNAPSHOT.capabilities;
}

function canTeach(c) {
  return !!(c.hardware && c.teach && !c.simMode);
}

export function variablenFlyout(workspace) {
  const snapshot = snapshotOf(workspace);
  const c = caps(snapshot);
  const restricted = restrictionOf(snapshot);
  const allowed = (item) => item.kind !== 'block' || !restricted || restricted.has(item.type);
  const index = buildWorkspaceAssetIndex(workspace, snapshot);
  // Registers the CREATE_VARIABLE button callback as a side effect.
  const core = Blockly.Variables.flyoutCategory(workspace, false);
  const getters = core.filter((it) => it.kind === 'block' && it.type === 'variables_get');
  const items = [...countLabel(index.counts.variablen, 'FLY_COUNT_VARIABLEN_ONE', 'FLY_COUNT_VARIABLEN')];
  for (const it of core) {
    if (it.kind === 'block' && it.type === 'variables_get') continue;
    if (!allowed(it)) continue;
    items.push(it);
    if (it.kind === 'button' && it.callbackkey === 'CREATE_VARIABLE' && c.drawer) {
      items.push(button(DE.FLY_MANAGE, SAMMLUNG_BUTTON_KEYS.MANAGE_VARIABLEN));
    }
  }
  for (const vm of index.variables) {
    items.push(...cardItems(vm));
    const getter = getters.find((g) => g.fields && g.fields.VAR && g.fields.VAR.name === vm.assetName);
    if (getter && allowed(getter)) items.push(getter);
  }
  return items;
}

function recordingsStatusLabel(snapshot) {
  const status = snapshot.trajectories && snapshot.trajectories.status;
  const c = caps(snapshot);
  if (status === 'idle' || status === 'loading') return DE.FLY_RECORDINGS_LOADING;
  if (status === 'error') return DE.FLY_RECORDINGS_ERROR;
  if (status === 'ready') {
    const rows = Array.isArray(snapshot.trajectories.items) ? snapshot.trajectories.items.length : 0;
    return rows > 0 ? formatDe(DE.FLY_RECORDINGS_COUNT, rows) : DE.FLY_RECORDINGS_EMPTY;
  }
  // 'none' (no saved workflow) and anything unknown.
  return c.hardware ? DE.FLY_RECORDINGS_EMPTY : DE.FLY_RECORDINGS_TEACHER;
}

export function aufnahmenFlyout(workspace) {
  const snapshot = snapshotOf(workspace);
  const c = caps(snapshot);
  const restricted = restrictionOf(snapshot);
  const replayAllowed = !restricted || restricted.has(REPLAY_BLOCK_TYPE);
  const index = buildWorkspaceAssetIndex(workspace, snapshot);
  const items = [label(recordingsStatusLabel(snapshot))];
  if (canTeach(c)) items.push(button(DE.FLY_TEACH_RECORDING, SAMMLUNG_BUTTON_KEYS.TEACH_RECORDING));
  if (c.drawer) items.push(button(DE.FLY_MANAGE, SAMMLUNG_BUTTON_KEYS.MANAGE_AUFNAHMEN));
  let prefilled = 0;
  for (const vm of index.recordings) {
    items.push(...cardItems(vm, c));
    if (replayAllowed) {
      items.push({ kind: 'block', type: REPLAY_BLOCK_TYPE, fields: { NAME: vm.assetName } });
      prefilled += 1;
    }
  }
  if (index.missingRecordings.length > 0) {
    items.push(label(DE.FLY_SECTION_MISSING));
    for (const vm of index.missingRecordings) items.push(...cardItems(vm));
  }
  if (prefilled === 0 && replayAllowed) items.push({ kind: 'block', type: REPLAY_BLOCK_TYPE });
  return items;
}

const DESTINATION_GENERIC_BLOCKS = Object.freeze([
  'edubotics_destination_pin',
  'edubotics_destination_ref',
  'edubotics_destination_current',
]);
const REF_TYPE = 'edubotics_destination_ref';

export function zieleFlyout(workspace) {
  const snapshot = snapshotOf(workspace);
  const c = caps(snapshot);
  const restricted = restrictionOf(snapshot);
  const allowedType = (type) => !restricted || restricted.has(type);
  const index = buildWorkspaceAssetIndex(workspace, snapshot);
  const items = [...countLabel(index.counts.ziele, 'FLY_COUNT_ZIELE_ONE', 'FLY_COUNT_ZIELE')];
  if (canTeach(c)) items.push(button(DE.FLY_TEACH_ZIEL, SAMMLUNG_BUTTON_KEYS.TEACH_ZIEL));
  if (c.hardware && c.pinCamera && !c.simMode) {
    items.push(button(DE.FLY_PIN_CAMERA, SAMMLUNG_BUTTON_KEYS.PIN_CAMERA));
  }
  if (c.pinSim && c.simMode) items.push(button(DE.FLY_PIN_SIM, SAMMLUNG_BUTTON_KEYS.PIN_SIM));
  if (c.drawer) items.push(button(DE.FLY_MANAGE, SAMMLUNG_BUTTON_KEYS.MANAGE_ZIELE));
  for (const type of DESTINATION_GENERIC_BLOCKS) {
    if (allowedType(type)) items.push({ kind: 'block', type });
  }
  items.push(label(DE.FLY_SECTION_YOURS));
  if (index.pins.length === 0) items.push(label(DE.FLY_PLACES_EMPTY));
  for (const vm of index.pins) {
    items.push(...cardItems(vm, c));
    if (allowedType(REF_TYPE)) items.push({ kind: 'block', type: REF_TYPE, fields: { NAME: vm.assetName } });
  }
  if (index.programPins.length > 0) {
    items.push(label(DE.FLY_SECTION_PROGRAM));
    for (const vm of index.programPins) items.push(...cardItems(vm));
  }
  return items;
}

export function positionenFlyout(workspace) {
  const snapshot = snapshotOf(workspace);
  const c = caps(snapshot);
  const restricted = restrictionOf(snapshot);
  const refAllowed = !restricted || restricted.has(REF_TYPE);
  const index = buildWorkspaceAssetIndex(workspace, snapshot);
  const items = [...countLabel(index.counts.positionen, 'FLY_COUNT_POSITIONEN_ONE', 'FLY_COUNT_POSITIONEN')];
  if (canTeach(c)) items.push(button(DE.FLY_TEACH_POSE, SAMMLUNG_BUTTON_KEYS.TEACH_POSE));
  if (c.drawer) items.push(button(DE.FLY_MANAGE, SAMMLUNG_BUTTON_KEYS.MANAGE_POSITIONEN));
  if (index.poses.length === 0) items.push(label(DE.FLY_POSES_EMPTY));
  for (const vm of index.poses) {
    items.push(...cardItems(vm, c));
    if (refAllowed) items.push({ kind: 'block', type: REF_TYPE, fields: { NAME: vm.assetName } });
  }
  return items;
}

const CATEGORY_CALLBACKS = Object.freeze([
  [SAMMLUNG_CATEGORY_KEYS.VARIABLEN, variablenFlyout],
  [SAMMLUNG_CATEGORY_KEYS.AUFNAHMEN, aufnahmenFlyout],
  [SAMMLUNG_CATEGORY_KEYS.ZIELE, zieleFlyout],
  [SAMMLUNG_CATEGORY_KEYS.POSITIONEN, positionenFlyout],
]);

function selectedToolboxItemId(workspace) {
  try {
    const toolbox = workspace.getToolbox && workspace.getToolbox();
    const item = toolbox && toolbox.getSelectedItem && toolbox.getSelectedItem();
    const id = item && item.getId && item.getId();
    return typeof id === 'string' ? id : '';
  } catch (_) {
    return '';
  }
}

/**
 * Re-populate the open flyout when it is a Sammlung group. While a drag is in
 * progress (a block dragged OUT of that very flyout) re-populating would
 * dispose the flyout under the gesture, so the refresh is deferred until the
 * drag ends or another category is selected. A press that has not yet become a
 * drag defers too: Blockly 12's refreshToolboxSelection does NOTHING while any
 * gesture exists, so clearing the dirty flag then dropped the refresh. The
 * deferred refresh runs at the drag end, the click that ends a press, or the
 * next category selection. Returns true when it refreshed.
 */
export function refreshIfOpen(workspace) {
  if (!workspace || workspace.isFlyout) return false;
  if (!selectedToolboxItemId(workspace).startsWith(SAMMLUNG_ID_PREFIX)) return false;
  try {
    if (workspace.isDragging() || workspace.currentGesture_) {
      dirtyWorkspaces.add(workspace);
      return false;
    }
    dirtyWorkspaces.delete(workspace);
    workspace.refreshToolboxSelection();
    return true;
  } catch (err) {
    console.warn('Sammlung flyout refresh failed', err);
    return false;
  }
}

const VARIABLE_EVENTS = () => [
  Blockly.Events.VAR_CREATE,
  Blockly.Events.VAR_RENAME,
  Blockly.Events.VAR_DELETE,
];

/**
 * Register the four category callbacks and the button callbacks on
 * `workspace`, and keep an open Sammlung flyout current. `providerRef.current`
 * is read at event time. Returns a disposer that removes every listener,
 * subscription and timer.
 */
export function registerSammlungCategories(workspace, providerRef) {
  if (!workspace) return () => {};
  const ref = providerRef && typeof providerRef === 'object' ? providerRef : { current: null };
  providerRefs.set(workspace, ref);

  for (const [key, fn] of CATEGORY_CALLBACKS) {
    workspace.registerToolboxCategoryCallback(key, fn);
  }
  const buttons = [
    [SAMMLUNG_BUTTON_KEYS.TEACH_RECORDING, () => ({ type: 'teach', focus: 'recording' })],
    [SAMMLUNG_BUTTON_KEYS.TEACH_POSE, () => ({ type: 'teach', focus: 'pose' })],
    [SAMMLUNG_BUTTON_KEYS.TEACH_ZIEL, () => ({ type: 'teach', focus: 'ziel' })],
    [SAMMLUNG_BUTTON_KEYS.PIN_CAMERA, () => ({ type: 'pinCamera' })],
    [SAMMLUNG_BUTTON_KEYS.PIN_SIM, () => ({ type: 'pinSim' })],
    [SAMMLUNG_BUTTON_KEYS.MANAGE_VARIABLEN, () => ({ type: 'manage', tab: 'variablen', focusId: null })],
    [SAMMLUNG_BUTTON_KEYS.MANAGE_AUFNAHMEN, () => ({ type: 'manage', tab: 'aufnahmen', focusId: null })],
    [SAMMLUNG_BUTTON_KEYS.MANAGE_ZIELE, () => ({ type: 'manage', tab: 'ziele', focusId: null })],
    [SAMMLUNG_BUTTON_KEYS.MANAGE_POSITIONEN, () => ({ type: 'manage', tab: 'positionen', focusId: null })],
  ];
  for (const [key, make] of buttons) {
    workspace.registerButtonCallback(key, () => dispatch(workspace, make()));
  }
  // Plan B: the asset rides on the button's own JSON.
  const assetOf = (btn) => {
    const info = (btn && btn.info) || {};
    return {
      assetKind: info['edu-asset-kind'],
      assetId: info['edu-asset-id'],
      assetName: info['edu-asset-name'],
    };
  };
  workspace.registerButtonCallback(SAMMLUNG_BUTTON_KEYS.CARD_PREVIEW, (btn) => {
    dispatch(workspace, {
      type: 'preview',
      asset: previewAssetFor(assetOf(btn), snapshotOf(workspace)),
    });
  });
  workspace.registerButtonCallback(SAMMLUNG_BUTTON_KEYS.CARD_MANAGE, (btn) => {
    dispatch(workspace, manageActionFor(assetOf(btn)));
  });

  let disposed = false;
  let timer = null;
  // Coalesced, not restarted: a steady stream (a running program pushes
  // variable values many times a second) must still refresh every 150 ms
  // instead of starving the refresh or rebuilding the flyout per message.
  const scheduleRefresh = () => {
    if (disposed || timer) return;
    timer = setTimeout(() => {
      timer = null;
      if (!disposed) refreshIfOpen(workspace);
    }, REFRESH_DEBOUNCE_MS);
  };

  // The provider may be swapped behind the ref; follow it.
  let subscribedProvider = null;
  let unsubscribeProvider = () => {};
  const syncProvider = () => {
    const current = ref.current || null;
    if (current === subscribedProvider) return;
    unsubscribeProvider();
    subscribedProvider = current;
    unsubscribeProvider = current && typeof current.subscribe === 'function'
      ? current.subscribe(scheduleRefresh)
      : () => {};
  };
  syncProvider();

  const unsubscribeStore = getDestinationStore(workspace).subscribe(() => refreshIfOpen(workspace));

  const varEvents = VARIABLE_EVENTS();
  const onChange = (e) => {
    if (disposed || !e) return;
    syncProvider();
    if (dirtyWorkspaces.has(workspace)) {
      const dragEnded = e.type === Blockly.Events.BLOCK_DRAG && !e.isStart;
      if (dragEnded || e.type === Blockly.Events.CLICK
        || e.type === Blockly.Events.TOOLBOX_ITEM_SELECT) {
        refreshIfOpen(workspace);
      }
    }
    if (!e.isUiEvent || varEvents.includes(e.type)) scheduleRefresh();
  };
  workspace.addChangeListener(onChange);

  return () => {
    if (disposed) return;
    disposed = true;
    if (timer) clearTimeout(timer);
    timer = null;
    try {
      workspace.removeChangeListener(onChange);
    } catch (_) { /* already disposed */ }
    unsubscribeStore();
    unsubscribeProvider();
    dirtyWorkspaces.delete(workspace);
    providerRefs.delete(workspace);
  };
}
