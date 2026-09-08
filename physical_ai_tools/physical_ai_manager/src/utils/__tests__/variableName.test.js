/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-49 — the `[VAR:name=json]` name gate, as a pure contract.
//
// This file pins the PREDICATE. It is deliberately not the whole story: a
// predicate test cannot see whether anyone calls it, and that is exactly how
// RS-49 survived its own fix (the hook was widened, a second stricter copy in
// `workshopSlice::setVariable` was not, and this shape of test stayed green
// while the Variablen panel stayed empty). The two outcome suites are
//   • features/workshop/__tests__/workshopSlice.variables.test.js — the store
//   • components/Workshop/__tests__/VariableInspector.test.jsx     — the panel
// and the rejecting direction of the wire call site is in
//   • hooks/__tests__/useRosTopicSubscription.variables.test.js
//
// MEASURED against Blockly 12.5.1's real interactive gate,
// `Variables.promptName` — the single funnel for both „Variable erstellen" and
// Rename. Its entire normalization is `.replace(/[\s\xa0]+/g,' ').trim()`, and
// an empty result creates no variable. Driving it headless over 32 realistic
// names, 28 came back as names Blockly WILL create and the old C-identifier
// regex dropped — including "meine Zahl", "Anzahl Würfel", "zähler-2",
// "2te_zahl", "3", "Öl-Stand" and "Test 123".

import { isDisplayableVariableName, VAR_NAME_MAX_LEN } from '../variableName';
import {
  BLOCKLY_REAL_NAMES,
  NAMES_THE_OLD_REGEX_DROPPED,
} from './blocklyVariableNames.fixture';

describe('isDisplayableVariableName — names Blockly really produces', () => {
  test.each(BLOCKLY_REAL_NAMES)('accepts %j', (name) => {
    expect(isDisplayableVariableName(name)).toBe(true);
  });

  test('the old C-identifier regex rejected most of those', () => {
    // 15 of the 19 — this is the size of the bug, not a decorative number.
    expect(NAMES_THE_OLD_REGEX_DROPPED).toHaveLength(15);
    for (const name of NAMES_THE_OLD_REGEX_DROPPED) {
      expect(isDisplayableVariableName(name)).toBe(true);
    }
  });
});

describe('isDisplayableVariableName — guards that must NOT be weakened', () => {
  test('rejects empty and whitespace-only names', () => {
    expect(isDisplayableVariableName('')).toBe(false);
    expect(isDisplayableVariableName('   ')).toBe(false);
    expect(isDisplayableVariableName('\t\n ')).toBe(false);
  });

  test('keeps the 64-character cap', () => {
    expect(VAR_NAME_MAX_LEN).toBe(64);
    expect(isDisplayableVariableName('a'.repeat(64))).toBe(true);
    expect(isDisplayableVariableName('a'.repeat(65))).toBe(false);
  });

  test('rejects the sentinel framing characters = [ ]', () => {
    // `[VAR:name=json]` is parsed with /^\[VAR:([^=]+)=(.*)\]$/ — an '=' in the
    // name re-splits the frame and the value absorbs the rest of the name.
    expect(isDisplayableVariableName('a=b')).toBe(false);
    expect(isDisplayableVariableName('a]b')).toBe(false);
    expect(isDisplayableVariableName('a[0]')).toBe(false);
  });

  test('rejects control characters (C0, DEL, C1)', () => {
    // Blockly's own collapse only folds `\s`; NUL / BEL / ESC are NOT
    // `\s` and survive it — and rosbridge authenticates nobody, so the wire is
    // untrusted regardless of what the editor can produce.
    expect(isDisplayableVariableName('a\u0000b')).toBe(false);
    expect(isDisplayableVariableName('a\u0007b')).toBe(false);
    expect(isDisplayableVariableName('a\u001Bb')).toBe(false);
    expect(isDisplayableVariableName('a\u007Fb')).toBe(false);
    expect(isDisplayableVariableName('a\u0085b')).toBe(false);
    expect(isDisplayableVariableName('a\nb')).toBe(false);
  });

  test('rejects non-strings', () => {
    expect(isDisplayableVariableName(null)).toBe(false);
    expect(isDisplayableVariableName(undefined)).toBe(false);
    expect(isDisplayableVariableName(42)).toBe(false);
    expect(isDisplayableVariableName({})).toBe(false);
  });

  test("the prototype-pollution names are NOT this predicate's job", () => {
    // They are perfectly displayable; they are simply unsafe as object keys.
    // Each CALL SITE keeps its own check, so deleting one is visible to a test
    // — see the reducer and hook suites. If this ever starts returning false,
    // those two mutations stop being killable independently.
    expect(isDisplayableVariableName('__proto__')).toBe(true);
    expect(isDisplayableVariableName('constructor')).toBe(true);
    expect(isDisplayableVariableName('prototype')).toBe(true);
  });
});
