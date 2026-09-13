/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// The readiness overlay's „settled" test is per 150 ms step, whatever rate the
// samples ARRIVE at. rosbridge serves one rate per topic per client — the
// fastest any subscriber asked for — so with a 3D twin open (~30 ms) this
// component's /joint_states callback runs five times as often as the 150 ms it
// requests. Counted per message, a re-home's slow tail (< 0.33 rad/s) read as
// „still" after 120 ms and the overlay cleared on a moving arm.

import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import LeaderToggle from '../LeaderToggle';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

vi.mock('react-hot-toast', () => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return { __esModule: true, default: t };
});

vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { ros: { isConnected: true } },
}));

// Topic mock: capture each subscription's callback by topic name.
const mockTopics = vi.hoisted(() => ({}));
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function Topic(opts) {
      this.subscribe = (cb) => { mockTopics[opts.name] = { cb, opts }; };
      this.unsubscribe = () => { delete mockTopics[opts.name]; };
    },
  },
}));

const PREPARING = /Roboter Studio wird vorbereitet/;
const T0 = new Date('2026-09-11T12:00:00Z').getTime();

beforeEach(() => {
  mockState = { tasks: { taskStatus: { phase: 0, capabilities: { has_leader: true } } } };
  Object.keys(mockTopics).forEach((k) => delete mockTopics[k]);
  global.fetch = vi.fn((url, opts) => {
    const body = opts && opts.method === 'POST'
      ? { ok: true, message: 'Roboter Studio bereit.' }
      : { follower_only: false, busy: false };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  });
});

afterEach(() => {
  vi.useRealTimers();
});

async function flush() {
  for (let i = 0; i < 10; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await Promise.resolve(); });
  }
}

// One delivery, outside any loop (a closure declared in the loop would capture
// the loop's variables — eslint's no-loop-func).
const deliver = (topic, payload) => {
  const sub = mockTopics[topic];
  if (sub) act(() => sub.cb(payload));
};
const tick = (ms) => act(() => { vi.advanceTimersByTime(ms); });

// Stream `seconds` of 30 ms /joint_states samples in which joint1 moves at
// `speed` rad/s, with a scene-camera frame every 90 ms, ticking the readiness
// interval as fake time passes. Returns the joint1 value it ended at.
function stream(seconds, speed, from) {
  let j1 = from;
  const steps = Math.round((seconds * 1000) / 30);
  for (let i = 0; i < steps; i += 1) {
    tick(30);
    j1 += speed * 0.03;
    deliver('/joint_states', { position: [j1, -1.0, 1.0, 0.0, 0.0, 0.8] });
    if (i % 3 === 0) deliver('/scene/image_raw/compressed', {});
  }
  return j1;
}

test('a slow re-home tail streamed at the twin\'s 30 ms rate does NOT clear the overlay', async () => {
  render(<LeaderToggle isActive />);
  const button = await screen.findByText(/Leader abschalten/);
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
  vi.setSystemTime(T0);
  fireEvent.click(button);
  await flush();
  expect(screen.getByText(PREPARING)).toBeInTheDocument();
  expect(mockTopics['/joint_states'].opts.throttle_rate).toBe(150);

  // The re-home: 2 s of brisk motion, then 4 s of slow tail at 0.1 rad/s —
  // 3 mrad per 30 ms message (under the 0.01 rad step), 15 mrad per 150 ms.
  let j1 = stream(2, 0.6, 0.0);
  j1 = stream(4, 0.1, j1);
  // 6 s in: past PREP_MIN_MS, camera + joints live — and still moving.
  expect(screen.getByText(PREPARING)).toBeInTheDocument();

  // The arm really stops: four still 150 ms steps later the overlay clears.
  stream(1.5, 0.0, j1);
  expect(screen.queryByText(PREPARING)).not.toBeInTheDocument();
});

test('an arm that is already still settles on the same 600 ms, however fast samples arrive', async () => {
  render(<LeaderToggle isActive />);
  const button = await screen.findByText(/Leader abschalten/);
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
  vi.setSystemTime(T0);
  fireEvent.click(button);
  await flush();
  // Seen moving once (the re-home), then still.
  const j1 = stream(1, 0.6, 0.0);
  stream(3.5, 0.0, j1);                     // reaches PREP_MIN_MS (4 s)
  expect(screen.queryByText(PREPARING)).not.toBeInTheDocument();
});
