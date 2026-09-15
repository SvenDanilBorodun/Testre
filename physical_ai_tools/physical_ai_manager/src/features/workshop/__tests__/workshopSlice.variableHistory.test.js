/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Sammlung drawer's „Zuletzt" list: `workshop.variableHistory`, a SEPARATE
// map next to `variables`, so `variables[name]` stays exactly {value, ts}. It is
// server-pushed run output, so it must be retired by the same two events as
// `variables` — every Start (clearVariables) and sign-out.

import reducer, { VAR_HISTORY_LIMIT, clearVariables, setVariable } from '../workshopSlice';
import { signedOut } from '../../session/sessionActions';

const initial = reducer(undefined, { type: '@@INIT' });
let clock = 1_000;

beforeEach(() => {
  clock = 1_000;
  vi.spyOn(Date, 'now').mockImplementation(() => { clock += 10; return clock; });
});

afterEach(() => {
  vi.restoreAllMocks();
});

const set = (state, name, value) => reducer(state, setVariable({ name, value }));
const valuesOf = (state, name) => (state.variableHistory[name] || []).map((h) => h.value);

describe('workshop.variableHistory', () => {
  test('starts empty', () => {
    expect(initial.variableHistory).toEqual({});
    expect(VAR_HISTORY_LIMIT).toBe(5);
  });

  test('newest first, at most five entries, each with its own ts', () => {
    let state = initial;
    for (let i = 1; i <= 7; i += 1) state = set(state, 'Zahl', i);
    expect(valuesOf(state, 'Zahl')).toEqual([7, 6, 5, 4, 3]);
    const ts = state.variableHistory.Zahl.map((h) => h.ts);
    expect(ts).toEqual([...ts].sort((a, b) => b - a));
    expect(state.variableHistory.Zahl[0].ts).toBe(state.variables.Zahl.ts);
  });

  test('an unchanged value is not appended again (objects compared as JSON)', () => {
    let state = set(initial, 'Punkt', { x: 0.1, y: 0, z: 0.05 });
    state = set(state, 'Punkt', { x: 0.1, y: 0, z: 0.05 });
    state = set(state, 'Punkt', { x: 0.1, y: 0, z: 0.05 });
    expect(state.variableHistory.Punkt).toHaveLength(1);
    state = set(state, 'Punkt', { x: 0.2, y: 0, z: 0.05 });
    state = set(state, 'Punkt', { x: 0.1, y: 0, z: 0.05 });
    expect(valuesOf(state, 'Punkt')).toEqual([
      { x: 0.1, y: 0, z: 0.05 }, { x: 0.2, y: 0, z: 0.05 }, { x: 0.1, y: 0, z: 0.05 },
    ]);
  });

  test('the variables entry itself stays exactly {value, ts}', () => {
    let state = set(initial, 'meine Zahl', 6);
    state = set(state, 'meine Zahl', 7);
    expect(state.variables['meine Zahl']).toEqual({ value: 7, ts: expect.any(Number) });
    expect(Object.keys(state.variables['meine Zahl'])).toEqual(['value', 'ts']);
  });

  test('a name the gate refuses records no history either', () => {
    const state = set(initial, 'a=b', 1);
    expect(state.variableHistory).toEqual({});
  });

  test('evicting a variable at the cap deletes its history', () => {
    let state = initial;
    for (let i = 0; i < 256; i += 1) state = set(state, `v${i}`, i);
    expect(state.variableHistory.v0).toEqual([{ value: 0, ts: expect.any(Number) }]);
    state = set(state, 'neu', 1);
    expect('v0' in state.variables).toBe(false);
    expect('v0' in state.variableHistory).toBe(false);
    expect(Object.keys(state.variableHistory)).toHaveLength(256);
    expect(valuesOf(state, 'neu')).toEqual([1]);
  });

  test('clearVariables (every Start) clears the history', () => {
    let state = set(initial, 'Zahl', 1);
    state = set(state, 'Zahl', 2);
    state = reducer(state, clearVariables());
    expect(state.variableHistory).toEqual({});
    expect(state.variables).toEqual({});
  });

  test('signedOut clears the history', () => {
    let state = set(initial, 'Zahl', 1);
    state = set(state, 'Punkt', { x: 0, y: 0, z: 0 });
    state = reducer(state, signedOut());
    expect(state.variableHistory).toEqual({});
  });
});
