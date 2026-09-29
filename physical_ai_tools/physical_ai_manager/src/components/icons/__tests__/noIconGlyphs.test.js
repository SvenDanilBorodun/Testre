/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The fence that keeps emoji and symbol glyphs out of the UI for good (owner
// decision D7: one icon style, components/icons). Three rules:
//
//   1. No shipped source string — a String, Template or JSX text/attribute
//      token of any non-test src/**/*.{js,jsx}, after decoding \u / \u{} / \x
//      escapes and HTML entities — nor any string in src/**/*.json or
//      public/tutorials/*.json, holds a character of the icon ranges below. A
//      token that is nothing but „×" is an icon too. Comments are not tokens,
//      so they may still name a glyph. Typography stays allowed: ← ↑ → ↓ ↔,
//      „×" inside text, … · — „ ".
//      Nor may a glyph be BUILT from a number (`String.fromCodePoint(0x25B6)`,
//      `String.fromCharCode(…)` with a literal in the ranges), be written into
//      the page HTML (index.html, public/**/*.html), or be a CSS `content:`
//      value in src/**/*.css.
//   2. Only src/components/icons/** imports react-icons, and only its /lu and
//      /lib entry points (the registry is the one importer of /lu).
//   3. An inline <svg> (JSX, markup, or a data:image/svg URI in a string)
//      appears only in the files listed in INLINE_SVG_ALLOWED — each a chart,
//      an illustration, the brand mark or a Blockly image — never as a
//      hand-drawn icon beside the icon set (review round 1, R1-O1).
//
// Tokenised with espree (the parser eslint itself uses), so a glyph can hide
// in no string the parser sees.

import fs from 'fs';
import path from 'path';
import * as espree from 'espree';
import { describe, expect, it } from 'vitest';

const APP_ROOT = path.resolve(__dirname, '../../../..');
const SRC = path.join(APP_ROOT, 'src');
const TUTORIALS = path.join(APP_ROOT, 'public', 'tutorials');
const ICONS_DIR = path.join(SRC, 'components', 'icons');

export const BANNED_RANGES = Object.freeze([
  [0x1F000, 0x1FAFF], // emoji and pictographs
  [0x2300, 0x23FF], // miscellaneous technical (⏺ ⏸ ⏳ ⌒ …)
  [0x25A0, 0x25FF], // geometric shapes (■ ▶ ▾ ● …)
  [0x2600, 0x27BF], // miscellaneous symbols and dingbats (⚠ ✓ ✕ ✎ ★ …)
  [0x2195, 0x21FF], // arrows past ← ↑ → ↓ ↔ (↩ ↪ ↶ ↻ ⇪ …)
  [0x2900, 0x297F], // supplemental arrows (⤢ …)
  [0x2B00, 0x2BFF], // miscellaneous symbols and arrows
  [0x2139, 0x2139], // ℹ
  [0x22EF, 0x22EF], // ⋯
  [0xFE0F, 0xFE0F], // emoji presentation selector
  [0x20E3, 0x20E3], // combining keycap
  [0x2460, 0x24FF], // enclosed alphanumerics (ⓘ ① …)
  [0x27C0, 0x27FF], // misc. mathematical symbols A + supplemental arrows A (⟳ ⟲ …)
  [0x2800, 0x28FF], // Braille patterns (the old ⠋⠙⠹ spinner)
]);

export function bannedCodePoints(text) {
  const out = [];
  for (const ch of String(text)) {
    const cp = ch.codePointAt(0);
    if (BANNED_RANGES.some(([lo, hi]) => cp >= lo && cp <= hi)) out.push(`U+${cp.toString(16).toUpperCase()}`);
  }
  return out;
}

const NAMED_ENTITIES = Object.freeze({
  times: '×', hellip: '…', middot: '·', mdash: '—', ndash: '–', nbsp: ' ', amp: '&', lt: '<', gt: '>',
  quot: '"', apos: "'", bull: '•', laquo: '«', raquo: '»', larr: '←', rarr: '→', uarr: '↑', darr: '↓',
  harr: '↔', check: '✓', cross: '✗', star: '☆', starf: '★', hearts: '♥', spades: '♠',
});

// Every way a source string can spell a code point without writing it.
export function decodeSourceText(raw) {
  return String(raw)
    .replace(/\\u\{([0-9a-fA-F]+)\}/g, (_m, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/\\u([0-9a-fA-F]{4})/g, (_m, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/\\x([0-9a-fA-F]{2})/g, (_m, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/&#x([0-9a-fA-F]+);/g, (_m, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#([0-9]+);/g, (_m, dec) => String.fromCodePoint(parseInt(dec, 10)))
    .replace(/&([a-zA-Z]+);/g, (m, name) => (NAMED_ENTITIES[name] !== undefined ? NAMED_ENTITIES[name] : m));
}

// The text a token carries, without its delimiters.
function tokenText(token) {
  const v = token.value;
  if (token.type === 'String') return v.slice(1, -1);
  if (token.type === 'Template') return v.replace(/^[`}]/, '').replace(/(\$\{|`)$/, '');
  if (token.type === 'JSXText' && /^["']/.test(v) && v.length >= 2 && v[v.length - 1] === v[0]) {
    return v.slice(1, -1);
  }
  return v;
}

function walk(dir, keep, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'node_modules') walk(p, keep, out);
    } else if (keep(p)) {
      out.push(p);
    }
  }
  return out;
}

const isTestFile = (p) => p.split(path.sep).includes('__tests__') || /\.(test|spec)\.jsx?$/.test(p);
const sourceFiles = () => walk(SRC, (p) => /\.jsx?$/.test(p) && !isTestFile(p));

/** Every banned glyph in the string tokens of `source`: `[{line, text, codePoints}]`. */
export function glyphHitsInSource(source) {
  const tokens = espree.tokenize(source, {
    ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true }, loc: true,
  });
  const hits = [];
  for (const token of tokens) {
    if (token.type !== 'String' && token.type !== 'Template' && token.type !== 'JSXText') continue;
    const text = decodeSourceText(tokenText(token));
    const codePoints = bannedCodePoints(text);
    if (text.trim() === '×') codePoints.push('a lone ×');
    if (codePoints.length) hits.push({ line: token.loc.start.line, text: text.slice(0, 80), codePoints });
  }
  return hits;
}

const inBanned = (cp) => BANNED_RANGES.some(([lo, hi]) => cp >= lo && cp <= hi);

function walkAst(node, visit) {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node)) { node.forEach((n) => walkAst(n, visit)); return; }
  if (typeof node.type === 'string') visit(node);
  for (const [k, v] of Object.entries(node)) {
    if (k !== 'loc' && k !== 'range' && v && typeof v === 'object') walkAst(v, visit);
  }
}

/**
 * `String.fromCodePoint(…)` / `String.fromCharCode(…)` calls whose literal
 * argument lands in an icon range: `[{line, codePoints}]`.
 */
export function builtGlyphHits(source) {
  const ast = espree.parse(source, {
    ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true }, loc: true,
  });
  const hits = [];
  walkAst(ast, (node) => {
    if (node.type !== 'CallExpression' || node.callee.type !== 'MemberExpression') return;
    const { object, property } = node.callee;
    if (object.type !== 'Identifier' || object.name !== 'String') return;
    const method = property.type === 'Identifier' ? property.name : property.value;
    if (method !== 'fromCodePoint' && method !== 'fromCharCode') return;
    const codePoints = node.arguments
      .filter((a) => a.type === 'Literal' && typeof a.value === 'number' && inBanned(a.value))
      .map((a) => `U+${a.value.toString(16).toUpperCase()}`);
    if (codePoints.length) hits.push({ line: node.loc.start.line, codePoints });
  });
  return hits;
}

// CSS escapes (`\25B6 `, `\1F600`) in a `content:` value.
export function cssContentGlyphs(css) {
  const hits = [];
  for (const m of String(css).matchAll(/content\s*:\s*(["'])((?:\\.|(?!\1).)*)\1/g)) {
    const value = m[2].replace(/\\([0-9a-fA-F]{1,6})\s?/g, (_x, hex) => String.fromCodePoint(parseInt(hex, 16)));
    const codePoints = bannedCodePoints(value);
    if (codePoints.length) hits.push({ value: m[2], codePoints });
  }
  return hits;
}

function jsonStrings(value, at, out) {
  if (typeof value === 'string') out.push({ at, value });
  else if (Array.isArray(value)) value.forEach((v, i) => jsonStrings(v, `${at}[${i}]`, out));
  else if (value && typeof value === 'object') {
    Object.entries(value).forEach(([k, v]) => { jsonStrings(k, `${at}{key}`, out); jsonStrings(v, `${at}.${k}`, out); });
  }
  return out;
}

describe('no icon glyph in the shipped UI (D7)', () => {
  it('the detector sees every spelling of a glyph and allows the typography', () => {
    const src = [
      'const a = "▶ Start";',
      "const b = '\\u25B6';",
      "const c = '\\u{1F600}';",
      // eslint-disable-next-line no-template-curly-in-string
      'const d = `\\u2713 ${x} ✎`;',
      'const e = <p title="&#10003;">A &#x2139; B</p>;',
      "const f = '×';",
      '// a comment may say ▶',
      "const g = 'x ← y → z ↑ ↓ ↔ · … — „Zitat“ 3 × 4';",
    ].join('\n');
    const hits = glyphHitsInSource(src);
    // Line 4 is two template chunks (before and after `${x}`), each with a glyph.
    expect(hits.map((h) => h.line)).toEqual([1, 2, 3, 4, 4, 5, 5, 6]);
    expect(hits.find((h) => h.line === 6).codePoints).toEqual(['a lone ×']);
    expect(hits.some((h) => h.line === 7 || h.line === 8)).toBe(false);
  });

  it('no String, Template or JSX text in any non-test src file holds an icon glyph', () => {
    const files = sourceFiles();
    // A zero-file scan must never pass.
    expect(files.length).toBeGreaterThan(200);
    const violations = [];
    for (const file of files) {
      const hits = glyphHitsInSource(fs.readFileSync(file, 'utf8'));
      for (const h of hits) {
        violations.push(`${path.relative(APP_ROOT, file)}:${h.line} ${h.codePoints.join(',')} ${JSON.stringify(h.text)}`);
      }
    }
    expect(violations).toEqual([]);
  });

  it('the detector sees a glyph built from a number and one in a CSS content value', () => {
    const src = [
      'const a = String.fromCodePoint(0x25B6);',
      'const b = String.fromCharCode(10003);',
      'const c = String.fromCodePoint(0x2800 + i);',
      "const d = String['fromCodePoint'](0x24D8);",
      'const e = String.fromCodePoint(65, 0x1F600);',
      'const f = String.fromCharCode(0x2192);',
    ].join('\n');
    expect(builtGlyphHits(src).map((h) => h.line)).toEqual([1, 2, 4, 5]);
    expect(cssContentGlyphs(".a::before { content: '\\25B6'; } .b::after { content: \"→\"; }"))
      .toEqual([{ value: '\\25B6', codePoints: ['U+25B6'] }]);
    expect(cssContentGlyphs('.c::before { content: ""; }')).toEqual([]);
    expect(bannedCodePoints('\u24D8 \u27F3 \u280B')).toEqual(['U+24D8', 'U+27F3', 'U+280B']);
  });

  it('no non-test src file BUILDS a glyph from a number (String.fromCodePoint / fromCharCode)', () => {
    const files = sourceFiles();
    expect(files.length).toBeGreaterThan(200);
    const violations = [];
    for (const file of files) {
      for (const h of builtGlyphHits(fs.readFileSync(file, 'utf8'))) {
        violations.push(`${path.relative(APP_ROOT, file)}:${h.line} ${h.codePoints.join(',')}`);
      }
    }
    expect(violations).toEqual([]);
  });

  it('the page HTML and every src/**/*.css `content:` value hold no icon glyph', () => {
    // Vite's page is the app-root index.html (public/ holds static assets).
    const pages = [path.join(APP_ROOT, 'index.html'), ...walk(path.join(APP_ROOT, 'public'), (p) => p.endsWith('.html'))];
    expect(fs.existsSync(pages[0])).toBe(true);
    for (const page of pages) {
      expect(`${path.relative(APP_ROOT, page)} ${bannedCodePoints(decodeSourceText(fs.readFileSync(page, 'utf8')))}`)
        .toBe(`${path.relative(APP_ROOT, page)} `);
    }
    const cssFiles = walk(SRC, (p) => p.endsWith('.css'));
    expect(cssFiles.length).toBeGreaterThan(0);
    const violations = [];
    for (const file of cssFiles) {
      for (const h of cssContentGlyphs(fs.readFileSync(file, 'utf8'))) {
        violations.push(`${path.relative(APP_ROOT, file)} ${h.codePoints.join(',')} ${JSON.stringify(h.value)}`);
      }
    }
    expect(violations).toEqual([]);
  });

  it('no string in src/**/*.json or public/tutorials/*.json holds an icon glyph', () => {
    const files = [
      ...walk(SRC, (p) => p.endsWith('.json') && !isTestFile(p)),
      ...walk(TUTORIALS, (p) => p.endsWith('.json')),
    ];
    expect(files.some((f) => f.startsWith(TUTORIALS))).toBe(true);
    const violations = [];
    for (const file of files) {
      const strings = jsonStrings(JSON.parse(fs.readFileSync(file, 'utf8')), '', []);
      for (const { at, value } of strings) {
        const codePoints = bannedCodePoints(decodeSourceText(value));
        if (value.trim() === '×') codePoints.push('a lone ×');
        if (codePoints.length) violations.push(`${path.relative(APP_ROOT, file)}${at} ${codePoints.join(',')}`);
      }
    }
    expect(violations).toEqual([]);
  });
});

// Where an inline <svg> may stand, and why. Everything else draws its icons
// through components/icons (review round 1, R1-O1).
export const INLINE_SVG_ALLOWED = Object.freeze({
  'src/components/EbUI.js': 'the EduBotics brand mark (LogoMark), not an icon',
  'src/components/CollisionModal.js': 'the remedy illustration (RemedyDiagram)',
  'src/components/CompactSystemStatus.js': 'the circular usage gauge, a chart',
  'src/components/Home/RobotHero.jsx': 'the Startseite illustration',
  'src/components/Record/PhaseOverlay.jsx':
    'the Aufnahme countdown ring and the warm-up/reset pictograms (illustrations)',
  'src/components/Workshop/SimScene.jsx': 'the 2D simulator scene',
  'src/components/Workshop/CameraFeedOverlay.jsx': 'the marker overlay on the camera image',
  'src/components/Workshop/teach/ReviewStrip.jsx': 'the review chart of a take',
  'src/components/Workshop/blocks/control.js':
    '@blockly/block-plus-minus\'s own ⊕/⊖ field images, byte-identical to the plugin\'s other blocks',
  'src/components/Workshop/blocks/destinations.js': 'the drive chip, whose play glyph IS iconMarkup(\'play\')',
});

function hasInlineSvg(source) {
  const tokens = espree.tokenize(source, {
    ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true },
  });
  for (let i = 0; i < tokens.length; i += 1) {
    const t = tokens[i];
    if (t.type === 'JSXIdentifier' && t.value === 'svg' && tokens[i - 1] && tokens[i - 1].value === '<') return true;
    if ((t.type === 'String' || t.type === 'Template') && /<svg[\s>]|data:image\/svg/i.test(t.value)) return true;
  }
  return false;
}

describe('an inline <svg> only where the list says why (R1-O1)', () => {
  it('no other non-test src file draws an inline svg, and every allowed file still does', () => {
    const files = sourceFiles().filter((f) => !f.startsWith(ICONS_DIR + path.sep));
    const drawing = files.filter((f) => hasInlineSvg(fs.readFileSync(f, 'utf8')))
      .map((f) => path.relative(APP_ROOT, f).split(path.sep).join('/'));
    expect(drawing.filter((f) => !Object.prototype.hasOwnProperty.call(INLINE_SVG_ALLOWED, f))).toEqual([]);
    // A stale entry (the file no longer draws one) must be removed from the list.
    expect(Object.keys(INLINE_SVG_ALLOWED).filter((f) => !drawing.includes(f))).toEqual([]);
  });
});

// A CSS-drawn spinner (a rounded border turned by `animate-spin`) is an icon
// in another style: every spinner is <Icon name="loading" className="animate-spin" />.
export function cssSpinnerHits(source) {
  const ast = espree.parse(source, {
    ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true }, loc: true,
  });
  const strings = (node, out = []) => {
    if (!node || typeof node !== 'object') return out;
    if (Array.isArray(node)) { node.forEach((n) => strings(n, out)); return out; }
    if (node.type === 'Literal' && typeof node.value === 'string') out.push(node.value);
    if (node.type === 'TemplateLiteral') node.quasis.forEach((q) => out.push(q.value.cooked));
    for (const [k, v] of Object.entries(node)) {
      if (k !== 'loc' && k !== 'range' && v && typeof v === 'object') strings(v, out);
    }
    return out;
  };
  const spins = (list) => list.some((t) => /(^|\s)animate-spin(\s|$)/.test(t));
  const bordered = (list) => list.some((t) => /(^|\s)(rounded-full|border-[a-z0-9-/]+)(\s|$)/.test(t));
  const hits = [];
  walkAst(ast, (node) => {
    if (node.type === 'JSXOpeningElement') {
      const name = node.name && node.name.name;
      const cls = node.attributes.find((a) => a.type === 'JSXAttribute' && a.name.name === 'className');
      if (cls && name !== 'Icon' && spins(strings(cls.value)) && bordered(strings(cls.value))) {
        hits.push(node.loc.start.line);
      }
    }
    if (node.type === 'CallExpression' && node.callee.type === 'Identifier' && node.callee.name === 'clsx') {
      const list = strings(node.arguments);
      if (spins(list) && bordered(list)) hits.push(node.loc.start.line);
    }
  });
  return hits;
}

describe('no CSS-drawn spinner (R1-O1: one style for the loader too)', () => {
  it('the detector sees a bordered spinning div and a clsx one, not an Icon', () => {
    const src = [
      'const a = <div className="animate-spin rounded-full h-4 w-4 border-b-2" />;',
      "const b = clsx('animate-spin', 'rounded-full', 'border-b-2');",
      'const c = <Icon name="loading" className="animate-spin text-teal-600" />;',
      "const d = <Icon name=\"refresh\" className={busy ? 'animate-spin' : ''} />;",
    ].join('\n');
    expect(cssSpinnerHits(src)).toEqual([1, 2]);
  });

  it('no non-test src file draws one', () => {
    const files = sourceFiles();
    expect(files.length).toBeGreaterThan(200);
    const violations = [];
    for (const file of files) {
      for (const line of cssSpinnerHits(fs.readFileSync(file, 'utf8'))) {
        violations.push(`${path.relative(APP_ROOT, file)}:${line}`);
      }
    }
    expect(violations).toEqual([]);
  });
});

describe('react-icons has exactly one importer (D7)', () => {
  const IMPORT_RE = /(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|^\s*import\s+)['"](react-icons(?:\/[^'"]*)?)['"]/gm;

  it('only src/components/icons/** imports react-icons, and only /lu and /lib', () => {
    const files = walk(SRC, (p) => /\.jsx?$/.test(p));
    expect(files.length).toBeGreaterThan(200);
    const importers = [];
    const violations = [];
    for (const file of files) {
      const source = fs.readFileSync(file, 'utf8');
      for (const m of source.matchAll(IMPORT_RE)) {
        const rel = path.relative(APP_ROOT, file);
        importers.push(`${rel} ${m[1]}`);
        const inIcons = file.startsWith(ICONS_DIR + path.sep) && !isTestFile(file);
        if (!inIcons || !['react-icons/lu', 'react-icons/lib'].includes(m[1])) violations.push(`${rel} ${m[1]}`);
      }
    }
    expect(violations).toEqual([]);
    // The two sanctioned importers really are the ones found.
    expect(importers.sort()).toEqual([
      'src/components/icons/custom.js react-icons/lib',
      'src/components/icons/registry.js react-icons/lu',
    ]);
  });
});
