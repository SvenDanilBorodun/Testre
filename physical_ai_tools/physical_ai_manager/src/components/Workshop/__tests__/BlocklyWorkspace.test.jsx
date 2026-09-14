/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Roboter-Studio block canvas, mounted for real: real Blockly 12, real
// plugin modules, real inject into jsdom. Pins the four defects behind
// "scrollbars over the blocks / trash + zoom vanish" (2026-09-11):
//   1. plugin modules must be evaluated BEFORE inject — after it, backpack and
//      zoom-to-fit throw "CSS already injected" at import and never exist;
//   2. the host must re-fit Blockly on ANY size change of its box (Blockly
//      itself listens to window 'resize' only);
//   3. the host carries no min-height (it clipped the bottom controls);
//   4. the wheel scrolls; Ctrl/⌘+wheel zooms;
// plus the corner-control order (core trash + zoom first) and the German
// „Vorschläge" placeholder.
//
// This file must stay the FIRST thing in its module graph to inject a Blockly
// workspace — that is what makes test 1 a real regression test (vitest
// isolates module state per file).

import React from 'react';
import { render, waitFor, act } from '@testing-library/react';
import * as Blockly from 'blockly/core';
import BlocklyWorkspace from '../BlocklyWorkspace';
import { DE } from '../blocks/messages_de';
import { SAMMLUNG_CATEGORY_KEYS } from '../blocks/toolbox';
import { createSammlungProvider } from '../sammlung/provider';
import { getDestinationStore } from '../sammlung/destinationStore';

// jsdom has no ResizeObserver; capture instances so a test can fire one.
const observers = [];
class FakeResizeObserver {
  constructor(callback) {
    this.callback = callback;
    this.targets = [];
    this.disconnected = false;
    observers.push(this);
  }

  observe(target) { this.targets.push(target); }

  disconnect() { this.disconnected = true; }

  fire() { this.callback([], this); }
}

// jsdom lays nothing out (every offsetWidth is 0), and Blockly sizes itself
// from the injection div's offsetWidth/offsetHeight. Give that one element a
// settable size; everything else keeps jsdom's 0.
const editorBox = { width: 800, height: 600 };
const realOffset = {
  width: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth'),
  height: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight'),
};
function isInjectionDiv(el) {
  return !!el.classList && el.classList.contains('injectionDiv');
}

let warnSpy;
beforeEach(() => {
  observers.length = 0;
  editorBox.width = 800;
  editorBox.height = 600;
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true,
    get() { return isInjectionDiv(this) ? editorBox.width : realOffset.width.get.call(this); },
  });
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get() { return isInjectionDiv(this) ? editorBox.height : realOffset.height.get.call(this); },
  });
  globalThis.ResizeObserver = FakeResizeObserver;
  warnSpy = vi.spyOn(console, 'warn');
});
afterEach(() => {
  warnSpy.mockRestore();
  delete globalThis.ResizeObserver;
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', realOffset.width);
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', realOffset.height);
});

// Resize the editor box and deliver it the way the browser would.
function resizeEditor(container, width, height) {
  editorBox.width = width;
  editorBox.height = height;
  const host = hostOf(container);
  const observer = observers.find((o) => o.targets.includes(host));
  act(() => observer.fire());
}

// For every corner control: is it drawn (not display:none), and does its
// layout rectangle lie wholly inside the editor box?
function cornerState(ws) {
  const manager = ws.getComponentManager();
  const svg = ws.getParentSvg();
  const roots = {
    trashcan: find(svg, '.blocklyTrash'),
    // eslint-disable-next-line testing-library/no-node-access
    zoomControls: find(svg, '.blocklyZoom')?.parentNode,
    // eslint-disable-next-line testing-library/no-node-access
    backpack: find(svg, '.blocklyBackpack')?.parentNode,
    zoomToFit: find(svg, '.zoomToFit'),
  };
  const { width, height } = ws.getCachedParentSvgSize();
  const out = {};
  Object.entries(roots).forEach(([id, el]) => {
    const r = manager.getComponent(id).getBoundingRectangle();
    out[id] = {
      shown: el.style.display !== 'none',
      whole: r.top >= 0 && r.left >= 0 && r.bottom <= height && r.right <= width,
    };
  });
  return out;
}

// Blockly renders its own SVG/DOM with no roles or labels, so Testing Library
// queries cannot reach it; these three helpers are the only node access here.
function hostOf(container) {
  // eslint-disable-next-line testing-library/no-node-access
  return container.firstChild;
}
function find(root, selector) {
  // eslint-disable-next-line testing-library/no-node-access
  return root.querySelector(selector);
}
function findInDocument(selector) {
  // eslint-disable-next-line testing-library/no-node-access
  return document.querySelector(selector);
}

async function mountEditor(props = {}) {
  const onWorkspaceReady = vi.fn();
  const utils = render(<BlocklyWorkspace onWorkspaceReady={onWorkspaceReady} {...props} />);
  await waitFor(() => expect(onWorkspaceReady).toHaveBeenCalled());
  const ws = onWorkspaceReady.mock.calls[0][0];
  return { ...utils, ws, onWorkspaceReady };
}

function importFailures() {
  return warnSpy.mock.calls
    .map((args) => args.map(String).join(' '))
    .filter((line) => /import failed|unavailable/.test(line));
}

describe('BlocklyWorkspace — plugins load before inject', () => {
  it('constructs the backpack and zoom-to-fit (no "CSS already injected")', async () => {
    const { ws, unmount } = await mountEditor();
    // Let any REJECTED import land first. Without this the assertion is vacuous
    // against the pre-fix component, whose `initPlugins()` was fire-and-forget:
    // nothing had rejected yet by the time mounting returned.
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    expect(importFailures()).toEqual([]);
    const manager = ws.getComponentManager();
    expect(manager.getComponent('backpack')).toBeTruthy();
    expect(manager.getComponent('zoomToFit')).toBeTruthy();
    unmount();
  });

  // A FORWARD guard, not a regression test: pre-fix these two failed to import
  // anyway, so the minimap was absent for the wrong reason. Now that a plugin
  // import would succeed, re-adding either has to be a deliberate act.
  it('keeps the minimap and multiselect out', async () => {
    const { ws, unmount } = await mountEditor();
    expect(findInDocument('.blockly-minimap')).toBeNull();
    // Only the editor's own workspace became Blockly's main workspace.
    expect(Blockly.getMainWorkspace()).toBe(ws);
    unmount();
  });

  it('confirms the premise: Blockly refuses plugin CSS once a workspace exists', async () => {
    const { unmount } = await mountEditor();
    expect(() => Blockly.Css.register('.late-plugin{}')).toThrow(/CSS already injected/);
    unmount();
  });
});

describe('BlocklyWorkspace — corner controls', () => {
  it('lays out trash and zoom BEFORE the plugin controls', async () => {
    const { ws, unmount } = await mountEditor();
    const ids = ws.getComponentManager()
      .getComponents(Blockly.ComponentManager.Capability.POSITIONABLE, true)
      .map((c) => c.id);
    const at = (id) => ids.indexOf(id);
    expect(at('trashcan')).toBeGreaterThanOrEqual(0);
    expect(at('trashcan')).toBeLessThan(at('zoomControls'));
    expect(at('zoomControls')).toBeLessThan(at('backpack'));
    expect(at('backpack')).toBeLessThan(at('zoomToFit'));
    unmount();
  });

  it('keeps the backpack a drag target after the re-registration', async () => {
    const { ws, unmount } = await mountEditor();
    const manager = ws.getComponentManager();
    const { Capability } = Blockly.ComponentManager;
    expect(manager.hasCapability('backpack', Capability.DRAG_TARGET)).toBe(true);
    expect(manager.hasCapability('backpack', Capability.AUTOHIDEABLE)).toBe(true);
    expect(manager.hasCapability('zoomToFit', Capability.POSITIONABLE)).toBe(true);
    unmount();
  });

  it('shows all four controls whole in a roomy editor', async () => {
    const { ws, unmount } = await mountEditor();
    Object.entries(cornerState(ws)).forEach(([id, s]) => {
      expect({ id, ...s }).toEqual({ id, shown: true, whole: true });
    });
    unmount();
  });

  it('never draws a control half-clipped: whole, or hidden', async () => {
    const { ws, container, unmount } = await mountEditor();
    [600, 420, 376, 360, 330, 300, 270, 245, 220, 180, 140, 110].forEach((h) => {
      resizeEditor(container, 800, h);
      Object.entries(cornerState(ws)).forEach(([id, s]) => {
        expect({ id, h, halfClipped: s.shown && !s.whole }).toEqual({ id, h, halfClipped: false });
      });
    });
    unmount();
  });

  it('gives up the plugin extras before the core trash can + zoom cluster', async () => {
    const { ws, container, unmount } = await mountEditor();
    resizeEditor(container, 800, 300);
    const s = cornerState(ws);
    expect(s.trashcan.shown).toBe(true);
    expect(s.zoomControls.shown).toBe(true);
    expect(s.backpack.shown && s.zoomToFit.shown).toBe(false);
    unmount();
  });

  it('a hidden backpack stops taking drops, and takes them again once it fits', async () => {
    const { ws, container, unmount } = await mountEditor();
    const manager = ws.getComponentManager();
    const { DRAG_TARGET } = Blockly.ComponentManager.Capability;
    expect(manager.hasCapability('backpack', DRAG_TARGET)).toBe(true);
    const hiddenAt = [360, 330, 300, 270, 240, 200].find((h) => {
      resizeEditor(container, 800, h);
      return !cornerState(ws).backpack.shown;
    });
    expect(hiddenAt).toBeDefined();
    expect(manager.hasCapability('backpack', DRAG_TARGET)).toBe(false);
    resizeEditor(container, 800, 600);
    expect(cornerState(ws).backpack.shown).toBe(true);
    expect(manager.hasCapability('backpack', DRAG_TARGET)).toBe(true);
    unmount();
  });

  it('hides controls MONOTONICALLY as the editor shrinks', async () => {
    // The backpack is top-anchored while the rest stack from the bottom, so
    // without the priority cascade shrinking the editor made the backpack
    // vanish while zoom-to-fit popped back IN — a flicker while dragging the
    // dock divider. Each step's shown set must be a subset of the previous.
    const { ws, container, unmount } = await mountEditor();
    const steps = [600, 420, 380, 366, 340, 320, 300, 286, 260, 234, 200, 150].map((h) => {
      resizeEditor(container, 800, h);
      const shown = Object.entries(cornerState(ws))
        .filter(([, s]) => s.shown)
        .map(([id]) => id);
      return { h, shown };
    });
    // Shrinking must never bring a control BACK.
    const regained = steps
      .slice(1)
      .map((step, i) => ({ h: step.h, gained: step.shown.filter((id) => !steps[i].shown.includes(id)) }))
      .filter((step) => step.gained.length);
    expect(regained).toEqual([]);
    unmount();
  });

  it('gives a read-only preview no backpack (and no trash can)', async () => {
    const { ws, unmount } = await mountEditor({ readOnly: true });
    const manager = ws.getComponentManager();
    expect(manager.getComponent('backpack')).toBeUndefined();
    expect(manager.getComponent('trashcan')).toBeUndefined();
    unmount();
  });
});

describe('BlocklyWorkspace — sizing', () => {
  it('re-fits the SVG whenever its host box changes size', async () => {
    const { ws, container, unmount } = await mountEditor();
    const host = hostOf(container);
    const observer = observers.find((o) => o.targets.includes(host));
    expect(observer).toBeTruthy();

    resizeEditor(container, 640, 360);

    expect(ws.getCachedParentSvgSize()).toEqual(expect.objectContaining({ width: 640, height: 360 }));
    const svg = find(find(host, '.injectionDiv'), 'svg.blocklySvg');
    expect(svg.getAttribute('width')).toBe('640px');
    expect(svg.getAttribute('height')).toBe('360px');
    unmount();
  });

  it('puts no min-height on the host (it clipped the bottom controls)', async () => {
    const { container, unmount } = await mountEditor();
    expect(hostOf(container).className).not.toMatch(/min-h-/);
    unmount();
  });

  it('the host isolates Blockly\'s stacking context', async () => {
    // The toolbox is z-index 70 in the ROOT stacking context without it, and
    // paints over the Sammlung drawer, the Vormachen overlay and the CollisionModal.
    const { container, unmount } = await mountEditor();
    expect(hostOf(container).style.isolation).toBe('isolate');
    expect(hostOf(container).style.minHeight).toBe('');
    expect(hostOf(container).className).not.toMatch(/min-h-/);
    unmount();
  });

  it('disconnects the observer and disposes the workspace on unmount', async () => {
    const { container, onWorkspaceReady, unmount } = await mountEditor();
    const host = hostOf(container);
    const observer = observers.find((o) => o.targets.includes(host));
    unmount();
    expect(observer.disconnected).toBe(true);
    expect(onWorkspaceReady).toHaveBeenLastCalledWith(null);
    expect(findInDocument('.injectionDiv')).toBeNull();
  });

  it('never injects when unmounted before the plugin modules resolve', async () => {
    const onWorkspaceReady = vi.fn();
    const { unmount } = render(<BlocklyWorkspace onWorkspaceReady={onWorkspaceReady} />);
    unmount();
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(onWorkspaceReady).not.toHaveBeenCalled();
    expect(findInDocument('.injectionDiv')).toBeNull();
  });
});

describe('BlocklyWorkspace — Sammlung groups', () => {
  it('registers the four Sammlung category callbacks on a read-only workspace', async () => {
    const { ws, unmount } = await mountEditor({ readOnly: true });
    for (const key of Object.values(SAMMLUNG_CATEGORY_KEYS)) {
      expect(ws.getToolboxCategoryCallback(key)).toEqual(expect.any(Function));
    }
    unmount();
  });

  it('disposes the Sammlung listeners on unmount', async () => {
    const provider = createSammlungProvider();
    const unsubscribes = [];
    const realSubscribe = provider.subscribe;
    provider.subscribe = (fn) => {
      const off = vi.fn(realSubscribe(fn));
      unsubscribes.push(off);
      return off;
    };
    const { ws, unmount } = await mountEditor({ sammlungProvider: provider });
    // The toolbox groups and the reference warnings each follow the provider.
    expect(unsubscribes).toHaveLength(2);
    const store = getDestinationStore(ws);
    expect(store.listeners_.size).toBe(2);
    unmount();
    unsubscribes.forEach((off) => expect(off).toHaveBeenCalledTimes(1));
    expect(store.listeners_.size).toBe(0);
  });
});

describe('BlocklyWorkspace — input and text', () => {
  it('scrolls on the wheel and zooms only with Ctrl/⌘ (both options on)', async () => {
    const { ws, unmount } = await mountEditor();
    expect(ws.options.moveOptions.wheel).toBe(true);
    expect(ws.options.zoomOptions.wheel).toBe(true);
    unmount();
  });

  it('shows the backpack context menu in German, including the entries whose text is captured at registration', async () => {
    const { unmount } = await mountEditor();
    // All five strings the plugin sets in English.
    expect(Blockly.Msg.COPY_TO_BACKPACK).toBe(DE.BACKPACK_COPY);
    expect(Blockly.Msg.COPY_ALL_TO_BACKPACK).toBe(DE.BACKPACK_COPY_ALL);
    expect(Blockly.Msg.PASTE_ALL_FROM_BACKPACK).toBe(DE.BACKPACK_PASTE_ALL);
    expect(Blockly.Msg.REMOVE_FROM_BACKPACK).toBe(DE.BACKPACK_REMOVE);
    expect(Blockly.Msg.EMPTY_BACKPACK).toBe(DE.BACKPACK_EMPTY);
    // This plugin version registers three of the five by default
    // (`copyAllToBackpack` / `pasteAllToBackpack` are off). `remove_from_backpack`
    // reads Msg when the plugin REGISTERS it inside init(), so it fails if the
    // German assignment ever moves after the Backpack is constructed;
    // `copy_to_backpack` is a function evaluated when the menu opens.
    const registry = Blockly.ContextMenuRegistry.registry;
    expect(registry.getItem('remove_from_backpack').displayText).toBe(DE.BACKPACK_REMOVE);
    expect(registry.getItem('copy_to_backpack').displayText({ block: {} })).toBe(DE.BACKPACK_COPY);
    unmount();
  });

  it('shows the empty „Vorschläge" category in German', async () => {
    const { ws, unmount } = await mountEditor();
    const items = ws.getToolboxCategoryCallback('MOST_USED')(ws);
    expect(items).toEqual([expect.objectContaining({ text: DE.SUGGESTED_EMPTY })]);
    expect(JSON.stringify(items)).not.toMatch(/No blocks have been used/);
    unmount();
  });
});
