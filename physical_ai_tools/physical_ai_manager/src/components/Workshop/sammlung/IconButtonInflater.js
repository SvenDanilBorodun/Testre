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
 * Flyout buttons with an icon (inflater type `edubotics_icon_button`): the
 * Sammlung creation buttons („Bewegung vormachen" with its kind icon, …) and
 * Plan B's ▶/⋯. JSON: `{kind: 'edubotics_icon_button', text, callbackkey,
 * icon}` — `icon` a components/icons registry name.
 *
 * COMPOSITION, NEVER A SUBCLASS AT IMPORT. Nine page test files mock
 * `blockly/core` with only `svgResize`/`Events`, and WorkshopPage imports this
 * module through toolboxCategories.js: a top-level
 * `class … extends Blockly.ButtonFlyoutInflater` would throw at import and take
 * every page test down. So Blockly's own inflater is built lazily and does the
 * real work: it creates a real `FlyoutButton` — its callback
 * (`registerButtonCallback`), its keyboard navigation (the core
 * FlyoutButtonNavigationPolicy is `instanceof FlyoutButton`), its focus and its
 * touch handling all stay Blockly's own — and this module only DECORATES that
 * button's DOM: an icon group at the start, the two rects and `width` widened
 * by the icon and its gap, the centred text shifted by the same.
 *
 * The decoration depends on FlyoutButton's DOM shape (a group holding a shadow
 * rect, a background rect and one centred <text>); `decorateFlyoutButton`
 * throws if that shape changes, and IconButtonInflater.test.jsx fails loudly
 * (docs/KNOWN-ISSUES.md, R4). In the running app that throw costs only the
 * icon: `load` warns once and returns the undecorated button, still of this
 * inflater's type, so a Blockly update can never empty a whole Sammlung
 * category. The decoration mutates nothing until the icon is drawn, and undoes
 * a half-drawn icon, so the fallback is always Blockly's own plain button.
 * CSS cannot do this: SVG text has no ::before and an SVG element no
 * background image.
 *
 * The icon is as large as the button's text: 14 px beside Blockly's default
 * 11 pt flyout text, larger in a theme with a larger font (the high-contrast
 * theme's 16 pt), because it is measured off the rendered text the way
 * FlyoutButton measures that text itself.
 */

import * as Blockly from 'blockly/core';
import { appendSvgIcon } from '../../icons/svg';
import { isIconName } from '../../icons/registry';

export const ICON_BUTTON_FLYOUT_TYPE = 'edubotics_icon_button';
// At Blockly's default flyout font (11 pt ≈ 14.7 px): a 14 px icon, 4 px gap.
export const ICON_BUTTON_ICON_SIZE = 14;
export const ICON_BUTTON_ICON_GAP = 4;
const DEFAULT_FONT_PX = (11 * 4) / 3;
const ICON_PER_FONT_PX = ICON_BUTTON_ICON_SIZE / DEFAULT_FONT_PX;
const GAP_PER_ICON_PX = ICON_BUTTON_ICON_GAP / ICON_BUTTON_ICON_SIZE;
const MIN_ICON_PX = 12;
const MAX_ICON_PX = 32;
const FALLBACK_STROKE = '#fff';

let warnedDecorateFailure = false;

const svg = (tag, attrs, parent) => Blockly.utils.dom.createSvgElement(tag, attrs, parent);

// The icon is painted like the button's text: Blockly's renderer CSS sets
// `.blocklyText { fill }`, and the button group's own `fill` would otherwise
// leak into the icon (it is set explicitly on the icon group instead).
function textPaintOf(textEl) {
  try {
    const fill = window.getComputedStyle(textEl).fill;
    if (typeof fill === 'string' && fill && fill !== 'none' && !fill.startsWith('url')) return fill;
  } catch (_) { /* no computed style (a detached node) */ }
  return FALLBACK_STROKE;
}

// A CSS font size (`14.6667px`, `11pt`, …) in px; null when unreadable.
export function fontSizePx(value) {
  const m = /^\s*([0-9]*\.?[0-9]+)\s*(px|pt)?\s*$/i.exec(String(value || ''));
  if (!m) return null;
  const n = Number(m[1]);
  if (!Number.isFinite(n) || n <= 0) return null;
  return (m[2] || 'px').toLowerCase() === 'pt' ? (n * 4) / 3 : n;
}

// The rendered font size of the button's text, read the way FlyoutButton
// itself reads it (Blockly.utils.style.getComputedStyle), else null.
function textFontPx(textEl) {
  try {
    const read = Blockly.utils.style && Blockly.utils.style.getComputedStyle;
    const value = read ? read(textEl, 'fontSize') : window.getComputedStyle(textEl).fontSize;
    return fontSizePx(value);
  } catch (_) {
    return null;
  }
}

/**
 * The icon size and gap for a text of `fontPx` pixels: 14 + 4 at Blockly's
 * default 11 pt, scaling with the font, clamped to [12, 32]; 14 + 4 when the
 * font size is unknown.
 */
export function iconMetricsForFont(fontPx) {
  if (!(Number.isFinite(fontPx) && fontPx > 0)) {
    return { size: ICON_BUTTON_ICON_SIZE, gap: ICON_BUTTON_ICON_GAP };
  }
  const size = Math.min(MAX_ICON_PX, Math.max(MIN_ICON_PX, Math.round(fontPx * ICON_PER_FONT_PX)));
  return { size, gap: Math.round(size * GAP_PER_ICON_PX) };
}

/**
 * Draw icon `iconName` into a real FlyoutButton and make room for it. Returns
 * the icon group. Throws when the button's DOM is not the shape it expects —
 * before anything is changed — and, should drawing the icon itself fail,
 * removes what it drew and rethrows, so a throw always leaves Blockly's own
 * button exactly as it was.
 */
export function decorateFlyoutButton(button, iconName) {
  const root = button.getSvgRoot();
  const rects = Array.from(root.children).filter((el) => el.tagName.toLowerCase() === 'rect');
  const texts = Array.from(root.children).filter((el) => el.tagName.toLowerCase() === 'text');
  if (rects.length < 1 || texts.length !== 1) {
    throw new Error('FlyoutButton DOM changed: expected rect(s) and one text');
  }
  const text = texts[0];
  const { size, gap } = iconMetricsForFont(textFontPx(text));
  const shift = size + gap;
  const margin = Blockly.FlyoutButton.TEXT_MARGIN_X;
  const rtl = !!(button.getWorkspace && button.getWorkspace().RTL);
  const width = button.width + shift;
  // LTR: [margin][icon][gap][text][margin] — the text centre moves right by
  // the whole shift. RTL: the icon sits at the end and the text keeps its place.
  const x = rtl ? width - margin - size : margin;
  const y = (button.height - size) / 2;
  let group = null;
  try {
    group = appendSvgIcon(root, iconName, {
      x, y, size, stroke: textPaintOf(text), create: svg,
    });
    group.setAttribute('class', 'eduFlyoutButtonIcon');
  } catch (err) {
    const partial = root.querySelector(':scope > g[data-icon]');
    if (partial) partial.remove();
    throw err;
  }
  button.width = width;
  for (const rect of rects) rect.setAttribute('width', String(width));
  if (!rtl) text.setAttribute('x', String(Number(text.getAttribute('x')) + shift));
  return group;
}

export class IconButtonInflater {
  constructor() {
    this.base = null;
  }

  // Built on first use, never at module scope (see the header).
  base_() {
    if (!this.base) this.base = new Blockly.ButtonFlyoutInflater();
    return this.base;
  }

  load(state, flyout) {
    const item = this.base_().load(state, flyout);
    const button = item.getElement();
    const icon = state ? state.icon : null;
    if (isIconName(icon)) {
      try {
        decorateFlyoutButton(button, icon);
      } catch (err) {
        // A future Blockly changed FlyoutButton's DOM: the button stays, plain
        // and working; the tests fail loudly on the same change (R4).
        if (!warnedDecorateFailure) {
          warnedDecorateFailure = true;
          console.warn('IconButtonInflater: flyout button left without its icon:', err);
        }
      }
    }
    return new Blockly.FlyoutItem(button, ICON_BUTTON_FLYOUT_TYPE);
  }

  gapForItem(state, defaultGap) {
    return this.base_().gapForItem(state, defaultGap);
  }

  disposeItem(item) {
    this.base_().disposeItem(item);
  }

  getType() {
    return ICON_BUTTON_FLYOUT_TYPE;
  }
}

/** Tests only: let the next decoration failure warn again. */
export function __resetDecorateWarningForTests() {
  warnedDecorateFailure = false;
}

/** Register the inflater CLASS (Blockly instantiates one per flyout). Idempotent. */
export function registerIconButtonInflater() {
  const { registry } = Blockly;
  if (registry.hasItem(registry.Type.FLYOUT_INFLATER, ICON_BUTTON_FLYOUT_TYPE)) return;
  registry.register(registry.Type.FLYOUT_INFLATER, ICON_BUTTON_FLYOUT_TYPE, IconButtonInflater);
}
