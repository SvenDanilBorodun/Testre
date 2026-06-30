/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Toast-sentinel round-trip lock.
//
// The workflow runtime emits inline output sentinels on the single
// /workflow/status log channel; interceptToken() parses them and routes a
// [TOAST:level:sec:text] sentinel to react-hot-toast (variant by level), while
// blanking it from the user-visible log strip. interceptToken is a useCallback
// closed over the hook, not exported, so we drive it through its REAL call site:
// render the hook, subscribe to /workflow/status (captured via a mocked roslib
// Topic), feed messages, and assert the toast variant/options + the log blanking.
//
// Mock idiom mirrors UrdfTwin.test.jsx (vi.mock hoisted; `mock*`-prefixed
// factory vars). react-redux is stubbed so no Provider is needed.

import { renderHook, act } from '@testing-library/react';
import toast from 'react-hot-toast';
import { useRosTopicSubscription } from '../useRosTopicSubscription';

// --- react-redux: no-op dispatch + a fixed rosbridgeUrl (non-empty so the
//     subscribe path doesn't early-return). ---
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useDispatch: () => mockDispatch,
  useSelector: (sel) => sel({ ros: { rosbridgeUrl: 'ws://localhost:9090' } }),
}));

// --- react-hot-toast: the assertion target. Default export is a callable with
//     .success/.error variants. ---
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.custom = vi.fn();
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn };
});

// --- rosbridge connection manager: resolve a connected ros handle. ---
vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: {
    getConnection: vi.fn(() =>
      Promise.resolve({ isConnected: true, on: () => {}, off: () => {} })
    ),
  },
}));

// --- roslib Topic: record each subscribe(name -> callback) so the test can grab
//     the /workflow/status callback the hook installs. ---
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

// The latest callback installed for /workflow/status (the channel interceptToken
// runs on).
function workflowStatusCallback() {
  const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === '/workflow/status');
  return calls.length ? calls[calls.length - 1][1] : null;
}

// The action object dispatched for the workflow status (the only dispatch whose
// payload carries `log_message`).
function dispatchedWorkflowStatus() {
  return mockDispatch.mock.calls
    .map(([action]) => action)
    .find(
      (a) => a && a.payload
        && Object.prototype.hasOwnProperty.call(a.payload, 'log_message')
    );
}

async function mountAndSubscribe() {
  const { result } = renderHook(() => useRosTopicSubscription());
  await act(async () => {
    await result.current.subscribeToWorkflowStatus();
  });
  // Flush the fire-and-forget mount-effect subscriptions (task/heartbeat/...).
  await act(async () => { await Promise.resolve(); });
  const cb = workflowStatusCallback();
  return { cb };
}

beforeEach(() => {
  mockDispatch.mockClear();
  mockTopicSubscribe.mockClear();
  toast.mockClear();
  toast.success.mockClear();
  toast.error.mockClear();
});

describe('useRosTopicSubscription — [TOAST] sentinel interception', () => {
  test('the hook installs a /workflow/status subscription', async () => {
    const { cb } = await mountAndSubscribe();
    expect(typeof cb).toBe('function');
  });

  test('each of the 4 levels maps to the correct react-hot-toast variant + duration', async () => {
    const { cb } = await mountAndSubscribe();

    // info → plain toast (no icon).
    act(() => cb({ log_message: '[TOAST:info:2:Hinweis]', phase: 'running' }));
    expect(toast).toHaveBeenLastCalledWith('Hinweis', { duration: 2000 });

    // warning → plain toast + a warning icon.
    act(() => cb({ log_message: '[TOAST:warning:3:Achtung]', phase: 'running' }));
    expect(toast).toHaveBeenLastCalledWith('Achtung', { duration: 3000, icon: '⚠️' });

    // success → toast.success.
    act(() => cb({ log_message: '[TOAST:success:5:Erledigt]', phase: 'running' }));
    expect(toast.success).toHaveBeenCalledWith('Erledigt', { duration: 5000 });

    // error → toast.error.
    act(() => cb({ log_message: '[TOAST:error:4:Fehler]', phase: 'running' }));
    expect(toast.error).toHaveBeenCalledWith('Fehler', { duration: 4000 });
  });

  test('text containing a colon is preserved verbatim (greedy last capture)', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[TOAST:info:2:5:00 Uhr]', phase: 'running' }));
    expect(toast).toHaveBeenCalledWith('5:00 Uhr', { duration: 2000 });
  });

  test('a de-bracketed plain-text message renders verbatim', async () => {
    const { cb } = await mountAndSubscribe();
    // The backend strips ']' so the sentinel's closing bracket is unambiguous;
    // a normal message body therefore carries no brackets and renders verbatim.
    act(() => cb({ log_message: '[TOAST:warning:4:Greifer 1 von 3]', phase: 'running' }));
    expect(toast).toHaveBeenCalledWith('Greifer 1 von 3', { duration: 4000, icon: '⚠️' });
  });

  test('empty text yields an empty-string toast', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[TOAST:info:3:]', phase: 'running' }));
    expect(toast).toHaveBeenCalledWith('', { duration: 3000 });
  });

  test('duration is clamped to the [1, 15] second window', async () => {
    const { cb } = await mountAndSubscribe();
    act(() => cb({ log_message: '[TOAST:info:99:Lang]', phase: 'running' }));
    expect(toast).toHaveBeenCalledWith('Lang', { duration: 15000 });

    toast.mockClear();
    act(() => cb({ log_message: '[TOAST:info:0.2:Kurz]', phase: 'running' }));
    expect(toast).toHaveBeenCalledWith('Kurz', { duration: 1000 });
  });

  test('an intercepted [TOAST] is blanked in the dispatched workflow log_message', async () => {
    const { cb } = await mountAndSubscribe();
    mockDispatch.mockClear();
    act(() => cb({ log_message: '[TOAST:success:2:Erledigt]', phase: 'running' }));

    expect(toast.success).toHaveBeenCalledWith('Erledigt', { duration: 2000 });
    // Round-trip: the sentinel is consumed, so the log strip never shows it.
    const wf = dispatchedWorkflowStatus();
    expect(wf).toBeTruthy();
    expect(wf.payload.log_message).toBe('');
  });

  test('a non-[TOAST] log message passes through untouched (no toast, log preserved)', async () => {
    const { cb } = await mountAndSubscribe();
    mockDispatch.mockClear();
    act(() => cb({ log_message: 'Bewege Arm zu A', phase: 'running' }));

    expect(toast).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
    const wf = dispatchedWorkflowStatus();
    expect(wf).toBeTruthy();
    expect(wf.payload.log_message).toBe('Bewege Arm zu A');
  });

  test('a malformed [TOAST:<unknown-level>...] is NOT intercepted and stays visible', async () => {
    const { cb } = await mountAndSubscribe();
    mockDispatch.mockClear();
    act(() => cb({ log_message: '[TOAST:debug:3:Test]', phase: 'running' }));

    expect(toast).not.toHaveBeenCalled();
    const wf = dispatchedWorkflowStatus();
    expect(wf).toBeTruthy();
    expect(wf.payload.log_message).toBe('[TOAST:debug:3:Test]');
  });
});
