// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Pi-mode stream gating (audit fix 4): the MJPEG <img> URL must not be built
// off the DEFAULT piMode=false before the static /pi-mode.json marker resolves
// — on a Pi that would point the feed at the direct :8080 host port (filtered
// by school firewalls) instead of the same-origin /video nginx proxy. The
// stream effect waits for `piModeResolved`, then routes by the real flag.

import React from 'react';
import { render, screen } from '@testing-library/react';
import CameraFeedOverlay from '../CameraFeedOverlay';

// react-redux: selector-aware stub backed by a mutable module-level state.
let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

// piMode: controllable usePiMode snapshot; videoStreamBase stays REAL via
// importActual so the tests assert the actual /video-vs-:8080 URL routing.
let mockPiState;
vi.mock('../../../utils/piMode', async () => {
  const actual = await vi.importActual('../../../utils/piMode');
  return { ...actual, usePiMode: () => mockPiState };
});

vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ callService: vi.fn() }),
}));

vi.mock('../../../utils/cloudMode', () => ({
  __esModule: true,
  isCloudOnlyMode: () => false,
}));

// No live rosbridge → the F24 liveness subscription short-circuits.
vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { ros: null },
}));

beforeEach(() => {
  mockState = { ros: { rosHost: '192.168.0.5' }, workshop: { detections: [] } };
  mockPiState = { piMode: false, piModeResolved: true };
});

describe('CameraFeedOverlay — Pi-mode stream gating (piModeResolved)', () => {
  it('builds no stream <img> while the Pi-mode marker is still resolving', async () => {
    mockPiState = { piMode: false, piModeResolved: false };
    render(<CameraFeedOverlay camera="scene" />);
    // One microtask tick — the gated effect appends synchronously when armed,
    // so an <img> here would mean the piModeResolved gate regressed.
    await Promise.resolve();
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('routes the stream through the same-origin /video proxy once resolved as Pi', async () => {
    mockPiState = { piMode: true, piModeResolved: true };
    render(<CameraFeedOverlay camera="scene" />);
    const img = await screen.findByRole('img');
    expect(img.getAttribute('src')).toContain('/video/stream');
    expect(img.getAttribute('src')).not.toContain(':8080');
    // The /compressed suffix is stripped for web_video_server (it re-appends it).
    expect(img.getAttribute('src')).toContain('topic=/scene/image_raw&');
  });

  it('rides the SAME same-origin /video proxy on a resolved non-Pi rig', async () => {
    // Changed 2026-08-06: the Windows student rig no longer publishes :8080
    // (nor :9090) to the host at all — both robot transports go through the
    // manager's nginx proxy so the unauthenticated rosbridge stops being
    // reachable cross-origin from any page in the student's browser. A URL
    // naming :8080 here would now point at a closed port.
    render(<CameraFeedOverlay camera="scene" />);
    const img = await screen.findByRole('img');
    expect(img.getAttribute('src')).toContain('/video/stream');
    expect(img.getAttribute('src')).not.toContain(':8080');
  });
});
