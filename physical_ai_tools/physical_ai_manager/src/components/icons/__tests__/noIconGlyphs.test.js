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
// decision D7: one icon style, components/icons). Two rules:
//
//   1. No shipped source string — a String, Template or JSX text/attribute
//      token of any non-test src/**/*.{js,jsx}, after decoding \u / \u{} / \x
//      escapes and HTML entities — nor any string in src/**/*.json or
//      public/tutorials/*.json, holds a character of the icon ranges below. A
//      token that is nothing but „×" is an icon too. Comments are not tokens,
//      so they may still name a glyph. Typography stays allowed: ← ↑ → ↓ ↔,
//      „×" inside text, … · — „ ".
//   2. Only src/components/icons/** imports react-icons, and only its /lu and
//      /lib entry points (the registry is the one importer of /lu).
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
