/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Shared fixture — NOT a suite (the vitest `test.include` glob is
// `src/**/*.{test,spec}.{js,jsx}`, so this file is imported, never collected).
//
// Every entry is a verbatim `Variables.promptName` OUTPUT recorded from Blockly
// 12.5.1 — i.e. a name a student can actually end up with after „Variable
// erstellen" or Rename. THREE suites assert against this one list, on purpose:
// the predicate (utils/__tests__/variableName.test.js), the store
// (features/workshop/__tests__/workshopSlice.variables.test.js) and the panel
// (components/Workshop/__tests__/VariableInspector.test.jsx). RS-49 was exactly
// a name that passed one layer and was dropped by the next, so a list that
// existed only next to the predicate could not have caught it.
export const BLOCKLY_REAL_NAMES = [
  'meine Zahl',
  'Anzahl Würfel',
  'zähler-2',
  '2te_zahl',
  '3',
  'Öl-Stand',
  'Test 123',
  'weiß der Geier',
  'a b',
  'a.b',
  'a:b',
  'a,b',
  'a(b)',
  "a'b",
  'a/b',
  // The four the OLD C-identifier regex already accepted — kept so the
  // widening is provably a superset, not a swap.
  'zaehler',
  'Zähler',
  'größe',
  'x',
];

// The gate RS-49 replaced, kept ONLY so the suites can state the size of the
// bug against the same list. Never import this into src/ — it is the defect.
export const OLD_C_IDENTIFIER_RE = /^[A-Za-zÄÖÜäöüß_][A-Za-zÄÖÜäöüß0-9_]*$/;

// The 15 of 19 that the old regex silently dropped. Measured, not asserted by
// hand: every suite recomputes it from the two constants above.
export const NAMES_THE_OLD_REGEX_DROPPED = BLOCKLY_REAL_NAMES.filter(
  (n) => !OLD_C_IDENTIFIER_RE.test(n)
);
