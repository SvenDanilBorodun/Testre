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
import { act, renderHook, waitFor } from '@testing-library/react';
import useRsBridgeStatus, { RS_STATUS_POLL_MS, RS_STATUS_TIMEOUT_MS } from '../useRsBridgeStatus';

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
    expect(result.current.probed).toBe(false);
  });
});

// Owner decision 2026-09-15 (B1): `probed` tells „not answered yet" from
// „answered: unavailable". Every way a first probe can SETTLE flips it.
describe('useRsBridgeStatus — probed', () => {
  test('false before the first answer, true after a bridge answer', async () => {
    let answer;
    global.fetch = vi.fn(() => new Promise((resolve) => { answer = resolve; }));
    const { result } = renderHook(() => useRsBridgeStatus());
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(result.current).toMatchObject({ probed: false, available: false, leaderOn: false });
    await act(async () => {
      answer({ ok: true, json: () => Promise.resolve({ follower_only: false }) });
    });
    await waitFor(() => expect(result.current.probed).toBe(true));
    expect(result.current).toMatchObject({ available: true, leaderOn: true });
  });

  test('an HTTP error is an answer: probed, unavailable', async () => {
    global.fetch = vi.fn(() => Promise.resolve({ ok: false, status: 502, json: () => Promise.resolve({}) }));
    const { result } = renderHook(() => useRsBridgeStatus());
    await waitFor(() => expect(result.current.probed).toBe(true));
    expect(result.current).toMatchObject({ available: false, leaderOn: false });
  });

  test('a network error is an answer: probed, unavailable', async () => {
    global.fetch = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
    const { result } = renderHook(() => useRsBridgeStatus());
    await waitFor(() => expect(result.current.probed).toBe(true));
    expect(result.current).toMatchObject({ available: false, leaderOn: false });
  });

  test('the RS_STATUS_TIMEOUT_MS abort is an answer: probed only once the timeout fired', async () => {
    vi.useFakeTimers();
    try {
      // A bridge that never answers: like real fetch, only the abort signal ends it.
      global.fetch = vi.fn((_url, { signal }) => new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      }));
      const { result } = renderHook(() => useRsBridgeStatus());
      await act(async () => { await Promise.resolve(); });
      expect(global.fetch).toHaveBeenCalledTimes(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(RS_STATUS_TIMEOUT_MS - 1); });
      expect(result.current.probed).toBe(false);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(result.current).toMatchObject({ probed: true, available: false, leaderOn: false });
    } finally {
      vi.useRealTimers();
    }
  });

  test('a later failed poll after an answer never returns to pending', async () => {
    vi.useFakeTimers();
    try {
      global.fetch = vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ follower_only: true }) }));
      const { result } = renderHook(() => useRsBridgeStatus());
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(result.current).toMatchObject({ probed: true, available: true, followerOnly: true });
      global.fetch = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
      await act(async () => { await vi.advanceTimersByTimeAsync(RS_STATUS_POLL_MS); });
      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(result.current).toMatchObject({ probed: true, available: false });
    } finally {
      vi.useRealTimers();
    }
  });

  test('no probe before Pi mode has resolved: still pending', async () => {
    respondWith({ follower_only: false });
    mockPiMode = { piMode: false, piModeResolved: false };
    const { result } = renderHook(() => useRsBridgeStatus());
    await new Promise((r) => { setTimeout(r, 20); });
    expect(result.current.probed).toBe(false);
  });
});
