/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-50 — the WIRE half of the [CNT:name=int] („Zähler") sentinel: the branch
// inside `interceptToken`, not the predicate.
//
// The contract is in utils/__tests__/counterName.test.js, the store in
// features/workshop/__tests__/workshopSlice.counters.test.js, the screen in
// components/Workshop/__tests__/VariableInspector.counters.test.jsx. This file
// owns what none of those can see: whether THIS module still calls the gate, in
// BOTH directions — and the two frame decisions that are specific to [CNT:].
//
// FRAME DECISION 1 — the name capture is GREEDY (`(.+)=`), not `[^=]+` as the
// [VAR:] frame's is. `blocks/counters.js::counterNameValidator` forbids only
// `[\r\n\0[\]]`, so „Punkte=2" is a counter name a student can type TODAY, and
// the validator may not be tightened (Blockly runs field validators during
// DESERIALIZATION, so a stricter one rewrites names inside already-saved
// workflows). Greedy + a digits-only value splits on the LAST `=`, which is
// unambiguous because a value can contain none — so such a sentinel still
// FRAMES, is refused by the name gate, and is CONSUMED. With `[^=]+` the frame
// would fail to match and `[CNT:Punkte=2=5]` would print itself into the
// student's Protokoll on every single increment.
//
// FRAME DECISION 2 — the value capture is digits, not JSON. A counter is an
// integer by construction, so the capture IS the type check.

import { renderHook, act } from '@testing-library/react';
import { useRosTopicSubscription } from '../useRosTopicSubscription';
import {
  COUNTER_NAMES_SHOWN,
  COUNTER_NAMES_HIDDEN,
} from '../../utils/__tests__/counterNames.fixture';

const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useDispatch: () => mockDispatch,
  useSelector: (sel) => sel({ ros: { rosbridgeUrl: 'ws://localhost:9090' } }),
}));

vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.custom = vi.fn();
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn };
});

vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: {
    getConnection: vi.fn(() =>
      Promise.resolve({ isConnected: true, on: () => {}, off: () => {} })
    ),
  },
}));

// The [CNT:] branch dispatches on the imported singleton store, NOT the hook's
// useDispatch — capture that separately.
const mockStoreDispatch = vi.fn();
vi.mock('../../store/store', () => ({
  __esModule: true,
  default: { dispatch: (...args) => mockStoreDispatch(...args) },
}));

const mockTopicSubscribe = vi.fn();
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.name = opts ? opts.name : undefined;
      this.subscribe = (cb) => { mockTopicSubscribe(this.name, cb); };
      this.unsubscribe = () => {};
    },
  },
}));

function workflowStatusCallback() {
  const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === '/workflow/status');
  return calls.length ? calls[calls.length - 1][1] : null;
}

async function mountAndSubscribe() {
  const { result } = renderHook(() => useRosTopicSubscription());
  await act(async () => { await result.current.subscribeToWorkflowStatus(); });
  await act(async () => { await Promise.resolve(); });
  return { cb: workflowStatusCallback() };
}

const dispatchedCounter = () => mockStoreDispatch.mock.calls
  .map(([action]) => action)
  .find((a) => a && a.type && a.type.endsWith('/setCounter'));

const dispatchedVariable = () => mockStoreDispatch.mock.calls
  .map(([action]) => action)
  .find((a) => a && a.type && a.type.endsWith('/setVariable'));

// The WorkflowStatus action the hook dispatches through `useDispatch` — its
// `log_message` is '' when a token was consumed.
const forwardedLogMessage = () => {
  const a = mockDispatch.mock.calls
    .map(([x]) => x)
    .find((x) => x && x.payload
      && Object.prototype.hasOwnProperty.call(x.payload, 'log_message'));
  return a ? a.payload.log_message : undefined;
};

beforeEach(() => {
  mockDispatch.mockClear();
  mockStoreDispatch.mockClear();
  mockTopicSubscribe.mockClear();
});

describe('[CNT:] sentinel round-trip through the hook', () => {
  test('„erhöhe Zähler Punkte um 1" reaches the store', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:Punkte=1]', phase: 'running' }));
    expect(dispatchedCounter().payload).toEqual({ name: 'Punkte', value: 1 });
  });

  test.each(COUNTER_NAMES_SHOWN)('%j is dispatched', async (name) => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: `[CNT:${name}=12]`, phase: 'running' }));
    expect(dispatchedCounter().payload).toEqual({ name, value: 12 });
  });

  test('the value arrives as a NUMBER, not the digit string', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:Punkte=0]', phase: 'running' }));
    expect(dispatchedCounter().payload.value).toBe(0);
    expect(typeof dispatchedCounter().payload.value).toBe('number');
  });

  test('the sentinel is consumed — it never prints into the Protokoll', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:Punkte=3]', phase: 'running' }));
    expect(forwardedLogMessage()).toBe('');
  });

  test('a [CNT:] never dispatches setVariable, and a [VAR:] never setCounter', async () => {
    // The two stores are independent all the way down. „Punkte" is the default
    // counter name AND a name a student can give a variable.
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:Punkte=5]', phase: 'running' }));
    expect(dispatchedVariable()).toBeFalsy();

    mockStoreDispatch.mockClear();
    act(() => cb({ log_message: '[VAR:Punkte="Text"]', phase: 'running' }));
    expect(dispatchedCounter()).toBeFalsy();
    expect(dispatchedVariable().payload).toEqual({ name: 'Punkte', value: 'Text' });
  });
});

describe('the greedy frame — a counter named „Punkte=2"', () => {
  test('frames, is refused, and is CONSUMED rather than printed', async () => {
    // The whole reason the capture is greedy. With `[^=]+` this message would
    // not match, would fall through as an ordinary log line, and the student
    // would watch `[CNT:Punkte=2=5]` scroll past on every increment.
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:Punkte=2=5]', phase: 'running' }));
    expect(dispatchedCounter()).toBeFalsy();
    expect(forwardedLogMessage()).toBe('');
  });

  test('the split is on the LAST `=`, which is what makes it unambiguous', async () => {
    // Proof the greediness does not mis-split an ordinary name: the value can
    // never contain `=`, so the last one is always the real separator.
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:Runde 3=12]', phase: 'running' }));
    expect(dispatchedCounter().payload).toEqual({ name: 'Runde 3', value: 12 });
  });
});

describe('the gate REJECTING — the direction that goes missing', () => {
  // Each name here is refused ONLY by `isDisplayableVariableName`: inside the
  // 64-char cap, not one of the three prototype names. If the call site stops
  // asking, these are the tests that notice. All of it is what an
  // unauthenticated rosbridge peer can put on the wire.
  test.each(COUNTER_NAMES_HIDDEN)('%j is never dispatched', async (name) => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: `[CNT:${name}=1]`, phase: 'running' }));
    expect(dispatchedCounter()).toBeFalsy();
  });

  test.each([
    ['[CNT:a\u0000b=1]', 'a NUL in the name'],
    ['[CNT:a\nb=1]', 'a newline in the name'],
    ['[CNT:a]b=1]', 'the frame terminator in the name'],
    ['[CNT:   =1]', 'a whitespace-only name'],
  ])('%j is never dispatched (%s)', async (message) => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: message, phase: 'running' }));
    expect(dispatchedCounter()).toBeFalsy();
  });

  test('a refused name is still CONSUMED — no control character in the log', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:a\u001Bb=1]', phase: 'running' }));
    expect(forwardedLogMessage()).toBe('');
  });

  test('the three prototype names are refused', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:__proto__=1]', phase: 'running' }));
    act(() => cb({ log_message: '[CNT:constructor=1]', phase: 'running' }));
    act(() => cb({ log_message: '[CNT:prototype=1]', phase: 'running' }));
    expect(dispatchedCounter()).toBeFalsy();
  });

  test('an over-long name is refused (64-char cap)', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: `[CNT:${'n'.repeat(65)}=1]`, phase: 'running' }));
    expect(dispatchedCounter()).toBeFalsy();
  });

  test('an absurd digit run is refused before parseInt sees it', async () => {
    // 15 digits keeps parseInt exact; the server's own clamp is 1e9 (10).
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: `[CNT:Punkte=${'9'.repeat(16)}]`, phase: 'running' }));
    expect(dispatchedCounter()).toBeFalsy();

    mockStoreDispatch.mockClear();
    act(() => cb({ log_message: `[CNT:Punkte=${'9'.repeat(15)}]`, phase: 'running' }));
    expect(dispatchedCounter().payload.value).toBe(999999999999999);
  });

  test('a non-integer value does not even frame', async () => {
    // It falls through as an ordinary log line — same as a malformed [TOAST:],
    // which is deliberately left visible so a producer bug is debuggable.
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[CNT:Punkte=1.5]', phase: 'running' }));
    expect(dispatchedCounter()).toBeFalsy();
    expect(forwardedLogMessage()).toBe('[CNT:Punkte=1.5]');
  });
});
