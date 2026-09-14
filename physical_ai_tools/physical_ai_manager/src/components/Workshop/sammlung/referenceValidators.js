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
 * Keyed canvas warnings for a NAME the program uses that this workflow does
 * not have: a „spiele Bewegung ab" whose recording is not in the list, and a
 * „Ziel"-reference that is neither in the Sammlung nor set by an enabled
 * statement of the program.
 *
 * The NAME fields stay free-text (`field_input`) — suggestions come from the
 * flyout cards — so this warning is how a typo or a deleted recording becomes
 * visible BEFORE Start (which still aborts on a replay without data).
 *
 * KEYED (`edubotics_asset_missing`): `setWarningText(null)` with no id disposes
 * the whole warning icon, taking every other writer's text with it. We write
 * only on change (a per-block cache) — hence the FORCE flag, which re-applies
 * after an unkeyed writer may have wiped the icon.
 *
 * Fail open: while the recording list is not `ready`, no replay block is
 * warned — an unknown list proves nothing missing.
 */

import { DE, formatDe } from '../blocks/messages_de';
import { REPLAY_BLOCK_TYPE } from '../blocks/trajectories';
import { getDestinationStore } from './destinationStore';
import { collectBlockUsage } from './blockUsage';
import { DEFAULT_SAMMLUNG_SNAPSHOT } from './provider';

export const ASSET_MISSING_WARNING_ID = 'edubotics_asset_missing';

const REF_TYPE = 'edubotics_destination_ref';

// Main workspace → { ref, cache: Map<blockId, text|null>, timer, force }.
const states = new WeakMap();

function snapshotOf(state) {
  let snap = null;
  try {
    snap = state.ref.current ? state.ref.current.getSnapshot() : null;
  } catch (_) {
    snap = null;
  }
  return snap && typeof snap === 'object' ? snap : DEFAULT_SAMMLUNG_SNAPSHOT;
}

function nameOf(block) {
  try {
    const raw = block.getFieldValue('NAME');
    return typeof raw === 'string' ? raw.trim() : '';
  } catch (_) {
    return '';
  }
}

function isRunnable(block) {
  try {
    return block.isEnabled() && !block.getInheritedDisabled();
  } catch (_) {
    return false;
  }
}

function recompute(workspace, state, force) {
  const snapshot = snapshotOf(state);
  const trajectories = snapshot.trajectories || {};
  const ready = trajectories.status === 'ready';
  const recordingNames = new Set((Array.isArray(trajectories.items) ? trajectories.items : [])
    .map((it) => (it && typeof it.name === 'string' ? it.name : '')));
  const storeNames = new Set(getDestinationStore(workspace).getEntries().map((e) => e.name));
  const usage = collectBlockUsage(workspace);
  const programNames = new Set();
  for (const [name, pin] of usage.pinStatements) if (pin.enabled) programNames.add(name);
  for (const [name, cur] of usage.currentStatements) if (cur.enabled) programNames.add(name);

  let blocks = [];
  try {
    blocks = workspace.getAllBlocks(false);
  } catch (_) {
    return;
  }
  const seen = new Set();
  for (const block of blocks) {
    if (!block || block.isInFlyout) continue;
    if (block.type !== REPLAY_BLOCK_TYPE && block.type !== REF_TYPE) continue;
    if (typeof block.setWarningText !== 'function') continue;
    seen.add(block.id);
    const name = nameOf(block);
    const runs = isRunnable(block);
    let text = null;
    if (block.type === REPLAY_BLOCK_TYPE) {
      if (ready && runs && name && !recordingNames.has(name)) {
        text = formatDe(DE.WARN_MISSING_RECORDING, name);
      }
    } else if (runs && name && !storeNames.has(name) && !programNames.has(name)) {
      text = formatDe(DE.WARN_MISSING_DESTINATION, name);
    }
    const prev = state.cache.has(block.id) ? state.cache.get(block.id) : null;
    if (!force && prev === text) continue;
    try {
      block.setWarningText(text, ASSET_MISSING_WARNING_ID);
      state.cache.set(block.id, text);
    } catch (_) {
      // A block disposed in between.
    }
  }
  for (const id of Array.from(state.cache.keys())) {
    if (!seen.has(id)) state.cache.delete(id);
  }
}

/**
 * Recompute the warnings of `workspace` on the next tick (coalesced). `force`
 * rewrites every warning even when the cached text did not change.
 */
export function refreshAssetReferenceWarnings(workspace, { force = false } = {}) {
  const state = workspace && states.get(workspace);
  if (!state) return;
  state.force = state.force || !!force;
  if (state.timer) return;
  state.timer = setTimeout(() => {
    state.timer = null;
    const doForce = state.force;
    state.force = false;
    if (state.disposed) return;
    try {
      recompute(workspace, state, doForce);
    } catch (err) {
      console.warn('Sammlung reference warnings failed', err);
    }
  }, 0);
}

/** Watch `workspace` (events, destination store, provider). Returns a disposer. */
export function attachAssetReferenceValidators(workspace, providerRef) {
  if (!workspace || workspace.isFlyout) return () => {};
  const ref = providerRef && typeof providerRef === 'object' ? providerRef : { current: null };
  const state = { ref, cache: new Map(), timer: null, force: false, disposed: false };
  states.set(workspace, state);
  const schedule = () => refreshAssetReferenceWarnings(workspace);

  let subscribedProvider = null;
  let unsubscribeProvider = () => {};
  const syncProvider = () => {
    const current = ref.current || null;
    if (current === subscribedProvider) return;
    unsubscribeProvider();
    subscribedProvider = current;
    unsubscribeProvider = current && typeof current.subscribe === 'function'
      ? current.subscribe(schedule)
      : () => {};
  };
  syncProvider();

  const unsubscribeStore = getDestinationStore(workspace).subscribe(schedule);
  const onChange = (e) => {
    if (state.disposed || !e || e.isUiEvent) return;
    syncProvider();
    schedule();
  };
  workspace.addChangeListener(onChange);
  schedule();

  return () => {
    if (state.disposed) return;
    state.disposed = true;
    if (state.timer) clearTimeout(state.timer);
    state.timer = null;
    try {
      workspace.removeChangeListener(onChange);
    } catch (_) { /* already disposed */ }
    unsubscribeStore();
    unsubscribeProvider();
    states.delete(workspace);
  };
}
