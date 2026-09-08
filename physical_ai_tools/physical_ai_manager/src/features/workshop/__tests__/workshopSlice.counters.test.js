/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-50 — the STORE half of the [CNT:name=int] („Zähler") sentinel.
//
// Asserts OUTCOMES on `state.counters`, never the predicate: RS-49 was a name
// that passed the wire gate and was silently dropped by a second, stricter copy
// in the reducer, and every predicate-shaped test stayed green while the panel
// stayed empty.
//
// The two things this file owns that no other suite can see:
//   • RETIREMENT — `clearCounters` (RunControls fires it on every „Start") and
//     the `session/signedOut` broadcast. Without the second one, the next
//     student at a shared classroom Windows account opens Roboter Studio and
//     reads the previous student's tally. CLAUDE.md, „Editor UI invariants":
//     every server-pushed Roboter-Studio field needs an event that RETIRES it.
//   • INDEPENDENCE — a variable „Punkte" and a counter „Punkte" are two rows in
//     two maps. „Punkte" is the text pre-filled into all four Zähler blocks, so
//     the collision is the ordinary case, not an edge case.

import reducer, {
  setVariable,
  setCounter,
  clearCounters,
  clearVariables,
} from '../workshopSlice';
import { signedOut } from '../../session/sessionActions';
import {
  COUNTER_NAMES_SHOWN,
  COUNTER_NAMES_HIDDEN,
} from '../../../utils/__tests__/counterNames.fixture';

const initial = reducer(undefined, { type: '@@INIT' });

const put = (state, name, value = 1) => reducer(state, setCounter({ name, value }));
const keys = (state) => Object.keys(state.counters);

describe('setCounter accepts every name the Zähler field really produces', () => {
  test.each(COUNTER_NAMES_SHOWN)('%j reaches the store', (name) => {
    const state = put(initial, name, 7);
    expect(keys(state)).toContain(name);
    expect(state.counters[name].value).toBe(7);
    expect(typeof state.counters[name].ts).toBe('number');
  });

  test('„Punkte" specifically — the name every counter block ships with', () => {
    expect(put(initial, 'Punkte', 4).counters.Punkte).toEqual({
      value: 4,
      ts: expect.any(Number),
    });
  });

  test('a later increment replaces the value in place', () => {
    let state = put(initial, 'Punkte', 1);
    state = put(state, 'Punkte', 2);
    expect(keys(state)).toEqual(['Punkte']);
    expect(state.counters.Punkte.value).toBe(2);
  });
});

describe('a variable „Punkte" and a counter „Punkte" are independent', () => {
  // THE reason the separate-map design was chosen over a prefixed shared map.
  test('both are stored, neither overwrites the other', () => {
    let state = reducer(initial, setVariable({ name: 'Punkte', value: 'ein Text' }));
    state = put(state, 'Punkte', 42);
    expect(Object.keys(state.variables)).toEqual(['Punkte']);
    expect(state.counters.Punkte.value).toBe(42);
    expect(state.variables.Punkte.value).toBe('ein Text');
  });

  test('the reverse write order behaves the same', () => {
    let state = put(initial, 'Punkte', 42);
    state = reducer(state, setVariable({ name: 'Punkte', value: 'ein Text' }));
    expect(state.counters.Punkte.value).toBe(42);
    expect(state.variables.Punkte.value).toBe('ein Text');
  });

  test('clearing one leaves the other alone', () => {
    let state = reducer(initial, setVariable({ name: 'Punkte', value: 1 }));
    state = put(state, 'Punkte', 9);
    expect(Object.keys(reducer(state, clearCounters()).variables)).toEqual(['Punkte']);
    expect(Object.keys(reducer(state, clearVariables()).counters)).toEqual(['Punkte']);
  });
});

describe('setCounter refuses what the wire must never write', () => {
  test.each(COUNTER_NAMES_HIDDEN)('%j is dropped', (name) => {
    expect(keys(put(initial, name))).toEqual([]);
  });

  test.each([
    ['a\u0000b', 'NUL'],
    ['a\nb', 'newline'],
    ['a]b', 'the frame terminator'],
    ['a[0]', 'the frame brackets'],
    ['   ', 'whitespace only'],
    ['K'.repeat(65), 'over the 64-char cap'],
  ])('%j is dropped (%s)', (name) => {
    expect(keys(put(initial, name))).toEqual([]);
  });

  test('a non-string or empty name is dropped', () => {
    expect(keys(reducer(initial, setCounter({ name: '', value: 1 })))).toEqual([]);
    expect(keys(reducer(initial, setCounter({ name: 42, value: 1 })))).toEqual([]);
    expect(keys(reducer(initial, setCounter({ name: null, value: 1 })))).toEqual([]);
    expect(keys(reducer(initial, setCounter(undefined)))).toEqual([]);
  });

  test('a non-integer value is dropped — a Zähler is a whole number', () => {
    // The wire frame already captures digits only; this is the store's own half
    // of that contract, so a direct `setCounter` dispatch cannot put "abc" (or
    // 1.5, or NaN) under a column the panel labels „Wert" for an integer.
    expect(keys(reducer(initial, setCounter({ name: 'Punkte', value: 'abc' })))).toEqual([]);
    expect(keys(reducer(initial, setCounter({ name: 'Punkte', value: 1.5 })))).toEqual([]);
    expect(keys(reducer(initial, setCounter({ name: 'Punkte', value: NaN })))).toEqual([]);
    expect(keys(reducer(initial, setCounter({ name: 'Punkte', value: null })))).toEqual([]);
    expect(keys(reducer(initial, setCounter({ name: 'Punkte', value: 0 })))).toEqual(['Punkte']);
  });
});

describe('setCounter prototype-pollution guard', () => {
  // `isDisplayableVariableName` deliberately says TRUE for all three, so this
  // guard is the only thing between the wire and them — deleting it fails here.
  test('__proto__ neither becomes a key nor re-points the prototype', () => {
    const state = put(initial, '__proto__', 1);
    expect(keys(state)).toEqual([]);
    expect(Object.getPrototypeOf(state.counters)).toBe(Object.prototype);
  });

  test.each(['constructor', 'prototype'])('%s is refused', (name) => {
    expect(keys(put(initial, name, 1))).toEqual([]);
  });
});

describe('setCounter cap — 256 names, FIFO by ts', () => {
  // A „wiederhole"-loop building a fresh counter name per pass would otherwise
  // pin unbounded state into Redux for the rest of the session; `clearCounters`
  // only runs on the NEXT „Start".
  const fill = (count) => {
    let state = initial;
    for (let i = 0; i < count; i += 1) state = put(state, `c${i}`, i);
    return state;
  };

  test('256 distinct counters all fit', () => {
    expect(keys(fill(256))).toHaveLength(256);
  });

  test('the 257th evicts the oldest, not the newest', () => {
    const state = put(fill(256), 'c256', 256);
    expect(keys(state)).toHaveLength(256);
    expect(keys(state)).not.toContain('c0');
    expect(keys(state)).toContain('c1');
    expect(keys(state)).toContain('c256');
  });

  test('overwriting an existing counter at the cap evicts nothing', () => {
    const state = put(fill(256), 'c0', 99);
    expect(keys(state)).toHaveLength(256);
    expect(state.counters.c0.value).toBe(99);
  });

  test('a runaway 1000-name loop stays capped at 256', () => {
    expect(keys(fill(1000))).toHaveLength(256);
  });
});

describe('RETIREMENT — a counter must not outlive its run or its student', () => {
  test('clearCounters empties the section (RunControls fires it on „Start")', () => {
    expect(keys(reducer(put(initial, 'Punkte', 7), clearCounters()))).toEqual([]);
  });

  test('session/signedOut empties it too — the shared classroom PC', () => {
    // Without this the next student opens Roboter Studio and reads the previous
    // student's tally, before pressing anything.
    const state = reducer(put(initial, 'Punkte', 7), signedOut());
    expect(keys(state)).toEqual([]);
    expect(Object.keys(state.variables)).toEqual([]);
  });

  test('signedOut retires counters even when nothing else changed', () => {
    // Pins that `state.counters = {}` is its own line in the signedOut handler
    // and not a side effect of some broader reset.
    const state = reducer({ ...initial, counters: { Punkte: { value: 3, ts: 1 } } }, signedOut());
    expect(state.counters).toEqual({});
  });
});
