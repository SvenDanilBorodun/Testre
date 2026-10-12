// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The robot's cloud state as React state: the last VALID message, and null —
// no facts — once it is older than STATE_STALE_MS or while disabled.

import { act, renderHook } from '@testing-library/react';
import useCloudInferenceState from '../useCloudInferenceState';
import { STATE_STALE_MS } from '../../features/cloudInference/robotContract';

vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel({ ros: { rosbridgeUrl: 'ws://localhost/rosbridge' } }),
}));

const mockSub = vi.hoisted(() => ({ onState: null, unsubscribed: 0 }));
vi.mock('../../features/cloudInference/robotCalls', () => ({
  __esModule: true,
  subscribeCloudState: vi.fn((url, onState) => {
    mockSub.onState = onState;
    return Promise.resolve(() => { mockSub.unsubscribed += 1; });
  }),
}));

const STATE = { v: 1, seq: 1, active: true, phase: 'running', rtt_ms_p50: 100, plan_age_ms: 50, gpu_ms_p50: 20, hold_s: null, ends_in_s: 600, stale_camera: null, end_text_de: null };

beforeEach(() => {
  vi.useFakeTimers();
  mockSub.onState = null;
  mockSub.unsubscribed = 0;
});
afterEach(() => vi.useRealTimers());

it('answers the last state, then null once it is stale', async () => {
  const { result, unmount } = renderHook(() => useCloudInferenceState({ enabled: true }));
  await act(async () => { await Promise.resolve(); });
  expect(result.current).toBeNull();
  act(() => mockSub.onState(STATE));
  expect(result.current).toEqual(STATE);
  await act(async () => { await vi.advanceTimersByTimeAsync(STATE_STALE_MS + 1500); });
  expect(result.current).toBeNull();
  unmount();
  expect(mockSub.unsubscribed).toBe(1);
});

it('subscribes to nothing while disabled', async () => {
  const { result } = renderHook(() => useCloudInferenceState({ enabled: false }));
  await act(async () => { await Promise.resolve(); });
  expect(mockSub.onState).toBeNull();
  expect(result.current).toBeNull();
});
