/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-49, the half that shipped broken — `setVariable` held a SECOND, stricter
// copy of the wire gate:
//     /^[A-Za-zÄÖÜäöüß_][A-Za-zÄÖÜäöüß0-9_]{0,63}$/
// and silently `return`ed on anything else. Widening only
// `useRosTopicSubscription` therefore fixed nothing a student could see: the
// action was dispatched, the reducer dropped it, and the Variablen panel stayed
// on „Noch keine Variablen." while the new test — which asserted the PURE
// PREDICATE — passed.
//
// Measured against the real reducer before the fix: of the 19 names Blockly's
// own `Variables.promptName` produces, 4 survived and 15 were dropped
// („meine Zahl", „Anzahl Würfel", „zähler-2", „Öl-Stand", „2te_zahl", „3",
// „Test 123", „weiß der Geier", „a b", „a.b", „a:b", „a,b", „a(b)", „a'b",
// „a/b").
//
// So this file asserts the OUTCOME — a name present in `state.variables` — and
// never the predicate. The predicate's own contract is in
// utils/__tests__/variableName.test.js; the rendered panel is in
// components/Workshop/__tests__/VariableInspector.test.jsx.
//
// REAL-WORLD TRIGGER: a student opens „Variablen" → „Variable erstellen …",
// types „meine Zahl", drops a „setze meine Zahl auf 7" block and presses
// „Start". The server emits `[VAR:meine Zahl=7.0]` on /workflow/status
// (verified by executing the real Interpreter against a workspace payload with
// that variable table), so the name on the wire is the name the student typed.

import reducer, { setVariable, clearVariables } from '../workshopSlice';
import {
  BLOCKLY_REAL_NAMES,
  NAMES_THE_OLD_REGEX_DROPPED,
} from '../../../utils/__tests__/blocklyVariableNames.fixture';

const initial = reducer(undefined, { type: '@@INIT' });

const set = (state, name, value = 1) => reducer(state, setVariable({ name, value }));

// Own-key test that is honest about `__proto__`: assigning that key does NOT
// create an own property, it re-points the prototype — so `hasOwnProperty`
// alone would report "absent" for a successful pollution.
const ownKeys = (state) => Object.keys(state.variables);

describe('setVariable accepts every name Blockly really produces', () => {
  test.each(BLOCKLY_REAL_NAMES)('%j reaches the store', (name) => {
    const state = set(initial, name, 7);
    expect(ownKeys(state)).toContain(name);
    expect(state.variables[name].value).toBe(7);
    expect(typeof state.variables[name].ts).toBe('number');
  });

  test('the 15 names the old regex dropped all land now', () => {
    let state = initial;
    for (const name of NAMES_THE_OLD_REGEX_DROPPED) state = set(state, name);
    expect(NAMES_THE_OLD_REGEX_DROPPED).toHaveLength(15);
    expect(ownKeys(state).sort()).toEqual([...NAMES_THE_OLD_REGEX_DROPPED].sort());
  });

  test('„meine Zahl" specifically — the name in the bug report', () => {
    const state = set(initial, 'meine Zahl', 7);
    expect(state.variables['meine Zahl']).toEqual({
      value: 7,
      ts: expect.any(Number),
    });
  });

  test('an overwrite updates in place and does not duplicate the row', () => {
    let state = set(initial, 'meine Zahl', 7);
    state = set(state, 'meine Zahl', 8);
    expect(ownKeys(state)).toEqual(['meine Zahl']);
    expect(state.variables['meine Zahl'].value).toBe(8);
  });
});

describe('setVariable still refuses what the wire must never write', () => {
  // These are the reducer's OWN refusals — the hook filters the same things one
  // layer earlier, but rosbridge authenticates nobody and `setVariable` is a
  // plain exported action, so the store keeps its own gate.
  test.each([
    ['a\u0000b', 'NUL'],
    ['a\u0007b', 'BEL'],
    ['a\u001Bb', 'ESC'],
    ['a\u007Fb', 'DEL'],
    ['a\u0085b', 'C1 NEL'],
    ['a\nb', 'newline'],
    ['a=b', 'the sentinel separator'],
    ['a]b', 'the sentinel terminator'],
    ['a[0]', 'the sentinel brackets'],
    ['   ', 'whitespace only'],
    ['a'.repeat(65), 'over the 64-char cap'],
  ])('%j is dropped (%s)', (name) => {
    expect(ownKeys(set(initial, name))).toEqual([]);
  });

  test('a non-string or empty name is dropped', () => {
    expect(ownKeys(reducer(initial, setVariable({ name: '', value: 1 })))).toEqual([]);
    expect(ownKeys(reducer(initial, setVariable({ name: 42, value: 1 })))).toEqual([]);
    expect(ownKeys(reducer(initial, setVariable({ name: null, value: 1 })))).toEqual([]);
    expect(ownKeys(reducer(initial, setVariable(undefined)))).toEqual([]);
  });
});

describe('setVariable prototype-pollution guard', () => {
  // `isDisplayableVariableName` deliberately says TRUE for all three (they are
  // displayable; they are just unsafe keys), so this guard is the only thing
  // standing between the wire and them — deleting it must fail here.
  test('__proto__ neither becomes a key nor re-points the prototype', () => {
    const state = set(initial, '__proto__', { pwned: true });
    expect(ownKeys(state)).toEqual([]);
    expect(Object.getPrototypeOf(state.variables)).toBe(Object.prototype);
    expect(state.variables.pwned).toBeUndefined();
  });

  test.each(['constructor', 'prototype'])('%s is refused', (name) => {
    const state = set(initial, name, 1);
    expect(ownKeys(state)).toEqual([]);
  });

  test('the predicate alone would NOT have refused them', () => {
    // Pins WHY the guard has to exist separately: if someone folds the three
    // names into `isDisplayableVariableName` and deletes this guard, this
    // assertion is the reminder that the reducer then has no check of its own.
    expect(BLOCKLY_REAL_NAMES).not.toContain('__proto__');
    const state = set(set(initial, '__proto__', 1), 'meine Zahl', 2);
    expect(ownKeys(state)).toEqual(['meine Zahl']);
  });
});

describe('setVariable VAR_LIMIT — 256 names, FIFO by ts', () => {
  // Audit round-3 §BJ. A `wiederhole` loop that assigns a fresh name per pass
  // („v0", „v1", …) would otherwise pin unbounded state into Redux for the rest
  // of the session — `clearVariables` only runs on the NEXT „Start".
  const fill = (count) => {
    let state = initial;
    for (let i = 0; i < count; i += 1) state = set(state, `v${i}`, i);
    return state;
  };

  test('256 distinct names all fit', () => {
    expect(ownKeys(fill(256))).toHaveLength(256);
  });

  test('the 257th evicts the oldest, not the newest', () => {
    const state = set(fill(256), 'v256', 256);
    const keys = ownKeys(state);
    expect(keys).toHaveLength(256);
    expect(keys).not.toContain('v0');
    expect(keys).toContain('v1');
    expect(keys).toContain('v256');
  });

  test('overwriting an existing name at the cap evicts nothing', () => {
    const state = set(fill(256), 'v0', 99);
    expect(ownKeys(state)).toHaveLength(256);
    expect(state.variables.v0.value).toBe(99);
  });

  test('a runaway 1000-name loop stays capped at 256', () => {
    expect(ownKeys(fill(1000))).toHaveLength(256);
  });
});

describe('clearVariables', () => {
  test('empties the inspector (RunControls does this on every „Start")', () => {
    const state = reducer(set(initial, 'meine Zahl', 7), clearVariables());
    expect(ownKeys(state)).toEqual([]);
  });
});
