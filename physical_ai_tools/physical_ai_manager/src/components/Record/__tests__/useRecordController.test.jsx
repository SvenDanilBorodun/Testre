// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// useRecordController (spec §3.7) against a real reducer store: the window
// key listener, validation on Start, the Benutzer-ID auto-reload / auto-select
// (only when editable and never chosen), the 700 Hz ticks at 3/2/1, the sound
// primed inside the start, the „Beenden" question and the finish actions.
// The ROS transport, the rig-fact hooks and the clock are mocked.

import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import tasksReducer, { setHeartbeatStatus, setTaskInfo, setTaskStatus } from '../../../features/tasks/taskSlice';
import uiReducer, { moveToPage } from '../../../features/ui/uiSlice';
import rosReducer from '../../../features/ros/rosSlice';
import trainingReducer from '../../../features/training/trainingSlice';
import PageType from '../../../constants/pageType';
import TaskPhase from '../../../constants/taskPhases';
import useRecordController from '../useRecordController';
import { VIEW } from '../model/phaseModel';

const mockSend = vi.fn();
const mockGetHfUsers = vi.fn();
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({ sendRecordCommand: mockSend, getRegisteredHFUser: mockGetHfUsers }),
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

const mockSounds = { prime: vi.fn(), tick: vi.fn(), dispose: vi.fn() };
vi.mock('../recordSounds', async (importOriginal) => ({
  ...(await importOriginal()),
  createRecordSounds: () => mockSounds,
}));

vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  return { __esModule: true, default: fn };
});

const VALID_FORM = {
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
  robotType: 'omx_f', phase: TaskPhase.READY, running: false, topicReceived: true, taskType: '',
  receivedAt: performance.now(), receivedWallMs: Date.now(), ...patch,
});
const recordTick = (patch = {}) => idleTick({
  running: true, taskType: 'record', taskName: 'Würfel in die Schale', numEpisodes: 3, episodeTime: 20,
  warmupTime: 5, resetTime: 5, fps: 30, pushToHub: true, ...patch,
});

function makeStore({ form = VALID_FORM, connected = true } = {}) {
  const store = configureStore({
    reducer: { tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer },
  });
  store.dispatch(moveToPage(PageType.RECORD));
  store.dispatch(setTaskInfo(form));
  if (connected) store.dispatch(setHeartbeatStatus('connected'));
  store.dispatch(idleTick());
  return store;
}

function mount(store, props = {}) {
  const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
  return renderHook(() => useRecordController(props), { wrapper });
}

// The page's own act(), called by id (named apart from testing-library's act).
const pageAct = (result, id) => result.current.act(id);

const press = (key, extra = {}) => {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...extra });
  act(() => { window.dispatchEvent(event); });
  return event;
};

beforeEach(() => {
  mockSend.mockReset();
  mockSend.mockResolvedValue({ success: true, message: '' });
  mockGetHfUsers.mockReset();
  mockGetHfUsers.mockResolvedValue({ success: true, user_id_list: ['schule-A', 'schule-B'] });
  mockSounds.prime.mockClear();
  mockSounds.tick.mockClear();
  mockClock = { subscribe: () => () => {}, secondsLeft: 0, elapsed: 0 };
  mockSignal = { payload: null, receivedAt: null };
  try { localStorage.removeItem('edubotics_audio_muted'); } catch { /* none */ }
});

describe('the key listener', () => {
  it('Space in READY validates, primes the sound and starts; the page then shows STARTING', async () => {
    const store = makeStore();
    const { result } = mount(store);
    expect(result.current.view).toBe(VIEW.READY);
    const ev = press(' ');
    expect(ev.defaultPrevented).toBe(true);
    await waitFor(() => expect(mockSend).toHaveBeenCalledWith('start_record'));
    expect(mockSounds.prime).toHaveBeenCalledTimes(1);
    expect(store.getState().tasks.recordSession.pendingStart.snapshot).toMatchObject({
      taskName: 'Würfel in die Schale', userId: 'schule-A', robotType: 'omx_f', numEpisodes: 3,
    });
    await waitFor(() => expect(result.current.view).toBe(VIEW.STARTING));
  });

  it('an invalid form is refused on the click, with the reason at the field', async () => {
    const store = makeStore({ form: { ...VALID_FORM, taskName: '' } });
    const { result } = mount(store);
    press(' ');
    expect(mockSend).not.toHaveBeenCalled();
    expect(mockSounds.prime).not.toHaveBeenCalled();
    expect(result.current.problem).toEqual({ kind: 'warn', textDe: 'Bitte gib einen Aufgabennamen ein.' });
    expect(result.current.invalid).toEqual({ field: 'taskName', messageDe: 'Bitte gib einen Aufgabennamen ein.' });
  });

  it('typing into a field is not a command; an inactive page does not listen', () => {
    const store = makeStore();
    const { unmount: unmountFirst } = mount(store);
    const input = document.createElement('input');
    document.body.appendChild(input);
    act(() => { input.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })); });
    expect(mockSend).not.toHaveBeenCalled();
    input.remove();
    unmountFirst();

    const other = makeStore();
    mount(other, { isActive: false });
    press(' ');
    expect(mockSend).not.toHaveBeenCalled();
  });

  it('→ skips the warm-up; Strg+Umschalt+X in RECORDING asks first, Esc closes the question', async () => {
    const store = makeStore();
    const { result } = mount(store);
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.WARMING_UP, totalTime: 5 })); });
    expect(result.current.view).toBe(VIEW.WARMUP);
    press('ArrowRight');
    await waitFor(() => expect(mockSend).toHaveBeenCalledWith('next'));

    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 3 })); });
    mockClock = { ...mockClock, elapsed: 3, secondsLeft: 17 };
    await waitFor(() => expect(result.current.busy).toBe(false));
    mockSend.mockClear();
    press('X', { ctrlKey: true, shiftKey: true });
    expect(mockSend).not.toHaveBeenCalled();
    expect(result.current.question).toMatchObject({ kind: 'end', episode: 1, title: 'Episode 1 ist noch nicht fertig.' });
    expect(result.current.question.answers.map((a) => a.id)).toEqual(['keepAndEnd', 'discardAndEnd', 'back']);
    press('ArrowLeft'); // nothing else while the question is open
    expect(mockSend).not.toHaveBeenCalled();
    press('Escape');
    expect(result.current.question).toBeNull();
  });

  it('the question closes itself when the phase leaves RECORDING', () => {
    const store = makeStore();
    const { result } = mount(store);
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 3 })); });
    act(() => pageAct(result, 'end'));
    expect(result.current.question).not.toBeNull();
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.SAVING, totalTime: 0 })); });
    expect(result.current.question).toBeNull();
  });

  it('„Verwerfen und beenden" sends RERECORD then FINISH', async () => {
    const store = makeStore();
    const { result } = mount(store);
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 3 })); });
    act(() => pageAct(result, 'end'));
    act(() => pageAct(result, 'discardAndEnd'));
    await waitFor(() => expect(mockSend.mock.calls.map((c) => c[0])).toEqual(['rerecord', 'finish']));
    expect(result.current.question).toBeNull();
  });
});

describe('render budget (V2-2)', () => {
  it('30 identical running ticks — status plus the adopt path\'s fresh arrays — cause no render', () => {
    const store = makeStore();
    let hookRuns = 0;
    const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
    renderHook(() => { hookRuns += 1; return useRecordController(); }, { wrapper });
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 3 })); });
    act(() => { store.dispatch(setTaskInfo({ taskInstruction: ['Greife den Würfel.'], tags: ['omx_f', 'edubotics'] })); });
    const runsBefore = hookRuns;
    act(() => {
      for (let i = 0; i < 30; i += 1) {
        store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 3 }));
        // what useRosTopicSubscription's adopt path sends on every running tick
        store.dispatch(setTaskInfo({
          taskName: 'Würfel in die Schale',
          taskInstruction: ['Greife den Würfel.'],
          tags: ['omx_f', 'edubotics'],
          fps: 30,
          episodeTime: 20,
          resetTime: 5,
          numEpisodes: 3,
          pushToHub: true,
          warmupTime: 5,
        }));
      }
    });
    expect(hookRuns).toBe(runsBefore);
    // a real change still renders
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 4 })); });
    expect(hookRuns).toBeGreaterThan(runsBefore);
  });
});

describe('Benutzer-ID (ported from InfoPanel)', () => {
  it('an empty list reloads silently; an unchosen id picks the first account', async () => {
    const store = makeStore({ form: { ...VALID_FORM, userId: undefined } });
    const { result } = mount(store);
    await waitFor(() => expect(mockGetHfUsers).toHaveBeenCalled());
    await waitFor(() => expect(store.getState().tasks.taskInfo.userId).toBe('schule-A'));
    expect(result.current.hfUsers.list).toEqual(['schule-A', 'schule-B']);
  });

  it('never overrides a chosen id', async () => {
    const store = makeStore({ form: { ...VALID_FORM, userId: 'schule-B' } });
    mount(store);
    await waitFor(() => expect(store.getState().ui.hfUserList).toEqual(['schule-A', 'schule-B']));
    expect(store.getState().tasks.taskInfo.userId).toBe('schule-B');
  });

  it('does not auto-select while the form is locked', async () => {
    const store = makeStore({ form: { ...VALID_FORM, userId: undefined } });
    store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20 }));
    mount(store);
    await waitFor(() => expect(store.getState().ui.hfUserList).toHaveLength(2));
    expect(store.getState().tasks.taskInfo.userId).toBeUndefined();
  });

  it('does not reload while offline', async () => {
    const store = makeStore({ connected: false });
    mount(store);
    await act(async () => { await Promise.resolve(); });
    expect(mockGetHfUsers).not.toHaveBeenCalled();
  });
});

describe('the countdown ticks', () => {
  const instanceOf = (store) => store.getState().tasks.phaseAnchor.instance;

  it('700 Hz at 3, 2 and 1 s left of a warm-up — not before, not at 0', () => {
    const store = makeStore();
    const { rerender } = mount(store);
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.WARMING_UP, totalTime: 5 })); });
    for (const left of [5, 4, 3, 2, 1, 0]) {
      mockClock = { ...mockClock, secondsLeft: left, elapsed: 5 - left, instance: instanceOf(store) };
      rerender();
    }
    expect(mockSounds.tick).toHaveBeenCalledTimes(3);
  });

  it('none while recording (the hook has its own beeps there)', () => {
    const store = makeStore();
    const { rerender } = mount(store);
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20 })); });
    for (const left of [4, 3, 2, 1]) {
      mockClock = { ...mockClock, secondsLeft: left, instance: instanceOf(store) };
      rerender();
    }
    expect(mockSounds.tick).not.toHaveBeenCalled();
  });

  it('never for the previous phase\'s seconds (a redo with 3 s left, then Zurücksetzen)', () => {
    const store = makeStore();
    const { rerender } = mount(store);
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20, proceedTime: 17 })); });
    mockClock = { ...mockClock, secondsLeft: 3, instance: instanceOf(store) };
    rerender();
    const recordingInstance = instanceOf(store);
    // the reset begins; for one render the clock still shows the recording's 3 s
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RESETTING, totalTime: 5 })); });
    expect(instanceOf(store)).not.toBe(recordingInstance);
    rerender();
    expect(mockSounds.tick).not.toHaveBeenCalled();
    mockClock = { ...mockClock, secondsLeft: 5, instance: instanceOf(store) };
    rerender();
    expect(mockSounds.tick).not.toHaveBeenCalled();
  });

  it('c.clock stands in the robot\'s own count while the clock still shows the previous phase', () => {
    const store = makeStore();
    const { result, rerender } = mount(store);
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RESETTING, totalTime: 5, proceedTime: 1 })); });
    mockClock = { ...mockClock, secondsLeft: 3, elapsed: 17, instance: -1 };
    rerender();
    expect(result.current.clock).toMatchObject({ secondsLeft: 4, elapsed: 1 });
    expect(result.current.model.pill.sub).toBe('noch 4 s · dann Episode 1');
    mockClock = { ...mockClock, secondsLeft: 4, elapsed: 1.5, instance: instanceOf(store) };
    rerender();
    expect(result.current.clock).toMatchObject({ secondsLeft: 4, elapsed: 1.5 });
  });

  it('onPhaseTick listeners hear every whole-second change', () => {
    const store = makeStore();
    const { result, rerender } = mount(store);
    const heard = [];
    act(() => { result.current.onPhaseTick((e) => heard.push(e.secondsLeft)); });
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RESETTING, totalTime: 3, currentEpisodeNumber: 1 })); });
    for (const left of [3, 2, 1]) {
      mockClock = { ...mockClock, secondsLeft: left, instance: instanceOf(store) };
      rerender();
    }
    expect(heard).toEqual(expect.arrayContaining([2, 1]));
    expect(mockSounds.tick).toHaveBeenCalled();
  });
});

describe('signal facts', () => {
  const payload = (patch = {}) => ({
    v: 1, seq: 1, uptime_s: 30, recording: false,
    sources: [
      { kind: 'camera', name: 'gripper', topic: '/gripper/image_raw', hz: 29.8, age_s: 0.03 },
      { kind: 'camera', name: 'scene', topic: '/scene/image_raw', hz: 11.2, age_s: 0.05 },
      { kind: 'follower', name: 'follower', topic: '/joint_states', hz: 99, age_s: 0.01 },
      { kind: 'leader', name: 'leader', topic: '/leader/joint_trajectory', hz: null, age_s: null },
    ],
    disk: { free_bytes: 5e10, start_floor_bytes: 3e9, critical_floor_bytes: 1e9 },
    ...patch,
  });

  it('no payload (an older image): no badges, no banner, no block', () => {
    const store = makeStore();
    const { result } = mount(store);
    expect(result.current.signal).toBeNull();
    expect(result.current.problem).toBeNull();
    expect(result.current.model.startBlock).toBeNull();
  });

  it('badges per source; a silent leader refuses Start with its reason', () => {
    mockSignal = { payload: payload(), receivedAt: performance.now() };
    const store = makeStore();
    const { result } = mount(store);
    expect(result.current.signal).toEqual([
      { kind: 'camera', name: 'gripper', labelDe: 'Greifer-Kamera', hz: 29.8, hzText: '29,8 Hz', verdict: 'ok' },
      { kind: 'camera', name: 'scene', labelDe: 'Szenen-Kamera', hz: 11.2, hzText: '11,2 Hz', verdict: 'slow' },
      { kind: 'follower', name: 'follower', labelDe: 'Follower-Arm', hz: 99, hzText: '99 Hz', verdict: 'ok' },
      { kind: 'leader', name: 'leader', labelDe: 'Leader-Arm', hz: null, hzText: '—', verdict: 'stalled' },
    ]);
    expect(result.current.model.startBlock.kind).toBe('source');
    expect(result.current.problem.textDe)
      .toBe('Der Leader-Arm sendet keine Daten. Prüfe Kabel und Stromversorgung des Leader-Arms.');
    press(' ');
    expect(mockSend).not.toHaveBeenCalled();
  });

  it('low disk refuses Start first', () => {
    mockSignal = {
      payload: payload({ disk: { free_bytes: 2e9, start_floor_bytes: 3e9, critical_floor_bytes: 1e9 } }),
      receivedAt: performance.now(),
    };
    const store = makeStore();
    const { result } = mount(store);
    expect(result.current.model.startBlock.kind).toBe('disk');
    expect(result.current.model.buttons[0].disabled).toBe(true);
    expect(result.current.problem.kind).toBe('bad');
  });
});

describe('form, mute and the finish actions', () => {
  it('setField writes only while editable; the instruction is one line', () => {
    const store = makeStore();
    const { result } = mount(store);
    act(() => result.current.setField('taskInstruction', 'Lege den Würfel ab.'));
    expect(store.getState().tasks.taskInfo.taskInstruction).toEqual(['Lege den Würfel ab.']);
    expect(result.current.form.taskInstruction).toBe('Lege den Würfel ab.');
    act(() => result.current.setField('taskName', 'x'.repeat(80)));
    expect(store.getState().tasks.taskInfo.taskName).toHaveLength(60);
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20 })); });
    act(() => result.current.setField('episodeTime', 30));
    expect(store.getState().tasks.taskInfo.episodeTime).toBe(20);
  });

  it('the estimate, the save name and the steppers', () => {
    const store = makeStore({ form: { ...VALID_FORM, warmupTime: 5, episodeTime: 20, resetTime: 5, numEpisodes: 5 } });
    const { result } = mount(store);
    expect(result.current.estimate.totalS).toBe(125);
    expect(result.current.estimate.text).toMatch(/^≈ 2:05 min für 5 Episoden/);
    expect(result.current.repoPreview).toBe('schule-A/omx_f_Wuerfel-in-die-Schale');
    expect(result.current.saveName).toMatchObject({ text: 'Wird privat auf Hugging Face gespeichert als', public: false });
    expect(result.current.steppers.map((s) => [s.field, s.min, s.max])).toEqual([
      ['warmupTime', 0, 60], ['episodeTime', 3, 120], ['resetTime', 0, 60], ['numEpisodes', 1, 100],
    ]);
  });

  it('the mute switch uses the existing key', () => {
    const store = makeStore();
    const { result } = mount(store);
    expect(result.current.muted).toBe(false);
    act(() => result.current.toggleMute());
    expect(result.current.muted).toBe(true);
    expect(localStorage.getItem('edubotics_audio_muted')).toBe('1');
  });

  it('„Weiter zum Training" selects the dataset and moves; „Neue Aufnahme" clears the card', () => {
    const store = makeStore();
    const { result } = mount(store);
    act(() => { pageAct(result, 'start'); });
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.RECORDING, totalTime: 20 })); });
    act(() => { store.dispatch(recordTick({ phase: TaskPhase.SAVING, totalTime: 0, currentEpisodeNumber: 1 })); });
    act(() => {
      store.dispatch(recordTick({ phase: TaskPhase.READY, running: false, currentEpisodeNumber: 1, receivedWallMs: Date.now() }));
    });
    expect(result.current.view).toBe(VIEW.FINISHING);
    expect(result.current.finish).toMatchObject({ visible: true, state: 'uploading' });
    act(() => result.current.goToTraining());
    const st = store.getState();
    expect(st.training.selectedUser).toBe('schule-A');
    expect(st.training.selectedDataset).toBe('omx_f_Wuerfel-in-die-Schale');
    expect(st.training.trainingInfo.datasetRepoId).toBe('schule-A/omx_f_Wuerfel-in-die-Schale');
    expect(st.ui.currentPage).toBe(PageType.TRAINING);
    act(() => result.current.dismissFinish());
    expect(store.getState().tasks.recordSession.finish.dismissed).toBe(true);
    expect(store.getState().tasks.recordNotice).toBeNull();
  });

  it('a recordable=false manifest leaves the page', () => {
    const store = makeStore();
    mount(store);
    act(() => {
      store.dispatch(setTaskStatus({
        capabilities: {
          recordable: false, editable: false, trainable: false, inferable: true, roboter_studio: true, has_leader: false,
        },
      }));
    });
    expect(store.getState().ui.currentPage).toBe(PageType.HOME);
  });
});
