// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// taskInfoWire (D28, R-5): the cloud start's TaskInfo is the page's own form,
// mapped EXACTLY like useRosServiceCaller.sendRecordCommand maps it, with four
// overrides. The robot echoes it on /task/status while the run lasts and
// useRosTopicSubscription adopts the echo into `tasks.taskInfo`: the Aufnahme
// form then differs only in what the overrides changed (policyPath, fps,
// recordInferenceMode) — never in the student's own fields.
//
// Both halves run for real: the hook that builds sendRecordCommand's request
// (its roslib call captured) and the hook that adopts the echo (its dispatch
// captured and applied to the REAL tasks reducer).

import { act, renderHook } from '@testing-library/react';
import { taskInfoWire } from '../taskInfoWire';
import { useRosServiceCaller } from '../../../hooks/useRosServiceCaller';
import { useRosTopicSubscription } from '../../../hooks/useRosTopicSubscription';
import PageType from '../../../constants/pageType';
import TaskPhase from '../../../constants/taskPhases';
import tasksReducer, { setTaskInfo } from '../../tasks/taskSlice';
import { selectRecordForm } from '../../tasks/recordSelectors';
import realStore from '../../../store/store';
import { moveToPage } from '../../ui/uiSlice';
import { signedOut } from '../../session/sessionActions';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn };
});

vi.mock('../../../services/datasetsApi', () => ({
  __esModule: true,
  registerDataset: vi.fn(() => Promise.resolve({})),
}));

vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ isConnected: true, on: () => {}, off: () => {} })) },
  isLocalRosbridgeUrl: () => true,
}));

const mockRequests = [];
const mockTopicSubscribe = vi.fn();
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Service: function ServiceMock(opts) {
      this.name = opts.name;
      this.callService = (req, ok) => {
        mockRequests.push({ name: this.name, req });
        ok({ success: true, message: '' });
      };
    },
    ServiceRequest: function ServiceRequestMock(req) { Object.assign(this, req); },
    Topic: function TopicMock(opts) {
      this.name = opts ? opts.name : undefined;
      this.subscribe = (cb) => { mockTopicSubscribe(this.name, cb); };
      this.unsubscribe = () => {};
    },
  },
}));

const FORM = Object.freeze({
  taskName: 'Würfel',
  taskType: '',
  taskInstruction: ['  ', ' Greife den Würfel. ', 'zweite Anweisung'],
  policyPath: '',
  recordInferenceMode: true,
  userId: 'schule-A',
  fps: 15,
  tags: ['omx_f', 'edubotics'],
  warmupTime: 4,
  episodeTime: 25,
  resetTime: 6,
  numEpisodes: 7,
  pushToHub: true,
  privateMode: true,
  useOptimizedSave: false,
  recordRosBag2: true,
});

const OVERRIDES = Object.freeze({
  task_type: 'inference',
  policy_path: 'EduBotics-Solutions/m',
  fps: 30,
  record_inference_mode: false,
});

beforeEach(() => {
  mockRequests.length = 0;
  mockDispatch.mockClear();
  mockTopicSubscribe.mockClear();
});

describe('taskInfoWire', () => {
  it('maps the form exactly like sendRecordCommand on the Inferenz page', async () => {
    mockState = {
      tasks: { taskInfo: FORM },
      training: { trainingInfo: {} },
      editDataset: {},
      ui: { currentPage: PageType.INFERENCE },
      ros: { rosbridgeUrl: 'ws://localhost/rosbridge' },
    };
    const { result } = renderHook(() => useRosServiceCaller());
    await result.current.sendRecordCommand('start_inference');
    const sent = mockRequests[0].req.task_info;
    expect(taskInfoWire(FORM, { task_type: 'inference' })).toEqual(sent);
  });

  it('applies the overrides last, and only them', () => {
    const wire = taskInfoWire(FORM, OVERRIDES);
    const plain = taskInfoWire(FORM);
    expect(wire).toEqual({ ...plain, ...OVERRIDES });
    expect(wire.task_instruction).toEqual([' Greife den Würfel. ', 'zweite Anweisung']);
    expect(Object.keys(wire)).toEqual([
      'task_name', 'task_type', 'user_id', 'task_instruction', 'policy_path', 'record_inference_mode', 'fps',
      'tags', 'warmup_time_s', 'episode_time_s', 'reset_time_s', 'num_episodes', 'push_to_hub', 'private_mode',
      'use_optimized_save_mode', 'record_rosbag2',
    ]);
  });

  it('is total on an empty or missing form', () => {
    expect(taskInfoWire(undefined, OVERRIDES)).toMatchObject({ task_name: '', user_id: '', tags: [], ...OVERRIDES });
    expect(taskInfoWire({ taskInstruction: 'x' }).task_instruction).toEqual([]);
  });
});

describe('R-5: the status echo changes only the overridden fields of the form', () => {
  it('after start → echo → adopt, selectRecordForm differs only in fps', async () => {
    // The Aufnahme form as a student left it (no blank instruction: the form
    // never holds one after the page's own validation).
    const form = { ...FORM, taskInstruction: ['Greife den Würfel.'] };
    let state = tasksReducer(undefined, { type: '@@init' });
    state = tasksReducer(state, setTaskInfo(form));
    const before = selectRecordForm({ tasks: state });

    realStore.dispatch(signedOut());
    realStore.dispatch(moveToPage(PageType.INFERENCE));
    mockState = { ros: { rosbridgeUrl: 'ws://localhost/rosbridge' } };
    const { result } = renderHook(() => useRosTopicSubscription());
    await act(async () => { await result.current.subscribeToTaskStatus(); });
    await act(async () => { await Promise.resolve(); });
    const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === '/task/status');
    const taskCb = calls[calls.length - 1][1];

    const echo = taskInfoWire(form, OVERRIDES);
    act(() => taskCb({
      robot_type: 'omx_f', robot_profile: '', capabilities_json: '', phase: TaskPhase.INFERENCING, error: '',
      total_time: 0, proceed_time: 0, encoding_progress: 0, current_episode_number: 0, current_scenario_number: 0,
      current_task_instruction: '', used_storage_size: 0, total_storage_size: 0, used_cpu: 0, used_ram_size: 0,
      total_ram_size: 0, joint_dist_to_home: [], task_info: echo,
    }));
    const adopted = mockDispatch.mock.calls.map(([a]) => a).filter((a) => a && a.type === setTaskInfo.type);
    expect(adopted.length).toBeGreaterThan(0);
    for (const action of adopted) state = tasksReducer(state, action);
    const after = selectRecordForm({ tasks: state });

    const changed = Object.keys(before).filter((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]));
    expect(changed).toEqual(['fps']);
    expect(state.taskInfo.policyPath).toBe('EduBotics-Solutions/m');
    expect(state.taskInfo.recordInferenceMode).toBe(false);
    expect(after.fps).toBe(30);
  });
});
