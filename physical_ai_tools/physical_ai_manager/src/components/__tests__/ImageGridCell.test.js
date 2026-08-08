// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// H1 wiring tests: during a classroom-Jetson session the camera cell must
// render from the CompressedImage topic over the JWT-proxied rosbridge
// (web_video_server :8080 is loopback-only on the Jetson). This can't be
// validated against a real Jetson from CI, so we lock the wiring: the
// `/compressed` topic suffix, the messageType, the throttle/queue, and the
// base64 -> data: URL frame plumbing — plus that the local (non-Jetson)
// path still uses web_video_server.

import React from 'react';
import { render, screen, waitFor, act } from '@testing-library/react';
// Safe to import the component here even though the vi.mock() calls sit
// below: Vitest HOISTS every vi.mock() above all imports (like babel-jest's
// jest.mock), so the component always resolves the mocked react-redux/
// rosConnectionManager/roslib modules regardless of source order. The
// `mock*`-prefixed variables referenced inside the factories are exempt from
// the hoist-time "cannot access before initialization" guard (same naming
// convention Jest used).
import ImageGridCell from '../ImageGridCell';

// react-redux: a selector-aware stub backed by a mutable module-level state.
let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

// piMode: controllable usePiMode snapshot (defaults to a resolved non-Pi rig,
// matching the provider-less DEFAULT_CONTEXT the pre-mock tests relied on).
// videoStreamBase & friends stay REAL via importActual — the tests below assert
// the actual /video-vs-:8080 URL routing.
let mockPiState;
vi.mock('../../utils/piMode', async () => {
  const actual = await vi.importActual('../../utils/piMode');
  return { ...actual, usePiMode: () => mockPiState };
});

// rosbridge connection manager: resolve a dummy ros handle.
vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ rosHandle: true })) },
}));

// roslib Topic: capture constructor opts + the subscribe callback.
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

const noop = () => {};

function renderCell(topic = '/gripper/image_raw') {
  return render(
    <ImageGridCell topic={topic} idx={1} isActive onClose={noop} onPlusClick={noop} />
  );
}

beforeEach(() => {
  mockTopicCtor.mockClear();
  mockSubscribe.mockClear();
  mockUnsubscribe.mockClear();
  mockPiState = { piMode: false, piModeResolved: true };
});

describe('ImageGridCell — Jetson camera transport (H1)', () => {
  test('jetson-connected: subscribes to <topic>/compressed and feeds base64 frames as a data URL', async () => {
    mockState = {
      ros: { rosHost: 'student-pc', rosbridgeUrl: 'ws://jetson-lan:9091' },
      jetson: { status: 'connected' },
    };
    renderCell('/gripper/image_raw');

    await waitFor(() => expect(mockTopicCtor).toHaveBeenCalledTimes(1));
    const opts = mockTopicCtor.mock.calls[0][0];
    expect(opts.name).toBe('/gripper/image_raw/compressed');
    expect(opts.messageType).toBe('sensor_msgs/msg/CompressedImage');
    expect(opts.throttle_rate).toBe(100);
    expect(opts.queue_length).toBe(1);
    expect(mockSubscribe).toHaveBeenCalledTimes(1);

    // Deliver a frame; the cell's <img> src becomes a JPEG data URL.
    // getByRole('img') finds the imperatively-appended element — the
    // component sets img.alt = topic (non-empty), so role 'img' applies.
    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() => onMsg({ data: 'QUJD' }));
    const img = screen.getByRole('img');
    expect(img.getAttribute('src')).toBe('data:image/jpeg;base64,QUJD');
  });

  test('not jetson-connected: uses the same-origin /video proxy, no rosbridge subscription', async () => {
    // Changed 2026-08-06: web_video_server is no longer host-published on the
    // student rig — both robot transports ride the manager's nginx proxy, so
    // the unauthenticated rosbridge is no longer reachable cross-origin from
    // any page open in the student's browser. What stays load-bearing here is
    // the BRANCH: a non-Jetson cell must use an <img> stream (Origin-less, and
    // therefore deliberately ungated in nginx.conf) and must NOT open a
    // rosbridge topic subscription.
    mockState = {
      ros: { rosHost: '192.168.0.5', rosbridgeUrl: 'ws://192.168.0.5:9090' },
      jetson: { status: 'available' },
    };
    renderCell('/gripper/image_raw');

    // getByRole throws while the imperative <img> hasn't been appended yet,
    // which is exactly the retry condition waitFor needs.
    const img = await screen.findByRole('img');
    expect(img.getAttribute('src')).toContain('/video/stream');
    expect(img.getAttribute('src')).not.toContain(':8080');
    expect(img.getAttribute('src')).toContain('topic=/gripper/image_raw');
    expect(mockTopicCtor).not.toHaveBeenCalled();
  });
});

describe('ImageGridCell — Pi-mode stream gating (piModeResolved)', () => {
  test('builds no stream until the marker resolves, then rides the /video proxy on a Pi', async () => {
    mockState = {
      ros: { rosHost: '192.168.0.5', rosbridgeUrl: 'ws://192.168.0.5:9090' },
      jetson: { status: 'available' },
    };
    // Marker still resolving → the effect must NOT build a stream URL off the
    // default piMode=false (on a Pi that's the direct :8080 host port, not the
    // same-origin /video proxy — audit fix 4).
    mockPiState = { piMode: false, piModeResolved: false };
    const { rerender } = renderCell('/gripper/image_raw');
    // One microtask tick: the unresolved-marker path appends nothing, and the
    // idx=1 non-Jetson append is fully synchronous — so an <img> here would
    // mean the gate regressed. (Same bare-flush pattern as the PiUpdateGate
    // "never probes" test.)
    await Promise.resolve();
    expect(screen.queryByRole('img')).toBeNull();

    // Marker resolves to Pi → the stream rides the same-origin /video proxy.
    mockPiState = { piMode: true, piModeResolved: true };
    rerender(
      <ImageGridCell topic="/gripper/image_raw" idx={1} isActive onClose={noop} onPlusClick={noop} />
    );
    const img = await screen.findByRole('img');
    expect(img.getAttribute('src')).toContain('/video/stream');
    expect(img.getAttribute('src')).not.toContain(':8080');
  });
});
