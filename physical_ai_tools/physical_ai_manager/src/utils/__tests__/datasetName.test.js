// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The browser twin of the server's dataset-name functions (owner decision Q2).
// The fixture is generated from the SERVER reference and shared byte for byte
// with robotis_ai_setup/tests/test_dataset_name_transliteration.py, so a row
// that passes here and there is one name both sides agree on.

import fs from 'fs';
import path from 'path';
import {
  DE_TRANSLITERATION,
  datasetRepoId,
  hfRepoNameProblem,
  safeTaskName,
  safeUserId,
} from '../datasetName';

const FIXTURE = path.join(__dirname, 'datasetName.cases.json');

function loadCases() {
  return JSON.parse(fs.readFileSync(FIXTURE, 'utf8')).cases;
}

describe('the shared name fixture', () => {
  it('has at least 44 rows, including the rows the spec names', () => {
    const cases = loadCases();
    expect(cases.length).toBeGreaterThanOrEqual(44);
    const inputs = cases.map((c) => c.in);
    // a DECOMPOSED row (u + combining diaeresis), an astral row, ẞ, the accents,
    // and the two runs HEAD's sanitiser let through.
    expect(inputs.some((s) => s.normalize('NFC') !== s)).toBe(true);
    expect(inputs.some((s) => [...s].some((ch) => ch.codePointAt(0) > 0xffff))).toBe(true);
    expect(inputs).toContain('ẞ');
    expect(inputs).toContain('Café Crème');
    expect(inputs).toContain('--');
    expect(inputs).toContain('..');
  });

  it('reproduces every row byte for byte (task and user)', () => {
    const bad = loadCases().filter(
      (c) => safeTaskName(c.in) !== c.task || safeUserId(c.in) !== c.user,
    ).map((c) => `${JSON.stringify(c.in)} → ${JSON.stringify(safeTaskName(c.in))}/${JSON.stringify(safeUserId(c.in))}`
      + ` expected ${JSON.stringify(c.task)}/${JSON.stringify(c.user)}`);
    expect(bad).toEqual([]);
  });
});

describe('safeTaskName', () => {
  it('transliterates the German pairs, folds accents and collapses runs', () => {
    expect(safeTaskName('Würfel in die Schale')).toBe('Wuerfel-in-die-Schale');
    expect(safeTaskName('GROẞE')).toBe('GROSSE');
    expect(safeTaskName('Crème brûlée')).toBe('Creme-brulee');
    expect(safeTaskName('a  b')).toBe('a-b');
    expect(safeTaskName('Übung 2.0..')).toBe('Uebung-2.0.');
    expect(safeTaskName('   ')).toBe('');
    expect(safeTaskName(undefined)).toBe('');
  });

  it('treats a decomposed umlaut like the composed one (NFC first)', () => {
    expect(safeTaskName('über')).toBe('ueber');
  });

  it('keeps the transliteration table the server uses', () => {
    expect(DE_TRANSLITERATION).toEqual([
      ['ä', 'ae'], ['ö', 'oe'], ['ü', 'ue'], ['Ä', 'Ae'], ['Ö', 'Oe'], ['Ü', 'Ue'], ['ß', 'ss'], ['ẞ', 'SS'],
    ]);
    expect(Object.isFrozen(DE_TRANSLITERATION)).toBe(true);
  });
});

describe('safeUserId (HEAD rule: no transliteration, no collapse)', () => {
  it('replaces one code point with one dash — the u flag is mandatory', () => {
    // Without the `u` flag an astral character is two UTF-16 code units and
    // turns into TWO dashes; Python replaces the one code point with one.
    expect(safeUserId('a😀b')).toBe('a-b');
    expect('a😀b'.replace(/[^a-zA-Z0-9._-]/g, '-')).toBe('a--b'); // the regression this guards
  });

  it('falls back to unknown-user for empty and dot-only ids', () => {
    expect(safeUserId('')).toBe('unknown-user');
    expect(safeUserId(undefined)).toBe('unknown-user');
    expect(safeUserId('...')).toBe('unknown-user');
    expect(safeUserId('--')).toBe('unknown-user');
    expect(safeUserId('schule-A')).toBe('schule-A');
  });
});

describe('datasetRepoId', () => {
  it('builds <user>/<robotType>_<task> like the server', () => {
    expect(datasetRepoId('schule-A', 'omx_f', 'Würfel in die Schale'))
      .toBe('schule-A/omx_f_Wuerfel-in-die-Schale');
    expect(datasetRepoId(undefined, 'omx_f', 'x')).toBe('unknown-user/omx_f_x');
  });
});

describe('hfRepoNameProblem', () => {
  it.each([
    ['omx_f_Wuerfel', null],
    ['', 'empty'],
    ['x'.repeat(96), null],
    ['x'.repeat(97), 'too_long'],
    ['omx_f_a--b', 'double'],
    ['omx_f_a..b', 'double'],
    ['omx_f_a.', 'edge'],
    ['.omx', 'edge'],
    ['-omx', 'edge'],
    ['omx-', 'edge'],
    ['omx_f_repo.git', 'git'],
    ['omx_f_Uebung-2.0', null],
  ])('%j → %j', (name, problem) => {
    expect(hfRepoNameProblem(name)).toBe(problem);
  });
});
