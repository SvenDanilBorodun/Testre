/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Robot-type/capability delivery over /task/status, driven through the hook's
// REAL /task/status subscribe callback (same idiom as
// useRosTopicSubscription.toast.test.js: hoisted vi.mocks, a mocked roslib Topic
// that records subscribe(name -> cb), a dispatch spy). Locks:
//  - D10 parse-once: the same capabilities_json string across ticks yields ONE
//    stable object identity; a new string yields a new one.
//  - omit-when-empty: an empty robot_profile / capabilities_json never appears
//    in the setTaskStatus payload (so a bare/collision tick can't wipe them).
//  - D8 companion #1: an idle tick (empty task_info, not running) must NOT
//    dispatch setTaskInfo (would wipe the Record/Inference form every ~3 s).

import { renderHook, act } from '@testing-library/react';
import { useRosTopicSubscription } from '../useRosTopicSubscription';
import TaskPhase from '../../constants/taskPhases';

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

function taskStatusCallback() {
  const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === '/task/status');
  return calls.length ? calls[calls.length - 1][1] : null;
}

function dispatchedByType(type) {
  return mockDispatch.mock.calls
    .map(([a]) => a)
    .filter((a) => a && a.type === type);
}

// A full-ish TaskStatus wire message (idle READY by default).
function status(overrides = {}) {
  return {
    robot_type: 'omx_f',
    robot_profile: '',
    capabilities_json: '',
    phase: TaskPhase.READY,
    error: '',
    total_time: 0,
    proceed_time: 0,
    encoding_progress: 0,
    current_episode_number: 0,
    current_scenario_number: 0,
    current_task_instruction: '',
    used_storage_size: 0,
    total_storage_size: 0,
    used_cpu: 0,
    used_ram_size: 0,
    total_ram_size: 0,
    joint_dist_to_home: [],
    task_info: {
      task_name: '',
      task_type: '',
      task_instruction: [],
      policy_path: '',
      record_inference_mode: false,
      user_id: '',
      fps: 0,
      episode_time_s: 0,
      reset_time_s: 0,
      num_episodes: 0,
      push_to_hub: false,
      private_mode: false,
      use_optimized_save_mode: false,
      record_rosbag2: false,
      tags: [],
      warmup_time_s: 0,
    },
    ...overrides,
  };
}

const CAPS_FULL = JSON.stringify({
  recordable: true, editable: true, trainable: true,
  inferable: true, roboter_studio: true, has_leader: true,
});
const CAPS_FOLLOWER = JSON.stringify({
  recordable: false, editable: false, trainable: false,
  inferable: true, roboter_studio: true, has_leader: false,
});

async function mountAndGetCallback() {
  renderHook(() => useRosTopicSubscription());
  await act(async () => { await Promise.resolve(); });
  return taskStatusCallback();
}

beforeEach(() => {
  mockDispatch.mockClear();
  mockTopicSubscribe.mockClear();
});

describe('useRosTopicSubscription — capability delivery', () => {
  it('parses capabilities_json ONCE per distinct string → stable object identity across ticks (D10)', async () => {
    const cb = await mountAndGetCallback();
    expect(typeof cb).toBe('function');

    mockDispatch.mockClear();
    act(() => cb(status({ robot_profile: 'omx_full', capabilities_json: CAPS_FULL })));
    act(() => cb(status({ robot_profile: 'omx_full', capabilities_json: CAPS_FULL })));

    const statuses = dispatchedByType('tasks/setTaskStatus');
    expect(statuses.length).toBe(2);
    // Same string → the module cache hands back the SAME object reference.
    expect(statuses[0].payload.capabilities).toBe(statuses[1].payload.capabilities);
    expect(statuses[0].payload.capabilities).toEqual({
      recordable: true, editable: true, trainable: true,
      inferable: true, roboter_studio: true, has_leader: true,
    });
  });

  it('a DIFFERENT capabilities_json string yields a NEW object reference', async () => {
    const cb = await mountAndGetCallback();
    mockDispatch.mockClear();
    act(() => cb(status({ capabilities_json: CAPS_FULL })));
    act(() => cb(status({ capabilities_json: CAPS_FOLLOWER })));
    const statuses = dispatchedByType('tasks/setTaskStatus');
    expect(statuses[0].payload.capabilities).not.toBe(statuses[1].payload.capabilities);
    expect(statuses[1].payload.capabilities.recordable).toBe(false);
  });

  it('the parse cache is MODULE-scope: TWO live hook instances share one object identity (D10 v3.2)', async () => {
    // The hook is instantiated twice in production (StudentApp + WorkshopPage),
    // both subscribing /task/status. A per-instance useRef cache would hand the
    // two instances ALTERNATING object identities per tick on the Workshop page
    // — exactly the churn D10 exists to kill. Mount two instances and alternate
    // ticks between their callbacks: every dispatch must carry the SAME ref.
    renderHook(() => useRosTopicSubscription());
    renderHook(() => useRosTopicSubscription());
    await act(async () => { await Promise.resolve(); });
    const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === '/task/status');
    expect(calls.length).toBeGreaterThanOrEqual(2);
    const cbA = calls[calls.length - 2][1];
    const cbB = calls[calls.length - 1][1];

    mockDispatch.mockClear();
    act(() => cbA(status({ robot_profile: 'omx_full', capabilities_json: CAPS_FULL })));
    act(() => cbB(status({ robot_profile: 'omx_full', capabilities_json: CAPS_FULL })));
    act(() => cbA(status({ robot_profile: 'omx_full', capabilities_json: CAPS_FULL })));

    const statuses = dispatchedByType('tasks/setTaskStatus');
    expect(statuses.length).toBe(3);
    expect(statuses[0].payload.capabilities).toBe(statuses[1].payload.capabilities);
    expect(statuses[1].payload.capabilities).toBe(statuses[2].payload.capabilities);
  });

  it('omits robotProfile + capabilities from the payload when the wire fields are empty', async () => {
    const cb = await mountAndGetCallback();
    mockDispatch.mockClear();
    act(() => cb(status({ robot_profile: '', capabilities_json: '' })));
    const s = dispatchedByType('tasks/setTaskStatus')[0];
    expect(s).toBeTruthy();
    expect('robotProfile' in s.payload).toBe(false);
    expect('capabilities' in s.payload).toBe(false);
    // robotType still rides through unconditionally.
    expect(s.payload.robotType).toBe('omx_f');
  });

  it('a malformed capabilities_json is skipped (key omitted), never crashes', async () => {
    const cb = await mountAndGetCallback();
    mockDispatch.mockClear();
    expect(() => act(() => cb(status({ capabilities_json: '{not json' })))).not.toThrow();
    const s = dispatchedByType('tasks/setTaskStatus')[0];
    expect('capabilities' in s.payload).toBe(false);
  });
});

describe('useRosTopicSubscription — idle-tick taskInfo gate (D8 companion #1)', () => {
  it('an idle tick (empty task_info, not running) does NOT dispatch setTaskInfo', async () => {
    const cb = await mountAndGetCallback();
    mockDispatch.mockClear();
    act(() => cb(status()));
    expect(dispatchedByType('tasks/setTaskStatus').length).toBe(1); // status DOES flow
    expect(dispatchedByType('tasks/setTaskInfo').length).toBe(0);   // form is NOT touched
  });

  it('a real task tick (non-empty task_name) DOES dispatch setTaskInfo', async () => {
    const cb = await mountAndGetCallback();
    mockDispatch.mockClear();
    act(() => cb(status({
      phase: TaskPhase.WARMING_UP,
      task_info: { ...status().task_info, task_name: 'Wuerfel greifen', task_instruction: ['x'] },
    })));
    expect(dispatchedByType('tasks/setTaskInfo').length).toBe(1);
  });
});
