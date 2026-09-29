// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// useSignalStatus subscribes to /edubotics/signal_status (std_msgs/String,
// JSON, schema v1), keeps the last good payload with its arrival time, and
// holds nothing while disabled — a rig fact read by one page, not Redux.

import { renderHook, act, waitFor } from '@testing-library/react';
import useSignalStatus from '../useSignalStatus';

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

const PAYLOAD = JSON.stringify({
  v: 1, seq: 1, uptime_s: 20, recording: false,
  sources: [{ kind: 'camera', name: 'scene', topic: '/scene/image_raw', hz: 30, age_s: 0.02 }],
  disk: { free_bytes: 5e10, start_floor_bytes: 3e9, critical_floor_bytes: 1e9 },
});

beforeEach(() => {
  mockTopics.length = 0;
});

describe('useSignalStatus', () => {
  it('subscribes with the throttled options and parses each payload', async () => {
    const { result } = renderHook(() => useSignalStatus({ enabled: true }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    const topic = mockTopics[0];
    expect(topic.opts).toMatchObject({
      name: '/edubotics/signal_status',
      messageType: 'std_msgs/msg/String',
      throttle_rate: 500,
      queue_length: 1,
    });
    expect(result.current).toEqual({ payload: null, receivedAt: null });
    act(() => topic.cb({ data: PAYLOAD }));
    expect(result.current.payload.sources[0].name).toBe('scene');
    expect(typeof result.current.receivedAt).toBe('number');
  });

  it('keeps the last good payload when a malformed one arrives', async () => {
    const { result } = renderHook(() => useSignalStatus({ enabled: true }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    act(() => mockTopics[0].cb({ data: PAYLOAD }));
    const good = result.current;
    act(() => mockTopics[0].cb({ data: '{kaputt' }));
    expect(result.current).toBe(good);
  });

  it('disabled: no subscription and no payload; disabling unsubscribes and forgets', async () => {
    const { result, rerender } = renderHook(({ enabled }) => useSignalStatus({ enabled }), {
      initialProps: { enabled: false },
    });
    await act(async () => { await Promise.resolve(); });
    expect(mockTopics).toHaveLength(0);
    rerender({ enabled: true });
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    act(() => mockTopics[0].cb({ data: PAYLOAD }));
    expect(result.current.payload).not.toBeNull();
    rerender({ enabled: false });
    expect(mockTopics[0].unsubscribed).toBe(true);
    expect(result.current).toEqual({ payload: null, receivedAt: null });
  });

  it('unsubscribes on unmount', async () => {
    const { unmount } = renderHook(() => useSignalStatus({ enabled: true }));
    await waitFor(() => expect(mockTopics).toHaveLength(1));
    unmount();
    expect(mockTopics[0].unsubscribed).toBe(true);
  });
});
