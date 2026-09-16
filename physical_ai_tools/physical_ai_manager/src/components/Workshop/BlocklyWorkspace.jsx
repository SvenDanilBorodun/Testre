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
import { buildToolbox } from './blocks/toolbox';
import { DE } from './blocks/messages_de';
import { registerMotionBlocks, attachMotionWorkspaceValidators } from './blocks/motion';
import { registerPerceptionBlocks } from './blocks/perception';
import { registerDestinationBlocks } from './blocks/destinations';
import { registerOutputBlocks } from './blocks/output';
import { registerEventBlocks } from './blocks/events';
import {
  registerControlBlocks,
  registerControlsIfElseMutator,
  attachControlWorkspaceValidators,
} from './blocks/control';
import { registerCounterBlocks } from './blocks/counters';
import { registerTrajectoryBlocks } from './blocks/trajectories';
import { registerProcedureCallArgumentFix } from './blocks/procedures';
import { attachSavedValueWarnings } from './blocks/savedValueWarnings';
import { registerDestinationSerializer } from './sammlung/destinationStore';
import { registerAssetCardInflater } from './sammlung/AssetCardInflater';
import { registerSammlungCategories } from './sammlung/toolboxCategories';
import { attachAssetReferenceValidators } from './sammlung/referenceValidators';
import { EMPTY_SAMMLUNG_PROVIDER } from './sammlung/provider';

let blocksRegistered = false;
function registerAllBlocksOnce() {
  if (blocksRegistered) return;
  Blockly.setLocale(De);
  registerMotionBlocks();
  registerPerceptionBlocks();
  registerDestinationBlocks();
  registerOutputBlocks();
  registerEventBlocks();
  registerControlBlocks();
  registerCounterBlocks();
  registerTrajectoryBlocks();
  // The Ziele/Positionen document serializer + its undoable change event.
  // Explicit, never at module import: blocklyPayload.test.js pins the CORE
  // serializer inventory and page tests mock `blockly/core` minimally.
  registerDestinationSerializer();
  // The Sammlung flyout card item (a registry CLASS, instantiated per flyout).
  registerAssetCardInflater();
  removeEnglishHelpMenuItem();
  blocksRegistered = true;
}

// „Hilfe" in a block's right-click menu opens `block.getHelpUrl()`. Every
// EduBotics block leaves that empty (measured null), so the entry only ever
// appears on Blockly's own built-ins — where all 34 URLs in the German catalog
// point at ENGLISH pages (the Blockly wiki, Wikipedia). Sending a German-
// speaking student there is worse than not offering the entry, and overriding
// 34 upstream URLs by hand is a maintenance burden with no owner. Unregistering
// the item removes it from the built-ins and changes nothing for our own blocks,
// which never showed it. Global (ContextMenuRegistry is), hence once, here.
function removeEnglishHelpMenuItem() {
  try {
    if (Blockly.ContextMenuRegistry.registry.getItem('blockHelp')) {
      Blockly.ContextMenuRegistry.registry.unregister('blockHelp');
    }
  } catch (e) {
    console.warn('help context-menu item could not be removed', e);
  }
}

// Plugin MODULES are loaded BEFORE the first Blockly.inject() and never after.
// Backpack and zoom-to-fit (and the dropped minimap + multiselect) call
// `Blockly.Css.register()` at module top level, and Blockly 12 throws
// "CSS already injected" from that call once ANY workspace has been injected.
// The old order — inject, then `await import()` — made those imports reject on
// every page load, so the controls never existed and only a console.warn said
// so. They stay lazy chunks (the entry-bundle rule); the injection effect
// awaits this ONE shared promise, so no inject can ever precede them.
//
// Deliberately NOT loaded: @blockly/workspace-minimap (it positions itself
// against the wrong containing block and, on this small canvas, covers the
// trash can + zoom-to-fit; its inject() also makes it Blockly's main
// workspace) and @mit-app-inventor/blockly-plugin-workspace-multiselect (peer
// range `>=11 <12`, and its `init({})` throws — measured:
// „Cannot read properties of undefined (reading 'hideIcon')", because it reads
// `options.multiselectIcon` unconditionally).
let pluginModulesPromise = null;
function loadPluginModules() {
  if (!pluginModulesPromise) {
    const load = (label, importer) => importer().catch((e) => {
      console.warn(`${label} import failed`, e);
      return null;
    });
    pluginModulesPromise = Promise.all([
      load('plugin-workspace-search', () => import('@blockly/plugin-workspace-search')),
      load('workspace-backpack', () => import('@blockly/workspace-backpack')),
      load('zoom-to-fit', () => import('@blockly/zoom-to-fit')),
      load('block-plus-minus', () => import('@blockly/block-plus-minus')),
      load('suggested-blocks', () => import('@blockly/suggested-blocks')),
    // NOTHING in this `.then` may throw: the promise is cached for the page's
    // lifetime, so a throw here would leave EVERY later mount without a
    // workspace, not just without one plugin. Hence each step is guarded.
    ]).then(([search, backpack, zoomToFit, plusMinus, suggested]) => {
      if (backpack) {
        try {
          germanizeBackpackMenu();
        } catch (e) {
          console.warn('backpack German menu strings failed', e);
        }
      }
      if (plusMinus) {
        // MUST run after that import: the plugin unregisters + re-registers
        // `controls_if_mutator` at import time, and its replacement can never
        // add an ELSE clause (its plus() only ever adds an else-if). Re-register
        // ours on top so a freshly dragged „wenn" block can grow a „sonst" row.
        // It writes the GLOBAL Blockly extension registry, so it runs once,
        // here, not per workspace. Behind a try/catch because THIS promise is
        // cached for the page's lifetime: anything that throws here would leave
        // every later mount without a workspace at all, not just without a
        // „sonst" row.
        try {
          registerControlsIfElseMutator();
        } catch (e) {
          console.warn('controls_if mutator re-registration failed', e);
        }
        // Same seam, same reason: the plugin replaces the „Funktion" DEFINITION
        // blocks at import time, and its ⊖ made every CALL block re-attach its
        // arguments by POSITION — so removing a parameter silently moved the
        // remaining values onto the wrong parameters (blocks/procedures.js).
        try {
          registerProcedureCallArgumentFix();
        } catch (e) {
          console.warn('procedure call argument fix failed', e);
        }
        // The plugin also writes `Blockly.Msg.PROCEDURE_VARIABLE = "variable:"`
        // at module evaluation, which is the one English string it shows.
        try {
          Blockly.Msg.PROCEDURE_VARIABLE = DE.PROCEDURE_VARIABLE;
        } catch (e) {
          console.warn('procedure German strings failed', e);
        }
      }
      return { search, backpack, zoomToFit, suggested };
    });
  }
  return pluginModulesPromise;
}

// @blockly/workspace-backpack assigns its five context-menu strings to
// `Blockly.Msg` in ENGLISH when its module loads, and Blockly's German catalog
// defines none of them — so making the plugin load at all (2026-09-11) would
// have put „Copy to Backpack" into an otherwise German editor, on the student
// AND the teacher-web build. Assign them right after that module has set its
// defaults and BEFORE any `Backpack` is constructed: two of the four menu
// entries (`REMOVE_FROM_BACKPACK`, `COPY_ALL_TO_BACKPACK`) read `Msg` at
// REGISTRATION time inside `init()`, so a later assignment is too late for
// them. The CI German lint is a Python AST walker and cannot see this file.
function germanizeBackpackMenu() {
  Blockly.Msg.COPY_TO_BACKPACK = DE.BACKPACK_COPY;
  Blockly.Msg.COPY_ALL_TO_BACKPACK = DE.BACKPACK_COPY_ALL;
  Blockly.Msg.PASTE_ALL_FROM_BACKPACK = DE.BACKPACK_PASTE_ALL;
  Blockly.Msg.REMOVE_FROM_BACKPACK = DE.BACKPACK_REMOVE;
  Blockly.Msg.EMPTY_BACKPACK = DE.BACKPACK_EMPTY;
}

// @blockly/plugin-workspace-search 10.1.8 hardcodes its four strings in
// English — it reads no `Blockly.Msg` key for them, so they have to be fixed on
// the DOM the plugin just built: the Ctrl+F bar's input placeholder and the
// aria-labels of its next / previous / close buttons. Measured in the shipped
// bundle: placeholder "Search", labels "Find next", "Find previous", "Close
// search bar". Best-effort by design — a plugin version that renames a class
// simply leaves the English in place rather than breaking the editor.
const SEARCH_BAR_STRINGS = [
  ['.blockly-ws-search-input input', 'placeholder', () => DE.SEARCH_PLACEHOLDER],
  ['.blockly-ws-search-next-btn', 'aria-label', () => DE.SEARCH_NEXT],
  ['.blockly-ws-search-previous-btn', 'aria-label', () => DE.SEARCH_PREVIOUS],
  ['.blockly-ws-search-close-btn', 'aria-label', () => DE.SEARCH_CLOSE],
];
function germanizeSearchBar(workspace) {
  const root = workspace.getInjectionDiv
    ? workspace.getInjectionDiv()
    : null;
  const bar = root && root.querySelector('.blockly-ws-search');
  if (!bar) return;
  SEARCH_BAR_STRINGS.forEach(([selector, attribute, text]) => {
    const el = bar.querySelector(selector);
    if (el) el.setAttribute(attribute, text());
  });
}

// The only text @blockly/suggested-blocks shows a student is this hardcoded
// English label (an empty „Vorschläge" category). Swap it for German.
const SUGGESTED_EMPTY_EN = 'No blocks have been used yet!';
function germanizeSuggestionPlaceholder(workspace) {
  ['MOST_USED', 'RECENTLY_USED'].forEach((key) => {
    const original = workspace.getToolboxCategoryCallback(key);
    if (typeof original !== 'function') return;
    workspace.registerToolboxCategoryCallback(key, (ws) => {
      const items = original(ws);
      if (!Array.isArray(items)) return items;
      return items.map((item) => (
        item && item.text === SUGGESTED_EMPTY_EN
          ? { ...item, text: DE.SUGGESTED_EMPTY }
          : item
      ));
    });
  });
}

// Core lays out its corner controls in WEIGHT order and bumps each one past the
// ones already placed. Trash (2) and the zoom cluster (3) are core; backpack and
// zoom-to-fit register at 2, i.e. AHEAD of the zoom cluster — so in an editor
// shorter than ~356 px the zoom +/− cluster was the one bumped above the top
// edge, the exact "plus and minus disappear" symptom. Re-registering the two
// plugin controls after the core ones keeps trash + zoom in their natural spots
// at every height; the plugin extras are what run out of room first (the
// toolbar's „Ansicht anpassen" duplicates zoom-to-fit).
const PLUGIN_CONTROL_WEIGHTS = [['backpack', 10], ['zoomToFit', 11]];
function positionPluginControlsAfterCoreControls(workspace) {
  const manager = workspace.getComponentManager();
  const { Capability } = Blockly.ComponentManager;
  const known = [
    Capability.POSITIONABLE,
    Capability.DRAG_TARGET,
    Capability.DELETE_AREA,
    Capability.AUTOHIDEABLE,
  ];
  PLUGIN_CONTROL_WEIGHTS.forEach(([id, weight]) => {
    const component = manager.getComponent(id);
    if (!component) return;
    const capabilities = known.filter((c) => manager.hasCapability(id, c));
    manager.removeComponent(id);
    manager.addComponent({ component, weight, capabilities });
  });
  workspace.resize();
}

// The right-hand corner column needs ~376 px for all four controls (trash,
// zoom cluster, zoom-to-fit, backpack) and ~245 px for trash + zoom alone. In a
// shorter editor Blockly's bump pushes the last ones PART-way past an edge — a
// half-drawn button at the top, a backpack sliver under the scrollbar. Never
// half: a control that does not fit whole is hidden, and a hidden trash can or
// backpack also stops being a drop target (else it would still swallow blocks
// dropped where it would have been). `root` finds each control's own element.
const CORNER_CONTROLS = [
  { id: 'trashcan', root: (svg) => svg.querySelector('.blocklyTrash') },
  { id: 'zoomControls', root: (svg) => svg.querySelector('.blocklyZoom')?.parentNode || null },
  { id: 'backpack', root: (svg) => svg.querySelector('.blocklyBackpack')?.parentNode || null },
  { id: 'zoomToFit', root: (svg) => svg.querySelector('.zoomToFit') },
];
function hideCornerControlsThatDoNotFit(workspace, droppedTargets) {
  const manager = workspace.getComponentManager();
  const svg = workspace.getParentSvg();
  const { width, height } = workspace.getCachedParentSvgSize();
  const { DRAG_TARGET } = Blockly.ComponentManager.Capability;
  let targetsChanged = false;
  // CORNER_CONTROLS is in PRIORITY order, and once one control is dropped every
  // lower-priority one goes with it. Without that cascade the shown set is not
  // monotone in height: the backpack is TOP-anchored while the rest stack from
  // the bottom, so Blockly's bump resolves differently either side of ~315 px
  // and shrinking the editor made the backpack vanish while zoom-to-fit popped
  // back IN — a flicker while dragging the dock divider.
  let dropRest = false;
  CORNER_CONTROLS.forEach(({ id, root }) => {
    const component = manager.getComponent(id);
    const el = component && svg && root(svg);
    if (!el || typeof component.getBoundingRectangle !== 'function') return;
    const r = component.getBoundingRectangle();
    const fits = !dropRest
      && !!r && r.top >= 0 && r.left >= 0 && r.bottom <= height && r.right <= width;
    if (!fits) dropRest = true;
    el.style.display = fits ? '' : 'none';
    if (!fits && manager.hasCapability(id, DRAG_TARGET)) {
      manager.removeCapability(id, DRAG_TARGET);
      droppedTargets.add(id);
      targetsChanged = true;
    } else if (fits && droppedTargets.has(id)) {
      manager.addCapability(id, DRAG_TARGET);
      droppedTargets.delete(id);
      targetsChanged = true;
    }
  });
  if (targetsChanged) workspace.recordDragTargets();
}

// Runs synchronously right after inject and BEFORE the initial JSON loads, so
// the backpack + suggested-blocks serializers restore their (autosaved) state.
function initPlugins(workspace, { search, backpack, zoomToFit, suggested }, { readOnly, hasInitialJson }) {
  const guard = (label, fn) => {
    try {
      fn();
    } catch (e) {
      console.warn(label, 'unavailable', e);
    }
  };
  if (search) {
    guard('plugin-workspace-search', () => {
      const Cls = search.WorkspaceSearch || search.default;
      if (!Cls) return;
      new Cls(workspace).init();
      germanizeSearchBar(workspace);
    });
  }
  // A backpack is a drag/delete target — meaningless on a read-only preview,
  // which also gets no trash can.
  if (backpack && !readOnly) {
    guard('workspace-backpack', () => {
      const Cls = backpack.Backpack || backpack.default;
      if (Cls) new Cls(workspace).init();
    });
  }
  if (zoomToFit) {
    guard('zoom-to-fit', () => {
      const Cls = zoomToFit.ZoomToFitControl || zoomToFit.default;
      if (Cls) new Cls(workspace).init();
    });
  }
  if (suggested) {
    guard('suggested-blocks', () => {
      const init = suggested.init || suggested.default;
      if (typeof init !== 'function') return;
      // The plugin ignores blocks created before it sees FINISHED_LOADING, so
      // the loaded program does not count as "used". Blockly fires events
      // asynchronously, so the load that follows in this same tick still
      // delivers that event to it. An empty editor has no load — and so no
      // FINISHED_LOADING — and must record from the first block on.
      init(workspace, 10, hasInitialJson);
      germanizeSuggestionPlaceholder(workspace);
    });
  }
  guard('corner-controls', () => positionPluginControlsAfterCoreControls(workspace));
}

function BlocklyWorkspace({
  initialJson,
  onChange,
  onWorkspaceReady,
  readOnly = false,
  restrictedBlocks = null,
  sammlungProvider = EMPTY_SAMMLUNG_PROVIDER,
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
  // The Sammlung provider is read through a ref at event time: a new provider
  // (or a new snapshot) must never re-inject the editor.
  const sammlungProviderRef = useRef(sammlungProvider || EMPTY_SAMMLUNG_PROVIDER);
  useEffect(() => {
    sammlungProviderRef.current = sammlungProvider || EMPTY_SAMMLUNG_PROVIDER;
  }, [sammlungProvider]);
  // Injection waits for the plugin modules, so the toolbox is built from the
  // restriction current AT INJECT TIME, not the one captured when the effect
  // started (a tutorial step can change it in between).
  const restrictedBlocksRef = useRef(restrictedBlocks);
  useEffect(() => {
    restrictedBlocksRef.current = restrictedBlocks;
  }, [restrictedBlocks]);

  // Apply toolbox restriction in a *separate* effect so changing the
  // restricted set (e.g. tutorial step advance) updates the toolbox in
  // place via workspace.updateToolboxDefinition() instead of disposing
  // and re-injecting the entire workspace (which would lose any
  // in-progress block layout). Audit §12.b found this regression.
  useEffect(() => {
    const ws = workspaceRef.current;
    if (!ws || typeof ws.updateToolbox !== 'function') return;
    try {
      const next = buildToolbox(restrictedBlocks);
      ws.updateToolbox(next);
    } catch (e) {
      console.warn('BlocklyWorkspace: updateToolbox failed', e);
    }
  }, [restrictedBlocks]);

  useEffect(() => {
    registerAllBlocksOnce();
    let disposed = false;
    let teardown = null;

    const mount = (plugins) => {
      const container = containerRef.current;
      if (disposed || !container) return;

      const workspace = Blockly.inject(container, {
        toolbox: buildToolbox(restrictedBlocksRef.current),
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
        // The wheel SCROLLS the canvas (Shift+wheel sideways); Ctrl/⌘+wheel and
        // a trackpad pinch zoom — Blockly zooms on a plain wheel only while
        // `move.wheel` is off. With it off, one Windows notch (deltaY ≈ 100)
        // was two 1.2 zoom steps (×1.44): a student "scrolling" hit the 0.5/1.5
        // clamp in two notches and grew blocks over the trash can.
        move: { scrollbars: true, drag: true, wheel: true },
        // v12 — sound effects are subtle but disable on prefers-reduced-motion.
        sounds: !window.matchMedia
          || !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
      });
      workspaceRef.current = workspace;
      setReadyTick((n) => n + 1);
      // A MINIMAL teardown from the moment the workspace exists, replaced by
      // the full one at the end of setup. Without it, anything that threw
      // between here and there (a validator, the ResizeObserver ctor) left an
      // injected, undisposed workspace behind that no unmount could reach.
      teardown = () => {
        workspace.dispose();
        workspaceRef.current = null;
      };

      // Every re-layout of the corner controls goes through resize() — our
      // observer (via svgResize), Blockly's own window-resize handler, each
      // plugin's init(). Re-judge which controls fit whole after each one.
      const droppedTargets = new Set();
      const baseResize = workspace.resize.bind(workspace);
      workspace.resize = () => {
        baseResize();
        hideCornerControlsThatDoNotFit(workspace, droppedTargets);
      };

      // Audit §motion-r1: wire the wait_seconds numericClamp validator at
      // the workspace level. Returns a disposer we call in the cleanup
      // path below so the listener doesn't outlive the workspace.
      const disposeMotionValidators = attachMotionWorkspaceValidators(workspace);
      // Same shape: flags blocks snapped under „wiederhole fortlaufend", which
      // can never run. Its disposer is called alongside the motion one below.
      const disposeControlValidators = attachControlWorkspaceValidators(workspace);
      // The Sammlung toolbox groups (on EVERY workspace — an unregistered custom
      // category key throws when opened) and the keyed missing-name warnings.
      // Both read the provider through the ref at event time.
      const disposeSammlung = registerSammlungCategories(workspace, sammlungProviderRef);
      const disposeReferenceValidators = attachAssetReferenceValidators(
        workspace,
        sammlungProviderRef,
      );
      // A saved value the robot refuses is KEPT (blocks/fieldLoad.js) and
      // marked on the block instead of being silently repaired.
      const disposeSavedValueWarnings = attachSavedValueWarnings(workspace);

      initPlugins(workspace, plugins, { readOnly, hasInitialJson: !!initialJson });

      // Blockly 12 re-measures its container ONLY on a window 'resize'. Every
      // in-page size change — the Code-Vorschau, the Protokoll (auto-opens on
      // Start), the run-bar banners, a toolbar that re-wraps when the autosave
      // label changes, the dock divider/collapse, the simulator swap — left the
      // SVG at its old size: scrollbars, trash and zoom drawn over the blocks
      // when the box grew, clipped away when it shrank. Re-fit on every change
      // of the host box. Synchronous (the callback runs after layout, before
      // paint) and loop-free: the host's size never depends on the SVG's.
      let observer = null;
      if (typeof ResizeObserver === 'function') {
        observer = new ResizeObserver(() => {
          if (disposed) return;
          try { Blockly.svgResize(workspace); } catch (_) { /* torn down */ }
        });
        observer.observe(container);
      }

      if (typeof onWorkspaceReadyRef.current === 'function') {
        onWorkspaceReadyRef.current(workspace);
      }

      // Suppress the synthetic change events Blockly fires while loading the
      // initial JSON; otherwise the parent's onChange handler mirrors a program
      // the student has not touched back into Redux and the autosave (audit
      // §1.5).
      //
      // A flag set around the `load()` CALL cannot do this: Blockly delivers
      // every load event ASYNCHRONOUSLY, long after the call has returned, so
      // the old `loadingInitial` was always false by the time they arrived —
      // measured 2026-09-16, 19 onChange calls with no user action. The
      // workspace's own FINISHED_LOADING event is the end of the load, and it
      // is queued behind them, so it is the signal that actually separates "the
      // document as saved" from "the student changed something".
      let loadingInitial = !!initialJson;
      if (initialJson) {
        try {
          Blockly.serialization.workspaces.load(initialJson, workspace);
        } catch (e) {
          console.error('BlocklyWorkspace: failed to load initial JSON', e);
          // A load that threw fires no FINISHED_LOADING; forward again at once
          // rather than going deaf for the workspace's lifetime.
          loadingInitial = false;
        }
      }

      const handleChange = (event) => {
        if (event && event.type === Blockly.Events.FINISHED_LOADING) {
          loadingInitial = false;
          return;
        }
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

      teardown = () => {
        if (observer) observer.disconnect();
        workspace.removeChangeListener(handleChange);
        // Detach the motion-validator listener before disposing the
        // workspace so we don't fire one last clamp on a torn-down host.
        try {
          disposeMotionValidators();
        } catch (_) { /* already disposed */ }
        try {
          disposeControlValidators();
        } catch (_) { /* already disposed */ }
        try {
          disposeSammlung();
        } catch (_) { /* already disposed */ }
        try {
          disposeReferenceValidators();
        } catch (_) { /* already disposed */ }
        try {
          disposeSavedValueWarnings();
        } catch (_) { /* already disposed */ }
        workspace.dispose();
        workspaceRef.current = null;
        const readyFn = onWorkspaceReadyRef.current;
        if (typeof readyFn === 'function') {
          readyFn(null);
        }
      };
    };

    loadPluginModules()
      .then(mount)
      .catch((err) => {
        if (disposed) return;
        // Either the plugin modules never resolved, or setup threw after the
        // workspace was injected — in which case `teardown` (assigned right
        // after inject) still disposes it on unmount.
        console.error('BlocklyWorkspace: workspace setup failed', err);
      });

    // Without an explicit dispose() Blockly leaks a workspace per mount and the
    // SVG defs accumulate; the 5x mount/unmount test in the verification gate
    // depends on this cleanup. An unmount that lands BEFORE the plugin promise
    // resolves never injects at all (`disposed` is checked first in mount()).
    return () => {
      disposed = true;
      if (teardown) teardown();
    };
    // Audit §A1 — onChange, onWorkspaceReady and restrictedBlocks are routed
    // through refs (above), so a parent rebuilding them on every render does
    // NOT re-inject the workspace; restrictedBlocks changes are applied in
    // place by the separate `updateToolbox` effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialJson, readOnly]);

  // No min-height here: it has to be the box the student actually SEES. A
  // floor on this div inside the page's overflow-hidden wrapper made Blockly
  // lay out for more height than was visible and draw the horizontal
  // scrollbar and the trash can into the clipped strip.
  //
  // Blockly's toolbox is `position:absolute; z-index:70` and nothing above this
  // div creates a stacking context, so without isolation the category column
  // paints over the Sammlung drawer, the Vormachen overlay and the
  // CollisionModal (measured in Chromium). Isolation keeps Blockly's internal
  // z-indices inside the editor box; its WidgetDiv/DropDownDiv live on <body>
  // and are unaffected.
  return <div ref={containerRef} className="w-full h-full" style={{ isolation: 'isolate' }} />;
}

export default BlocklyWorkspace;
