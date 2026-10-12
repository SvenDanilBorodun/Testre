/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The canvas's serialization must never hand the store a LIVE editor-plugin
// object, nor adopt a frozen one from it.
//
// `@blockly/suggested-blocks` serializes by returning its own
// `defaultJsonForBlockLookup` and `recentlyUsedBlocks` (not copies) and loads by
// adopting the objects it is given. WorkshopPage::setUnsavedJson dispatches the
// serialization into Redux, whose Immer deep-freezes it — and from then on
// every BLOCK_CREATE threw „object is not extensible" inside the plugin's
// listener, which aborted Blockly's event batch before
// BlocklyWorkspace::handleChange heard it: a paste, a duplicate or a Vormachen
// „Als Programm einfügen" left the document reading „Gespeichert". The same
// happened the other way round when the page remounted the editor from the
// store's (frozen) copy.
//
// Real Blockly, the real plugin modules (through BlocklyWorkspace's own
// loader), the REAL store and the page's own dispatch.

import React from 'react';
import { render, waitFor, act } from '@testing-library/react';
import * as Blockly from 'blockly/core';
import BlocklyWorkspace from '../BlocklyWorkspace';
import { store } from '../../../store/store';
import { setUnsavedBlocklyJson } from '../../../features/workshop/workshopSlice';

// jsdom lays nothing out; Blockly sizes itself from the injection div.
const realOffset = {
  width: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth'),
  height: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight'),
};
function isInjectionDiv(el) {
  return !!el.classList && el.classList.contains('injectionDiv');
}
class FakeResizeObserver {
  observe() {}

  disconnect() {}
}

// Every exception a change listener throws (the plugin's „object is not
// extensible" was one), recorded and re-thrown unchanged: Blockly's event
// delivery behaves exactly as without the spy.
let listenerErrors;
let fireSpy;

beforeEach(() => {
  listenerErrors = [];
  const realFire = Blockly.WorkspaceSvg.prototype.fireChangeListener;
  fireSpy = vi.spyOn(Blockly.WorkspaceSvg.prototype, 'fireChangeListener')
    .mockImplementation(function fire(event) {
      try {
        return realFire.call(this, event);
      } catch (e) {
        listenerErrors.push(String(e && e.message));
        throw e;
      }
    });
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true,
    get() { return isInjectionDiv(this) ? 800 : realOffset.width.get.call(this); },
  });
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get() { return isInjectionDiv(this) ? 600 : realOffset.height.get.call(this); },
  });
  globalThis.ResizeObserver = FakeResizeObserver;
  store.dispatch(setUnsavedBlocklyJson(null));
});
afterEach(() => {
  fireSpy.mockRestore();
  delete globalThis.ResizeObserver;
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', realOffset.width);
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', realOffset.height);
});

// Blockly delivers events via requestAnimationFrame(() => setTimeout(fireNow, 0)).
// The same chain registered after it runs after fireNow, however loaded the
// machine is (a fixed 30 ms wait lost the race to a stalled frame and missed
// the paste's create under the full suite); twice, so an event fired while the
// first batch is delivered is delivered too.
async function flushEvents() {
  await act(async () => {
    for (let i = 0; i < 2; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => { requestAnimationFrame(() => { setTimeout(r, 0); }); });
    }
  });
}

// WorkshopPage::handleEditorChange, reduced to the two halves that matter:
// the store dispatch (setUnsavedJson) and „a non-UI event is an edit".
async function mountEditor(initialJson = null) {
  const onWorkspaceReady = vi.fn();
  const edits = [];
  const onChange = (json, event) => {
    store.dispatch(setUnsavedBlocklyJson(json || null));
    if (event && event.isUiEvent === true) return;
    edits.push(event ? event.type : 'unknown');
  };
  const utils = render(
    <BlocklyWorkspace initialJson={initialJson} onChange={onChange} onWorkspaceReady={onWorkspaceReady} />,
  );
  await waitFor(() => expect(onWorkspaceReady).toHaveBeenCalled());
  const ws = onWorkspaceReady.mock.calls[0][0];
  await flushEvents();
  edits.length = 0;
  return { ...utils, ws, edits };
}

function storedBlockCount() {
  const json = store.getState().workshop.unsavedBlocklyJson;
  return json && json.blocks ? json.blocks.blocks.length : 0;
}

function recentlyUsedTypes(ws) {
  return ws.getToolboxCategoryCallback('RECENTLY_USED')(ws)
    .filter((item) => item.kind === 'BLOCK')
    .map((item) => item.type);
}

// The three ways a block arrives that verifier A found deaf: Vormachen's
// „Als Programm einfügen" (teach/insertProgram.js appends), paste, and the
// context menu's „Duplizieren".
function appendBlock(ws, type) {
  return Blockly.serialization.blocks.append({ type, x: 40, y: 40 }, ws, { recordUndo: true });
}
function pasteCopy(ws, block) {
  return Blockly.clipboard.paste(block.toCopyData(), ws);
}
function duplicate(block) {
  Blockly.ContextMenuRegistry.registry.getItem('blockDuplicate').callback({ block });
}

describe('BlocklyWorkspace — the store never holds (or hands back) live plugin state', () => {
  it('a paste, a duplicate and an insertion after a store dispatch are all reported as edits', async () => {
    const { ws, edits, unmount } = await mountEditor();
    // The first creation reaches the page and is dispatched into the store,
    // which freezes whatever the serialization holds.
    const first = appendBlock(ws, 'controls_if');
    await flushEvents();
    expect(listenerErrors).toEqual([]);
    expect(edits).toContain(Blockly.Events.BLOCK_CREATE);
    expect(Object.isFrozen(store.getState().workshop.unsavedBlocklyJson)).toBe(true);
    expect(storedBlockCount()).toBe(1);

    edits.length = 0;
    act(() => { pasteCopy(ws, first); });
    await flushEvents();
    expect(listenerErrors).toEqual([]);
    expect(edits).toContain(Blockly.Events.BLOCK_CREATE);
    expect(storedBlockCount()).toBe(2);

    edits.length = 0;
    act(() => { duplicate(first); });
    await flushEvents();
    expect(listenerErrors).toEqual([]);
    expect(edits).toContain(Blockly.Events.BLOCK_CREATE);
    expect(storedBlockCount()).toBe(3);

    edits.length = 0;
    act(() => { appendBlock(ws, 'math_number'); });
    await flushEvents();
    expect(listenerErrors).toEqual([]);
    expect(edits).toContain(Blockly.Events.BLOCK_CREATE);
    expect(storedBlockCount()).toBe(4);

    // „Vorschläge" keeps recording what the student used.
    expect(recentlyUsedTypes(ws)).toEqual(['math_number', 'controls_if']);
    unmount();
  });

  it('an editor remounted from the store copy keeps reporting new blocks', async () => {
    // What WorkshopPage hands BlocklyWorkspace on re-activation, after an
    // autosave restore or a version restore: the store's (frozen) document,
    // here with the plugin's state as the full serializer writes it.
    store.dispatch(setUnsavedBlocklyJson({
      blocks: { languageVersion: 0, blocks: [{ type: 'controls_if', x: 0, y: 0 }] },
      'suggested-blocks': {
        defaultJsonForBlockLookup: { controls_if: { type: 'controls_if' } },
        recentlyUsedBlocks: ['controls_if'],
      },
    }));
    const frozen = store.getState().workshop.unsavedBlocklyJson;
    expect(Object.isFrozen(frozen['suggested-blocks'].recentlyUsedBlocks)).toBe(true);

    const { ws, edits, unmount } = await mountEditor(frozen);
    const [loaded] = ws.getTopBlocks(false);
    act(() => { pasteCopy(ws, loaded); });
    await flushEvents();
    expect(listenerErrors).toEqual([]);
    expect(edits).toContain(Blockly.Events.BLOCK_CREATE);
    expect(storedBlockCount()).toBe(2);

    act(() => { appendBlock(ws, 'math_number'); });
    await flushEvents();
    expect(listenerErrors).toEqual([]);
    expect(storedBlockCount()).toBe(3);
    expect(recentlyUsedTypes(ws)).toEqual(['math_number', 'controls_if']);
    // The store's copy is untouched by the editor's later edits.
    expect(frozen['suggested-blocks'].recentlyUsedBlocks).toEqual(['controls_if']);
    unmount();
  });
});
