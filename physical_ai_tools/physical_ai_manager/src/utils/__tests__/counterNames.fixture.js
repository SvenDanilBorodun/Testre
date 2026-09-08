/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Shared fixture for the [CNT:name=int] („Zähler") sentinel — NOT a suite (the
// vitest `test.include` glob is `src/**/*.{test,spec}.{js,jsx}`, so this file is
// imported, never collected).
//
// FIVE suites assert against this one list, deliberately, because RS-49 was a
// name that passed one layer and was dropped by the next:
//   utils/__tests__/counterName.test.js                        the contract
//   hooks/__tests__/useRosTopicSubscription.counters.test.js   the wire, both ways
//   features/workshop/__tests__/workshopSlice.counters.test.js the store
//   components/Workshop/__tests__/VariableInspector.counters.test.jsx  the screen
//   components/Workshop/__tests__/RunControls.counters.test.jsx        retirement
//
// The two lists below are LITERAL, not derived from `isDisplayableVariableName`.
// That is the whole point: a list computed with the predicate under test moves
// its own entries when the predicate is mutated, so every assertion written
// against it stays green — the failure mode the round already produced twice.
// `counterName.test.js` proves the partition against the predicate exactly once,
// and proves every entry is a name the block's own field really hands back.

import { counterNameValidator, NAME_MAX_LEN } from
  '../../components/Workshop/blocks/counters';

export { NAME_MAX_LEN };

// Names the „Zähler" NAME field produces AND the Debug-Panel shows.
// „Punkte" leads because it is the text pre-filled into all four counter blocks
// (`blocks/counters.js::COUNTER_BLOCKS`), so it is the name most students have.
export const COUNTER_NAMES_SHOWN = [
  'Punkte',
  'Treffer',
  'Anzahl Würfel',
  'zähler-2',
  'Runde 3',
  'grüne Würfel',
  'Fehlversuche',
  '3',
  'x',
  'Größe',
  'weiß',
  'Peters Zähler',
  'a.b',
  'a(b)',
  'a/b',
];

// Names the field ALSO produces — `counterNameValidator` forbids only
// `[\r\n\0[\]]` — but the shared display predicate refuses, so they never reach
// the panel. This is a DOCUMENTED LIMITATION, not an oversight: tightening the
// validator is forbidden (a Blockly field validator also runs during
// DESERIALIZATION, so it would silently rewrite names inside already-saved
// workflows), and widening the predicate would put `=` and raw control
// characters onto a display fed by an unauthenticated rosbridge.
export const COUNTER_NAMES_HIDDEN = [
  'Punkte=2',       // `=` — legal in the field today; the frame splits on it
  'a\u001Bb',       // ESC — a terminal escape sequence in a student-facing panel
  'a\u0007b',       // BEL
  'a\u007Fb',       // DEL
  'a\u0085b',       // C1 NEL
];

// A typed name longer than the field's own cap. The validator SLICES to
// NAME_MAX_LEN (40), which is 24 under the sentinel's 64, so length can never be
// what hides a student's counter — asserted in counterName.test.js rather than
// stated here.
export const OVERLONG_TYPED_NAME = 'K'.repeat(60);

// The exact value the field hands back for it.
export const OVERLONG_FIELD_NAME = counterNameValidator(OVERLONG_TYPED_NAME);
