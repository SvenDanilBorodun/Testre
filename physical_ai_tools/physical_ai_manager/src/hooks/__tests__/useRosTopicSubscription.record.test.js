// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Aufnahme 2.0 on the /task/status and /huggingface/status feeds (spec §3.3,
// owner decision F3), driven through the hook's REAL subscribe callbacks (the
// idiom of useRosTopicSubscription.caps.test.js). The hook reads the page and
// the session from the REAL store singleton, so the tests set those there;
// what the hook dispatches is captured by the mocked react-redux dispatch.
//
// Locks:
//  - a RECORD tick's [WARNUNG] → a warn notice, the tick still dispatched with
//    error '' and the stripped text in `recordWarn`; a toast only OFF the
//    Aufnahme page;
//  - an INFERENZ [WARNUNG] tick keeps HEAD: red toast, dropped, no notice;
//  - a hard error with a record session (or a pending start) → an error
//    notice, no toast ON the Aufnahme page, the tick dropped;
//  - `error` is '' in every dispatched setTaskStatus payload;
//  - the statusPayload carries the §3.2 fields;
//  - HF upload statuses and the cloud registration reach the session.

import { renderHook, act } from '@testing-library/react';
import toast from 'react-hot-toast';
import { useRosTopicSubscription } from '../useRosTopicSubscription';
import TaskPhase from '../../constants/taskPhases';
import PageType from '../../constants/pageType';
import realStore from '../../store/store';
import { moveToPage } from '../../features/ui/uiSlice';
import { recordIntent, recordSessionDismiss, setTaskStatus } from '../../features/tasks/taskSlice';
import { setSession } from '../../features/auth/authSlice';
import { signedOut } from '../../features/session/sessionActions';
import { registerDataset } from '../../services/datasetsApi';
import { toastIcon } from '../../components/icons/toast';

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
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn };
});

vi.mock('../../services/datasetsApi', () => ({
  __esModule: true,
  registerDataset: vi.fn(() => Promise.resolve({})),
}));

vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: {
    getConnection: vi.fn(() =>
      Promise.resolve({ isConnected: true, on: () => {}, off: () => {} })
    ),
  },
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

function callbackFor(topic) {
  const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === topic);
  return calls.length ? calls[calls.length - 1][1] : null;
}

const dispatched = (type) => mockDispatch.mock.calls.map(([a]) => a).filter((a) => a && a.type === type);

function status(overrides = {}) {
  return {
    robot_type: 'omx_f',
    robot_profile: '',
    capabilities_json: '',
    phase: TaskPhase.RECORDING,
    error: '',
    total_time: 20,
    proceed_time: 3,
    encoding_progress: 0,
    current_episode_number: 1,
    current_scenario_number: 0,
    current_task_instruction: '',
    used_storage_size: 0,
    total_storage_size: 0,
    used_cpu: 0,
    used_ram_size: 0,
    total_ram_size: 0,
    joint_dist_to_home: [],
    task_info: {
      task_name: 'Würfel',
      task_type: 'record',
      task_instruction: ['Greife den Würfel.'],
      policy_path: '',
      record_inference_mode: false,
      user_id: '',
      fps: 30,
      episode_time_s: 20,
      reset_time_s: 5,
      num_episodes: 5,
      push_to_hub: true,
      private_mode: true,
      use_optimized_save_mode: true,
      record_rosbag2: false,
      tags: [],
      warmup_time_s: 5,
    },
    ...overrides,
  };
}

const inferenceStatus = (overrides = {}) => status({
  phase: TaskPhase.INFERENCING,
  task_info: { ...status().task_info, task_type: 'inference' },
  ...overrides,
});

async function mount() {
  const { result } = renderHook(() => useRosTopicSubscription());
  await act(async () => {
    await result.current.subscribeToTaskStatus();
    await result.current.subscribeHFStatus();
  });
  await act(async () => { await Promise.resolve(); });
  return { taskCb: callbackFor('/task/status'), hfCb: callbackFor('/huggingface/status') };
}

function startSessionInRealStore() {
  realStore.dispatch(recordIntent({ kind: 'start', at: Date.now(), snapshot: {
    taskName: 'Würfel', userId: 'schule-A', robotType: 'omx_f', pushToHub: true, privateMode: true,
    numEpisodes: 5, episodeTime: 20, warmupTime: 5, resetTime: 5, fps: 30,
  } }));
}

beforeEach(() => {
  mockDispatch.mockClear();
  mockTopicSubscribe.mockClear();
  toast.mockClear();
  toast.success.mockClear();
  toast.error.mockClear();
  registerDataset.mockClear();
  realStore.dispatch(signedOut());
  realStore.dispatch(recordSessionDismiss());
  realStore.dispatch(moveToPage(PageType.HOME));
});

describe('record [WARNUNG] ticks', () => {
  it('become a warn notice and are still dispatched with error "" (on the Aufnahme page: no toast)', async () => {
    realStore.dispatch(moveToPage(PageType.RECORD));
    const { taskCb } = await mount();
    act(() => taskCb(status({ error: '[WARNUNG] Die Szenen-Kamera zeigt seit über 2 s dasselbe Bild.' })));
    const notices = dispatched('tasks/recordNoticeSet');
    expect(notices).toHaveLength(1);
    expect(notices[0].payload).toMatchObject({
      kind: 'warn', text: 'Die Szenen-Kamera zeigt seit über 2 s dasselbe Bild.',
    });
    const ticks = dispatched('tasks/setTaskStatus');
    expect(ticks).toHaveLength(1);
    expect(ticks[0].payload.error).toBe('');
    expect(ticks[0].payload.recordWarn).toBe('Die Szenen-Kamera zeigt seit über 2 s dasselbe Bild.');
    expect(toast).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('… and toast as a warning off the Aufnahme page', async () => {
    const { taskCb } = await mount();
    act(() => taskCb(status({ error: '[WARNUNG] Speicher knapp.' })));
    expect(toast).toHaveBeenCalledWith('Speicher knapp.', { icon: toastIcon('warning') });
    expect(toast.error).not.toHaveBeenCalled();
    expect(dispatched('tasks/setTaskStatus')).toHaveLength(1);
  });

  it('the terminating READY tick carrying the upload-blocked reason is processed too', async () => {
    realStore.dispatch(moveToPage(PageType.RECORD));
    const { taskCb } = await mount();
    act(() => taskCb(status({ phase: TaskPhase.READY, error: '[WARNUNG] Hochladen blockiert.' })));
    const ticks = dispatched('tasks/setTaskStatus');
    expect(ticks).toHaveLength(1);
    expect(ticks[0].payload).toMatchObject({ phase: TaskPhase.READY, running: false, recordWarn: 'Hochladen blockiert.' });
  });
});

describe('Inferenz [WARNUNG] ticks keep HEAD', () => {
  it('red toast, tick dropped, no notice', async () => {
    realStore.dispatch(moveToPage(PageType.INFERENCE));
    const { taskCb } = await mount();
    act(() => taskCb(inferenceStatus({ error: '[WARNUNG] Kamera "scene" zeigt dasselbe Bild.' })));
    expect(toast.error).toHaveBeenCalledWith('[WARNUNG] Kamera "scene" zeigt dasselbe Bild.');
    expect(dispatched('tasks/setTaskStatus')).toHaveLength(0);
    expect(dispatched('tasks/recordNoticeSet')).toHaveLength(0);
  });
});

describe('hard errors', () => {
  it('with a pending start on the Aufnahme page: an error notice, no toast, dropped', async () => {
    realStore.dispatch(moveToPage(PageType.RECORD));
    startSessionInRealStore();
    const { taskCb } = await mount();
    act(() => taskCb(status({ phase: TaskPhase.READY, error: 'Der Leader-Arm sendet keine Daten.' })));
    const notices = dispatched('tasks/recordNoticeSet');
    expect(notices).toHaveLength(1);
    expect(notices[0].payload).toMatchObject({ kind: 'error', text: 'Der Leader-Arm sendet keine Daten.' });
    expect(toast.error).not.toHaveBeenCalled();
    expect(dispatched('tasks/setTaskStatus')).toHaveLength(0);
  });

  it('with a session but off the page: notice AND the red toast', async () => {
    startSessionInRealStore();
    const { taskCb } = await mount();
    act(() => taskCb(status({ error: 'Die Kameras senden keine Bilder.' })));
    expect(dispatched('tasks/recordNoticeSet')).toHaveLength(1);
    expect(toast.error).toHaveBeenCalledWith('Die Kameras senden keine Bilder.');
  });

  it('without a session: HEAD (toast, dropped, no notice)', async () => {
    realStore.dispatch(moveToPage(PageType.RECORD));
    const { taskCb } = await mount();
    act(() => taskCb(status({ error: 'irgendwas' })));
    expect(toast.error).toHaveBeenCalledWith('irgendwas');
    expect(dispatched('tasks/recordNoticeSet')).toHaveLength(0);
    expect(dispatched('tasks/setTaskStatus')).toHaveLength(0);
  });
});

describe('the dispatched statusPayload', () => {
  it('carries the plan and the arrival times, never an error', async () => {
    const { taskCb } = await mount();
    const before = Date.now();
    act(() => taskCb(status()));
    const [tick] = dispatched('tasks/setTaskStatus');
    expect(tick.payload).toMatchObject({
      taskType: 'record',
      fps: 30,
      numEpisodes: 5,
      episodeTime: 20,
      warmupTime: 5,
      resetTime: 5,
      pushToHub: true,
      recordWarn: '',
      error: '',
      running: true,
    });
    expect(typeof tick.payload.receivedAt).toBe('number');
    expect(tick.payload.receivedWallMs).toBeGreaterThanOrEqual(before);
  });

  it('an idle tick has empty plan fields', async () => {
    const { taskCb } = await mount();
    act(() => taskCb(status({ phase: TaskPhase.READY, task_info: undefined })));
    const [tick] = dispatched('tasks/setTaskStatus');
    expect(tick.payload).toMatchObject({ taskType: '', fps: 0, numEpisodes: 0, pushToHub: false });
  });

  it('„Aufnahme gestartet!" toasts only off the Aufnahme page', async () => {
    realStore.dispatch(moveToPage(PageType.RECORD));
    const { taskCb } = await mount();
    act(() => taskCb(status({ phase: TaskPhase.WARMING_UP })));
    act(() => taskCb(status({ phase: TaskPhase.RECORDING })));
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('a collision payload carries its arrival time', async () => {
    const { taskCb } = await mount();
    act(() => taskCb(status({ phase: TaskPhase.COLLISION })));
    const [col] = dispatched('tasks/setCollision');
    expect(col.payload).toMatchObject({ active: true, stage: 'stopped' });
    expect(typeof col.payload.receivedAt).toBe('number');
    expect(typeof col.payload.receivedWallMs).toBe('number');
  });
});

describe('/huggingface/status and the cloud registration', () => {
  const hf = (overrides = {}) => ({
    status: 'Uploading',
    operation: 'upload',
    repo_id: 'schule-A/omx_f_Wuerfel',
    local_path: '',
    message: 'Wird hochgeladen',
    progress_current: 1,
    progress_total: 4,
    progress_percentage: 25,
    ...overrides,
  });

  it('on the Aufnahme page the finish card speaks for its own upload — until it is dismissed', async () => {
    realStore.dispatch(moveToPage(PageType.RECORD));
    startSessionInRealStore();
    const base = {
      taskType: 'record', taskName: 'Würfel', robotType: 'omx_f', numEpisodes: 5, episodeTime: 20,
      pushToHub: true, topicReceived: true,
    };
    realStore.dispatch(setTaskStatus({
      ...base, running: true, phase: TaskPhase.SAVING, currentEpisodeNumber: 1,
      receivedAt: 1, receivedWallMs: Date.now() - 10,
    }));
    realStore.dispatch(setTaskStatus({
      ...base, running: false, phase: TaskPhase.READY, currentEpisodeNumber: 1,
      receivedAt: 2, receivedWallMs: Date.now() - 5,
    }));
    expect(realStore.getState().tasks.recordSession.finish).toMatchObject({
      state: 'uploading', expectedRepoId: 'schule-A/omx_f_Wuerfel',
    });
    const { hfCb } = await mount();
    act(() => hfCb(hf({ repo_id: 'schule-A/omx_f_Wuerfel', status: 'Failed', message: 'Fehlgeschlagen.' })));
    expect(toast.error).not.toHaveBeenCalled();
    // another repo still toasts
    act(() => hfCb(hf({ repo_id: 'schule-A/omx_f_anders', status: 'Failed', message: 'Anderes.' })));
    expect(toast.error).toHaveBeenCalledWith('Anderes.');
    // after „Neue Aufnahme" nothing on the page shows it: toast again
    realStore.dispatch(recordSessionDismiss());
    act(() => hfCb(hf({ repo_id: 'schule-A/omx_f_Wuerfel', status: 'Failed', message: 'Fehlgeschlagen.' })));
    expect(toast.error).toHaveBeenCalledWith('Fehlgeschlagen.');
  });

  it('every upload status reaches the session tracker', async () => {
    const { hfCb } = await mount();
    act(() => hfCb(hf()));
    const [u] = dispatched('tasks/recordUploadStatus');
    expect(u.payload).toMatchObject({
      repoId: 'schule-A/omx_f_Wuerfel', status: 'Uploading', percentage: 25, message: 'Wird hochgeladen',
    });
    expect(typeof u.payload.at).toBe('number');
  });

  it('downloads do not', async () => {
    const { hfCb } = await mount();
    act(() => hfCb(hf({ operation: 'download', status: 'Downloading' })));
    expect(dispatched('tasks/recordUploadStatus')).toHaveLength(0);
  });

  it('a successful upload without a sign-in registers as skipped (and still toasts off the page)', async () => {
    const { hfCb } = await mount();
    act(() => hfCb(hf({ status: 'Success', message: 'Hochgeladen.' })));
    expect(toast.success).toHaveBeenCalledWith('Hochgeladen.');
    expect(dispatched('tasks/recordRegisterStatus').map((a) => a.payload.state)).toEqual(['pending', 'skipped']);
  });

  it('signed in: pending → done', async () => {
    realStore.dispatch(setSession({ access_token: 'jwt', user: { id: 'u1' } }));
    const { hfCb } = await mount();
    await act(async () => { hfCb(hf({ status: 'Success', message: 'Hochgeladen.' })); });
    await act(async () => { await Promise.resolve(); });
    expect(registerDataset).toHaveBeenCalledTimes(1);
    expect(dispatched('tasks/recordRegisterStatus').map((a) => a.payload.state)).toEqual(['pending', 'done']);
  });

  it('a 409 counts as done; a final failure as failed', async () => {
    realStore.dispatch(setSession({ access_token: 'jwt', user: { id: 'u1' } }));
    registerDataset.mockImplementationOnce(() => Promise.reject(Object.assign(new Error('x'), { status: 409 })));
    const { hfCb } = await mount();
    await act(async () => { hfCb(hf({ status: 'Success' })); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(dispatched('tasks/recordRegisterStatus').map((a) => a.payload.state)).toEqual(['pending', 'done']);

    mockDispatch.mockClear();
    registerDataset.mockImplementationOnce(() => Promise.reject(Object.assign(new Error('x'), { status: 422 })));
    await act(async () => { hfCb(hf({ status: 'Success' })); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(dispatched('tasks/recordRegisterStatus').map((a) => a.payload.state)).toEqual(['pending', 'failed']);
    expect(toast.error).toHaveBeenCalled(); // the German register warning stays
  });
});
