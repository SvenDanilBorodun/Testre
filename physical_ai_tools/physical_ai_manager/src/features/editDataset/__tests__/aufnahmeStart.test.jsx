// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Aufnahme page's two Daten 2.0 changes (spec §G11):
//   R-8  a Start that waits in STARTING for the SAME dataset's upload says
//        „Wartet, bis das Hochladen fertig ist …"; after 8 s the existing
//        „Dauert länger als gewohnt …"; with no topic (an older image) nothing
//        changes;
//   R-10 a stored Benutzer-ID the robot's list does not contain (an
//        organisation chosen before) is replaced by the account.

import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import tasksReducer, {
  recordIntent, recordUploadStatus, setHeartbeatStatus, setTaskInfo, setTaskStatus,
} from '../../tasks/taskSlice';
import uiReducer, { moveToPage } from '../../ui/uiSlice';
import rosReducer, { setRosbridgeUrl } from '../../ros/rosSlice';
import trainingReducer from '../../training/trainingSlice';
import PageType from '../../../constants/pageType';
import TaskPhase from '../../../constants/taskPhases';
import useRecordController from '../../../components/Record/useRecordController';
import { VIEW, deriveRecordView } from '../../../components/Record/model/phaseModel';
import RECORD_COPY from '../../../components/Record/model/recordCopy';
import { resetDatenStateForTests } from '../hooks/useDatenState';
import { EMPTY_RECORD_SESSION, applyRecordIntent, noteRecordNotice } from '../../tasks/recordSession';

const mockGetHfUsers = vi.fn();
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({ sendRecordCommand: vi.fn(), getRegisteredHFUser: mockGetHfUsers }),
}));
vi.mock('../../../hooks/useSmoothPhaseClock', () => ({ __esModule: true, default: () => ({ subscribe: () => () => {}, secondsLeft: 0, elapsed: 0 }) }));
vi.mock('../../../hooks/useSignalStatus', () => ({ __esModule: true, default: () => ({ payload: null, receivedAt: null }) }));
vi.mock('../../../hooks/useRsBridgeStatus', () => ({
  __esModule: true,
  default: () => ({ available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false, probed: true }),
}));
vi.mock('../../../hooks/useRobotActivation', () => ({ __esModule: true, default: () => ({ status: null }) }));
vi.mock('../../../components/Record/recordSounds', async (importOriginal) => ({
  ...(await importOriginal()),
  createRecordSounds: () => ({ prime: vi.fn(), tick: vi.fn(), dispose: vi.fn() }),
}));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  return { __esModule: true, default: fn };
});
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

const REPO = 'lena-schmidt/omx_f_Wuerfel';
const SNAP = {
  taskName: 'Würfel', userId: 'lena-schmidt', robotType: 'omx_f', pushToHub: true, privateMode: true,
  numEpisodes: 3, episodeTime: 20, warmupTime: 5, resetTime: 5, fps: 30,
};
const FORM = {
  taskName: 'Würfel', taskInstruction: ['Greife den Würfel.'], userId: 'lena-schmidt', fps: 30, warmupTime: 5,
  episodeTime: 20, resetTime: 5, numEpisodes: 3, pushToHub: true, privateMode: true, tags: [],
};

function makeStore({ form = FORM, url = 'ws://robot/rosbridge' } = {}) {
  const store = configureStore({ reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer } });
  store.dispatch(moveToPage(PageType.RECORD));
  store.dispatch(setTaskInfo(form));
  store.dispatch(setHeartbeatStatus('connected'));
  if (url) store.dispatch(setRosbridgeUrl(url));
  store.dispatch(setTaskStatus({
    robotType: 'omx_f', phase: TaskPhase.READY, running: false, topicReceived: true, taskType: '',
    receivedAt: performance.now(), receivedWallMs: Date.now(),
  }));
  return store;
}
const mount = (store) => renderHook(() => useRecordController(), {
  wrapper: ({ children }) => <Provider store={store}>{children}</Provider>,
});
const send = (busy) => {
  mockTopics.filter((t) => !t.unsubscribed && t.cb && t.name === '/edubotics/daten_state')
    .forEach((t) => t.cb({ data: JSON.stringify({ v: 1, seq: 1, busy, jobs: [], transfer: null }) }));
};

beforeEach(() => {
  mockTopics.length = 0;
  resetDatenStateForTests();
  mockGetHfUsers.mockReset();
  mockGetHfUsers.mockResolvedValue({ success: true, user_id_list: ['lena-schmidt'] });
});
afterEach(() => resetDatenStateForTests());

describe('R-8: the STARTING pill (pure)', () => {
  const base = {
    heartbeat: 'connected',
    status: { topicReceived: true, running: false, robotType: 'omx_f' },
    form: FORM,
  };
  it('waiting for the upload says so; after 8 s the existing line; otherwise the ordinary one', () => {
    const now = Date.now();
    const session = { pendingStart: { at: now - 1000 }, finish: { state: 'idle' } };
    expect(deriveRecordView({ ...base, session, nowWallMs: now, waitingForUpload: true }).pill.sub)
      .toBe('Wartet, bis das Hochladen fertig ist …');
    expect(deriveRecordView({ ...base, session, nowWallMs: now, waitingForUpload: false }).pill.sub)
      .toBe(RECORD_COPY.pill.startingSub);
    const slow = { pendingStart: { at: now - 9000 }, finish: { state: 'idle' } };
    expect(deriveRecordView({ ...base, session: slow, nowWallMs: now, waitingForUpload: true }).pill.sub)
      .toBe(RECORD_COPY.pill.startingSlow);
  });
});

describe('R-8: through useRecordController and /edubotics/daten_state', () => {
  function startSession(store) {
    act(() => { store.dispatch(recordIntent({ kind: 'start', at: Date.now(), snapshot: SNAP })); });
  }

  it('subscribes only in STARTING; an upload of the started repo shows the wait; another repo does not', async () => {
    const store = makeStore();
    const { result } = mount(store);
    expect(result.current.view).toBe(VIEW.READY);
    expect(mockTopics).toHaveLength(0);
    startSession(store);
    expect(result.current.view).toBe(VIEW.STARTING);
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    expect(result.current.model.pill.sub).toBe(RECORD_COPY.pill.startingSub);
    act(() => send([{ id: 'lena-schmidt/omx_f_anders', kind: 'upload' }]));
    expect(result.current.model.pill.sub).toBe(RECORD_COPY.pill.startingSub);
    act(() => send([{ id: REPO, kind: 'upload' }]));
    expect(result.current.model.pill.sub).toBe(RECORD_COPY.pill.waitUpload);
    act(() => send([]));
    expect(result.current.model.pill.sub).toBe(RECORD_COPY.pill.startingSub);
  });

  // R-8 from the tab that just recorded (V2-3 removed the page's own refusal)
  // and C-2 (the robot lists the waiting Start as `record` AND `upload`; the
  // pill must not depend on their order).
  it.each([
    ['record, upload', [{ id: REPO, kind: 'record' }, { id: REPO, kind: 'upload' }]],
    ['upload, record', [{ id: REPO, kind: 'upload' }, { id: REPO, kind: 'record' }]],
  ])('the same tab: Start is offered while its upload runs, and the wait shows (%s)', async (_order, busy) => {
    const store = makeStore();
    const { result } = mount(store);
    // the previous session ended here and its upload still runs
    act(() => { store.dispatch(recordIntent({ kind: 'start', at: Date.now(), snapshot: SNAP })); });
    const rec = (patch) => setTaskStatus({
      robotType: 'omx_f', running: true, topicReceived: true, taskType: 'record', taskName: 'Würfel',
      numEpisodes: 3, episodeTime: 20, warmupTime: 5, resetTime: 5, fps: 30, pushToHub: true,
      receivedAt: performance.now(), receivedWallMs: Date.now(), ...patch,
    });
    act(() => { store.dispatch(rec({ phase: TaskPhase.RECORDING, totalTime: 20 })); });
    act(() => { store.dispatch(rec({ phase: TaskPhase.SAVING, totalTime: 0, currentEpisodeNumber: 1 })); });
    act(() => { store.dispatch(rec({ phase: TaskPhase.READY, running: false, currentEpisodeNumber: 1 })); });
    act(() => { store.dispatch(recordUploadStatus({ repoId: REPO, status: 'Uploading', percentage: 30, message: '', at: Date.now() + 1 })); });
    act(() => result.current.dismissFinish());
    expect(result.current.view).toBe(VIEW.READY);
    expect(result.current.model.buttons[0]).toMatchObject({ id: 'start', disabled: false });
    // Start (the press dispatches this intent before START_RECORD goes out)
    act(() => { store.dispatch(recordIntent({ kind: 'start', at: Date.now(), snapshot: SNAP })); });
    expect(result.current.view).toBe(VIEW.STARTING);
    await waitFor(() => expect(mockTopics.filter((t) => !t.unsubscribed)).toHaveLength(1));
    act(() => send(busy));
    expect(result.current.model.pill.sub).toBe(RECORD_COPY.pill.waitUpload);
    // the upload ends, the robot records: the ordinary line again
    act(() => send([{ id: REPO, kind: 'record' }]));
    expect(result.current.model.pill.sub).toBe(RECORD_COPY.pill.startingSub);
  });

  it('no topic (an older image): the page is unchanged', async () => {
    const store = makeStore();
    const { result } = mount(store);
    startSession(store);
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    expect(result.current.model.pill.sub).toBe(RECORD_COPY.pill.startingSub);
  });

  it('leaving STARTING ends the subscription', async () => {
    const store = makeStore();
    mount(store);
    startSession(store);
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    act(() => {
      store.dispatch(setTaskStatus({
        robotType: 'omx_f', phase: TaskPhase.WARMING_UP, running: true, topicReceived: true, taskType: 'record',
        taskName: 'Würfel', totalTime: 5, proceedTime: 1, receivedAt: performance.now(), receivedWallMs: Date.now(),
      }));
    });
    await waitFor(() => expect(mockTopics[0].unsubscribed).toBe(true));
  });
});

describe('R-10: the Benutzer-ID list holds the account only', () => {
  it('a stored organisation id is replaced by the account before Start', async () => {
    const store = makeStore({ form: { ...FORM, userId: 'schule-org' } });
    const { result } = mount(store);
    await waitFor(() => expect(store.getState().tasks.taskInfo.userId).toBe('lena-schmidt'));
    expect(result.current.hfUsers.list).toEqual(['lena-schmidt']);
  });

  it('an id that IS in the list stays', async () => {
    mockGetHfUsers.mockResolvedValue({ success: true, user_id_list: ['lena-schmidt', 'schule-B'] });
    const store = makeStore({ form: { ...FORM, userId: 'schule-B' } });
    mount(store);
    await waitFor(() => expect(store.getState().ui.hfUserList).toHaveLength(2));
    expect(store.getState().tasks.taskInfo.userId).toBe('schule-B');
  });
});

describe('Offline Start (§C4, §G11): OFFLINE_START_DE is an ordinary record notice', () => {
  it('a warning, never the „ohne Hochladen" state', () => {
    const OFFLINE_START_DE = 'Hugging Face war beim Start nicht erreichbar. Die Aufnahme läuft trotzdem; beim Hochladen am Ende prüft EduBotics, dass auf Hugging Face nichts überschrieben wird.';
    const s0 = applyRecordIntent(EMPTY_RECORD_SESSION, { kind: 'start', at: 1, snapshot: SNAP });
    const s1 = noteRecordNotice(s0, { kind: 'warn', text: OFFLINE_START_DE, at: 2 });
    expect(s1.uploadOff).toBeFalsy();
    expect(s1.pendingStart).not.toBeNull();
  });
});
