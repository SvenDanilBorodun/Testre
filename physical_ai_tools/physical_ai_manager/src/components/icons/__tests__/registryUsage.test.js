/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Review round 1 (B6): every icon in the registry is USED somewhere in src
// outside components/icons, so an unused icon cannot ship (the bundle carries
// every registered Lucide tree). A name counts as used where a string literal
// stands in an icon position:
//   * the `name` attribute of <Icon>, or any JSX attribute whose name
//     contains „icon" (`icon=`, `kindIcon=` …), ternaries included;
//   * an object property or a variable whose name contains „icon"
//     (`icon: 'play'`, `const TEACH_KIND_ICON = { … }`);
//   * an argument of a call whose name contains „icon" (appendSvgIcon,
//     iconNodes, iconMarkup, iconButton) or React.createElement(Icon, …);
//   * a toast kind passed to toastIcon(…), through toast.js's own table.
// Parsed with espree (the parser eslint uses), never grepped: a string like
// 'loading' also names a status, and only its position makes it an icon.

import fs from 'fs';
import path from 'path';
import * as espree from 'espree';
import { describe, expect, it } from 'vitest';
import { ICON_NAMES } from '../registry';
import { TOAST_ICONS } from '../toast';

const SRC = path.resolve(__dirname, '../../..');
const ICONS_DIR = path.join(SRC, 'components', 'icons');
const ICONISH = /icon/i;

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'node_modules') walk(p, out);
    } else if (/\.jsx?$/.test(p)) {
      out.push(p);
    }
  }
  return out;
}

const isTest = (p) => p.split(path.sep).includes('__tests__') || /\.(test|spec)\.jsx?$/.test(p);

function literals(node, out = []) {
  if (!node || typeof node !== 'object') return out;
  if (Array.isArray(node)) {
    node.forEach((n) => literals(n, out));
    return out;
  }
  // A toast KIND inside toastIcon(…) is not an icon name (it is mapped below).
  if (node.type === 'CallExpression' && nameOf(node.callee) === 'toastIcon') return out;
  if (node.type === 'Literal' && typeof node.value === 'string') out.push(node.value);
  if (node.type === 'TemplateLiteral' && node.expressions.length === 0) out.push(node.quasis[0].value.cooked);
  for (const [k, v] of Object.entries(node)) {
    if (k !== 'loc' && k !== 'range' && v && typeof v === 'object') literals(v, out);
  }
  return out;
}

const nameOf = (n) => {
  if (!n) return '';
  if (n.type === 'Identifier' || n.type === 'JSXIdentifier') return n.name;
  if (n.type === 'MemberExpression') return nameOf(n.property);
  if (n.type === 'Literal') return String(n.value);
  return '';
};

/** Icon names and toast kinds referenced by `source`. */
export function iconReferences(source) {
  const names = new Set();
  const kinds = new Set();
  const ast = espree.parse(source, { ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true } });
  const visit = (node, element) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      node.forEach((n) => visit(n, element));
      return;
    }
    let el = element;
    if (node.type === 'JSXElement') el = nameOf(node.openingElement.name);
    if (node.type === 'JSXAttribute') {
      const attr = nameOf(node.name);
      if (ICONISH.test(attr) || (attr === 'name' && el === 'Icon')) literals(node.value).forEach((v) => names.add(v));
    }
    if (node.type === 'Property' && ICONISH.test(nameOf(node.key))) literals(node.value).forEach((v) => names.add(v));
    if (node.type === 'VariableDeclarator' && ICONISH.test(nameOf(node.id))) {
      literals(node.init).forEach((v) => names.add(v));
    }
    if (node.type === 'CallExpression') {
      const callee = nameOf(node.callee);
      if (callee === 'toastIcon') literals(node.arguments).forEach((k) => kinds.add(k));
      else if (ICONISH.test(callee)) literals(node.arguments).forEach((v) => names.add(v));
      if (callee === 'createElement' && nameOf(node.arguments[0]) === 'Icon') {
        literals(node.arguments[1]).forEach((v) => names.add(v));
      }
    }
    for (const [k, v] of Object.entries(node)) {
      if (k !== 'loc' && k !== 'range' && v && typeof v === 'object') visit(v, el);
    }
  };
  visit(ast, null);
  return { names, kinds };
}

describe('every registered icon is used (review round 1, B6)', () => {
  it('the detector sees each icon position, and not a bare status string', () => {
    const { names, kinds } = iconReferences([
      'const a = <Icon name="play" />;',
      "const b = <Icon name={x ? 'pause' : 'stop'} />;",
      "const c = <ActionButton icon=\"record\" kindIcon={'pose'} />;",
      "const d = { icon: 'camera', label: 'x' };",
      "const TEACH_KIND_ICON = { recording: 'ziel' };",
      "appendSvgIcon(g, 'more', {});",
      "toast('x', { icon: toastIcon('warning') });",
      "React.createElement(Icon, { name: 'hand' });",
      "const status = 'loading';",
      '<input name="search" />;',
    ].join('\n'));
    expect([...names].sort()).toEqual(['camera', 'hand', 'more', 'pause', 'play', 'pose', 'record', 'stop', 'ziel']);
    expect([...kinds]).toEqual(['warning']);
  });

  it('no registry entry is left unreferenced in src', () => {
    const files = walk(SRC).filter((f) => !isTest(f) && !f.startsWith(ICONS_DIR + path.sep));
    expect(files.length).toBeGreaterThan(200);
    const used = new Set();
    const kinds = new Set();
    for (const f of files) {
      const refs = iconReferences(fs.readFileSync(f, 'utf8'));
      refs.names.forEach((n) => used.add(n));
      refs.kinds.forEach((k) => kinds.add(k));
    }
    // A toast kind reaches its icon through toast.js's table. `success` and
    // `error` are every Toaster's defaults (src/toasterOptions.js calls them).
    for (const k of kinds) {
      if (Object.prototype.hasOwnProperty.call(TOAST_ICONS, k)) used.add(TOAST_ICONS[k]);
    }
    expect(ICON_NAMES.filter((n) => !used.has(n))).toEqual([]);
    // Every toast kind is asked for somewhere, too.
    expect(Object.keys(TOAST_ICONS).filter((k) => !kinds.has(k))).toEqual([]);
  });
});
