/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The fence that keeps the pre-052 credit wording out of the UI for good
// (credit system v2, spec §10.6, review M2 + review 2 m6). Training is paid in
// training HOURS from the teacher's pool and a started hour is charged, so no
// text a person reads may say „Credit", promise that something was
// „erstattet", or call the hours „Trainingsguthaben".
//
// Every non-test src/**/*.{js,jsx} is tokenised with espree (the parser eslint
// uses, as noIconGlyphs.test.js does) and the three words are looked for in
// every String, Template and JSXText token — JSX attribute values included,
// after decoding \u / \x escapes and HTML entities, so a word cannot hide in
// an escape. Identifiers (`trainingCredits`), CSS classes (`su-trn-credits`)
// and API paths (`/credits`) are no such text: the first is no string token,
// the other two never carry a capital C.
//
// The one exception is a MODULE SPECIFIER (review 3 m1): a String token whose
// previous token is `from`, or whose previous two tokens are `import (` or
// `require (` — `'./WorkgroupCreditsModal'`, `import('./CreditHistoryDrawer')`
// are file names, not text.

import fs from 'fs';
import path from 'path';
import * as espree from 'espree';
import { describe, expect, it } from 'vitest';

const SRC = path.resolve(__dirname, '../..');
export const LEGACY_WORDING = /erstattet|Credit|Trainingsguthaben/;

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

const isTestFile = (p) => p.split(path.sep).includes('__tests__') || /\.(test|spec)\.jsx?$/.test(p);
const sourceFiles = () => walk(SRC).filter((p) => !isTestFile(p));

const NAMED_ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

function decode(raw) {
  return String(raw)
    .replace(/\\u\{([0-9a-fA-F]+)\}/g, (_m, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/\\u([0-9a-fA-F]{4})/g, (_m, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/\\x([0-9a-fA-F]{2})/g, (_m, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/&#x([0-9a-fA-F]+);/g, (_m, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#([0-9]+);/g, (_m, dec) => String.fromCodePoint(parseInt(dec, 10)))
    .replace(/&([a-zA-Z]+);/g, (m, name) => (NAMED_ENTITIES[name] !== undefined ? NAMED_ENTITIES[name] : m));
}

/** True when the String token at `i` is a module specifier (review 3 m1). */
export function isModuleSpecifier(tokens, i) {
  const t = tokens[i];
  if (!t || t.type !== 'String') return false;
  const prev = tokens[i - 1];
  const prev2 = tokens[i - 2];
  if (prev && prev.value === 'from') return true;
  return !!(prev && prev.value === '(' && prev2 && (prev2.value === 'import' || prev2.value === 'require'));
}

/** Every legacy word in the text tokens of `source`: `[{line, text}]`. Throws on a parse error. */
export function legacyWordingHits(source) {
  const tokens = espree.tokenize(source, {
    ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true }, loc: true,
  });
  const hits = [];
  tokens.forEach((token, i) => {
    if (token.type !== 'String' && token.type !== 'Template' && token.type !== 'JSXText') return;
    if (isModuleSpecifier(tokens, i)) return;
    const text = decode(token.value);
    if (LEGACY_WORDING.test(text)) hits.push({ line: token.loc.start.line, text: text.slice(0, 90) });
  });
  return hits;
}

describe('no legacy credit wording in the UI (spec §10.6)', () => {
  it('scans a real tree', () => {
    expect(sourceFiles().length).toBeGreaterThan(300);
  });

  it('no non-test source says „Credit", „erstattet" or „Trainingsguthaben" outside a module specifier', () => {
    const found = [];
    for (const file of sourceFiles()) {
      // A file the parser cannot read fails here instead of being skipped.
      for (const hit of legacyWordingHits(fs.readFileSync(file, 'utf8'))) {
        found.push(`${path.relative(SRC, file)}:${hit.line}: ${hit.text}`);
      }
    }
    expect(found).toEqual([]);
  });
});

describe('the fence has teeth', () => {
  it('finds each word in a string, a template, JSX text and a JSX attribute', () => {
    expect(legacyWordingHits("const a = 'Credits: 3';")).toHaveLength(1);
    const BT = String.fromCharCode(96); // a backtick: a template token
    expect(legacyWordingHits(`const a = ${BT}Credits: ${BT};`)).toHaveLength(1);
    expect(legacyWordingHits('const a = <p>Trainingsguthaben wurde erstattet.</p>;')).toHaveLength(1);
    expect(legacyWordingHits('const a = <b title="Credit hinzufügen" />;')).toHaveLength(1);
    expect(legacyWordingHits("const a = 'Das Guthaben wurde erstattet.';")).toHaveLength(1);
  });

  it('decodes escapes and entities', () => {
    expect(legacyWordingHits("const a = 'Cr\\u0065dit';")).toHaveLength(1);
    expect(legacyWordingHits('const a = <p>Cr&#101;dit</p>;')).toHaveLength(1);
  });

  it('exempts module specifiers, identifiers, CSS classes and API paths', () => {
    expect(legacyWordingHits("import X from './WorkgroupCreditsModal';")).toEqual([]);
    expect(legacyWordingHits("export { X } from './admin/TeacherCreditsModal';")).toEqual([]);
    expect(legacyWordingHits("const X = import('./CreditHistoryDrawer');")).toEqual([]);
    expect(legacyWordingHits("const X = require('./CreditHistoryDrawer');")).toEqual([]);
    expect(legacyWordingHits("const trainingCredits = 1; const c = 'su-trn-credits'; const u = '/credits';")).toEqual([]);
  });

  it('a specifier-looking string elsewhere is still text', () => {
    expect(legacyWordingHits("const a = f('Credits');")).toHaveLength(1);
    expect(legacyWordingHits("const a = ['from', 'Credit'];")).toHaveLength(1);
  });

  it('passes every §10 sentence that names the hours', () => {
    const SPEC = [
      'Keine freien Trainingsstunden. Frag deine Lehrkraft nach weiteren Stunden.',
      'Std. frei · 2 Std. reserviert · Gruppenguthaben',
      'Was übrig bleibt, geht nach dem Training automatisch zurück an dein Guthaben.',
      'Die Stunden kommen aus dem Guthaben deiner Arbeitsgruppe „Rot“.',
      'Frei im Lehrer-Pool: 3 Stunden. Beim Löschen der Lehrkraft wird dieses Guthaben entzogen.',
      'Vor der Umstellung: 12 (alte Einheit)',
      'Die Stunden werden direkt dem Guthaben gutgeschrieben, das das Training bezahlt hat – nicht aus dem Lehrer-Pool.',
    ];
    for (const s of SPEC) expect(legacyWordingHits(`const a = ${JSON.stringify(s)};`)).toEqual([]);
  });
});
