// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The app-global liveness watchdog drives the heartbeat status transitions on
// EVERY page (the <HeartbeatStatus> pill — the only other place that did — is
// absent on Roboter Studio / Daten, so a node restart there produced no
// 'disconnected'->'connected' EDGE for the recovery-edge consumers).
// Robot identity itself no longer needs the edge: the server's idle identity
// tick re-delivers robot_type + capabilities over /task/status by itself.

import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import tasksReducer, {
  setHeartbeatStatus,
  setLastHeartbeatTime,
} from '../../features/tasks/taskSlice';
import { useHeartbeatWatchdog } from '../useHeartbeatWatchdog';

// Non-zero base so a real heartbeat timestamp is truthy (lastHeartbeatTime===0
// means "never seen" in the hook).
const BASE = 100000;

function makeStore() {
  return configureStore({ reducer: { tasks: tasksReducer } });
}
function wrapperFor(store) {
  return ({ children }) => <Provider store={store}>{children}</Provider>;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(BASE);
});
afterEach(() => {
  vi.useRealTimers();
});

describe('useHeartbeatWatchdog', () => {
  it('ages connected -> timeout -> disconnected as the heartbeat goes stale', () => {
    const store = makeStore();
    store.dispatch(setLastHeartbeatTime(Date.now())); // fresh beat at BASE
    store.dispatch(setHeartbeatStatus('connected'));
    renderHook(() => useHeartbeatWatchdog({ enabled: true }), { wrapper: wrapperFor(store) });

    // +3.5 s of silence -> timeout (>= 3000 ms).
    act(() => { vi.advanceTimersByTime(3500); });
    expect(store.getState().tasks.heartbeatStatus).toBe('timeout');

    // +11 s total -> disconnected (>= 10000 ms).
    act(() => { vi.advanceTimersByTime(8000); });
    expect(store.getState().tasks.heartbeatStatus).toBe('disconnected');
  });

  it('fires the recovery EDGE: a fresh beat flips disconnected -> connected', () => {
    const store = makeStore();
    store.dispatch(setLastHeartbeatTime(Date.now()));
    store.dispatch(setHeartbeatStatus('connected'));
    renderHook(() => useHeartbeatWatchdog({ enabled: true }), { wrapper: wrapperFor(store) });

    act(() => { vi.advanceTimersByTime(11000); });
    expect(store.getState().tasks.heartbeatStatus).toBe('disconnected');

    // Node recovers: a new heartbeat arrives (useRosTopicSubscription would do
    // this in production). The watchdog must transition back to 'connected' —
    // that disconnected->connected edge drives the UI liveness state (pill,
    // HomePage bridgeReady gate) on pages without a <HeartbeatStatus> mount.
    act(() => { store.dispatch(setLastHeartbeatTime(Date.now())); });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(store.getState().tasks.heartbeatStatus).toBe('connected');
  });

  it('does nothing when disabled (cloud-only mode)', () => {
    const store = makeStore();
    store.dispatch(setLastHeartbeatTime(Date.now()));
    store.dispatch(setHeartbeatStatus('connected'));
    renderHook(() => useHeartbeatWatchdog({ enabled: false }), { wrapper: wrapperFor(store) });

    act(() => { vi.advanceTimersByTime(20000); });
    // No interval ran, so the status is left untouched.
    expect(store.getState().tasks.heartbeatStatus).toBe('connected');
  });
});
