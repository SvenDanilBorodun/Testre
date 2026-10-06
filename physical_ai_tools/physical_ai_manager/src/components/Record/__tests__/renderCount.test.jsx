// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Review V2-2: the Aufnahme page re-rendered ~60 times a second while a task
// ran — every /task/status tick replaces the whole taskStatus object, and the
// shell (StudentApp) subscribed to it whole, re-rendering the page under it.
// Three halves, each pinned here: the page is memoised, the shell reads only
// the task fields it uses, and identical running ticks (same phase, second and
// episode; only the arrival time differs) cause no page render at all.
// A page render is counted through RecordHeader, which the page draws on every
// render of its own.

import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, render } from '@testing-library/react';
import { Provider, useSelector } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import tasksReducer, {
  recordIntent, setHeartbeatStatus, setTaskInfo, setTaskStatus,
} from '../../../features/tasks/taskSlice';
import uiReducer, { moveToPage } from '../../../features/ui/uiSlice';
import rosReducer, { setRosbridgeUrl } from '../../../features/ros/rosSlice';
import { resetDatenStateForTests } from '../../../features/editDataset/hooks/useDatenState';
import trainingReducer from '../../../features/training/trainingSlice';
import jetsonReducer from '../../../store/jetsonSlice';
import PageType from '../../../constants/pageType';
import TaskPhase from '../../../constants/taskPhases';
import RecordPage from '../../../pages/RecordPage';

let mockHeaderDraws = 0;
vi.mock('../RecordHeader', () => ({
  __esModule: true,
  default: () => { mockHeaderDraws += 1; return null; },
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({
    sendRecordCommand: vi.fn(),
    getRegisteredHFUser: () => Promise.resolve({ success: true, user_id_list: ['schule-A'] }),
    getImageTopicList: () => Promise.resolve({ success: true, image_topic_list: [] }),
  }),
}));
// A clock that does not tick: what is measured is the store, not the seconds.
const mockClock = { subscribe: () => () => {}, secondsLeft: 12, elapsed: 8 };
vi.mock('../../../hooks/useSmoothPhaseClock', () => ({ __esModule: true, default: () => mockClock }));
vi.mock('../../../hooks/useSignalStatus', () => ({ __esModule: true, default: () => ({ payload: null, receivedAt: null }) }));
vi.mock('../../../hooks/useRsBridgeStatus', () => ({ __esModule: true, default: () => ({ available: false, probed: true }) }));
vi.mock('../../../hooks/useRobotActivation', () => ({ __esModule: true, default: () => ({ status: null }) }));
vi.mock('../../HeartbeatStatus', () => ({ __esModule: true, default: () => null }));
// /edubotics/daten_state (Daten 2.0, R-8): the topic the STARTING wait reads.
const mockTopics = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.name = opts.name;
      this.cb = null;
      this.unsubscribed = false;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => { this.unsubscribed = true; };
      mockTopics.push(this);
    },
  },
}));
vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ isConnected: true })) },
}));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.dismiss = vi.fn();
  fn.error = vi.fn();
  return { __esModule: true, default: fn, useToasterStore: () => ({ toasts: [] }) };
});

const CAPS = {
  recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true,
  display_de: 'OMX – Voll', camera_roles: ['gripper', 'scene'],
};

// A running record tick as useRosTopicSubscription builds it; only the arrival
// times differ between identical ticks.
const tick = () => setTaskStatus({
  robotType: 'omx_f', robotProfile: 'omx_full', capabilities: CAPS, topicReceived: true,
  running: true, taskType: 'record', phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 8,
  currentEpisodeNumber: 1, numEpisodes: 3, episodeTime: 20, warmupTime: 5, resetTime: 5, fps: 30,
  pushToHub: true, receivedAt: performance.now(), receivedWallMs: Date.now(),
});

// The old shell's subscription: the WHOLE taskStatus and taskInfo — and, like
// StudentApp, it creates the page element in its own render (a `children`
// element passed from above would be skipped by React anyway).
function GreedyShell() {
  const status = useSelector((s) => s.tasks.taskStatus);
  useSelector((s) => s.tasks.taskInfo);
  return <RecordPage isActive={status.topicReceived || true} />;
}

describe('the Aufnahme page does not re-render per tick (V2-2)', () => {
  it('30 identical running ticks under a shell that reads the whole taskStatus: no page render', () => {
    const store = configureStore({
      reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer, jetson: jetsonReducer },
    });
    store.dispatch(moveToPage(PageType.RECORD));
    store.dispatch(setTaskInfo({ taskName: 'Würfel', taskInstruction: ['Greife.'], userId: 'schule-A' }));
    store.dispatch(setHeartbeatStatus('connected'));
    store.dispatch(tick());
    render(
      <Provider store={store}>
        <GreedyShell />
      </Provider>
    );
    act(() => { store.dispatch(tick()); });
    const drawsBefore = mockHeaderDraws;
    // One act per tick: each tick is its own render opportunity (a single act
    // would batch all of them into one).
    for (let i = 0; i < 30; i += 1) {
      act(() => {
        store.dispatch(tick());
        // The adopt path re-sends the same task_info with fresh arrays.
        const same = store.getState().tasks.taskInfo;
        store.dispatch(setTaskInfo({ taskName: same.taskName, taskInstruction: [...same.taskInstruction], tags: [...same.tags] }));
      });
    }
    expect(mockHeaderDraws - drawsBefore).toBe(0);
  });

  it('StudentApp reads the task fields it uses, never the whole taskStatus / taskInfo', () => {
    const app = fs.readFileSync(path.resolve(__dirname, '../../../StudentApp.js'), 'utf8');
    expect(app).not.toMatch(/useSelector\(\s*\(state\)\s*=>\s*state\.tasks\.taskStatus\s*\)/);
    expect(app).not.toMatch(/useSelector\(\s*\(state\)\s*=>\s*state\.tasks\.taskInfo\s*\)/);
    const page = fs.readFileSync(path.resolve(__dirname, '../../../pages/RecordPage.js'), 'utf8');
    expect(page).toMatch(/export default React\.memo\(RecordPage\)/);
  });

  it('Daten 2.0 (R-8, F-5): 30 daten_state messages while STARTING with an unchanged answer — no page render', async () => {
    resetDatenStateForTests();
    mockTopics.length = 0;
    const store = configureStore({
      reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer, jetson: jetsonReducer },
    });
    store.dispatch(moveToPage(PageType.RECORD));
    store.dispatch(setTaskInfo({ taskName: 'Würfel', taskInstruction: ['Greife.'], userId: 'schule-A' }));
    store.dispatch(setHeartbeatStatus('connected'));
    store.dispatch(setRosbridgeUrl('ws://robot/rosbridge'));
    store.dispatch(setTaskStatus({
      robotType: 'omx_f', robotProfile: 'omx_full', capabilities: CAPS, topicReceived: true,
      running: false, taskType: '', phase: TaskPhase.READY, receivedAt: performance.now(), receivedWallMs: Date.now(),
    }));
    render(
      <Provider store={store}>
        <GreedyShell />
      </Provider>
    );
    act(() => {
      store.dispatch(recordIntent({
        kind: 'start',
        at: Date.now(),
        snapshot: {
          taskName: 'Würfel', userId: 'schule-A', robotType: 'omx_f', pushToHub: true, privateMode: true,
          numEpisodes: 3, episodeTime: 20, warmupTime: 5, resetTime: 5, fps: 30,
        },
      }));
    });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const topic = mockTopics.find((t) => t.name === '/edubotics/daten_state' && !t.unsubscribed);
    expect(topic).toBeTruthy();
    const msg = () => ({ data: JSON.stringify({ v: 1, seq: 1, busy: [{ id: 'schule-A/omx_f_Wuerfel', kind: 'upload' }], jobs: [], transfer: null }) });
    act(() => { topic.cb(msg()); }); // the answer changes once: waiting
    const drawsBefore = mockHeaderDraws;
    for (let i = 0; i < 30; i += 1) {
      act(() => { topic.cb(msg()); });
    }
    expect(mockHeaderDraws - drawsBefore).toBe(0);
    resetDatenStateForTests();
  });
});
