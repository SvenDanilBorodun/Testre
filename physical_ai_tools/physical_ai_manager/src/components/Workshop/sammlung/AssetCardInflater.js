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
 * Asset cards in a Blockly flyout: a custom flyout item (inflater type
 * `edubotics_asset_card`) that draws one Sammlung asset — name, a German second
 * line, status chips, ▶ (simulator preview) and ⋯ (manage).
 *
 * THE CONTRACT A FLYOUT ITEM ELEMENT MUST MEET (Blockly 12.5.1):
 *   IBoundedElement  getBoundingRectangle, moveBy
 *   IFocusableNode   getFocusableElement, getFocusableTree, onNodeFocus,
 *                    onNodeBlur, canBeFocused
 *   IRenderedElement getSvgRoot
 * and `load(state, flyout)` RETURNS a `Blockly.FlyoutItem`.
 *
 * TOUCH. ▶ and ⋯ bind pointerdown AND pointerup with `conditionalBind(…, true)`
 * (no touch-identifier capture). A plain conditionalBind STORES the pointer id
 * on pointerdown and only a gesture's dispose clears it; stopping propagation
 * means no gesture ever starts, so the id stays held and Blockly ignores every
 * later touch of another pointer — the whole editor goes dead on a touch
 * screen (a mouse, whose id never changes, hides this).
 *
 * LIFETIME. Blockly disposes flyout items when it RE-POPULATES the flyout (a
 * re-open, `refreshToolboxSelection`), not on hide. So a card registers nothing
 * outside its own SVG subtree — no document listener, no subscription — and a
 * hidden flyout or a disposed workspace leaks nothing.
 */

import * as Blockly from 'blockly/core';
import { DE } from '../blocks/messages_de';
// A cycle with toolboxCategories.js, read only inside functions at event time.
import { getProviderForWorkspace } from './toolboxCategories';

export const ASSET_CARD_FLYOUT_TYPE = 'edubotics_asset_card';

export const CARD_WIDTH = 240;
const CARD_BASE_HEIGHT = 38;
const CARD_CHIP_ROW_HEIGHT = 20;
const STRIPE_WIDTH = 4;
const HIT_SIZE = 24;

const CHIP_COLOURS = Object.freeze({
  ok: { fill: '#dcfce7', text: '#166534' },
  warn: { fill: '#fef3c7', text: '#92400e' },
  bad: { fill: '#fee2e2', text: '#991b1b' },
});

const MANAGE_TABS = Object.freeze({
  recording: 'aufnahmen',
  missingRecording: 'aufnahmen',
  pin: 'ziele',
  pose: 'positionen',
  variable: 'variablen',
});

function svg(name, attrs, parent) {
  return Blockly.utils.dom.createSvgElement(name, attrs, parent);
}

function textNode(parent, attrs, text) {
  const el = svg(Blockly.utils.Svg.TEXT, attrs, parent);
  el.appendChild(document.createTextNode(text));
  return el;
}

const str = (v) => (typeof v === 'string' ? v : '');

/**
 * Chip positions, wrapping to a new row instead of dropping a chip: the chips
 * that come last (another robot, refused in the simulator) are the ones a
 * student must not miss. jsdom and a hidden flyout cannot measure text, so the
 * width is an estimate that errs wide (10 px text).
 */
export function layoutChips(chips, cardWidth = CARD_WIDTH) {
  const left = 12;
  const right = cardWidth - 8;
  const out = [];
  let x = left;
  let row = 0;
  for (const chip of chips) {
    const width = Math.min(Math.ceil(chip.text.length * 5.6) + 12, right - left);
    if (x > left && x + width > right) {
      row += 1;
      x = left;
    }
    out.push({ chip, x, row, width });
    x += width + 4;
  }
  return out;
}

/** The `{type:'manage'}` action for a card's asset (programPin jumps instead). */
export function manageActionFor(state) {
  const kind = str(state.assetKind);
  if (kind === 'programPin') return { type: 'jumpToBlock', blockId: str(state.assetId) };
  const byName = kind === 'recording' || kind === 'missingRecording';
  return {
    type: 'manage',
    tab: MANAGE_TABS[kind] || 'aufnahmen',
    focusId: byName ? str(state.assetName) : str(state.assetId),
  };
}

/** The `{type:'preview'}` asset; a recording's tag comes from the provider list. */
export function previewAssetFor(state, snapshot) {
  const kind = str(state.assetKind);
  const asset = { kind, id: str(state.assetId), name: str(state.assetName) };
  if (kind === 'recording') {
    const items = snapshot && snapshot.trajectories && Array.isArray(snapshot.trajectories.items)
      ? snapshot.trajectories.items : [];
    const row = items.find((it) => it && it.id === asset.id);
    asset.robotProfile = row && typeof row.robot_profile === 'string' ? row.robot_profile : null;
  }
  return asset;
}

/**
 * The marker a card's hover highlights (sammlung/markers.js ids), or null for a
 * card with no marker (recordings, program pins). A variable's marker is keyed
 * by NAME (`var:<name>`), like its point marker, not by the Blockly variable id.
 */
export function highlightAssetFor(state) {
  const kind = str(state.assetKind);
  if (kind === 'pin' || kind === 'pose') return { kind, id: str(state.assetId) };
  if (kind === 'variable') return { kind, id: `var:${str(state.assetName)}` };
  return null;
}

export class AssetCard {
  constructor(state, flyout) {
    this.state = state && typeof state === 'object' ? state : {};
    this.flyout = flyout;
    this.workspace = flyout.getWorkspace();
    this.targetWorkspace = flyout.targetWorkspace || flyout.getTargetWorkspace();
    this.position = new Blockly.utils.Coordinate(0, 0);
    this.bindings = [];
    this.disposed = false;
    this.chips = Array.isArray(this.state.chips)
      ? this.state.chips.filter((c) => c && typeof c.text === 'string') : [];
    this.width = CARD_WIDTH;
    this.chipLayout = layoutChips(this.chips, this.width);
    const rows = this.chipLayout.length ? this.chipLayout[this.chipLayout.length - 1].row + 1 : 0;
    this.height = CARD_BASE_HEIGHT + rows * CARD_CHIP_ROW_HEIGHT;
    this.id = Blockly.utils.idGenerator.getNextUniqueId();
    this.buildDom_();
    this.bind_();
    this.updateTransform_();
  }

  buildDom_() {
    const { Svg } = Blockly.utils;
    const name = str(this.state.assetName);
    const meta = str(this.state.meta);
    this.svgGroup = svg(Svg.G, {
      id: this.id,
      class: 'eduAssetCard',
      tabindex: '-1',
      role: 'group',
      'aria-label': meta ? `${name} — ${meta}` : name,
    }, this.workspace.getCanvas());
    svg(Svg.RECT, {
      width: this.width, height: this.height, rx: 6, ry: 6,
      fill: '#ffffff', stroke: '#e5e7eb',
    }, this.svgGroup);
    svg(Svg.RECT, {
      width: STRIPE_WIDTH, height: this.height, fill: str(this.state.colour) || '#64748b',
    }, this.svgGroup);
    textNode(this.svgGroup, {
      x: 12, y: 17, 'font-size': 13, 'font-weight': 600, fill: '#111827',
    }, str(this.state.title));
    textNode(this.svgGroup, { x: 12, y: 32, 'font-size': 11, fill: '#6b7280' }, meta);
    for (const { chip, x, row, width } of this.chipLayout) {
      const colours = CHIP_COLOURS[chip.level] || CHIP_COLOURS.ok;
      const y = 40 + row * CARD_CHIP_ROW_HEIGHT;
      svg(Svg.RECT, {
        x, y, width, height: 16, rx: 8, ry: 8, fill: colours.fill,
      }, this.svgGroup);
      textNode(this.svgGroup, { x: x + 6, y: y + 12, 'font-size': 10, fill: colours.text }, chip.text);
    }
    if (this.state.canPreview === true) {
      this.previewEl = this.hitArea_(this.width - 2 * HIT_SIZE - 8, '▶', DE.PREVIEW_START);
    } else {
      this.previewEl = null;
    }
    this.manageEl = this.hitArea_(this.width - HIT_SIZE - 4, '⋯', DE.CARD_MANAGE);
  }

  hitArea_(x, glyph, label) {
    const { Svg } = Blockly.utils;
    const g = svg(Svg.G, {
      class: 'eduAssetCardButton', role: 'button', 'aria-label': label,
      transform: `translate(${x},7)`,
    }, this.svgGroup);
    svg(Svg.RECT, {
      width: HIT_SIZE, height: HIT_SIZE, rx: 4, ry: 4, fill: '#f3f4f6',
    }, g);
    textNode(g, {
      x: HIT_SIZE / 2, y: 16, 'font-size': 12, fill: '#374151', 'text-anchor': 'middle',
    }, glyph);
    return g;
  }

  bind_() {
    const bind = (el, type, fn, noCapture) => {
      this.bindings.push(Blockly.browserEvents.conditionalBind(el, type, null, fn, noCapture));
    };
    // Body: start a flyout gesture (the flyout scrolls by drag), exactly like
    // a FlyoutButton; the gesture's own dispose clears the touch identifier.
    bind(this.svgGroup, 'pointerdown', (e) => this.onBodyDown_(e), false);
    bind(this.svgGroup, 'pointerup', (e) => this.onBodyUp_(e), false);
    // ▶/⋯: never store Blockly's touch identifier (see the module header).
    if (this.previewEl) {
      bind(this.previewEl, 'pointerdown', (e) => e.stopPropagation(), true);
      bind(this.previewEl, 'pointerup', (e) => {
        e.stopPropagation();
        this.dispatch_('preview');
      }, true);
    }
    bind(this.manageEl, 'pointerdown', (e) => e.stopPropagation(), true);
    bind(this.manageEl, 'pointerup', (e) => {
      e.stopPropagation();
      this.dispatch_('manage');
    }, true);
    // Hover highlights the asset's marker on the twin and the sim table. A plain
    // `bind`: enter/leave start no gesture and must never touch Blockly's touch
    // identifier.
    this.hovered = false;
    if (highlightAssetFor(this.state)) {
      this.bindings.push(Blockly.browserEvents.bind(this.svgGroup, 'pointerenter', null, () => {
        this.hovered = true;
        this.dispatchHighlight_(highlightAssetFor(this.state));
      }));
      this.bindings.push(Blockly.browserEvents.bind(this.svgGroup, 'pointerleave', null, () => {
        this.hovered = false;
        this.dispatchHighlight_(null);
      }));
    }
  }

  dispatchHighlight_(asset) {
    const provider = getProviderForWorkspace(this.targetWorkspace);
    if (!provider) return;
    try {
      provider.dispatchAction({ type: 'highlight', asset });
    } catch (err) {
      console.error('Sammlung card highlight failed:', err);
    }
  }

  onBodyDown_(e) {
    const gesture = this.targetWorkspace && this.targetWorkspace.getGesture(e);
    if (gesture) gesture.handleFlyoutStart(e, this.flyout);
  }

  onBodyUp_(e) {
    const gesture = this.targetWorkspace && this.targetWorkspace.getGesture(e);
    if (gesture) gesture.cancel();
    this.dispatch_('manage');
  }

  dispatch_(kind) {
    const provider = getProviderForWorkspace(this.targetWorkspace);
    if (!provider) return;
    const action = kind === 'preview'
      ? { type: 'preview', asset: previewAssetFor(this.state, provider.getSnapshot()) }
      : manageActionFor(this.state);
    try {
      provider.dispatchAction(action);
    } catch (err) {
      // A page handler that throws must not break Blockly's event dispatch.
      console.error('Sammlung card action failed:', err);
    }
  }

  updateTransform_() {
    this.svgGroup.setAttribute('transform', `translate(${this.position.x},${this.position.y})`);
  }

  moveBy(dx, dy) {
    this.position.x += dx;
    this.position.y += dy;
    this.updateTransform_();
  }

  getBoundingRectangle() {
    return new Blockly.utils.Rect(
      this.position.y, this.position.y + this.height,
      this.position.x, this.position.x + this.width,
    );
  }

  getSvgRoot() {
    return this.svgGroup;
  }

  getFocusableElement() {
    return this.svgGroup;
  }

  getFocusableTree() {
    return this.workspace;
  }

  onNodeFocus() {
    try {
      this.workspace.scrollBoundsIntoView(this.getBoundingRectangle());
    } catch (_) { /* a flyout that is not laid out */ }
  }

  onNodeBlur() {}

  canBeFocused() {
    return !this.disposed;
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    // A re-populated flyout replaces the card under a resting pointer, which
    // never sends pointerleave: drop the highlight it was holding.
    if (this.hovered) {
      this.hovered = false;
      this.dispatchHighlight_(null);
    }
    for (const data of this.bindings) Blockly.browserEvents.unbind(data);
    this.bindings = [];
    Blockly.utils.dom.removeNode(this.svgGroup);
  }
}

/**
 * The card's own navigation rules. Wrapped in Blockly's exported
 * `FlyoutNavigationPolicy`, which supplies next/previous from the flyout's
 * item order; a card has no children and no parent.
 */
export class AssetCardNavigationPolicy {
  getFirstChild() {
    return null;
  }

  getParent() {
    return null;
  }

  getNextSibling() {
    return null;
  }

  getPreviousSibling() {
    return null;
  }

  isNavigable(current) {
    return current.canBeFocused();
  }

  isApplicable(current) {
    return current instanceof AssetCard;
  }
}

// Flyout workspaces whose navigator already knows cards.
const policyRegistered = new WeakSet();

export class AssetCardInflater {
  load(state, flyout) {
    const card = new AssetCard(state, flyout);
    const flyoutWorkspace = flyout.getWorkspace();
    if (flyoutWorkspace && !policyRegistered.has(flyoutWorkspace)) {
      policyRegistered.add(flyoutWorkspace);
      flyoutWorkspace.getNavigator().addNavigationPolicy(
        new Blockly.FlyoutNavigationPolicy(new AssetCardNavigationPolicy(), flyout),
      );
    }
    return new Blockly.FlyoutItem(card, ASSET_CARD_FLYOUT_TYPE);
  }

  gapForItem(state, defaultGap) {
    const gap = state ? state.gap : undefined;
    return typeof gap === 'number' && Number.isFinite(gap) ? gap : defaultGap;
  }

  disposeItem(item) {
    const element = item.getElement();
    if (element instanceof AssetCard) element.dispose();
  }

  getType() {
    return ASSET_CARD_FLYOUT_TYPE;
  }
}

/** Register the inflater CLASS (Blockly instantiates one per flyout). Idempotent. */
export function registerAssetCardInflater() {
  const { registry } = Blockly;
  if (registry.hasItem(registry.Type.FLYOUT_INFLATER, ASSET_CARD_FLYOUT_TYPE)) return;
  registry.register(registry.Type.FLYOUT_INFLATER, ASSET_CARD_FLYOUT_TYPE, AssetCardInflater);
}
