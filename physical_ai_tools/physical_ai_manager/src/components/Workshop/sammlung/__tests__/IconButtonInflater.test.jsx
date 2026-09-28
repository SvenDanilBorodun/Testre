/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Icon flyout buttons in the REAL Blockly 12 flyout under jsdom — the same
// criteria (a)–(f) the asset cards are held to (AssetCardInflater.test.jsx),
// plus the decoration itself: the button is a real FlyoutButton, 18 px wider,
// its text shifted by 18 px, with an explicitly painted icon group holding the
// icon's own shapes. If Blockly changes FlyoutButton's DOM, these fail loudly
// (docs/KNOWN-ISSUES.md, R4).

import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import { buildToolbox, SAMMLUNG_TOOLBOX_IDS } from '../../blocks/toolbox';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { DE } from '../../blocks/messages_de';
import { createSammlungProvider } from '../provider';
import { registerSammlungCategories, SAMMLUNG_BUTTON_KEYS } from '../toolboxCategories';
import { registerAssetCardInflater } from '../AssetCardInflater';
import { registerDestinationSerializer } from '../destinationStore';
import {
  __resetDecorateWarningForTests,
  decorateFlyoutButton,
  fontSizePx,
  ICON_BUTTON_FLYOUT_TYPE,
  ICON_BUTTON_ICON_GAP,
  ICON_BUTTON_ICON_SIZE,
  iconMetricsForFont,
  IconButtonInflater,
  registerIconButtonInflater,
} from '../IconButtonInflater';
import { iconNodes } from '../../../icons/svg';

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
  registerIconButtonInflater();
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
    capabilities: { hardware: true, teach: true, drawer: true, preview: true, pinCamera: true },
    robotType: 'omx_f',
    trajectories: { status: 'ready', items: [] },
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

function iconButtonsOf(flyout) {
  return flyout.getContents()
    .filter((item) => item.getType() === ICON_BUTTON_FLYOUT_TYPE)
    .map((item) => item.getElement());
}

function pointer(type, pointerId) {
  return new PointerEvent(type, { bubbles: true, cancelable: true, pointerId, pointerType: 'touch' });
}

const iconGroupOf = (button) => button.getSvgRoot().querySelector('g[data-icon]');

describe('icon flyout buttons in the real flyout', () => {
  it('(a) the Aufnahmen flyout holds „Bewegung vormachen" as an icon button: a real FlyoutButton', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const [button] = iconButtonsOf(flyout);
    expect(button).toBeInstanceOf(Blockly.FlyoutButton);
    expect(button.getButtonText()).toBe(DE.FLY_TEACH_RECORDING);
    expect(button.callbackKey).toBe(SAMMLUNG_BUTTON_KEYS.TEACH_RECORDING);
    expect(iconGroupOf(button).getAttribute('data-icon')).toBe('record');
  });

  it('(b) the focus manager focuses it and the flyout workspace finds it by id', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const [button] = iconButtonsOf(flyout);
    Blockly.getFocusManager().focusNode(button);
    expect(document.activeElement).toBe(button.getFocusableElement());
    expect(flyout.getWorkspace().lookUpFocusableNode(button.getFocusableElement().id)).toBe(button);
  });

  it('(c) the flyout navigator steps from the status label to it and on to „Alle verwalten …"', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const contents = flyout.getContents().map((item) => item.getElement());
    const [button] = iconButtonsOf(flyout);
    const status = contents[0];
    const manage = contents.find((el) => el instanceof Blockly.FlyoutButton
      && el.getButtonText() === DE.FLY_MANAGE);
    const navigator = flyout.getWorkspace().getNavigator();
    expect(navigator.getPreviousSibling(button)).toBe(status);
    expect(navigator.getNextSibling(button)).toBe(manage);
    expect(navigator.getPreviousSibling(manage)).toBe(button);
  });

  it('(d) one tap dispatches the table\'s action exactly once', async () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.ZIELE);
    const buttons = iconButtonsOf(flyout);
    expect(buttons.map((b) => b.getButtonText())).toEqual([DE.FLY_TEACH_ZIEL, DE.FLY_PIN_CAMERA]);
    const root = buttons[0].getSvgRoot();
    root.dispatchEvent(pointer('pointerdown', 7));
    root.dispatchEvent(pointer('pointerup', 7));
    await flushEvents();
    expect(dispatched).toEqual([{ type: 'teach', kind: 'ziel' }]);
    // A tap on the icon itself is a tap on the button.
    const icon = iconGroupOf(buttons[1]).firstChild;
    icon.dispatchEvent(pointer('pointerdown', 8));
    icon.dispatchEvent(pointer('pointerup', 8));
    await flushEvents();
    expect(dispatched).toEqual([{ type: 'teach', kind: 'ziel' }, { type: 'pinCamera' }]);
  });

  it('(e) re-populating disposes each button once; hide and clearSelection keep it', () => {
    const toolbox = ws.getToolbox();
    let flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    let [button] = iconButtonsOf(flyout);
    let dispose = vi.spyOn(button, 'dispose');
    flyout.hide();
    toolbox.clearSelection();
    expect(dispose).not.toHaveBeenCalled();
    flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    expect(dispose).toHaveBeenCalledTimes(1);
    expect(button.getSvgRoot().isConnected).toBe(false);
    [button] = iconButtonsOf(flyout);
    dispose = vi.spyOn(button, 'dispose');
    ws.refreshToolboxSelection();
    expect(dispose).toHaveBeenCalledTimes(1);
  });

  it('(e) the decoration registers nothing: no listener, no subscription', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const docAdd = vi.spyOn(document, 'addEventListener');
    const winAdd = vi.spyOn(window, 'addEventListener');
    const subscribe = vi.spyOn(provider, 'subscribe');
    const bind = vi.spyOn(Blockly.browserEvents, 'conditionalBind');
    const item = new IconButtonInflater().load(
      { kind: ICON_BUTTON_FLYOUT_TYPE, text: 'X', callbackkey: SAMMLUNG_BUTTON_KEYS.TEACH_POSE, icon: 'pose' }, flyout,
    );
    const button = item.getElement();
    expect(docAdd).not.toHaveBeenCalled();
    expect(winAdd).not.toHaveBeenCalled();
    expect(subscribe).not.toHaveBeenCalled();
    // No binding of its own (FlyoutButton binds its two pointer handlers through
    // its own module import, which this spy does not see).
    expect(bind).not.toHaveBeenCalled();
    expect(button.getSvgRoot().querySelectorAll('g[data-icon]')).toHaveLength(1);
    button.dispose();
    [docAdd, winAdd, subscribe, bind].forEach((spy) => spy.mockRestore());
  });

  it('(f) after a tap, a second pointer still reaches the editor (no touch identifier held)', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.AUFNAHMEN);
    const [button] = iconButtonsOf(flyout);
    const root = button.getSvgRoot();
    root.dispatchEvent(pointer('pointerdown', 31));
    root.dispatchEvent(pointer('pointerup', 31));
    expect(dispatched.map((a) => a.type)).toEqual(['teach']);
    const gestureSpy = vi.spyOn(ws, 'getGesture');
    ws.getSvgGroup().dispatchEvent(pointer('pointerdown', 32));
    expect(gestureSpy).toHaveBeenCalledTimes(1);
  });
});

describe('the decoration', () => {
  const SHIFT = ICON_BUTTON_ICON_SIZE + ICON_BUTTON_ICON_GAP;

  function pair(flyout, text = DE.FLY_TEACH_POSE, icon = 'pose') {
    const plain = new Blockly.ButtonFlyoutInflater()
      .load({ kind: 'button', text, callbackkey: 'K' }, flyout).getElement();
    const decorated = new IconButtonInflater()
      .load({ kind: ICON_BUTTON_FLYOUT_TYPE, text, callbackkey: 'K', icon }, flyout).getElement();
    return { plain, decorated };
  }

  it('makes the button 18 px wider, both rects with it, and shifts the centred text by 18 px', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.POSITIONEN);
    const { plain, decorated } = pair(flyout);
    expect(SHIFT).toBe(18);
    expect(decorated.width).toBe(plain.width + SHIFT);
    expect(decorated.height).toBe(plain.height);
    expect(decorated.getBoundingRectangle().getWidth()).toBe(plain.width + SHIFT);
    const rects = decorated.getSvgRoot().querySelectorAll(':scope > rect');
    expect(rects).toHaveLength(2);
    for (const r of rects) expect(Number(r.getAttribute('width'))).toBe(decorated.width);
    const textX = (b) => Number(b.getSvgRoot().querySelector(':scope > text').getAttribute('x'));
    expect(textX(decorated)).toBe(textX(plain) + SHIFT);
    plain.dispose();
    decorated.dispose();
  });

  it('draws the icon\'s own shapes, painted explicitly (no fill leaking in), vertically centred', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.POSITIONEN);
    const { plain, decorated } = pair(flyout);
    const g = iconGroupOf(decorated);
    expect(g.getAttribute('data-icon')).toBe('pose');
    expect(g.getAttribute('aria-hidden')).toBe('true');
    expect(g.getAttribute('fill')).toBe('none');
    expect(g.getAttribute('stroke')).toMatch(/\S/);
    expect(g.getAttribute('transform')).toBe(
      `translate(${Blockly.FlyoutButton.TEXT_MARGIN_X},${(decorated.height - 14) / 2}) scale(${14 / 24})`,
    );
    const shapes = Array.from(g.children).map((el) => el.tagName.toLowerCase());
    expect(shapes).toEqual(iconNodes('pose').map((n) => n.tag));
    expect(iconGroupOf(plain)).toBeNull();
    plain.dispose();
    decorated.dispose();
  });

  it('an unknown or missing icon leaves a plain button (still of the icon type)', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.POSITIONEN);
    for (const icon of [undefined, 'no-such-icon']) {
      const item = new IconButtonInflater()
        .load({ kind: ICON_BUTTON_FLYOUT_TYPE, text: 'X', callbackkey: 'K', icon }, flyout);
      expect(item.getType()).toBe(ICON_BUTTON_FLYOUT_TYPE);
      expect(iconGroupOf(item.getElement())).toBeNull();
      item.getElement().dispose();
    }
  });

  it('fails loudly when FlyoutButton\'s DOM is not the shape it decorates', () => {
    const bogus = {
      getSvgRoot: () => Blockly.utils.dom.createSvgElement('g', {}, null),
      getWorkspace: () => ws,
      width: 10,
      height: 10,
    };
    expect(() => decorateFlyoutButton(bogus, 'play')).toThrow(/FlyoutButton DOM changed/);
  });

  it('the inflater delegates gap and disposal to Blockly\'s own, and registers once', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.POSITIONEN);
    const inflater = new IconButtonInflater();
    expect(inflater.getType()).toBe(ICON_BUTTON_FLYOUT_TYPE);
    expect(inflater.gapForItem({}, 24)).toBe(24);
    const item = inflater.load({ kind: ICON_BUTTON_FLYOUT_TYPE, text: 'X', callbackkey: 'K', icon: 'play' }, flyout);
    const root = item.getElement().getSvgRoot();
    expect(root.isConnected).toBe(true);
    inflater.disposeItem(item);
    expect(root.isConnected).toBe(false);
    expect(Blockly.registry.getClass(Blockly.registry.Type.FLYOUT_INFLATER, ICON_BUTTON_FLYOUT_TYPE))
      .toBe(IconButtonInflater);
    expect(() => registerIconButtonInflater()).not.toThrow();
  });

  it('builds no Blockly object before its first load (a page test mocks blockly/core minimally)', () => {
    const inflater = new IconButtonInflater();
    expect(inflater.base).toBeNull();
  });
});

describe('the icon follows the button text\'s size (review round 1, B7)', () => {
  it('reads a CSS font size in px or pt', () => {
    expect(fontSizePx('11pt')).toBeCloseTo(14.667, 3);
    expect(fontSizePx('21.3333px')).toBeCloseTo(21.333, 3);
    expect(fontSizePx(' 16 ')).toBe(16);
    for (const bad of ['', null, undefined, 'medium', '0px', '-3px']) expect(fontSizePx(bad)).toBeNull();
  });

  it('is 14 + 4 at Blockly\'s 11 pt, about 20 + 6 at the high-contrast theme\'s 16 pt, clamped, 14 + 4 when unknown', () => {
    expect(iconMetricsForFont((11 * 4) / 3)).toEqual({ size: 14, gap: 4 });
    expect(iconMetricsForFont((16 * 4) / 3)).toEqual({ size: 20, gap: 6 });
    expect(iconMetricsForFont(4)).toEqual({ size: 12, gap: 3 });
    expect(iconMetricsForFont(200)).toEqual({ size: 32, gap: 9 });
    for (const unknown of [null, undefined, NaN, 0, -1]) {
      expect(iconMetricsForFont(unknown)).toEqual({ size: ICON_BUTTON_ICON_SIZE, gap: ICON_BUTTON_ICON_GAP });
    }
  });

  it('a button whose text renders at 16 pt gets a 20 px icon and room for it', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.POSITIONEN);
    const button = new Blockly.ButtonFlyoutInflater()
      .load({ kind: 'button', text: DE.FLY_TEACH_POSE, callbackkey: 'K' }, flyout).getElement();
    const text = button.getSvgRoot().querySelector(':scope > text');
    text.style.fontSize = '16pt';
    const width = button.width;
    const x = Number(text.getAttribute('x'));
    const g = decorateFlyoutButton(button, 'pose');
    expect(g.getAttribute('transform')).toBe(
      `translate(${Blockly.FlyoutButton.TEXT_MARGIN_X},${(button.height - 20) / 2}) scale(${20 / 24})`,
    );
    expect(button.width).toBe(width + 26);
    expect(Number(text.getAttribute('x'))).toBe(x + 26);
    button.dispose();
  });
});

describe('a FlyoutButton whose DOM changed keeps the category working (review round 1, A5)', () => {
  beforeEach(() => __resetDecorateWarningForTests());

  it('load() warns ONCE and returns the plain button, still of the icon type and disposable', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.POSITIONEN);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const real = new Blockly.ButtonFlyoutInflater();
    const inflater = new IconButtonInflater();
    // A "future Blockly": the button grows a second <text>.
    inflater.base = {
      load: (state, fl) => {
        const item = real.load(state, fl);
        Blockly.utils.dom.createSvgElement('text', {}, item.getElement().getSvgRoot());
        return item;
      },
      gapForItem: (state, gap) => real.gapForItem(state, gap),
      disposeItem: (item) => real.disposeItem(item),
    };
    const plainWidth = real.load({ kind: 'button', text: 'X', callbackkey: 'K' }, flyout).getElement().width;
    const items = [1, 2].map(() => inflater.load(
      { kind: ICON_BUTTON_FLYOUT_TYPE, text: 'X', callbackkey: 'K', icon: 'pose' }, flyout,
    ));
    for (const item of items) {
      expect(item.getType()).toBe(ICON_BUTTON_FLYOUT_TYPE);
      const button = item.getElement();
      expect(button).toBeInstanceOf(Blockly.FlyoutButton);
      expect(iconGroupOf(button)).toBeNull();
      expect(button.width).toBe(plainWidth);
    }
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toMatch(/IconButtonInflater/);
    const root = items[0].getElement().getSvgRoot();
    inflater.disposeItem(items[0]);
    expect(root.isConnected).toBe(false);
    inflater.disposeItem(items[1]);
  });

  it('an icon that fails half-drawn is removed again and the button is left exactly as Blockly built it', () => {
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.POSITIONEN);
    const button = new Blockly.ButtonFlyoutInflater()
      .load({ kind: 'button', text: 'X', callbackkey: 'K' }, flyout).getElement();
    const root = button.getSvgRoot();
    const before = { width: button.width, html: root.innerHTML };
    const realCreate = Blockly.utils.dom.createSvgElement;
    const create = vi.spyOn(Blockly.utils.dom, 'createSvgElement').mockImplementation((tag, attrs, parent) => {
      if (tag === 'circle') throw new Error('boom');
      return realCreate(tag, attrs, parent);
    });
    expect(() => decorateFlyoutButton(button, 'pose')).toThrow('boom');
    create.mockRestore();
    expect(button.width).toBe(before.width);
    expect(root.innerHTML).toBe(before.html);
    button.dispose();
  });

  it('the real Ziele category still opens, and a tap on its plain buttons still dispatches once', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const realCreate = Blockly.utils.dom.createSvgElement;
    vi.spyOn(Blockly.utils.dom, 'createSvgElement').mockImplementation((tag, attrs, parent) => {
      if (tag === 'g' && attrs && attrs['data-icon']) throw new Error('shape changed');
      return realCreate(tag, attrs, parent);
    });
    const flyout = openCategory(SAMMLUNG_TOOLBOX_IDS.ZIELE);
    const buttons = iconButtonsOf(flyout);
    expect(buttons.map((b) => b.getButtonText())).toEqual([DE.FLY_TEACH_ZIEL, DE.FLY_PIN_CAMERA]);
    for (const b of buttons) expect(iconGroupOf(b)).toBeNull();
    expect(warn).toHaveBeenCalledTimes(1);
    const root = buttons[0].getSvgRoot();
    root.dispatchEvent(pointer('pointerdown', 41));
    root.dispatchEvent(pointer('pointerup', 41));
    await flushEvents();
    expect(dispatched).toEqual([{ type: 'teach', kind: 'ziel' }]);
  });
});

