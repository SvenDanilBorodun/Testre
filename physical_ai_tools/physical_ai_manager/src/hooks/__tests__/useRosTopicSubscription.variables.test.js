/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-49 — the WIRE half of the [VAR:name=json] gate: the call site inside
// `interceptToken`, not the predicate.
//
// The predicate's own contract lives in utils/__tests__/variableName.test.js;
// the store gate in features/workshop/__tests__/workshopSlice.variables.test.js;
// the rendered panel in components/Workshop/__tests__/VariableInspector.test.jsx.
// This file owns exactly what none of those can see: whether THIS module still
// calls the gate, in BOTH directions.
//
// The rejecting direction is the one that was missing (audit §5). Every
// pre-existing refusal test here — the over-long name, the over-long value, the
// three prototype names — is killed by a DIFFERENT check a few lines above the
// gate, so replacing `if (!isDisplayableVariableName(rawName))` with
// `if (false)` left the whole suite green. That gate is the control-character
// defence on a channel rosbridge does not authenticate, so it needs a test only
// it can pass.

import { renderHook, act } from '@testing-library/react';
import { useRosTopicSubscription } from '../useRosTopicSubscription';

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

// The [VAR:...] branch dispatches on the imported singleton store, NOT the
// hook's useDispatch — capture that separately.
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
  await act(async () => {
    await result.current.subscribeToWorkflowStatus();
  });
  await act(async () => { await Promise.resolve(); });
  return { cb: workflowStatusCallback() };
}

// The setVariable action dispatched onto the singleton store, if any.
function dispatchedVariable() {
  return mockStoreDispatch.mock.calls
    .map(([action]) => action)
    .find((a) => a && a.type && a.type.endsWith('/setVariable'));
}

beforeEach(() => {
  mockDispatch.mockClear();
  mockStoreDispatch.mockClear();
  mockTopicSubscribe.mockClear();
});

describe('[VAR:] sentinel round-trip through the hook', () => {
  test('a spaced German name is dispatched (regression: was dropped here)', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[VAR:meine Zahl=7]', phase: 'running' }));

    const action = dispatchedVariable();
    expect(action).toBeTruthy();
    expect(action.payload).toEqual({ name: 'meine Zahl', value: 7 });
  });

  test('a hyphenated / digit-leading name is dispatched', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[VAR:zähler-2=3]', phase: 'running' }));
    act(() => cb({ log_message: '[VAR:2te_zahl="ja"]', phase: 'running' }));

    const names = mockStoreDispatch.mock.calls
      .map(([a]) => a && a.payload && a.payload.name)
      .filter(Boolean);
    expect(names).toContain('zähler-2');
    expect(names).toContain('2te_zahl');
  });

  test('prototype-pollution names are still refused', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[VAR:__proto__=1]', phase: 'running' }));
    act(() => cb({ log_message: '[VAR:constructor=1]', phase: 'running' }));
    act(() => cb({ log_message: '[VAR:prototype=1]', phase: 'running' }));
    expect(dispatchedVariable()).toBeFalsy();
  });

  test('an over-long value is still refused (4096-char cap)', async () => {
    const { cb } = await mountAndSubscribe();
    const big = `"${'x'.repeat(5000)}"`;
    act(() => cb({ log_message: `[VAR:meine Zahl=${big}]`, phase: 'running' }));
    expect(dispatchedVariable()).toBeFalsy();
  });

  test('an over-long name is still refused (64-char cap)', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: `[VAR:${'n'.repeat(65)}=1]`, phase: 'running' }));
    expect(dispatchedVariable()).toBeFalsy();
  });

  // ── the gate's REJECTING direction — audit §5 ────────────────────────────
  // Each name below is refused ONLY by `isDisplayableVariableName`. It is
  // inside the 64-char cap, it is not one of the three prototype names, and the
  // 4096-char value cap does not apply — so if the call site stops asking, these
  // are the tests that notice. Everything here is what an unauthenticated
  // rosbridge peer can put on the wire; Blockly cannot produce any of it.
  test.each([
    ['[VAR:a\u0000b=1]', 'a NUL in the name'],
    ['[VAR:a\u0007b=1]', 'a BEL in the name'],
    ['[VAR:a\u001Bb=1]', 'an ESC in the name (terminal escape sequence)'],
    ['[VAR:a\u007Fb=1]', 'a DEL in the name'],
    ['[VAR:a\u0085b=1]', 'a C1 NEL in the name'],
    ['[VAR:a\nb=1]', 'a newline in the name'],
    ['[VAR:a]b=1]', 'the frame terminator in the name'],
    ['[VAR:   =1]', 'a whitespace-only name'],
  ])('%j is never dispatched (%s)', async (message) => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: message, phase: 'running' }));
    expect(dispatchedVariable()).toBeFalsy();
  });

  test('a refused name is still CONSUMED — it must not print itself into the log', async () => {
    // The refusal returns `{intercepted: true}`, so the raw bytes never reach
    // the Protokoll strip. A control character that renders there is a terminal
    // escape in a student-facing log.
    const { cb } = await mountAndSubscribe();
    mockDispatch.mockClear();
    act(() => cb({ log_message: '[VAR:a\u001Bb=1]', phase: 'running' }));
    const wf = mockDispatch.mock.calls
      .map(([a]) => a)
      .find((a) => a && a.payload
        && Object.prototype.hasOwnProperty.call(a.payload, 'log_message'));
    expect(wf.payload.log_message).toBe('');
  });

  test('the sentinel is consumed either way — nothing leaks into the log strip', async () => {
    const { cb } = await mountAndSubscribe();
    mockDispatch.mockClear();
    act(() => cb({ log_message: '[VAR:meine Zahl=7]', phase: 'running' }));

    const wf = mockDispatch.mock.calls
      .map(([a]) => a)
      .find((a) => a && a.payload
        && Object.prototype.hasOwnProperty.call(a.payload, 'log_message'));
    expect(wf).toBeTruthy();
    expect(wf.payload.log_message).toBe('');
  });
});
