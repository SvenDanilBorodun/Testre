/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// The Roboter-Studio control-bridge probe, extracted from RunControls. The real
// PiModeProvider takes no value prop and fetches /pi-mode.json itself, so
// usePiMode is mocked with a mutable value; importActual keeps the real
// rsControlBase (the CameraFeedOverlay.destinationName.test idiom).
import { renderHook, waitFor } from '@testing-library/react';
import useRsBridgeStatus from '../useRsBridgeStatus';

let mockPiMode = { piMode: false, piModeResolved: true };
vi.mock('../../utils/piMode', async () => ({
  ...(await vi.importActual('../../utils/piMode')),
  usePiMode: () => mockPiMode,
}));

function respondWith(body) {
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve(body) }));
}

beforeEach(() => {
  mockPiMode = { piMode: false, piModeResolved: true };
});

describe('useRsBridgeStatus', () => {
  test('leader on: bridge present, not follower-only', async () => {
    respondWith({ follower_only: false });
    const { result } = renderHook(() => useRsBridgeStatus());
    await waitFor(() => expect(result.current.available).toBe(true));
    expect(result.current).toMatchObject({
      available: true, followerOnly: false, leaderOn: true, busy: false,
    });
    expect(result.current.hasLeader).toBeUndefined();
    expect(global.fetch.mock.calls[0][0]).toBe('http://localhost:8769/roboter-studio/status');
  });

  test('follower-only rig without a leader', async () => {
    respondWith({ follower_only: true, has_leader: false });
    const { result } = renderHook(() => useRsBridgeStatus());
    await waitFor(() => expect(result.current.available).toBe(true));
    expect(result.current).toMatchObject({
      available: true, followerOnly: true, leaderOn: false, hasLeader: false,
    });
  });

  test('a failed probe fails OPEN', async () => {
    global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
    const { result } = renderHook(() => useRsBridgeStatus());
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    await waitFor(() => expect(result.current).toMatchObject({
      available: false, followerOnly: false, leaderOn: false,
    }));
  });

  test('no probe before Pi mode has resolved', async () => {
    respondWith({ follower_only: false });
    mockPiMode = { piMode: false, piModeResolved: false };
    const { result } = renderHook(() => useRsBridgeStatus());
    await new Promise((r) => { setTimeout(r, 20); });
    const calls = global.fetch.mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.includes('/roboter-studio/status'))).toBe(false);
    expect(result.current.leaderOn).toBe(false);
  });

  test('Pi mode routes the probe to the same-origin system proxy', async () => {
    respondWith({ follower_only: true });
    mockPiMode = { piMode: true, piModeResolved: true };
    renderHook(() => useRsBridgeStatus());
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(global.fetch.mock.calls[0][0]).toBe('/api/system/roboter-studio/status');
  });

  test('enabled: false never probes', async () => {
    respondWith({ follower_only: false });
    const { result } = renderHook(() => useRsBridgeStatus({ enabled: false }));
    await new Promise((r) => { setTimeout(r, 20); });
    expect(global.fetch).not.toHaveBeenCalled();
    expect(result.current.leaderOn).toBe(false);
  });
});
