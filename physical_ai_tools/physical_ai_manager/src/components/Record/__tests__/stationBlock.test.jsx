// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Aufnahme page while this tab holds the camera station claim (station
// spec v6.13, §9.2, D13): useRecordController reads ONE primitive from the
// station controller (mocked here), turns Start off with the `station` block,
// and its „Station verlassen" calls the controller's leave — a refusal during
// a program becomes the page's transient notice. The banner draws the button,
// disabled with its reason while a program runs.

import React from 'react';
import { act, render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import tasksReducer, { setHeartbeatStatus, setTaskInfo, setTaskStatus } from '../../../features/tasks/taskSlice';
import uiReducer, { moveToPage } from '../../../features/ui/uiSlice';
import rosReducer from '../../../features/ros/rosSlice';
import trainingReducer from '../../../features/training/trainingSlice';
import PageType from '../../../constants/pageType';
import TaskPhase from '../../../constants/taskPhases';
import useRecordController from '../useRecordController';
import ProblemBanner from '../ProblemBanner';
import RECORD_COPY from '../model/recordCopy';

const mockStation = vi.hoisted(() => {
  const listeners = new Set();
  const box = { snap: { held: false, phase: 'idle', workflowActive: false } };
  return {
    box,
    set(patch) {
      box.snap = { ...box.snap, ...patch };
      listeners.forEach((fn) => fn());
    },
    api: {
      subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
      getSnapshot: () => box.snap,
      leave: vi.fn(),
    },
  };
});
vi.mock('../../../features/station/controller', () => ({ __esModule: true, ...mockStation.api }));

vi.mock('../../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({ sendRecordCommand: vi.fn(), getRegisteredHFUser: vi.fn().mockResolvedValue({ success: false }) }),
}));
vi.mock('../../../hooks/useSmoothPhaseClock', () => ({
  __esModule: true,
  default: () => ({ subscribe: () => () => {}, secondsLeft: 0, elapsed: 0 }),
}));
vi.mock('../../../hooks/useSignalStatus', () => ({ __esModule: true, default: () => ({ payload: null, receivedAt: null }) }));
vi.mock('../../../hooks/useRsBridgeStatus', () => ({
  __esModule: true,
  default: () => ({ available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false, probed: true }),
}));
vi.mock('../../../hooks/useRobotActivation', () => ({ __esModule: true, default: () => ({ status: null }) }));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  return { __esModule: true, default: fn };
});

function makeStore() {
  const store = configureStore({
    reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer },
  });
  store.dispatch(moveToPage(PageType.RECORD));
  store.dispatch(setTaskInfo({
    taskName: 'Würfel', taskInstruction: ['Greife.'], userId: 'schule-A', fps: 30, warmupTime: 5,
    episodeTime: 20, resetTime: 5, numEpisodes: 3, pushToHub: true, privateMode: true, tags: [],
  }));
  store.dispatch(setHeartbeatStatus('connected'));
  store.dispatch(setTaskStatus({
    robotType: 'omx_f', phase: TaskPhase.READY, running: false, topicReceived: true, taskType: '',
    capabilities: { recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true, station_protocol: 6 },
    receivedAt: performance.now(), receivedWallMs: Date.now(),
  }));
  return store;
}

function mount(store) {
  const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
  return renderHook(() => useRecordController({ isActive: true }), { wrapper });
}

beforeEach(() => {
  mockStation.box.snap = { held: false, phase: 'idle', workflowActive: false };
  mockStation.api.leave.mockReset();
});

describe('useRecordController and the station claim', () => {
  it('without a claim nothing changes', () => {
    const { result } = mount(makeStore());
    expect(result.current.model.startBlock).toBe(null);
  });

  it('a held claim turns Start off with the station block and its banner', () => {
    mockStation.box.snap = { held: true, phase: 'held', workflowActive: false };
    const { result } = mount(makeStore());
    expect(result.current.model.startBlock.kind).toBe('station');
    expect(result.current.problem.textDe).toBe(RECORD_COPY.problem.station);
    expect(result.current.problem.stationLeave).toBe(true);
    expect(result.current.problem.stationLeaveDisabled).toBe(false);
    expect(result.current.model.buttons.find((b) => b.id === 'start').disabled).toBe(true);
  });

  it('follows the claim live: taking it and giving it up', () => {
    const { result } = mount(makeStore());
    act(() => mockStation.set({ held: true, phase: 'held' }));
    expect(result.current.model.startBlock.kind).toBe('station');
    act(() => mockStation.set({ workflowActive: true }));
    expect(result.current.problem.stationLeaveDisabled).toBe(true);
    act(() => mockStation.set({ held: false, phase: 'idle', workflowActive: false }));
    expect(result.current.model.startBlock).toBe(null);
  });

  it('„Station verlassen" calls the controller; a refusal becomes the page notice', async () => {
    mockStation.box.snap = { held: true, phase: 'held', workflowActive: false };
    mockStation.api.leave.mockResolvedValue({ left: false, message: 'Stoppe zuerst dein Programm.' });
    const { result } = mount(makeStore());
    await act(async () => { await result.current.leaveStation(); });
    expect(mockStation.api.leave).toHaveBeenCalledTimes(1);
    expect(result.current.problem.textDe).toBe('Stoppe zuerst dein Programm.');
  });
});

describe('ProblemBanner with the station block', () => {
  const problem = { kind: 'bad', textDe: RECORD_COPY.problem.station, stationLeave: true, stationLeaveDisabled: false };

  it('draws „Station verlassen" as a real button', async () => {
    const onLeave = vi.fn();
    render(
      <ProblemBanner
        problem={problem}
        stationLeaveLabel={RECORD_COPY.problem.stationLeave}
        stationLeaveBlocked={RECORD_COPY.problem.stationLeaveBlocked}
        onStationLeave={onLeave}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Station verlassen' }));
    expect(onLeave).toHaveBeenCalledTimes(1);
  });

  it('disables it with its reason while a program runs', () => {
    render(
      <ProblemBanner
        problem={{ ...problem, stationLeaveDisabled: true }}
        stationLeaveLabel={RECORD_COPY.problem.stationLeave}
        stationLeaveBlocked={RECORD_COPY.problem.stationLeaveBlocked}
        onStationLeave={() => {}}
      />,
    );
    const btn = screen.getByRole('button', { name: 'Station verlassen' });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('title', 'Stoppe zuerst dein Programm.');
  });

  it('draws no button for any other problem', () => {
    render(<ProblemBanner problem={{ kind: 'bad', textDe: 'Etwas anderes.' }} stationLeaveLabel="Station verlassen" onStationLeave={() => {}} />);
    expect(screen.queryByRole('button')).toBe(null);
  });
});
