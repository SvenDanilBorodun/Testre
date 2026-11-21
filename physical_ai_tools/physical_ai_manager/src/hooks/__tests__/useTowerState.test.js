// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The V1's camera-tower verdicts as the robot states them (spec 40 WP-HINT,
// 52 §5.1): /edubotics/tower_state from the driver (WP-T1) and
// /edubotics/home_glide_state from the server (WP-12). The page never computes
// the rule; it reads these, and an unreadable message or silence is null —
// today's UI. The names and vocabularies are read out of the driver's and the
// server's own source, so a rename on either side fails here.

import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { configureStore, createSlice } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';

import useTowerState, {
  FALL_BACK, FALL_FORWARD, FALL_UNKNOWN, GLIDE_CLEAR, GLIDE_REFUSED, GLIDE_UNJUDGEABLE,
  HOME_GLIDE_STATE_TOPIC, TOWER_ARM_PROFILE, TOWER_FALLS, TOWER_GLIDES,
  TOWER_STATE_TOPIC, parseHomeGlideState, parseTowerState, stopNeedsTowerWarning,
  useHomeGlideState, useOptionalRosbridgeUrl,
} from '../useTowerState';

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

const REPO = path.resolve(__dirname, '../../../../..');
const read = (...parts) => fs.readFileSync(path.join(REPO, ...parts), 'utf8');
const URL = 'ws://localhost/rosbridge';
const msg = (obj) => ({ data: JSON.stringify(obj) });

beforeEach(() => {
  mockTopics.length = 0;
});

describe('the contract with the robot', () => {
  it('names the driver\'s topic, verdicts and falls (feetech_arm_node.py, feetech_geometry.py)', () => {
    const driver = read('robotis_ai_setup', 'docker', 'open_manipulator', 'feetech_arm_node.py');
    const geometry = read('robotis_ai_setup', 'docker', 'open_manipulator', 'feetech_geometry.py');
    expect(driver).toContain(`TOWER_STATE_TOPIC = '${TOWER_STATE_TOPIC}'`);
    expect(driver).toContain(`ARM_PROFILE = '${TOWER_ARM_PROFILE}'`);
    expect(geometry).toContain(`TOWER_CLEAR = '${GLIDE_CLEAR}'`);
    expect(geometry).toContain(`TOWER_REFUSED = '${GLIDE_REFUSED}'`);
    expect(geometry).toContain(`TOWER_UNJUDGEABLE = '${GLIDE_UNJUDGEABLE}'`);
    for (const fall of TOWER_FALLS) expect(geometry).toContain(`'${fall}'`);
    // The payload's keys, in tower_state_payload's own words.
    expect(driver).toContain("json.dumps({'v': 1, 'glide': glide, 'fall': fall, 'part': part,");
  });

  it('names the server\'s home-glide topic and payload (physical_ai_server.py)', () => {
    const server = read('physical_ai_tools', 'physical_ai_server', 'physical_ai_server', 'physical_ai_server.py');
    expect(server).toContain(`_HOME_GLIDE_STATE_TOPIC = '${HOME_GLIDE_STATE_TOPIC}'`);
    expect(server).toContain("json.dumps({'v': 1, 'glide': judged[1], 'gen': self._home_glide_gen})");
    expect(server).toContain("camera.profile() != 'tower_v1'");
  });

  it('the vocabularies are frozen and complete', () => {
    expect(TOWER_GLIDES).toEqual(['clear', 'refused', 'unjudgeable']);
    expect(TOWER_FALLS).toEqual(['forward', 'back', 'unknown']);
    expect(Object.isFrozen(TOWER_GLIDES)).toBe(true);
    expect(Object.isFrozen(TOWER_FALLS)).toBe(true);
  });
});

describe('parseTowerState', () => {
  it('reads a driver message', () => {
    expect(parseTowerState(JSON.stringify({ v: 1, glide: 'refused', fall: 'back', part: 'head', gen: 7 })))
      .toEqual({ glide: 'refused', fall: 'back', part: 'head', gen: 7 });
    expect(parseTowerState(JSON.stringify({ v: 1, glide: 'clear', fall: 'forward', part: null, gen: 8 })))
      .toEqual({ glide: 'clear', fall: 'forward', part: null, gen: 8 });
  });

  it('a fall it does not know is unknown (the stop asks first)', () => {
    for (const fall of [undefined, null, 'sideways', 'Forward', 7]) {
      expect(parseTowerState(JSON.stringify({ v: 1, glide: 'clear', fall, gen: 1 })).fall).toBe(FALL_UNKNOWN);
    }
  });

  it('anything else is no verdict', () => {
    for (const data of [
      '{"v":2,"glide":"refused","fall":"back"}', '{"glide":"refused","fall":"back"}',
      '{"v":"1","glide":"refused"}', '{"v":1,"glide":"gesperrt","fall":"back"}', '{"v":1}',
      '[1]', '"refused"', 'refused', '{kaputt', '', null, undefined, 7, {},
    ]) {
      expect(parseTowerState(data)).toBeNull();
    }
  });

  it('a part or gen of the wrong shape is dropped, not trusted', () => {
    expect(parseTowerState(JSON.stringify({ v: 1, glide: 'refused', fall: 'back', part: 7, gen: 1.5 })))
      .toEqual({ glide: 'refused', fall: 'back', part: null, gen: null });
  });
});

describe('parseHomeGlideState', () => {
  it('reads a server message, and nothing else', () => {
    expect(parseHomeGlideState(JSON.stringify({ v: 1, glide: 'refused', gen: 3 }))).toEqual({ glide: 'refused', gen: 3 });
    expect(parseHomeGlideState(JSON.stringify({ v: 1, glide: 'clear', gen: 4 }))).toEqual({ glide: 'clear', gen: 4 });
    expect(parseHomeGlideState('{"v":1,"glide":"nope"}')).toBeNull();
    expect(parseHomeGlideState('{kaputt')).toBeNull();
  });
});

describe('stopNeedsTowerWarning', () => {
  it('asks unless the limp arm falls forward; no verdict asks too', () => {
    expect(stopNeedsTowerWarning({ fall: FALL_FORWARD })).toBe(false);
    expect(stopNeedsTowerWarning({ fall: FALL_BACK })).toBe(true);
    expect(stopNeedsTowerWarning({ fall: FALL_UNKNOWN })).toBe(true);
    expect(stopNeedsTowerWarning(null)).toBe(true);
  });
});

describe('useTowerState / useHomeGlideState', () => {
  it('subscribes to the driver topic and answers its verdict', async () => {
    const { result } = renderHook(() => useTowerState({ enabled: true, rosbridgeUrl: URL }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    expect(mockTopics[0].opts).toMatchObject({
      name: '/edubotics/tower_state', messageType: 'std_msgs/msg/String', queue_length: 1,
    });
    // Every 2 Hz message gets through.
    expect(mockTopics[0].opts.throttle_rate).toBeLessThan(500);
    expect(result.current).toBeNull();
    act(() => mockTopics[0].cb(msg({ v: 1, glide: 'refused', fall: 'back', part: 'head', gen: 1 })));
    expect(result.current).toEqual({ glide: 'refused', fall: 'back', part: 'head', gen: 1 });
    act(() => mockTopics[0].cb(msg({ v: 1, glide: 'clear', fall: 'forward', part: null, gen: 2 })));
    expect(result.current.glide).toBe('clear');
    act(() => mockTopics[0].cb({ data: '{kaputt' }));
    expect(result.current).toBeNull();
  });

  it('useHomeGlideState subscribes to the server topic', async () => {
    const { result } = renderHook(() => useHomeGlideState({ enabled: true, rosbridgeUrl: URL }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    expect(mockTopics[0].opts.name).toBe('/edubotics/home_glide_state');
    act(() => mockTopics[0].cb(msg({ v: 1, glide: 'refused', gen: 1 })));
    expect(result.current).toEqual({ glide: 'refused', gen: 1 });
  });

  it('disabled, or no URL: no subscription and no verdict', async () => {
    renderHook(() => useTowerState({ enabled: false, rosbridgeUrl: URL }));
    renderHook(() => useTowerState({ enabled: true, rosbridgeUrl: '' }));
    renderHook(() => useTowerState());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mockTopics).toHaveLength(0);
  });

  it('disabling unsubscribes and forgets the verdict', async () => {
    const { result, rerender } = renderHook(({ enabled }) => useTowerState({ enabled, rosbridgeUrl: URL }), {
      initialProps: { enabled: true },
    });
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    act(() => mockTopics[0].cb(msg({ v: 1, glide: 'refused', fall: 'back', part: 'head', gen: 1 })));
    expect(result.current.glide).toBe('refused');
    rerender({ enabled: false });
    expect(mockTopics[0].unsubscribed).toBe(true);
    expect(result.current).toBeNull();
  });

  it('unmounting unsubscribes', async () => {
    const { unmount } = renderHook(() => useTowerState({ enabled: true, rosbridgeUrl: URL }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    unmount();
    expect(mockTopics[0].unsubscribed).toBe(true);
  });

  it('a silent topic stays null: no timer turns silence into a verdict', async () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useTowerState({ enabled: true, rosbridgeUrl: URL }));
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      expect(mockTopics).toHaveLength(1);
      expect(vi.getTimerCount()).toBe(0);
      await act(async () => { vi.advanceTimersByTime(10 * 60 * 1000); });
      expect(result.current).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('useOptionalRosbridgeUrl', () => {
  it('is empty without a Redux Provider', () => {
    const { result } = renderHook(() => useOptionalRosbridgeUrl());
    expect(result.current).toBe('');
  });

  it('reads and follows the store\'s rosbridge URL', () => {
    const ros = createSlice({
      name: 'ros',
      initialState: { rosbridgeUrl: '' },
      reducers: { setUrl: (state, action) => { state.rosbridgeUrl = action.payload; } },
    });
    const store = configureStore({ reducer: { ros: ros.reducer } });
    const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
    const { result } = renderHook(() => useOptionalRosbridgeUrl(), { wrapper });
    expect(result.current).toBe('');
    act(() => { store.dispatch(ros.actions.setUrl(URL)); });
    expect(result.current).toBe(URL);
  });

  it('a store without a ros slice is empty too', () => {
    const store = configureStore({ reducer: { other: (s = {}) => s } });
    const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
    const { result } = renderHook(() => useOptionalRosbridgeUrl(), { wrapper });
    expect(result.current).toBe('');
  });
});
