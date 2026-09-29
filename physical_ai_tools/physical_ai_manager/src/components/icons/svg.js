/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Icons on surfaces React does not render: Blockly's flyout SVG and the
// CodeMirror gutter. A `react-icons` component is `(props) => element`, and the
// element it returns already carries the icon's whole tree (`props.attr` for
// the <svg>, `props.children` for its shapes) — so the shapes are READ off that
// element, with no render and no `react-dom/server` (which would put a second
// React renderer in the entry bundle).

import { ICONS, isSolidIcon } from './registry';

export const SVG_NS = 'http://www.w3.org/2000/svg';

const cache = new Map();

// camelCase React prop → SVG attribute (`strokeWidth` → `stroke-width`).
const kebab = (key) => key.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`);

function asList(children) {
  if (Array.isArray(children)) return children;
  return children ? [children] : [];
}

function toNodes(children) {
  return asList(children)
    .filter((el) => el && typeof el === 'object' && typeof el.type === 'string')
    .map((el) => {
      const { children: kids, ...props } = el.props || {};
      const attr = {};
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null) continue;
        attr[kebab(k)] = String(v);
      }
      return Object.freeze({ tag: el.type, attr: Object.freeze(attr), child: toNodes(kids) });
    });
}

/**
 * The shapes of icon `name` as `[{tag, attr, child}]` (SVG attribute names),
 * cached. Throws on an unknown name.
 */
export function iconNodes(name) {
  if (cache.has(name)) return cache.get(name);
  const Component = Object.prototype.hasOwnProperty.call(ICONS, name) ? ICONS[name] : null;
  if (!Component) throw new Error(`Unknown icon: ${String(name)}`);
  const element = Component({});
  const nodes = Object.freeze(toNodes(element && element.props ? element.props.children : null));
  cache.set(name, nodes);
  return nodes;
}

function defaultCreate(tag, attrs, parent) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  if (parent) parent.appendChild(el);
  return el;
}

function appendNodes(nodes, parent, create) {
  for (const node of nodes) {
    const el = create(node.tag, { ...node.attr }, parent);
    if (node.child.length) appendNodes(node.child, el, create);
  }
}

/**
 * Draw icon `name` into the SVG element `parent` as one `<g>`, `size` px
 * square with its top-left corner at (`x`, `y`). The paint is explicit on the
 * group (`fill` none, `stroke`), so a parent's fill (Blockly's flyout button
 * text is `fill:#888`-style) never leaks into the icon. `create` defaults to
 * `document.createElementNS`; Blockly callers pass
 * `Blockly.utils.dom.createSvgElement`. A solid icon (SOLID_ICON_NAMES) is
 * filled with the stroke colour unless `fill` is given. Returns the group.
 */
export function appendSvgIcon(parent, name, {
  x = 0, y = 0, size = 24, stroke = 'currentColor', fill, strokeWidth = 2, create = defaultCreate,
} = {}) {
  const nodes = iconNodes(name);
  const paint = fill !== undefined ? fill : (isSolidIcon(name) ? stroke : 'none');
  const scale = size / 24;
  const g = create('g', {
    transform: `translate(${x},${y}) scale(${scale})`,
    fill: paint,
    stroke,
    'stroke-width': strokeWidth,
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true',
    'data-icon': name,
  }, parent);
  appendNodes(nodes, g, create);
  return g;
}

const escapeAttr = (v) => String(v).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

function nodesMarkup(nodes) {
  return nodes.map((node) => {
    const attrs = Object.entries(node.attr).map(([k, v]) => ` ${k}="${escapeAttr(v)}"`).join('');
    return `<${node.tag}${attrs}>${nodesMarkup(node.child)}</${node.tag}>`;
  }).join('');
}

/**
 * Icon `name` as SVG MARKUP — one `<g>` string, placed and painted like
 * appendSvgIcon — for an image a surface takes as a data URI (a Blockly
 * FieldImage). Plain ASCII, so it survives `btoa`.
 */
export function iconMarkup(name, {
  x = 0, y = 0, size = 24, stroke = 'currentColor', fill, strokeWidth = 2,
} = {}) {
  const nodes = iconNodes(name);
  const paint = fill !== undefined ? fill : (isSolidIcon(name) ? stroke : 'none');
  return `<g transform="translate(${x},${y}) scale(${size / 24})" fill="${escapeAttr(paint)}" `
    + `stroke="${escapeAttr(stroke)}" stroke-width="${strokeWidth}" stroke-linecap="round" `
    + `stroke-linejoin="round" data-icon="${escapeAttr(name)}">${nodesMarkup(nodes)}</g>`;
}
