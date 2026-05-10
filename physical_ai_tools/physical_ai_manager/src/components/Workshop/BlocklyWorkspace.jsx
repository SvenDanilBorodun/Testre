/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import React, { useEffect, useRef, useState } from 'react';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { TOOLBOX, buildToolbox } from './blocks/toolbox';
import { registerMotionBlocks } from './blocks/motion';
import { registerPerceptionBlocks } from './blocks/perception';
import { registerDestinationBlocks } from './blocks/destinations';
import { registerOutputBlocks } from './blocks/output';
import { registerEventBlocks } from './blocks/events';

let blocksRegistered = false;
function registerAllBlocksOnce() {
  if (blocksRegistered) return;
  Blockly.setLocale(De);
  registerMotionBlocks();
  registerPerceptionBlocks();
  registerDestinationBlocks();
  registerOutputBlocks();
  registerEventBlocks();
  blocksRegistered = true;
}

// Lazy-init plugins so the main bundle stays small. Each plugin block
// checks the `isDisposed()` callback before instantiating so a
// dynamic-import that resolves *after* the workspace was disposed
// doesn't `init()` on a dead workspace. Audit §A2.
async function initPlugins(workspace, isDisposed) {
  const guard = (label, fn) => {
    if (isDisposed()) return;
    try {
      return fn();
    } catch (e) {
      console.warn(label, 'unavailable', e);
    }
  };
  try {
    const mod = await import('@blockly/plugin-workspace-search');
    guard('plugin-workspace-search', () => {
      const Cls = mod.WorkspaceSearch || mod.default;
      if (Cls) new Cls(workspace).init();
    });
  } catch (e) { console.warn('plugin-workspace-search import failed', e); }

  try {
    const mod = await import('@blockly/workspace-backpack');
    guard('workspace-backpack', () => {
      const Cls = mod.Backpack || mod.default;
      if (Cls) new Cls(workspace).init();
    });
  } catch (e) { console.warn('workspace-backpack import failed', e); }

  try {
    const mod = await import('@blockly/zoom-to-fit');
    guard('zoom-to-fit', () => {
      const Cls = mod.ZoomToFitControl || mod.default;
      if (Cls) new Cls(workspace).init();
    });
  } catch (e) { console.warn('zoom-to-fit import failed', e); }

  try {
    const mod = await import('@blockly/workspace-minimap');
    guard('workspace-minimap', () => {
      const Cls = mod.PositionedMinimap || mod.Minimap || mod.default;
      if (Cls) new Cls(workspace).init();
    });
  } catch (e) { console.warn('workspace-minimap import failed', e); }

  try {
    await import('@blockly/block-plus-minus');
  } catch (e) { console.warn('block-plus-minus import failed', e); }

  try {
    const mod = await import('@blockly/suggested-blocks');
    guard('suggested-blocks', () => {
      const init = mod.init || mod.default;
      if (typeof init === 'function') init(workspace);
    });
  } catch (e) { console.warn('suggested-blocks import failed', e); }

  try {
    const mod = await import('@mit-app-inventor/blockly-plugin-workspace-multiselect');
    guard('multiselect', () => {
      const Cls = mod.Multiselect || mod.default;
      if (Cls) {
        const ms = new Cls(workspace);
        if (typeof ms.init === 'function') ms.init({});
      }
    });
  } catch (e) { console.warn('workspace-multiselect import failed', e); }
}

function BlocklyWorkspace({
  initialJson,
  onChange,
  onWorkspaceReady,
  readOnly = false,
  restrictedBlocks = null,
}) {
  const containerRef = useRef(null);
  const workspaceRef = useRef(null);
  const [, setReadyTick] = useState(0);

  // Hold the latest onChange in a ref so a parent that rebuilds the
  // callback on every render doesn't trigger our injection effect.
  // The injection effect intentionally depends only on `initialJson`
  // and `readOnly`; we read the live `onChange` through the ref.
  // Audit §A1 — without this, every parent render disposes and
  // re-injects the workspace, wiping in-progress block layout.
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);
  const onWorkspaceReadyRef = useRef(onWorkspaceReady);
  useEffect(() => {
    onWorkspaceReadyRef.current = onWorkspaceReady;
  }, [onWorkspaceReady]);

  // Apply toolbox restriction in a *separate* effect so changing the
  // restricted set (e.g. tutorial step advance) updates the toolbox in
  // place via workspace.updateToolboxDefinition() instead of disposing
  // and re-injecting the entire workspace (which would lose any
  // in-progress block layout). Audit §12.b found this regression.
  useEffect(() => {
    const ws = workspaceRef.current;
    if (!ws || typeof ws.updateToolboxDefinition !== 'function') return;
    try {
      const next = restrictedBlocks ? buildToolbox(restrictedBlocks) : TOOLBOX;
      ws.updateToolboxDefinition(next);
    } catch (e) {
      console.warn('BlocklyWorkspace: updateToolboxDefinition failed', e);
    }
  }, [restrictedBlocks]);

  useEffect(() => {
    registerAllBlocksOnce();
    if (!containerRef.current) return undefined;

    const toolbox = restrictedBlocks
      ? buildToolbox(restrictedBlocks)
      : TOOLBOX;

    const workspace = Blockly.inject(containerRef.current, {
      toolbox,
      readOnly,
      trashcan: !readOnly,
      grid: { spacing: 20, length: 1, colour: '#e5e7eb', snap: true },
      zoom: {
        controls: true,
        wheel: true,
        startScale: 0.9,
        maxScale: 1.5,
        minScale: 0.5,
      },
      move: { scrollbars: true, drag: true, wheel: false },
      // v12 — sound effects are subtle but disable on prefers-reduced-motion.
      sounds: !window.matchMedia
        || !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    });
    workspaceRef.current = workspace;
    setReadyTick((n) => n + 1);

    // Plugins are async-imported; pass an isDisposed callback so
    // post-dispose resolutions don't init() against a dead workspace.
    let disposed = false;
    initPlugins(workspace, () => disposed).catch((err) => {
      if (disposed) return;
      console.warn('BlocklyWorkspace: plugin init failed', err);
    });

    if (typeof onWorkspaceReadyRef.current === 'function') {
      onWorkspaceReadyRef.current(workspace);
    }

    // Suppress the synthetic change event Blockly fires while loading
    // the initial JSON; otherwise the parent's onChange handler
    // dispatches setUnsavedBlocklyJson(null) on first mount and
    // clobbers Redux state (audit §1.5).
    let loadingInitial = false;
    if (initialJson) {
      try {
        loadingInitial = true;
        Blockly.serialization.workspaces.load(initialJson, workspace);
      } catch (e) {
        console.error('BlocklyWorkspace: failed to load initial JSON', e);
      } finally {
        loadingInitial = false;
      }
    }

    const handleChange = () => {
      if (disposed || loadingInitial) return;
      const fn = onChangeRef.current;
      if (typeof fn !== 'function') return;
      try {
        const json = Blockly.serialization.workspaces.save(workspace);
        fn(json);
      } catch (e) {
        console.error('BlocklyWorkspace: failed to serialize', e);
      }
    };
    workspace.addChangeListener(handleChange);

    // React 19 StrictMode mounts each effect twice; without an explicit
    // dispose() Blockly leaks a workspace per mount and the SVG defs
    // accumulate. The 5x mount/unmount test in the verification gate
    // depends on this cleanup.
    return () => {
      disposed = true;
      workspace.removeChangeListener(handleChange);
      workspace.dispose();
      workspaceRef.current = null;
      const readyFn = onWorkspaceReadyRef.current;
      if (typeof readyFn === 'function') {
        readyFn(null);
      }
    };
    // Audit §A1 — onChange + onWorkspaceReady are routed through
    // refs (above), so a parent rebuilding the callback identity on
    // every render does NOT re-inject the workspace. restrictedBlocks
    // is handled by the separate `updateToolboxDefinition` effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialJson, readOnly]);

  return <div ref={containerRef} className="w-full h-full min-h-[420px]" />;
}

export default BlocklyWorkspace;
