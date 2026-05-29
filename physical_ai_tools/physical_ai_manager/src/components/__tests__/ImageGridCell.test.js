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
import { render, waitFor, act } from '@testing-library/react';

// react-redux: a selector-aware stub backed by a mutable module-level state.
let mockState;
jest.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

// rosbridge connection manager: resolve a dummy ros handle.
jest.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: jest.fn(() => Promise.resolve({ rosHandle: true })) },
}));

// roslib Topic: capture constructor opts + the subscribe callback.
const mockTopicCtor = jest.fn();
const mockSubscribe = jest.fn();
const mockUnsubscribe = jest.fn();
jest.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      mockTopicCtor(opts);
      this.subscribe = mockSubscribe;
      this.unsubscribe = mockUnsubscribe;
    },
  },
}));

import ImageGridCell from '../ImageGridCell';

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
});

describe('ImageGridCell — Jetson camera transport (H1)', () => {
  test('jetson-connected: subscribes to <topic>/compressed and feeds base64 frames as a data URL', async () => {
    mockState = {
      ros: { rosHost: 'student-pc', rosbridgeUrl: 'ws://jetson-lan:9091' },
      jetson: { status: 'connected' },
    };
    const { container } = renderCell('/gripper/image_raw');

    await waitFor(() => expect(mockTopicCtor).toHaveBeenCalledTimes(1));
    const opts = mockTopicCtor.mock.calls[0][0];
    expect(opts.name).toBe('/gripper/image_raw/compressed');
    expect(opts.messageType).toBe('sensor_msgs/msg/CompressedImage');
    expect(opts.throttle_rate).toBe(100);
    expect(opts.queue_length).toBe(1);
    expect(mockSubscribe).toHaveBeenCalledTimes(1);

    // Deliver a frame; the cell's <img> src becomes a JPEG data URL.
    const onMsg = mockSubscribe.mock.calls[0][0];
    act(() => onMsg({ data: 'QUJD' }));
    const img = container.querySelector('img');
    expect(img).not.toBeNull();
    expect(img.getAttribute('src')).toBe('data:image/jpeg;base64,QUJD');
  });

  test('not jetson-connected: uses web_video_server :8080, no rosbridge subscription', async () => {
    mockState = {
      ros: { rosHost: '192.168.0.5', rosbridgeUrl: 'ws://192.168.0.5:9090' },
      jetson: { status: 'available' },
    };
    const { container } = renderCell('/gripper/image_raw');

    await waitFor(() => {
      const img = container.querySelector('img');
      expect(img).not.toBeNull();
    });
    const img = container.querySelector('img');
    expect(img.getAttribute('src')).toContain('http://192.168.0.5:8080/stream');
    expect(img.getAttribute('src')).toContain('topic=/gripper/image_raw');
    expect(mockTopicCtor).not.toHaveBeenCalled();
  });
});
