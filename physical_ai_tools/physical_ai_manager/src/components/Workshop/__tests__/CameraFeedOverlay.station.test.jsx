// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The station preview (station spec v6.13, §9.2 Preview): the existing
// overlay on /station_scene/image_raw/compressed through web_video_server,
// with the station controller's `source_ok` IN PLACE of the rosbridge
// liveness probe — a throttled subscription to the 1080p topic would push
// ~700 KB of base64 per second through the shared websocket. The scene
// camera keeps its probe.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import CameraFeedOverlay from '../CameraFeedOverlay';
import { STATION_CAMERA, STATION_TOPIC } from '../../../features/station/stationContract';

let mockState;
vi.mock('react-redux', () => ({ __esModule: true, useSelector: (sel) => sel(mockState) }));
vi.mock('../../../utils/piMode', async () => {
  const actual = await vi.importActual('../../../utils/piMode');
  return { ...actual, usePiMode: () => ({ piMode: false, piModeResolved: true }) };
});
vi.mock('../../../hooks/useRosServiceCaller', () => ({ __esModule: true, useRosServiceCaller: () => ({ callService: vi.fn() }) }));
vi.mock('../../../utils/cloudMode', () => ({ __esModule: true, isCloudOnlyMode: () => false }));

// A LIVE rosbridge, so a probe would subscribe if the code asked for one.
const mockTopics = vi.hoisted(() => ({ created: [] }));
vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { ros: { isConnected: true } },
}));
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function Topic(opts) {
      mockTopics.created.push(opts.name);
      this.subscribe = () => {};
      this.unsubscribe = () => {};
    },
  },
}));

beforeEach(() => {
  mockState = { ros: { rosHost: 'localhost' }, workshop: { detections: [] } };
  mockTopics.created = [];
});

describe('the station preview', () => {
  it('streams the station topic through web_video_server (suffix stripped)', async () => {
    expect(STATION_TOPIC).toBe('/station_scene/image_raw/compressed');
    expect(STATION_CAMERA).toBe('station_scene');
    render(<CameraFeedOverlay camera={STATION_CAMERA} sourceOk />);
    const img = await screen.findByRole('img');
    expect(img.getAttribute('src')).toContain('type=ros_compressed');
    expect(img.getAttribute('src')).toContain('topic=/station_scene/image_raw&');
  });

  it('opens no rosbridge subscription for the station topic', async () => {
    render(<CameraFeedOverlay camera={STATION_CAMERA} sourceOk />);
    await screen.findByRole('img');
    expect(mockTopics.created).toEqual([]);
  });

  it('shows the frozen badge from `source_ok`', async () => {
    const { rerender } = render(<CameraFeedOverlay camera={STATION_CAMERA} sourceOk />);
    await screen.findByRole('img');
    expect(screen.queryByText('Kamera eingefroren')).toBe(null);
    rerender(<CameraFeedOverlay camera={STATION_CAMERA} sourceOk={false} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('Kamera eingefroren')).toBeInTheDocument();
    rerender(<CameraFeedOverlay camera={STATION_CAMERA} sourceOk />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByText('Kamera eingefroren')).toBe(null);
  });

  it('the scene camera keeps its rosbridge probe', async () => {
    render(<CameraFeedOverlay camera="scene" />);
    await screen.findByRole('img');
    expect(mockTopics.created).toEqual(['/scene/image_raw/compressed']);
  });
});
