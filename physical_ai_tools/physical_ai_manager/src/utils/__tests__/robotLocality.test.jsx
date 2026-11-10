// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// VC-01 (verification round C): whether the robot is REMOTE is decided from
// the page itself, fail-closed. The Pi marker fails OPEN (to „not a Pi") on a
// network error or after 4 s; that must never make a Pi page look like the
// Windows GUI's loopback page.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { fetchPiMarker, getPiModeSync, PiModeProvider, usePiMode } from '../piMode';
import {
  isLoopbackPage, LOOPBACK_PAGE_HOSTS, robotIsRemote, setPageHostnameForTests,
} from '../robotLocality';

afterEach(() => {
  setPageHostnameForTests(null);
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('robotIsRemote', () => {
  it('the three loopback hosts the student nginx serves are the only local pages', () => {
    expect(LOOPBACK_PAGE_HOSTS).toEqual(['localhost', '127.0.0.1', '[::1]']);
    for (const host of LOOPBACK_PAGE_HOSTS) {
      setPageHostnameForTests(host);
      expect(isLoopbackPage()).toBe(true);
      expect(robotIsRemote()).toBe(false);
    }
  });

  it('every other host is remote, however close it looks', () => {
    for (const host of [
      '192.168.1.50', 'raspi.local', 'pi.schule.intern', '10.0.0.7', '[fe80::1]', '[::ffff:127.0.0.1]',
      'localhost.', 'localhost.localdomain', '127.0.0.2', '127.1', '0.0.0.0', 'LOCALHOST', '',
    ]) {
      setPageHostnameForTests(host);
      expect(`${host}: ${robotIsRemote()}`).toBe(`${host}: true`);
    }
  });

  it('jsdom\'s own page (http://localhost:3000) is local, the default for every other test', () => {
    expect(window.location.hostname).toBe('localhost');
    expect(robotIsRemote()).toBe(false);
  });

  it('a Pi page whose marker failed (network error) is still remote', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
    expect(await fetchPiMarker()).toBe(false);
    expect(getPiModeSync()).toBe(false);
    setPageHostnameForTests('192.168.1.50');
    expect(robotIsRemote()).toBe(true);
  });

  it('a Pi page whose marker timed out is still remote', async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => new Promise((resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      setTimeout(() => resolve({ ok: true, json: async () => ({ pi: true }) }), 5000);
    }));
    const marker = fetchPiMarker();
    await vi.advanceTimersByTimeAsync(4100);
    expect(await marker).toBe(false);
    setPageHostnameForTests('raspi.local');
    expect(robotIsRemote()).toBe(true);
  });
});

describe('PiModeProvider: the marker only ADDS Pi mode to a loopback page', () => {
  function Probe() {
    const { piMode, piModeResolved } = usePiMode();
    return <span data-testid="pi">{piModeResolved ? String(piMode) : 'pending'}</span>;
  }

  async function resolvedPiMode() {
    render(<PiModeProvider><Probe /></PiModeProvider>);
    await waitFor(() => expect(screen.getByTestId('pi').textContent).not.toBe('pending'));
    return screen.getByTestId('pi').textContent;
  }

  it('a Pi page at its LAN address is a Pi even when /pi-mode.json failed', async () => {
    setPageHostnameForTests('192.168.1.50');
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
    expect(await resolvedPiMode()).toBe('true');
    expect(getPiModeSync()).toBe(true);
  });

  it('the Windows GUI page (localhost) stays non-Pi when the marker says so', async () => {
    setPageHostnameForTests('localhost');
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
    expect(await resolvedPiMode()).toBe('false');
    expect(getPiModeSync()).toBe(false);
  });

  it('a kiosk browser on the Pi (localhost + the marker) is a Pi', async () => {
    setPageHostnameForTests('localhost');
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => (
      String(url).startsWith('/pi-mode.json')
        ? { ok: true, status: 200, json: async () => ({ pi: true }) }
        : { ok: false, status: 502, json: async () => ({}) }
    ));
    expect(await resolvedPiMode()).toBe('true');
  });
});
