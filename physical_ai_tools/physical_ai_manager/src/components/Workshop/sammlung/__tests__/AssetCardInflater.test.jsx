/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Sammlung card spike (spec WP5 Step 0): asset cards as custom flyout items
// in the REAL Blockly 12 flyout under jsdom. Every criterion (a)–(f) is a named
// test; cards ship only while all of them hold (else Plan B, with these tests
// rewritten against the Plan B items).

import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { buildToolbox, SAMMLUNG_TOOLBOX_IDS } from '../../blocks/toolbox';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { DE } from '../../blocks/messages_de';
import { createSammlungProvider } from '../provider';
import { registerSammlungCategories, SAMMLUNG_FLYOUT_MODE } from '../toolboxCategories';
import {
  ASSET_CARD_FLYOUT_TYPE,
  AssetCard,
  AssetCardInflater,
  highlightAssetFor,
  layoutChips,
  registerAssetCardInflater,
} from '../AssetCardInflater';
import { registerDestinationSerializer } from '../destinationStore';

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

const RECORDING = {
  id: '11111111-aaaa-4bbb-8ccc-000000000001',
  name: 'Greifen links',
  point_count: 105,
  duration_s: 4.2,
  fps: 25,
  robot_profile: 'omx_f',
  created_at: '2026-09-13T10:42:00Z',
};

// jsdom lays nothing out; Blockly sizes from the injection div and measures
// text with getBBox / getComputedTextLength, which jsdom lacks.
const realOffset = {
  width: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth'),
  height: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight'),
};

let host;
let ws;
let provider;
let dispatched;
let disposeSammlung;

beforeAll(() => {
  Blockly.setLocale(De);
  registerTrajectoryBlocks();
  registerDestinationBlocks();
  registerDestinationSerializer();
  registerAssetCardInflater();
});

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true, get() { return 800; },
  });
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true, get() { return 600; },
  });
  if (!SVGElement.prototype.getBBox) {
    SVGElement.prototype.getBBox = () => ({ x: 0, y: 0, width: 40, height: 16 });
  }
  if (!SVGElement.prototype.getComputedTextLength) {
    SVGElement.prototype.getComputedTextLength = () => 40;
  }
  host = document.createElement('div');
  document.body.appendChild(host);
  ws = Blockly.inject(host, { toolbox: buildToolbox() });
  dispatched = [];
  provider = createSammlungProvider({
    capabilities: { hardware: true, preview: true },
    robotType: 'omx_f',
    trajectories: { status: 'ready', items: [RECORDING] },
  });
  provider.setActionHandler((a) => dispatched.push(a));
  disposeSammlung = registerSammlungCategories(ws, { current: provider });
});

afterEach(() => {
  Blockly.Touch.clearTouchIdentifier();
  vi.restoreAllMocks();
  disposeSammlung();
  ws.dispose();
  host.remove();
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', realOffset.width);
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', realOffset.height);
});

function openCategory(id) {
  const toolbox = ws.getToolbox();
  toolbox.setSelectedItem(toolbox.getToolboxItemById(id));
  return ws.getFlyout();
}

function cardsOf(flyout) {
  return flyout.getContents()
    .filter((item) => item.getType() === ASSET_CARD_FLYOUT_TYPE)
    .map((item) => item.getElement());
}

function pointer(type, pointerId) {
  return new PointerEvent(type, { bubbles: true, cancelable: true, pointerId, pointerType: 'touch' });
}

function cardState(overrides = {}) {
  return {
    kind: ASSET_CARD_FLYOUT_TYPE,
    assetKind: 'pin',
    assetId: 'd_1',
    assetName: 'Ablage',
    title: 'Ablage',
    meta: 'x 182 · y −64 mm · Kamera',
    chips: [{ text: 'nicht benutzt', level: 'ok' }],
    canPreview: true,
    colour: '#f59e0b',
    gap: 4,
    ...overrides,
  };
}

describe('Sammlung asset cards in the real flyout (spike)', () => {
  it('ships as cards', () => {
    expect(SAMMLUNG_FLYOUT_MODE).toBe('cards');
  });

  it('(a) a Sammlung callback opens in the real flyout with a card item', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    expect(flyout.isVisible()).toBe(true);
    const types = flyout.getContents().map((item) => item.getType());
    expect(types).toContain(ASSET_CARD_FLYOUT_TYPE);
    const [card] = cardsOf(flyout);
    expect(card).toBeInstanceOf(AssetCard);
    expect(card.getSvgRoot().getAttribute('aria-label'))
      .toBe(`Greifen links — 4,2 s · 105 Punkte`);
  });

  it('(b) the focus manager focuses a card and the flyout workspace finds it by id', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const [card] = cardsOf(flyout);
    Blockly.getFocusManager().focusNode(card);
    expect(document.activeElement).toBe(card.getFocusableElement());
    expect(flyout.getWorkspace().lookUpFocusableNode(card.getFocusableElement().id)).toBe(card);
  });

  it('(c) the flyout navigator steps between a card and its prefilled block', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const contents = flyout.getContents();
    const [card] = cardsOf(flyout);
    const cardIndex = contents.findIndex((item) => item.getElement() === card);
    // Raw policies return the FlyoutSeparator Blockly puts after every item;
    // only the Navigator skips separators, so the contract is asserted through it.
    expect(contents[cardIndex + 1].getType()).toBe('sep');
    const block = contents.slice(cardIndex + 1).map((item) => item.getElement())
      .find((el) => el instanceof Blockly.BlockSvg);
    expect(block.type).toBe('edubotics_replay_trajectory');
    expect(block.getFieldValue('NAME')).toBe('Greifen links');
    const navigator = flyout.getWorkspace().getNavigator();
    expect(navigator.getNextSibling(card)).toBe(block);
    expect(navigator.getPreviousSibling(block)).toBe(card);
    // The item before the card is the „1 von 16" status label.
    const status = contents[0].getElement();
    expect(status.getButtonText()).toBe('1 von 16');
    expect(navigator.getPreviousSibling(card)).toBe(status);
    expect(navigator.getFirstChild(card)).toBeNull();
    expect(navigator.getParent(card)).toBeNull();
  });

  it('(d) ▶ dispatches one preview without a flyout gesture; the body dispatches manage', async () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const [card] = cardsOf(flyout);
    const gestureSpy = vi.spyOn(ws, 'getGesture');
    const play = card.getSvgRoot().querySelector('[role="button"][aria-label="Im Simulator ansehen"]');
    play.dispatchEvent(pointer('pointerdown', 7));
    play.dispatchEvent(pointer('pointerup', 7));
    await flushEvents();
    expect(dispatched).toEqual([{
      type: 'preview',
      asset: { kind: 'recording', id: RECORDING.id, name: 'Greifen links', robotProfile: 'omx_f' },
    }]);
    expect(gestureSpy).not.toHaveBeenCalled();
    // A body click: pointerdown starts a flyout gesture (the flyout scrolls by
    // drag, like a FlyoutButton), pointerup cancels it and opens „Verwalten".
    const body = card.getSvgRoot().querySelector('rect');
    body.dispatchEvent(pointer('pointerdown', 7));
    expect(gestureSpy).toHaveBeenCalled();
    body.dispatchEvent(pointer('pointerup', 7));
    await flushEvents();
    expect(dispatched[1]).toEqual({ type: 'manage', tab: 'aufnahmen', focusId: 'Greifen links' });
    const manage = card.getSvgRoot().querySelector('[role="button"][aria-label="Verwalten"]');
    manage.dispatchEvent(pointer('pointerdown', 7));
    manage.dispatchEvent(pointer('pointerup', 7));
    expect(dispatched).toHaveLength(3);
    expect(dispatched[2]).toEqual({ type: 'manage', tab: 'aufnahmen', focusId: 'Greifen links' });
  });

  it('(e) re-populating disposes every card and its bindings; hide and clearSelection keep them', () => {
    const toolbox = ws.getToolbox();
    let flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    let [card] = cardsOf(flyout);
    let dispose = vi.spyOn(card, 'dispose');
    flyout.hide();
    toolbox.clearSelection();
    expect(dispose).not.toHaveBeenCalled();

    const unbind = vi.spyOn(Blockly.browserEvents, 'unbind');
    flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    expect(dispose).toHaveBeenCalledTimes(1);
    expect(card.getSvgRoot().isConnected).toBe(false);
    [card] = cardsOf(flyout);
    const bindings = card.bindings.slice();
    expect(bindings).toHaveLength(6);
    dispose = vi.spyOn(card, 'dispose');
    unbind.mockClear();
    ws.refreshToolboxSelection();
    expect(dispose).toHaveBeenCalledTimes(1);
    for (const data of bindings) expect(unbind).toHaveBeenCalledWith(data);
    unbind.mockRestore();
  });

  it('(e) a card registers nothing outside its own SVG subtree', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const docAdd = vi.spyOn(document, 'addEventListener');
    const winAdd = vi.spyOn(window, 'addEventListener');
    const subscribe = vi.spyOn(provider, 'subscribe');
    const bind = vi.spyOn(Blockly.browserEvents, 'conditionalBind');
    const item = new AssetCardInflater().load(cardState(), flyout);
    const card = item.getElement();
    expect(docAdd).not.toHaveBeenCalled();
    expect(winAdd).not.toHaveBeenCalled();
    expect(subscribe).not.toHaveBeenCalled();
    for (const [node] of bind.mock.calls) expect(card.getSvgRoot().contains(node)).toBe(true);
    card.dispose();
    [docAdd, winAdd, subscribe, bind].forEach((spy) => spy.mockRestore());
  });

  it('(f) a ▶ tap does not capture the touch identifier', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const [card] = cardsOf(flyout);
    const gestureSpy = vi.spyOn(ws, 'getGesture').mockReturnValue(null);
    const play = card.getSvgRoot().querySelector('[role="button"][aria-label="Im Simulator ansehen"]');
    play.dispatchEvent(pointer('pointerdown', 31));
    play.dispatchEvent(pointer('pointerup', 31));
    expect(dispatched.map((a) => a.type)).toEqual(['preview']);
    ws.getSvgGroup().dispatchEvent(pointer('pointerdown', 32));
    expect(gestureSpy).toHaveBeenCalledTimes(1);

    // Control: the naive binding (default capture + stopPropagation) leaves
    // id 41 held, and the editor then ignores the next pointer.
    Blockly.Touch.clearTouchIdentifier();
    gestureSpy.mockClear();
    const naive = Blockly.utils.dom.createSvgElement('g', {}, card.getSvgRoot());
    const data = Blockly.browserEvents.conditionalBind(naive, 'pointerdown', null, (e) => e.stopPropagation());
    naive.dispatchEvent(pointer('pointerdown', 41));
    ws.getSvgGroup().dispatchEvent(pointer('pointerdown', 42));
    expect(gestureSpy).not.toHaveBeenCalled();
    Blockly.browserEvents.unbind(data);
  });
});

describe('AssetCardInflater', () => {
  it('is registered under the card type', () => {
    expect(Blockly.registry.getClass(Blockly.registry.Type.FLYOUT_INFLATER, ASSET_CARD_FLYOUT_TYPE))
      .toBe(AssetCardInflater);
    expect(() => registerAssetCardInflater()).not.toThrow();
  });

  it('load returns a FlyoutItem of the card type; gapForItem honours state.gap; disposeItem removes the SVG', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const inflater = new AssetCardInflater();
    const item = inflater.load(cardState(), flyout);
    expect(item).toBeInstanceOf(Blockly.FlyoutItem);
    expect(item.getType()).toBe(ASSET_CARD_FLYOUT_TYPE);
    expect(inflater.getType()).toBe(ASSET_CARD_FLYOUT_TYPE);
    expect(inflater.gapForItem({ gap: 4 }, 24)).toBe(4);
    expect(inflater.gapForItem({}, 24)).toBe(24);
    const root = item.getElement().getSvgRoot();
    expect(root.isConnected).toBe(true);
    inflater.disposeItem(item);
    expect(root.isConnected).toBe(false);
  });

  it('hovering a Ziel/Position/Variable card highlights its marker; leaving clears it', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const inflater = new AssetCardInflater();
    const pin = inflater.load(cardState(), flyout).getElement();
    dispatched.length = 0;
    pin.getSvgRoot().dispatchEvent(new PointerEvent('pointerenter', { pointerId: 3 }));
    expect(dispatched).toEqual([{ type: 'highlight', asset: { kind: 'pin', id: 'd_1' } }]);
    pin.getSvgRoot().dispatchEvent(new PointerEvent('pointerleave', { pointerId: 3 }));
    expect(dispatched[1]).toEqual({ type: 'highlight', asset: null });
    // A variable marker is keyed by NAME (markers.js `var:<name>`).
    const variable = inflater.load(cardState({ assetKind: 'variable', assetId: 'Xy9=', assetName: 'Punkt' }), flyout)
      .getElement();
    variable.getSvgRoot().dispatchEvent(new PointerEvent('pointerenter', { pointerId: 3 }));
    expect(dispatched[2]).toEqual({ type: 'highlight', asset: { kind: 'variable', id: 'var:Punkt' } });
    // Disposed under the pointer (a flyout re-populate): the highlight is dropped.
    variable.dispose();
    expect(dispatched[3]).toEqual({ type: 'highlight', asset: null });
    // A recording card has no marker: hover dispatches nothing.
    const recording = inflater.load(cardState({ assetKind: 'recording', assetName: 'Greifen' }), flyout).getElement();
    recording.getSvgRoot().dispatchEvent(new PointerEvent('pointerenter', { pointerId: 3 }));
    expect(dispatched).toHaveLength(4);
    pin.dispose();
    recording.dispose();
    expect(dispatched).toHaveLength(4);
    expect(highlightAssetFor({ assetKind: 'programPin', assetId: 'b1' })).toBeNull();
  });

  it('draws no ▶ when the asset cannot be previewed, and sizes by chips', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const card = new AssetCardInflater().load(cardState({ canPreview: false, chips: [] }), flyout).getElement();
    expect(card.getSvgRoot().querySelector(`[aria-label="${DE.PREVIEW_START}"]`)).toBeNull();
    expect(card.getSvgRoot().querySelector(`[aria-label="${DE.CARD_MANAGE}"]`)).not.toBeNull();
    expect(card.getBoundingRectangle().getHeight()).toBe(38);
    card.moveBy(10, 20);
    expect(card.getBoundingRectangle()).toMatchObject({ top: 20, left: 10, right: 250, bottom: 58 });
    card.dispose();
  });

  it('wraps chips to another row rather than dropping the last (most serious) ones', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const chips = [
      { text: '3× benutzt', level: 'ok' },
      { text: '3 Versionen', level: 'warn' },
      { text: 'anderer Roboter', level: 'bad' },
      { text: 'im Simulator abgelehnt', level: 'bad' },
    ];
    const card = new AssetCardInflater().load(cardState({ chips }), flyout).getElement();
    const texts = Array.from(card.getSvgRoot().querySelectorAll('text')).map((t) => t.textContent);
    for (const c of chips) expect(texts).toContain(c.text);
    const rows = new Set(layoutChips(chips).map((p) => p.row));
    expect(rows.size).toBeGreaterThan(1);
    expect(card.getBoundingRectangle().getHeight()).toBe(38 + 20 * rows.size);
    for (const p of layoutChips(chips)) expect(p.x + p.width).toBeLessThanOrEqual(232);
    card.dispose();
  });
});
