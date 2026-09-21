/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code editor's German catalogue, held to two things a reviewer cannot see
// at a glance.
//
// (1) EVERY key is referenced. Three were not when the editor landed
// (EDITOR_FAILED, RUN_EMPTY, SAVE_EMPTY) and they were not harmless: two were
// near-duplicates of ERR_NO_FILES, which both the run and the save path
// already return, so a reader had to work out which of three identical
// sentences was the live one; the third named a failure nothing handled, and
// wiring it is what gave CodeWorkspace its editor boundary. An unreferenced
// student-facing string is either a missing feature or a duplicate, and both
// are worth failing on.
//
// (2) The catalogue is GERMAN, spelled with literal umlauts — `german-strings-
// lint` is a Python AST walker over `cloud_training_api/app` and `pi_agent`
// and structurally cannot see this file (CLAUDE.md §1), so the rule is
// enforced here instead of by review alone.

import fs from 'fs';
import path from 'path';
import { describe, expect, it } from 'vitest';
import { CODE_DE, formatCode } from '../codeMessagesDe';

const SRC = path.resolve(__dirname, '..', '..', '..', '..');

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__tests__' || entry.name === 'node_modules') continue;
      walk(abs, out);
    } else if (/\.jsx?$/.test(entry.name)) {
      out.push(abs);
    }
  }
  return out;
}

const SOURCES = walk(SRC)
  .filter((abs) => !abs.endsWith(path.join('code', 'codeMessagesDe.js')))
  .map((abs) => fs.readFileSync(abs, 'utf8'));

describe('the code editor’s German catalogue', () => {
  it('actually scanned the tree', () => {
    // Zero-file floor: a directory move must not make the assertions below
    // pass having read nothing.
    expect(SOURCES.length).toBeGreaterThan(100);
    expect(Object.keys(CODE_DE).length).toBeGreaterThan(30);
  });

  it('has no key the app never uses', () => {
    const unreferenced = Object.keys(CODE_DE).filter(
      (key) => !SOURCES.some((text) => new RegExp(`CODE_DE\\.${key}\\b`).test(text)),
    );
    expect(unreferenced).toEqual([]);
  });

  it('is German, with literal umlauts', () => {
    // Offenders are COLLECTED rather than asserted one by one, so a failure
    // names every bad key at once instead of the first.
    const entries = Object.entries(CODE_DE);
    expect(entries.filter(([, v]) => typeof v !== 'string' || !v.length).map(([k]) => k))
      .toEqual([]);
    // No `ä`-style escape and no HTML entity standing in for a letter a
    // German keyboard types directly.
    expect(entries.filter(([, v]) => /\\u00[0-9a-f]{2}/i.test(v)).map(([k]) => k))
      .toEqual([]);
    expect(entries.filter(([, v]) => /&(?:auml|ouml|uuml|szlig);/.test(v)).map(([k]) => k))
      .toEqual([]);
    // A spot check that the literals really are literals.
    expect(CODE_DE.FILE_DELETE).toBe('Löschen');
    expect(CODE_DE.ERR_FILE_TOO_BIG).toContain('zu groß');
  });

  it('fills every placeholder its refusals declare', () => {
    // A `{1}` in a sentence whose call site passes one argument ships the raw
    // brace to a student.
    expect(formatCode(CODE_DE.ERR_WRONG_EXT, 'Main.java', 'python'))
      .toBe('Die Datei „Main.java“ passt nicht zur Sprache python.');
    expect(formatCode(CODE_DE.ERR_TOO_MANY_FILES, 32)).not.toMatch(/\{\d\}/);
    expect(formatCode(CODE_DE.ERR_FILE_TOO_BIG, 'gross.py', 64)).not.toMatch(/\{\d\}/);
  });
});
