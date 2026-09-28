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
 * (docs/KNOWN-ISSUES.md, R4). CSS cannot do this: SVG text has no ::before and
 * an SVG element no background image.
 */

import * as Blockly from 'blockly/core';
import { appendSvgIcon } from '../../icons/svg';
import { isIconName } from '../../icons/registry';

export const ICON_BUTTON_FLYOUT_TYPE = 'edubotics_icon_button';
export const ICON_BUTTON_ICON_SIZE = 14;
export const ICON_BUTTON_ICON_GAP = 4;
const FALLBACK_STROKE = '#fff';

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

/**
 * Draw icon `iconName` into a real FlyoutButton and make room for it. Returns
 * the icon group. Throws when the button's DOM is not the shape it expects.
 */
export function decorateFlyoutButton(button, iconName) {
  const root = button.getSvgRoot();
  const rects = Array.from(root.children).filter((el) => el.tagName.toLowerCase() === 'rect');
  const texts = Array.from(root.children).filter((el) => el.tagName.toLowerCase() === 'text');
  if (rects.length < 1 || texts.length !== 1) {
    throw new Error('FlyoutButton DOM changed: expected rect(s) and one text');
  }
  const text = texts[0];
  const shift = ICON_BUTTON_ICON_SIZE + ICON_BUTTON_ICON_GAP;
  const margin = Blockly.FlyoutButton.TEXT_MARGIN_X;
  const rtl = !!(button.getWorkspace && button.getWorkspace().RTL);
  button.width += shift;
  for (const rect of rects) rect.setAttribute('width', String(button.width));
  // LTR: [margin][icon][gap][text][margin] — the text centre moves right by
  // the whole shift. RTL: the icon sits at the end and the text keeps its place.
  if (!rtl) text.setAttribute('x', String(Number(text.getAttribute('x')) + shift));
  const x = rtl ? button.width - margin - ICON_BUTTON_ICON_SIZE : margin;
  const y = (button.height - ICON_BUTTON_ICON_SIZE) / 2;
  const group = appendSvgIcon(root, iconName, {
    x, y, size: ICON_BUTTON_ICON_SIZE, stroke: textPaintOf(text), create: svg,
  });
  group.setAttribute('class', 'eduFlyoutButtonIcon');
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
    if (isIconName(icon)) decorateFlyoutButton(button, icon);
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

/** Register the inflater CLASS (Blockly instantiates one per flyout). Idempotent. */
export function registerIconButtonInflater() {
  const { registry } = Blockly;
  if (registry.hasItem(registry.Type.FLYOUT_INFLATER, ICON_BUTTON_FLYOUT_TYPE)) return;
  registry.register(registry.Type.FLYOUT_INFLATER, ICON_BUTTON_FLYOUT_TYPE, IconButtonInflater);
}
