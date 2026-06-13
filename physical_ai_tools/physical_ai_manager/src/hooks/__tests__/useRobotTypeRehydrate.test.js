// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Covers the server-side robot-type rehydrate: on a heartbeat-recovery EDGE
// into 'connected', re-issue /set_robot_type from the persisted value so a node
// restart no longer forces the student to re-select on the Start page. Guards:
// edge-only, never while a task runs, no-op without a persisted robot type,
// disabled flag honoured.

import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import tasksReducer, {
  setHeartbeatStatus,
  setTaskStatus,
  selectRobotType,
} from '../../features/tasks/taskSlice';
import workshopReducer, { setRunState } from '../../features/workshop/workshopSlice';
import { useRobotTypeRehydrate } from '../useRobotTypeRehydrate';
import TaskPhase from '../../constants/taskPhases';

const mockSetRobotType = vi.fn();
vi.mock('../useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ setRobotType: (...a) => mockSetRobotType(...a) }),
}));

function makeStore() {
  return configureStore({ reducer: { tasks: tasksReducer, workshop: workshopReducer } });
}

function wrapperFor(store) {
  return ({ children }) => <Provider store={store}>{children}</Provider>;
}

beforeEach(() => {
  mockSetRobotType.mockReset();
  mockSetRobotType.mockResolvedValue({ success: true });
});

describe('useRobotTypeRehydrate', () => {
  it('re-issues set_robot_type on the disconnected→connected edge', async () => {
    const store = makeStore();
    store.dispatch(selectRobotType('omx')); // persisted robot type present
    renderHook(() => useRobotTypeRehydrate({ enabled: true }), {
      wrapper: wrapperFor(store),
    });
    // starts 'disconnected' (slice initial) — no fire yet
    expect(mockSetRobotType).not.toHaveBeenCalled();
    await act(async () => {
      store.dispatch(setHeartbeatStatus('connected'));
    });
    expect(mockSetRobotType).toHaveBeenCalledTimes(1);
    expect(mockSetRobotType).toHaveBeenCalledWith('omx');
  });

  it('fires only on the edge, not on every connected render', async () => {
    const store = makeStore();
    store.dispatch(selectRobotType('omx'));
    renderHook(() => useRobotTypeRehydrate({ enabled: true }), {
      wrapper: wrapperFor(store),
    });
    await act(async () => {
      store.dispatch(setHeartbeatStatus('connected'));
    });
    expect(mockSetRobotType).toHaveBeenCalledTimes(1);
    // A further state change while staying 'connected' must NOT re-fire.
    await act(async () => {
      store.dispatch(setTaskStatus({ usedCpu: 5 }));
    });
    expect(mockSetRobotType).toHaveBeenCalledTimes(1);
  });

  it('does NOT fire while a task is running (would clobber recording)', async () => {
    const store = makeStore();
    store.dispatch(selectRobotType('omx'));
    store.dispatch(setTaskStatus({ running: true, phase: TaskPhase.RECORDING }));
    renderHook(() => useRobotTypeRehydrate({ enabled: true }), {
      wrapper: wrapperFor(store),
    });
    await act(async () => {
      store.dispatch(setHeartbeatStatus('connected'));
    });
    expect(mockSetRobotType).not.toHaveBeenCalled();
  });

  it('does NOT fire while a Roboter Studio workflow is running', async () => {
    const store = makeStore();
    store.dispatch(selectRobotType('omx'));
    store.dispatch(setRunState('running')); // workshop busy, but tasks.running stays false
    renderHook(() => useRobotTypeRehydrate({ enabled: true }), {
      wrapper: wrapperFor(store),
    });
    await act(async () => {
      store.dispatch(setHeartbeatStatus('connected'));
    });
    expect(mockSetRobotType).not.toHaveBeenCalled();
  });

  it('is a no-op when no robot type has been selected yet', async () => {
    const store = makeStore(); // robotType '' from initial state (no localStorage)
    renderHook(() => useRobotTypeRehydrate({ enabled: true }), {
      wrapper: wrapperFor(store),
    });
    await act(async () => {
      store.dispatch(setHeartbeatStatus('connected'));
    });
    expect(mockSetRobotType).not.toHaveBeenCalled();
  });

  it('does nothing when disabled (cloud-only / Jetson-routed)', async () => {
    const store = makeStore();
    store.dispatch(selectRobotType('omx'));
    renderHook(() => useRobotTypeRehydrate({ enabled: false }), {
      wrapper: wrapperFor(store),
    });
    await act(async () => {
      store.dispatch(setHeartbeatStatus('connected'));
    });
    expect(mockSetRobotType).not.toHaveBeenCalled();
  });

  it('recovers on the timeout→connected edge too (fast respawn)', async () => {
    const store = makeStore();
    store.dispatch(selectRobotType('omx'));
    renderHook(() => useRobotTypeRehydrate({ enabled: true }), {
      wrapper: wrapperFor(store),
    });
    await act(async () => {
      store.dispatch(setHeartbeatStatus('timeout'));
    });
    expect(mockSetRobotType).not.toHaveBeenCalled();
    await act(async () => {
      store.dispatch(setHeartbeatStatus('connected'));
    });
    expect(mockSetRobotType).toHaveBeenCalledTimes(1);
  });
});
