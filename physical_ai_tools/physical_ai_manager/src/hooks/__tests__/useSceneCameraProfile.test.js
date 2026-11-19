// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// useSceneCameraProfile reads the robot's own statement of its scene camera
// (decision D58): `/edubotics/scene_camera_profile`, std_msgs/String, JSON
// `{"v":1,"profile":"legacy"|"tower_v1"}`. The statement's profile, else null.
// Silence is UNKNOWN (null) for as long as it lasts: no timer turns it into
// „legacy". Nothing is stored anywhere.

import { renderHook, act, waitFor } from '@testing-library/react';
import useSceneCameraProfile, {
  SCENE_CAMERA_PROFILE_TOPIC,
  SCENE_CAMERA_PROFILES,
  parseSceneCameraStatement,
  sceneCameraProfileOf,
} from '../useSceneCameraProfile';

vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel({ ros: { rosbridgeUrl: 'ws://localhost/rosbridge' } }),
}));

vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ isConnected: true })) },
}));

const mockTopics = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.opts = opts;
      this.cb = null;
      this.unsubscribed = false;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => { this.unsubscribed = true; };
      mockTopics.push(this);
    },
  },
}));

const statement = (profile, v = 1) => ({ data: JSON.stringify({ v, profile }) });

beforeEach(() => {
  mockTopics.length = 0;
});

afterEach(() => {
  vi.useRealTimers();
});

describe('the statement', () => {
  it('names the robot topic and the two profiles', () => {
    expect(SCENE_CAMERA_PROFILE_TOPIC).toBe('/edubotics/scene_camera_profile');
    expect(SCENE_CAMERA_PROFILES).toEqual(['legacy', 'tower_v1']);
    expect(Object.isFrozen(SCENE_CAMERA_PROFILES)).toBe(true);
  });

  it('reads the profile of a v1 statement, else null', () => {
    expect(parseSceneCameraStatement('{"v":1,"profile":"tower_v1"}')).toBe('tower_v1');
    expect(parseSceneCameraStatement('{"v":1,"profile":"legacy"}')).toBe('legacy');
    for (const data of [
      '{"v":1,"profile":"tower_v2"}', '{"v":1,"profile":"Tower_V1"}', '{"v":1,"profile":null}', '{"v":1}',
      '{"v":2,"profile":"tower_v1"}', '{"profile":"tower_v1"}', '{"v":"1","profile":"tower_v1"}',
      '["tower_v1"]', '"tower_v1"', 'tower_v1', '{kaputt', '', null, undefined, 7, {},
    ]) {
      expect(parseSceneCameraStatement(data)).toBeNull();
    }
  });

  it('sceneCameraProfileOf keeps a known profile and nothing else', () => {
    expect(sceneCameraProfileOf('legacy')).toBe('legacy');
    expect(sceneCameraProfileOf('tower_v1')).toBe('tower_v1');
    for (const value of [undefined, null, '', 'tower', 'TOWER_V1', 1, {}, ['legacy'], 'constructor', '__proto__']) {
      expect(sceneCameraProfileOf(value)).toBeNull();
    }
  });
});

describe('useSceneCameraProfile', () => {
  it('subscribes to the robot topic and answers the statement', async () => {
    const { result } = renderHook(() => useSceneCameraProfile({ enabled: true }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    expect(mockTopics[0].opts).toMatchObject({
      name: '/edubotics/scene_camera_profile',
      messageType: 'std_msgs/msg/String',
      queue_length: 1,
    });
    expect(result.current).toBeNull();
    act(() => mockTopics[0].cb(statement('tower_v1')));
    expect(result.current).toBe('tower_v1');
    act(() => mockTopics[0].cb(statement('legacy')));
    expect(result.current).toBe('legacy');
  });

  it('a statement it cannot read is unknown', async () => {
    const { result } = renderHook(() => useSceneCameraProfile({ enabled: true }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    act(() => mockTopics[0].cb(statement('tower_v1')));
    act(() => mockTopics[0].cb(statement('tower_v2')));
    expect(result.current).toBeNull();
    act(() => mockTopics[0].cb({ data: '{kaputt' }));
    expect(result.current).toBeNull();
  });

  it('silence stays unknown: no timer turns it into legacy', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useSceneCameraProfile({ enabled: true }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mockTopics).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { vi.advanceTimersByTime(10 * 60 * 1000); });
    expect(result.current).toBeNull();
  });

  it('disabled: no subscription and unknown; disabling unsubscribes and forgets', async () => {
    const { result, rerender } = renderHook(({ enabled }) => useSceneCameraProfile({ enabled }), {
      initialProps: { enabled: false },
    });
    await act(async () => { await Promise.resolve(); });
    expect(mockTopics).toHaveLength(0);
    expect(result.current).toBeNull();
    rerender({ enabled: true });
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    act(() => mockTopics[0].cb(statement('tower_v1')));
    expect(result.current).toBe('tower_v1');
    rerender({ enabled: false });
    expect(mockTopics[0].unsubscribed).toBe(true);
    expect(result.current).toBeNull();
  });

  it('unmounting unsubscribes; a late message is ignored', async () => {
    const { unmount } = renderHook(() => useSceneCameraProfile({ enabled: true }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    const topic = mockTopics[0];
    unmount();
    expect(topic.unsubscribed).toBe(true);
    expect(() => topic.cb(statement('tower_v1'))).not.toThrow();
  });

  it('writes no browser storage', async () => {
    const before = [Object.keys(localStorage).length, Object.keys(sessionStorage).length];
    renderHook(() => useSceneCameraProfile({ enabled: true }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    act(() => mockTopics[0].cb(statement('tower_v1')));
    expect([Object.keys(localStorage).length, Object.keys(sessionStorage).length]).toEqual(before);
  });
});
