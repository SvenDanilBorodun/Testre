/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// /sim/objects wire contract + the scene delay that keeps a grasp in step with
// the 3D twin's interpolated arm (SimScene passes INTERP_DELAY_MS).

import { renderHook, act, waitFor } from '@testing-library/react';
import useSimObjects from '../useSimObjects';

const mockTopicCtor = vi.fn();
const mockSubscribe = vi.fn();
const mockUnsubscribe = vi.fn();
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      mockTopicCtor(opts);
      this.subscribe = mockSubscribe;
      this.unsubscribe = mockUnsubscribe;
    },
  },
}));
vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ rosHandle: true })) },
}));

const URL = 'ws://student-pc/rosbridge';
const payload = (held, x = 0.2, epoch = 3) => ({
  data: JSON.stringify({
    epoch,
    held,
    objects: [{ key: 0, type: 'wuerfel', tag_id: 20, x, y: 0.0, yaw: 0.0 }],
  }),
});

beforeEach(() => {
  mockTopicCtor.mockClear();
  mockSubscribe.mockClear();
  mockUnsubscribe.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

async function subscribed(opts) {
  const { result, unmount } = renderHook(() => useSimObjects(URL, true, opts));
  await waitFor(() => expect(mockSubscribe).toHaveBeenCalledTimes(1));
  return { result, unmount, onMsg: mockSubscribe.mock.calls[0][0] };
}

test('subscribes to /sim/objects as std_msgs/String with a 20 ms backstop throttle', async () => {
  await subscribed();
  const opts = mockTopicCtor.mock.calls[0][0];
  expect(opts.name).toBe('/sim/objects');
  expect(opts.messageType).toBe('std_msgs/msg/String');
  expect(opts.throttle_rate).toBe(20);
  expect(opts.queue_length).toBe(1);
});

test('without a delay a scene is delivered at once (the pre-delay behaviour)', async () => {
  const { result, onMsg } = await subscribed();
  act(() => onMsg(payload(0)));
  expect(result.current).toEqual({
    epoch: 3,
    held: 0,
    objects: [{ key: 0, type: 'wuerfel', tag_id: 20, x: 0.2, y: 0.0, yaw: 0.0 }],
  });
});

test('with a delay every scene of a run lands exactly that much later, in order', async () => {
  const { result, onMsg } = await subscribed({ delayMs: 100 });
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  act(() => onMsg(payload(null, 0.15)));     // the run's start scene: at once
  expect(result.current.held).toBeNull();
  act(() => onMsg(payload(0, 0.20)));        // capture
  act(() => { vi.advanceTimersByTime(40); });
  act(() => onMsg(payload(null, 0.25)));     // release 40 ms later
  expect(result.current.objects[0].x).toBe(0.15);
  act(() => { vi.advanceTimersByTime(59); });
  expect(result.current.held).toBeNull();
  act(() => { vi.advanceTimersByTime(1); });   // t = 100: the capture
  expect(result.current.held).toBe(0);
  act(() => { vi.advanceTimersByTime(40); });  // t = 140: the release
  expect(result.current.held).toBeNull();
  expect(result.current.objects[0].x).toBe(0.25);
});

test('a NEW EPOCH (a reset) lands at once and nothing older can land after it', async () => {
  // Delayed like the rest, a run's start scene arrived 100 ms after SimScene began
  // believing the server, which showed the PREVIOUS run's layout for that long.
  const { result, onMsg } = await subscribed({ delayMs: 100 });
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  act(() => onMsg(payload(null, 0.15, 3)));
  act(() => onMsg(payload(0, 0.21, 3)));      // still waiting out its delay …
  act(() => onMsg(payload(null, 0.15, 4)));   // … when the world is reset
  expect(result.current.epoch).toBe(4);       // at once
  act(() => { vi.advanceTimersByTime(500); });
  expect(result.current.epoch).toBe(4);       // the stale capture never landed
  expect(result.current.held).toBeNull();
});

test('a scene still waiting out its delay never lands after teardown', async () => {
  const { result, onMsg, unmount } = await subscribed({ delayMs: 100 });
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  act(() => onMsg(payload(null, 0.15)));
  act(() => onMsg(payload(0)));
  expect(result.current.held).toBeNull();
  unmount();
  expect(vi.getTimerCount()).toBe(0);
  expect(mockUnsubscribe).toHaveBeenCalledTimes(1);
});

test('a malformed payload is ignored — delayed or not', async () => {
  const { result, onMsg } = await subscribed({ delayMs: 10 });
  act(() => onMsg({ data: 'not json' }));
  act(() => onMsg({ data: JSON.stringify({ held: 0 }) }));
  await new Promise((r) => setTimeout(r, 30));
  expect(result.current).toBeNull();
});

test('a carry moves only the HELD object — no re-render; capture and release always land', async () => {
  // Nothing reads a held object's coordinates while it is held (the twin parents
  // that mesh to the gripper). ~30 carry updates a second used to re-render
  // SimScene and the twin's object layer each time.
  const { result, onMsg } = await subscribed();
  const scene2 = (held, x0, x1) => ({
    data: JSON.stringify({
      epoch: 5,
      held,
      objects: [
        { key: 0, type: 'wuerfel', tag_id: 20, x: x0, y: 0.0, yaw: 0.0 },
        { key: 1, type: 'wuerfel', tag_id: 21, x: x1, y: 0.1, yaw: 0.0 },
      ],
    }),
  });
  act(() => onMsg(scene2(null, 0.20, 0.15)));
  act(() => onMsg(scene2(0, 0.20, 0.15)));          // capture: delivered
  const captured = result.current;
  expect(captured.held).toBe(0);
  act(() => onMsg(scene2(0, 0.21, 0.15)));          // carry
  act(() => onMsg(scene2(0, 0.23, 0.15)));          // carry
  expect(result.current).toBe(captured);            // same object: no re-render
  act(() => onMsg(scene2(null, 0.25, 0.15)));       // release: delivered, final x
  expect(result.current.held).toBeNull();
  expect(result.current.objects[0].x).toBe(0.25);
});

test('a change to an object that is NOT held is never skipped', async () => {
  const { result, onMsg } = await subscribed();
  const two = (held, x1) => ({
    data: JSON.stringify({
      epoch: 5,
      held,
      objects: [
        { key: 0, type: 'wuerfel', tag_id: 20, x: 0.2, y: 0.0, yaw: 0.0 },
        { key: 1, type: 'wuerfel', tag_id: 21, x: x1, y: 0.1, yaw: 0.0 },
      ],
    }),
  });
  act(() => onMsg(two(0, 0.15)));
  act(() => onMsg(two(0, 0.18)));
  expect(result.current.objects[1].x).toBe(0.18);
});
