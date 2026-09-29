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
import {
  ICONS, ICON_NAMES, isIconName, isSolidIcon, SOLID_ICON_NAMES,
} from '../registry';
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
      // Lucide's paint: an outline drawn in the text colour — filled in that
      // colour for the solid media controls and dots.
      expect(svg.getAttribute('fill')).toBe(isSolidIcon(name) ? 'currentColor' : 'none');
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

  // Review round 1 (B3/B6): one icon per concept, named by meaning.
  it('merges draw mergeData (lines flowing into one); the git-merge icon and the misnamed status icons are gone', () => {
    expect(ICONS.mergeData.name).toBe('LuMerge');
    for (const gone of ['merge', 'errorCircle', 'alertCircle', 'gripper', 'search', 'arrowLeft', 'activity', 'clapperboard']) {
      expect(isIconName(gone)).toBe(false);
    }
    expect(ICONS.cancel.name).toBe('LuCircleX');
    expect(ICONS.failed.name).toBe('LuCircleAlert');
    const merge = fs.readFileSync(path.resolve(ICON_DIR, '../../features/editDataset/components/DatasetMergeSection.js'), 'utf8');
    expect(merge).toMatch(/<Icon name="mergeData"/);
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

  // Review round 1 (B2): a Stopp, Start or Pause read as a solid glyph before
  // the outline set; the fill carried meaning. One list, drawn filled by <Icon>
  // AND appendSvgIcon, so ControlPanel, the run bar and Vormachen agree.
  it('draws the media controls and the dots filled, everything else as an outline', () => {
    expect(SOLID_ICON_NAMES).toEqual(['play', 'pause', 'step', 'stop', 'skipForward', 'dot', 'liveRecording']);
    for (const name of SOLID_ICON_NAMES) expect(isIconName(name)).toBe(true);
    expect(isSolidIcon('record')).toBe(false); // the Bewegung KIND stays an outline
    expect(isSolidIcon('liveRecording')).toBe(true); // a recording in progress: the red dot
    const { container } = render(<><Icon name="stop" /><Icon name="record" /><Icon name="stop" fill="none" /></>);
    const [stop, record, override] = container.querySelectorAll('svg');
    expect(stop.getAttribute('fill')).toBe('currentColor');
    expect(record.getAttribute('fill')).toBe('none');
    expect(override.getAttribute('fill')).toBe('none');
  });

  it('passes size, className and fill through', () => {
    const { container } = render(<Icon name="dot" size={10} className="text-red-500" fill="currentColor" />);
    const svg = container.querySelector('svg');
    expect(svg.getAttribute('width')).toBe('10');
    expect(svg.getAttribute('class')).toContain('text-red-500');
    expect(svg.getAttribute('fill')).toBe('currentColor');
  });
});

// Points on an SVG elliptical arc (endpoint → centre parameterisation, SVG 1.1
// §F.6.5), sampled finely: the bounding box of an arc is its sampled hull.
function arcPoints(x1, y1, rxIn, ryIn, phiDeg, largeArc, sweep, x2, y2, n = 64) {
  let rx = Math.abs(rxIn);
  let ry = Math.abs(ryIn);
  if (rx === 0 || ry === 0) return [[x2, y2]];
  const phi = (phiDeg * Math.PI) / 180;
  const cos = Math.cos(phi);
  const sin = Math.sin(phi);
  const dx = (x1 - x2) / 2;
  const dy = (y1 - y2) / 2;
  const x1p = cos * dx + sin * dy;
  const y1p = -sin * dx + cos * dy;
  const lambda = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry);
  if (lambda > 1) {
    rx *= Math.sqrt(lambda);
    ry *= Math.sqrt(lambda);
  }
  const num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p;
  const den = rx * rx * y1p * y1p + ry * ry * x1p * x1p;
  const k = (largeArc === sweep ? -1 : 1) * Math.sqrt(Math.max(0, num / den));
  const cxp = (k * rx * y1p) / ry;
  const cyp = (-k * ry * x1p) / rx;
  const cx = cos * cxp - sin * cyp + (x1 + x2) / 2;
  const cy = sin * cxp + cos * cyp + (y1 + y2) / 2;
  const angle = (ux, uy, vx, vy) => Math.atan2(ux * vy - uy * vx, ux * vx + uy * vy);
  const t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry);
  let dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry);
  if (!sweep && dt > 0) dt -= 2 * Math.PI;
  else if (sweep && dt < 0) dt += 2 * Math.PI;
  const pts = [];
  for (let i = 0; i <= n; i += 1) {
    const t = t1 + (dt * i) / n;
    pts.push([
      cx + rx * Math.cos(t) * cos - ry * Math.sin(t) * sin,
      cy + rx * Math.cos(t) * sin + ry * Math.sin(t) * cos,
    ]);
  }
  return pts;
}

// The points that bound an SVG path: every endpoint, every control point (a
// Bézier stays inside their hull) and an arc sampled along its curve.
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
      const [rx, ry, rot, large, sweep] = [num(), num(), num(), num(), num()];
      const x = num(); const y = num();
      const [sx, sy] = [cx, cy];
      cx = rel ? cx + x : x; cy = rel ? cy + y : y;
      pts.push(...arcPoints(sx, sy, rx, ry, rot, large, sweep, cx, cy));
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

  // Review round 1 (B1): the first drawings used about two thirds of the grid
  // and read small beside Lucide's 18–20-unit icons. The geometry must now fill
  // it: at least 16 units on the longer axis and 14 on the shorter.
  it.each(Object.entries(CUSTOM_TREES))('%s fills the grid like its Lucide neighbours', (_name, tree) => {
    const pts = tree.child.flatMap(treePoints);
    const xs = pts.map(([x]) => x);
    const ys = pts.map(([, y]) => y);
    const w = Math.max(...xs) - Math.min(...xs);
    const h = Math.max(...ys) - Math.min(...ys);
    expect(Math.max(w, h)).toBeGreaterThanOrEqual(16);
    expect(Math.min(w, h)).toBeGreaterThanOrEqual(14);
  });

  it('the arc sampler measures an arc exactly (a half circle of r 2 spans 4 × 2)', () => {
    const pts = pathPoints('M2 10a2 2 0 0 1 4 0');
    const xs = pts.map(([x]) => x);
    const ys = pts.map(([, y]) => y);
    expect(Math.min(...xs)).toBeCloseTo(2, 6);
    expect(Math.max(...xs)).toBeCloseTo(6, 6);
    expect(Math.min(...ys)).toBeCloseTo(8, 2);
    expect(Math.max(...ys)).toBeCloseTo(10, 6);
  });

  it('the leader arm ends in a CLOSED grip where the follower\'s claw is open', () => {
    const own = (tree, other) => tree.child.filter(
      (c) => !other.child.some((o) => JSON.stringify(o) === JSON.stringify(c)),
    );
    const leaderOwn = own(CUSTOM_TREES.leaderArm, CUSTOM_TREES.robotArm);
    const followerOwn = own(CUSTOM_TREES.robotArm, CUSTOM_TREES.leaderArm);
    expect(leaderOwn.some((c) => c.tag === 'path' && /z\s*$/i.test(c.attr.d))).toBe(true);
    expect(followerOwn.some((c) => c.tag === 'path' && /z\s*$/i.test(c.attr.d))).toBe(false);
    // The grip is a tall shape (≥ 8 units), the claw a short open arch.
    const height = (nodes) => {
      const ys = nodes.flatMap(treePoints).map(([, y]) => y);
      return Math.max(...ys) - Math.min(...ys);
    };
    expect(height(leaderOwn.filter((c) => /z\s*$/i.test(c.attr.d || '')))).toBeGreaterThanOrEqual(8);
  });

  it('gripper is gone from the registry (it was never used)', () => {
    expect(isIconName('gripper')).toBe(false);
    expect(Object.keys(CUSTOM_TREES).sort()).toEqual(['leaderArm', 'python', 'robotArm']);
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

  it('fills a solid icon with its stroke colour on SVG surfaces too', () => {
    const parent = document.createElementNS(SVG_NS, 'svg');
    expect(appendSvgIcon(parent, 'play', { stroke: '#374151' }).getAttribute('fill')).toBe('#374151');
    expect(appendSvgIcon(parent, 'more', { stroke: '#374151' }).getAttribute('fill')).toBe('none');
    expect(appendSvgIcon(parent, 'play', { stroke: '#374151', fill: 'none' }).getAttribute('fill')).toBe('none');
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
