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
 * The Sammlung provider: the one channel between the page (Redux, rig state)
 * and the Blockly side (toolbox callbacks, cards, reference warnings).
 *
 * A plain object, not React context: Blockly calls the category callbacks and
 * card handlers outside any React render, so they read a snapshot and dispatch
 * actions through this object. `BlocklyWorkspace` holds it in a ref, so a new
 * snapshot never re-injects the editor.
 */

export const DEFAULT_SAMMLUNG_SNAPSHOT = Object.freeze({
  capabilities: Object.freeze({
    hardware: false,
    simMode: false,
    teach: false,
    drawer: false,
    preview: false,
    // True while the page's leader-status bridge has not answered once: every
    // sim-run ▶ is drawn disabled („Roboterstatus wird geprüft …").
    previewPending: false,
    previewVariables: false,
    pinCamera: false,
    pinSim: false,
  }),
  robotType: '',
  trajectories: Object.freeze({ status: 'none', items: Object.freeze([]) }),
  lastPreviewResult: Object.freeze({}),
  variableValues: Object.freeze({}),
  restrictedBlocks: null, // array of block types or null
});

// Keys whose objects are merged one level instead of replaced, so a page can
// push `{capabilities: {simMode: true}}` without restating the rest.
const MERGED_KEYS = ['capabilities', 'trajectories'];

function isPlainObject(v) {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

export function createSammlungProvider(initialSnapshot = {}) {
  let snapshot = { ...DEFAULT_SAMMLUNG_SNAPSHOT };
  const listeners = new Set();
  let actionHandler = null;

  function merge(partial) {
    if (!isPlainObject(partial)) return;
    const next = { ...snapshot };
    for (const [key, value] of Object.entries(partial)) {
      if (MERGED_KEYS.includes(key) && isPlainObject(value) && isPlainObject(next[key])) {
        next[key] = { ...next[key], ...value };
      } else {
        next[key] = value;
      }
    }
    snapshot = next;
  }

  merge(initialSnapshot);

  return {
    getSnapshot() {
      return snapshot;
    },
    /** Shallow-merge `partial` (one level for capabilities/trajectories), then notify synchronously. */
    setSnapshot(partial) {
      merge(partial);
      for (const fn of Array.from(listeners)) {
        try {
          fn(snapshot);
        } catch (e) {
          // One broken subscriber must not starve the others.
          console.error('Sammlung provider subscriber failed:', e);
        }
      }
    },
    subscribe(fn) {
      if (typeof fn !== 'function') return () => {};
      listeners.add(fn);
      return () => { listeners.delete(fn); };
    },
    setActionHandler(fn) {
      actionHandler = typeof fn === 'function' ? fn : null;
    },
    /** No handler → a no-op (a read-only preview offers no actions). */
    dispatchAction(action) {
      if (!actionHandler) return;
      actionHandler(action);
    },
  };
}

// The default for every editor that mounts no Sammlung page wiring (teacher
// templates, read-only previews): hardware false, no recordings, no actions.
export const EMPTY_SAMMLUNG_PROVIDER = Object.freeze({
  getSnapshot: () => DEFAULT_SAMMLUNG_SNAPSHOT,
  setSnapshot: () => {},
  subscribe: () => () => {},
  setActionHandler: () => {},
  dispatchAction: () => {},
});
