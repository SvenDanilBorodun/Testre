/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/* eslint-disable testing-library/no-container, testing-library/no-node-access */

import fs from 'fs';
import path from 'path';
import React from 'react';
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import Icon from '../Icon';
import { ICONS, ICON_NAMES, isIconName } from '../registry';
import { CUSTOM_TREES } from '../custom';
import { appendSvgIcon, iconNodes, SVG_NS } from '../svg';
import { toastIcon, TOAST_ICON_KINDS } from '../toast';

const ICON_DIR = path.resolve(__dirname, '..');

describe('the icon registry', () => {
  it('renders every registered name as one decorative svg', () => {
    expect(ICON_NAMES.length).toBeGreaterThan(50);
    for (const name of ICON_NAMES) {
      const { container, unmount } = render(<Icon name={name} />);
      const svg = container.querySelector('svg');
      expect(svg).not.toBeNull();
      expect(svg.getAttribute('aria-hidden')).toBe('true');
      expect(svg.getAttribute('focusable')).toBe('false');
      expect(svg.getAttribute('data-icon')).toBe(name);
      expect(svg.getAttribute('width')).toBe('1em');
      // Lucide's paint: an outline drawn in the text colour.
      expect(svg.getAttribute('fill')).toBe('none');
      expect(svg.getAttribute('stroke')).toBe('currentColor');
      expect(svg.children.length).toBeGreaterThan(0);
      unmount();
    }
  });

  it('is frozen, and isIconName answers exactly its keys', () => {
    expect(Object.isFrozen(ICONS)).toBe(true);
    expect(isIconName('play')).toBe(true);
    expect(isIconName('toString')).toBe(false);
    expect(isIconName('no-such-icon')).toBe(false);
    expect(isIconName(undefined)).toBe(false);
  });

  it('carries the three Vormachen kind icons the owner chose (D10)', () => {
    expect(ICONS.record.name).toBe('LuCircleDot');
    expect(ICONS.pose.name).toBe('LuMapPin');
    expect(ICONS.ziel.name).toBe('LuTarget');
  });

  it('throws on an unknown name outside production', () => {
    const spy = console.error;
    console.error = () => {};
    try {
      expect(() => render(<Icon name="no-such-icon" />)).toThrow(/Unknown icon/);
    } finally {
      console.error = spy;
    }
  });

  it('becomes a named image when given a title', () => {
    const { container } = render(<Icon name="warning" title="Achtung" />);
    const svg = container.querySelector('svg');
    expect(svg.getAttribute('aria-hidden')).toBeNull();
    expect(svg.getAttribute('role')).toBe('img');
    expect(svg.getAttribute('aria-label')).toBe('Achtung');
  });

  it('passes size, className and fill through', () => {
    const { container } = render(<Icon name="dot" size={10} className="text-red-500" fill="currentColor" />);
    const svg = container.querySelector('svg');
    expect(svg.getAttribute('width')).toBe('10');
    expect(svg.getAttribute('class')).toContain('text-red-500');
    expect(svg.getAttribute('fill')).toBe('currentColor');
  });
});

// A conservative bounding box of an SVG path: every endpoint, every control
// point (a Bézier stays inside their hull) and, for an arc, its endpoints
// grown by its radius.
function pathPoints(d) {
  const tokens = d.match(/[a-zA-Z]|-?\d*\.?\d+(?:e-?\d+)?/g);
  const pts = [];
  let i = 0;
  let cx = 0;
  let cy = 0;
  let cmd = null;
  const num = () => Number(tokens[i++]);
  while (i < tokens.length) {
    if (/[a-zA-Z]/.test(tokens[i])) cmd = tokens[i++];
    const rel = cmd === cmd.toLowerCase();
    const C = cmd.toUpperCase();
    if (C === 'Z') continue;
    if (C === 'M' || C === 'L') {
      const x = num(); const y = num();
      cx = rel ? cx + x : x; cy = rel ? cy + y : y;
      pts.push([cx, cy]);
      if (C === 'M') cmd = rel ? 'l' : 'L';
    } else if (C === 'H') {
      const x = num(); cx = rel ? cx + x : x; pts.push([cx, cy]);
    } else if (C === 'V') {
      const y = num(); cy = rel ? cy + y : y; pts.push([cx, cy]);
    } else if (C === 'C') {
      const c = [num(), num(), num(), num(), num(), num()];
      const base = rel ? [cx, cy] : [0, 0];
      pts.push([base[0] + c[0], base[1] + c[1]], [base[0] + c[2], base[1] + c[3]]);
      cx = base[0] + c[4]; cy = base[1] + c[5];
      pts.push([cx, cy]);
    } else if (C === 'A') {
      const r = Math.max(num(), num());
      num(); num(); num();
      const x = num(); const y = num();
      const [sx, sy] = [cx, cy];
      cx = rel ? cx + x : x; cy = rel ? cy + y : y;
      for (const [px, py] of [[sx, sy], [cx, cy]]) {
        pts.push([px - r, py - r], [px + r, py + r]);
      }
    } else {
      throw new Error(`unhandled path command ${cmd}`);
    }
  }
  return pts;
}

function treePoints(node) {
  const out = [];
  const a = node.attr;
  if (node.tag === 'path') out.push(...pathPoints(a.d));
  if (node.tag === 'circle') {
    const [x, y, r] = [Number(a.cx), Number(a.cy), Number(a.r)];
    out.push([x - r, y - r], [x + r, y + r]);
  }
  for (const c of node.child || []) out.push(...treePoints(c));
  return out;
}

describe('the custom icons follow Lucide\'s rules', () => {
  it.each(Object.entries(CUSTOM_TREES))('%s', (name, tree) => {
    expect(tree.attr).toEqual({
      viewBox: '0 0 24 24',
      fill: 'none',
      stroke: 'currentColor',
      strokeWidth: '2',
      strokeLinecap: 'round',
      strokeLinejoin: 'round',
    });
    expect(tree.child.length).toBeGreaterThan(0);
    for (const child of tree.child) {
      expect(['path', 'circle']).toContain(child.tag);
      // Paint comes from the root only.
      expect(child.attr.fill).toBeUndefined();
      expect(child.attr.stroke).toBeUndefined();
      expect(child.attr.strokeWidth).toBeUndefined();
    }
    const pts = tree.child.flatMap(treePoints);
    expect(pts.length).toBeGreaterThan(0);
    const outside = pts.filter(([x, y]) => x < 1 || x > 23 || y < 1 || y > 23);
    expect(outside).toEqual([]);
  });

  it('every custom tree is a registered icon', () => {
    for (const name of Object.keys(CUSTOM_TREES)) expect(isIconName(name)).toBe(true);
  });
});

describe('iconNodes / appendSvgIcon (SVG without react-dom)', () => {
  it('reads the play icon\'s one shape off the element, in SVG attribute names', () => {
    expect(iconNodes('play')).toEqual([
      { tag: 'polygon', attr: { points: '6 3 20 12 6 21 6 3' }, child: [] },
    ]);
  });

  it('turns camelCase props into SVG attributes and caches', () => {
    const nodes = iconNodes('python');
    expect(nodes.length).toBe(CUSTOM_TREES.python.child.length);
    expect(iconNodes('python')).toBe(nodes);
    const snake = nodes[0];
    expect(snake.tag).toBe('path');
    expect(Object.keys(snake.attr)).toEqual(['d']);
  });

  it('throws on an unknown name', () => {
    expect(() => iconNodes('no-such-icon')).toThrow(/Unknown icon/);
  });

  it('appends one painted, scaled group of real SVG elements', () => {
    const parent = document.createElementNS(SVG_NS, 'svg');
    const g = appendSvgIcon(parent, 'more', { x: 5, y: 6, size: 12, stroke: '#374151' });
    expect(g.parentNode).toBe(parent);
    expect(g.namespaceURI).toBe(SVG_NS);
    expect(g.getAttribute('transform')).toBe('translate(5,6) scale(0.5)');
    expect(g.getAttribute('fill')).toBe('none');
    expect(g.getAttribute('stroke')).toBe('#374151');
    expect(g.getAttribute('stroke-width')).toBe('2');
    expect(g.getAttribute('aria-hidden')).toBe('true');
    expect(g.getAttribute('data-icon')).toBe('more');
    const circles = g.querySelectorAll('circle');
    expect(circles.length).toBe(3);
    for (const c of circles) expect(c.namespaceURI).toBe(SVG_NS);
  });

  it('uses the caller\'s element factory (Blockly passes createSvgElement)', () => {
    const calls = [];
    const create = (tag, attrs, parent) => {
      calls.push([tag, attrs]);
      const el = document.createElementNS(SVG_NS, tag);
      if (parent) parent.appendChild(el);
      return el;
    };
    const parent = document.createElementNS(SVG_NS, 'g');
    appendSvgIcon(parent, 'play', { create });
    expect(calls.map(([tag]) => tag)).toEqual(['g', 'polygon']);
    expect(calls[1][1]).toEqual({ points: '6 3 20 12 6 21 6 3' });
  });
});

describe('toastIcon', () => {
  it('builds one icon element per kind, reused', () => {
    for (const kind of TOAST_ICON_KINDS) {
      const el = toastIcon(kind);
      expect(React.isValidElement(el)).toBe(true);
      expect(toastIcon(kind)).toBe(el);
      const { container, unmount } = render(el);
      expect(container.querySelector('svg')).not.toBeNull();
      unmount();
    }
  });

  it('throws on an unknown kind', () => {
    expect(() => toastIcon('no-such-kind')).toThrow(/Unknown toast icon kind/);
  });
});

describe('the icon module stays renderer-free', () => {
  it('imports no react-dom (and so no react-dom/server) anywhere', () => {
    const files = fs.readdirSync(ICON_DIR).filter((f) => /\.(js|jsx)$/.test(f));
    expect(files.length).toBeGreaterThanOrEqual(5);
    for (const f of files) {
      const src = fs.readFileSync(path.join(ICON_DIR, f), 'utf8');
      expect(`${f}: ${src}`).not.toMatch(/(?:from\s*|import\s*\(\s*|require\s*\(\s*)['"]react-dom/);
    }
  });
});
