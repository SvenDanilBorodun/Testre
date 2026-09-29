// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Aufnahme page end to end over a REAL reducer store (spec §4.1 C): the
// header, the offline card with its reason, READY → Start sends start_record
// and stays STARTING until the first record tick, the warm-up skip, the three
// answers to „Beenden" and the commands each one sends, the keys, the amber
// [WARNUNG] banner, no rate badge without /edubotics/signal_status, the task
// card locked while offline and while starting, the finish card, „Weiter zum
// Training" and „Neue Aufnahme". The ROS transport, the rig-fact hooks, the
// clock, the camera cells and the 3D twin are mocked.

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import tasksReducer, {
  recordNoticeSet,
  recordRegisterStatus,
  recordUploadStatus,
  setHeartbeatStatus,
  setTaskInfo,
  setTaskStatus,
} from '../../../features/tasks/taskSlice';
import uiReducer, { moveToPage } from '../../../features/ui/uiSlice';
import rosReducer from '../../../features/ros/rosSlice';
import trainingReducer from '../../../features/training/trainingSlice';
import jetsonReducer from '../../../store/jetsonSlice';
import PageType from '../../../constants/pageType';
import TaskPhase from '../../../constants/taskPhases';
import RecordPage, { RECORD_VIEW_KEY } from '../../../pages/RecordPage';

const mockSend = vi.fn();
const mockGetHfUsers = vi.fn();
const mockGetTopics = vi.fn();
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({
    sendRecordCommand: mockSend,
    getRegisteredHFUser: mockGetHfUsers,
    getImageTopicList: mockGetTopics,
  }),
}));

let mockClock = { subscribe: () => () => {}, secondsLeft: 0, elapsed: 0 };
vi.mock('../../../hooks/useSmoothPhaseClock', () => ({ __esModule: true, default: () => mockClock }));
let mockSignal = { payload: null, receivedAt: null };
vi.mock('../../../hooks/useSignalStatus', () => ({ __esModule: true, default: () => mockSignal }));
vi.mock('../../../hooks/useRsBridgeStatus', () => ({
  __esModule: true,
  default: () => ({ available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false, probed: true }),
}));
vi.mock('../../../hooks/useRobotActivation', () => ({ __esModule: true, default: () => ({ status: null }) }));
vi.mock('../../Record/recordSounds', async (importOriginal) => ({
  ...(await importOriginal()),
  createRecordSounds: () => ({ prime: vi.fn(), tick: vi.fn(), dispose: vi.fn() }),
}));
vi.mock('../../HeartbeatStatus', () => ({ __esModule: true, default: () => <span>Verbindungsanzeige</span> }));
vi.mock('../../ImageGridCell', () => ({
  __esModule: true,
  default: ({ topic }) => <span data-testid="stream" data-topic={topic} />,
}));
vi.mock('../../UrdfTwin', () => ({
  __esModule: true,
  default: ({ viewPreset }) => <span data-testid="twin" data-preset={viewPreset} />,
}));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn, useToasterStore: () => ({ toasts: [] }) };
});

const CAPS = {
  recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true,
  display_de: 'OMX – Voll', camera_roles: ['gripper', 'scene'],
};
const FORM = {
  taskName: 'Würfel in die Schale',
  taskInstruction: ['Greife den Würfel.'],
  userId: 'schule-A',
  fps: 30,
  warmupTime: 5,
  episodeTime: 20,
  resetTime: 5,
  numEpisodes: 3,
  pushToHub: true,
  privateMode: true,
  tags: ['omx_f', 'edubotics'],
};

const idleTick = (patch = {}) => setTaskStatus({
  robotType: 'omx_f', robotProfile: 'omx_full', capabilities: CAPS, phase: TaskPhase.READY, running: false,
  topicReceived: true, taskType: '', receivedAt: performance.now(), receivedWallMs: Date.now(), ...patch,
});
const recordTick = (patch = {}) => idleTick({
  running: true, taskType: 'record', taskName: FORM.taskName, numEpisodes: 3, episodeTime: 20,
  warmupTime: 5, resetTime: 5, fps: 30, pushToHub: true, currentEpisodeNumber: 0, ...patch,
});

function makeStore({ connected = true, form = FORM } = {}) {
  const store = configureStore({
    reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer, jetson: jetsonReducer },
  });
  store.dispatch(moveToPage(PageType.RECORD));
  store.dispatch(setTaskInfo(form));
  if (connected) store.dispatch(setHeartbeatStatus('connected'));
  store.dispatch(idleTick());
  return store;
}

function renderPage(store) {
  return render(
    <Provider store={store}>
      <RecordPage />
    </Provider>
  );
}

const dispatch = (store, action) => act(() => { store.dispatch(action); });
const page = () => screen.getByTestId('rec-page');
const bar = () => within(screen.getByTestId('rec-actionbar'));
const commandsSent = () => mockSend.mock.calls.map((c) => c[0]);
const press = (key, extra = {}) => act(() => {
  window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...extra }));
});

beforeEach(() => {
  mockSend.mockReset();
  mockSend.mockResolvedValue({ success: true, message: '' });
  mockGetHfUsers.mockReset();
  mockGetHfUsers.mockResolvedValue({ success: true, user_id_list: ['schule-A'] });
  mockGetTopics.mockReset();
  mockGetTopics.mockResolvedValue({ success: true, image_topic_list: ['/scene/image_raw', '/gripper/image_raw'] });
  mockClock = { subscribe: () => () => {}, secondsLeft: 0, elapsed: 0 };
  mockSignal = { payload: null, receivedAt: null };
  try { localStorage.removeItem(RECORD_VIEW_KEY); } catch { /* none */ }
});

describe('RecordPage — header and connection', () => {
  it('names the task and the robot; the page is the light Aufnahme page', async () => {
    renderPage(makeStore());
    expect(screen.getByRole('heading', { level: 1, name: 'Würfel in die Schale' })).toBeInTheDocument();
    expect(screen.getByText('OMX – Voll')).toBeInTheDocument();
    expect(page()).toHaveClass('rec-page');
    expect(page()).toHaveAttribute('data-view', 'READY');
    await waitFor(() => expect(screen.getAllByTestId('rec-camera-tile')).toHaveLength(2));
  });

  it('an empty task name reads „Neue Aufgabe"', () => {
    renderPage(makeStore({ form: { ...FORM, taskName: '' } }));
    expect(screen.getByRole('heading', { level: 1, name: 'Neue Aufgabe' })).toBeInTheDocument();
  });

  it('offline: the stage says so and why, Start is off, the task card is locked with the reason', () => {
    renderPage(makeStore({ connected: false }));
    expect(page()).toHaveAttribute('data-view', 'OFFLINE');
    const card = screen.getByTestId('rec-stage-card');
    expect(card).toHaveTextContent('Keine Verbindung zum Roboter');
    expect(card).toHaveTextContent('Umgebung starten');
    expect(bar().getByRole('button', { name: /Aufnahme starten/ })).toBeDisabled();
    expect(screen.getByTestId('rec-locked')).toHaveTextContent('Nicht verbunden.');
    expect(screen.getByLabelText('Aufgabenname')).toBeDisabled();
  });
});

describe('RecordPage — Start', () => {
  it('Start sends start_record and stays STARTING (locked, loader) until the first record tick', async () => {
    let resolveStart;
    mockSend.mockImplementation(() => new Promise((r) => { resolveStart = r; }));
    const store = makeStore();
    renderPage(store);
    fireEvent.click(bar().getByRole('button', { name: /Aufnahme starten/ }));
    await waitFor(() => expect(commandsSent()).toEqual(['start_record']));
    await waitFor(() => expect(page()).toHaveAttribute('data-view', 'STARTING'));
    const start = bar().getByRole('button', { name: /Aufnahme starten/ });
    expect(start).toBeDisabled();
    expect(start.querySelector('svg[data-icon="loading"]')).not.toBeNull(); // eslint-disable-line testing-library/no-node-access
    expect(screen.getByText('Startet …')).toBeInTheDocument();
    expect(screen.getByTestId('rec-locked')).toHaveTextContent('Während der Aufnahme gesperrt.');

    // The service answers — still STARTING: only a record tick ends it (F2).
    await act(async () => { resolveStart({ success: true, message: '' }); });
    expect(page()).toHaveAttribute('data-view', 'STARTING');

    mockClock = { ...mockClock, secondsLeft: 5, elapsed: 0 };
    dispatch(store, recordTick({ phase: TaskPhase.WARMING_UP, totalTime: 5, proceedTime: 0 }));
    expect(page()).toHaveAttribute('data-view', 'WARMUP');
    expect(screen.getByTestId('rec-phase-overlay')).toHaveAttribute('data-phase', 'warmup');
    expect(screen.getByText('Vor Episode 1')).toBeInTheDocument();
  });

  it('an invalid form is refused on the click with the reason, nothing is sent', async () => {
    renderPage(makeStore({ form: { ...FORM, taskName: '' } }));
    fireEvent.click(bar().getByRole('button', { name: /Aufnahme starten/ }));
    expect(await screen.findAllByText('Bitte gib einen Aufgabennamen ein.')).not.toHaveLength(0);
    expect(mockSend).not.toHaveBeenCalled();
  });
});

describe('RecordPage — during a session', () => {
  function inRecording({ proceed = 6, elapsed = 6, left = 14 } = {}) {
    const store = makeStore();
    renderPage(store);
    mockClock = { ...mockClock, secondsLeft: left, elapsed };
    dispatch(store, recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: proceed }));
    return store;
  }

  it('the warm-up skip sends next', async () => {
    const store = makeStore();
    renderPage(store);
    mockClock = { ...mockClock, secondsLeft: 4, elapsed: 1 };
    dispatch(store, recordTick({ phase: TaskPhase.WARMING_UP, totalTime: 5, proceedTime: 1 }));
    fireEvent.click(bar().getByRole('button', { name: /Jetzt starten/ }));
    await waitFor(() => expect(commandsSent()).toEqual(['next']));
  });

  it('recording: the frame, the buttons with their keys, and → saves early', async () => {
    inRecording();
    expect(screen.getByTestId('rec-stage')).toHaveClass('is-run');
    expect(screen.getByText('REC 00:06')).toBeInTheDocument();
    expect(bar().getByRole('button', { name: /Wiederholen/ })).toHaveTextContent('←');
    expect(bar().getByRole('button', { name: /Beenden/ })).toHaveTextContent('Strg+Umschalt+X');
    press('ArrowRight');
    await waitFor(() => expect(commandsSent()).toEqual(['next']));
  });

  it('„Beenden" → the question; Ctrl+Shift+X opens it too and sends NOTHING; Esc closes it', async () => {
    inRecording();
    press('X', { ctrlKey: true, shiftKey: true });
    const q = await screen.findByTestId('rec-question');
    expect(q).toHaveTextContent('Episode 1 ist noch nicht fertig.');
    expect(mockSend).not.toHaveBeenCalled();
    press('Escape');
    await waitFor(() => expect(screen.queryByTestId('rec-question')).toBeNull());
    fireEvent.click(bar().getByRole('button', { name: /Beenden/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Weiter aufnehmen/ }));
    await waitFor(() => expect(screen.queryByTestId('rec-question')).toBeNull());
    expect(mockSend).not.toHaveBeenCalled();
  });

  it('„Behalten und beenden" sends finish', async () => {
    inRecording();
    fireEvent.click(bar().getByRole('button', { name: /Beenden/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Behalten und beenden/ }));
    await waitFor(() => expect(commandsSent()).toEqual(['finish']));
  });

  it('„Verwerfen und beenden" sends rerecord, then finish', async () => {
    inRecording();
    fireEvent.click(bar().getByRole('button', { name: /Beenden/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Verwerfen und beenden/ }));
    await waitFor(() => expect(commandsSent()).toEqual(['rerecord', 'finish']));
  });

  it('a record [WARNUNG] becomes an amber banner on the page', async () => {
    const store = inRecording();
    dispatch(store, recordNoticeSet({
      kind: 'warn',
      text: 'Die Szenen-Kamera liefert nur 11 statt 30 Bilder pro Sekunde. Der Datensatz enthält wiederholte Bilder.',
      at: Date.now(),
    }));
    const banner = await screen.findByTestId('rec-banner');
    expect(banner).toHaveAttribute('data-kind', 'warn');
    expect(banner).toHaveTextContent('Die Szenen-Kamera liefert nur 11 statt 30 Bilder pro Sekunde.');
  });
});

describe('RecordPage — rate badges', () => {
  it('no badge without /edubotics/signal_status (an older image)', async () => {
    renderPage(makeStore());
    await waitFor(() => expect(screen.getAllByTestId('stream')).toHaveLength(2));
    expect(screen.queryByText(/ Hz$/)).toBeNull();
  });

  it('with signal status each camera shows its rate', async () => {
    mockSignal = {
      receivedAt: performance.now(),
      payload: {
        v: 1, seq: 3, uptime_s: 60, recording: false,
        sources: [
          { kind: 'camera', name: 'gripper', topic: '/gripper/image_raw', hz: 29.8, age_s: 0.03 },
          { kind: 'camera', name: 'scene', topic: '/scene/image_raw', hz: 29.9, age_s: 0.03 },
          { kind: 'follower', name: 'follower', topic: '/joint_states', hz: 99, age_s: 0.01 },
        ],
        disk: { free_bytes: 50e9, start_floor_bytes: 3e9, critical_floor_bytes: 1e9 },
      },
    };
    renderPage(makeStore());
    await waitFor(() => expect(screen.getByText('29,8 Hz')).toBeInTheDocument());
    expect(screen.getByText('29,9 Hz')).toBeInTheDocument();
  });
});

describe('RecordPage — view switch and the 3D tile', () => {
  it('„3D" mounts the twin with the preset buttons and is remembered; presets are not', async () => {
    const { unmount } = renderPage(makeStore());
    fireEvent.click(screen.getByRole('button', { name: '3D' }));
    const twin = await screen.findByTestId('twin');
    expect(twin).toHaveAttribute('data-preset', 'persp');
    expect(screen.queryAllByTestId('rec-camera-tile')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Oben' }));
    expect(screen.getByTestId('twin')).toHaveAttribute('data-preset', 'top');
    expect(localStorage.getItem(RECORD_VIEW_KEY)).toBe('3d');
    unmount();
    renderPage(makeStore());
    expect(await screen.findByTestId('twin')).toHaveAttribute('data-preset', 'persp');
  });
});

describe('RecordPage — the finish', () => {
  async function recordOneAndEnd(store, { pushToHub }) {
    mockClock = { ...mockClock, secondsLeft: 10, elapsed: 10 };
    dispatch(store, recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 10, pushToHub }));
    dispatch(store, recordTick({ phase: TaskPhase.SAVING, totalTime: 0, proceedTime: 0, pushToHub }));
    dispatch(store, recordTick({ phase: TaskPhase.RESETTING, totalTime: 5, proceedTime: 0, currentEpisodeNumber: 1, pushToHub }));
    dispatch(store, idleTick({ currentEpisodeNumber: 1 }));
  }

  it('upload on: uploading → done, then „Weiter zum Training" opens the dataset there', async () => {
    const store = makeStore();
    renderPage(store);
    await recordOneAndEnd(store, { pushToHub: true });
    const card = await screen.findByTestId('rec-finish-card');
    expect(card).toHaveTextContent('Zu Hugging Face hochladen');
    const repoId = store.getState().tasks.recordSession.finish.expectedRepoId || 'schule-A/omx_f_Wuerfel-in-die-Schale';
    const at = Date.now() + 1;
    dispatch(store, recordUploadStatus({ repoId, status: 'Uploading', percentage: 40, message: '', at }));
    expect(await screen.findByText('40 %')).toBeInTheDocument();
    dispatch(store, recordUploadStatus({ repoId, status: 'Success', percentage: 100, message: '', at: at + 1 }));
    dispatch(store, recordRegisterStatus({ repoId, state: 'done', at: at + 2 }));
    expect(await screen.findByText('Dein Datensatz ist bereit')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Weiter zum Training' }));
    expect(store.getState().ui.currentPage).toBe(PageType.TRAINING);
    expect(store.getState().training.trainingInfo.datasetRepoId).toBe(repoId);
    expect(store.getState().training.selectedUser).toBe('schule-A');
  });

  it('upload off: saved on this computer; „Neue Aufnahme" brings Start back', async () => {
    const store = makeStore({ form: { ...FORM, pushToHub: false } });
    renderPage(store);
    await recordOneAndEnd(store, { pushToHub: false });
    const card = await screen.findByTestId('rec-finish-card');
    expect(card).toHaveTextContent('Nicht hochgeladen (Hochladen ist ausgeschaltet)');
    fireEvent.click(within(card).getByRole('button', { name: 'Neue Aufnahme' }));
    await waitFor(() => expect(screen.queryByTestId('rec-finish-card')).toBeNull());
    expect(page()).toHaveAttribute('data-view', 'READY');
    expect(bar().getByRole('button', { name: /Aufnahme starten/ })).toBeEnabled();
    // The session list keeps what happened.
    expect(screen.getByTestId('rec-session-card')).toBeInTheDocument();
  });
});
