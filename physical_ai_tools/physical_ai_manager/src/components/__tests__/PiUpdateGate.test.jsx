// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Orange Pi forced-update gate: the Pi's parity for the Windows GUI's
// non-closable startup update modal. These lock the load-bearing UX: the modal
// shows ONLY when the cloud advertises a newer release AND we're in Pi mode, and
// the „Ohne Update fortfahren" skip appears ONLY after a FAILED attempt.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import PiUpdateGate from '../PiUpdateGate';

// usePiMode — controllable snapshot.
let mockPi;
vi.mock('../../utils/piMode', () => ({
  __esModule: true,
  usePiMode: () => mockPi,
}));

function jsonRes(body, { ok = true, status = 200 } = {}) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
}

// Per-test knobs for the fetch dispatcher.
let checkPayload;
let statusPayload;

beforeEach(() => {
  mockPi = { piMode: true, piModeResolved: true };
  checkPayload = { update_available: true, latest_version: '2.13.0', current_version: '2.12.2' };
  statusPayload = { status: 'succeeded', message: 'Aktualisierung abgeschlossen.' };

  global.fetch = vi.fn((url, opts = {}) => {
    const u = String(url);
    if (u.includes('/update/check')) return jsonRes(checkPayload);
    if (u.includes('/update/status/')) return jsonRes(statusPayload);
    if (u.includes('/update') && (opts.method || 'GET') === 'POST') {
      return jsonRes({ ok: true, job_id: 'j1', status: 'running' }, { status: 202 });
    }
    return jsonRes({});
  });
});

describe('PiUpdateGate', () => {
  it('shows the forced modal when the cloud advertises a newer release', async () => {
    render(<PiUpdateGate />);
    expect(await screen.findByText('Ein Update ist verfügbar!')).toBeInTheDocument();
    // The advertised + current versions are shown (GUI parity).
    expect(screen.getByText('2.13.0')).toBeInTheDocument();
    expect(screen.getByText(/2\.12\.2/)).toBeInTheDocument();
    // Primary action present; skip hidden until a failure.
    expect(screen.getByRole('button', { name: 'Jetzt aktualisieren' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Ohne Update fortfahren' })).toBeNull();
  });

  it('renders nothing when no update is available', async () => {
    checkPayload = { update_available: false, latest_version: '', current_version: '2.12.2' };
    render(<PiUpdateGate />);
    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/update/check'),
        expect.anything()
      )
    );
    expect(screen.queryByText('Ein Update ist verfügbar!')).toBeNull();
  });

  it('renders nothing (and never probes) when not in Pi mode', async () => {
    mockPi = { piMode: false, piModeResolved: true };
    render(<PiUpdateGate />);
    // Flush any pending microtasks so a stray probe would have fired.
    await Promise.resolve();
    expect(screen.queryByText('Ein Update ist verfügbar!')).toBeNull();
    const probed = global.fetch.mock.calls.some((c) => String(c[0]).includes('/update/check'));
    expect(probed).toBe(false);
  });

  it('reveals „Ohne Update fortfahren" only after a failed attempt, then dismisses on skip', async () => {
    statusPayload = { status: 'failed', message: 'Aktualisierung fehlgeschlagen.' };
    render(<PiUpdateGate />);

    await screen.findByText('Ein Update ist verfügbar!');
    // Skip is hidden before any attempt.
    expect(screen.queryByRole('button', { name: 'Ohne Update fortfahren' })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: 'Jetzt aktualisieren' }));

    // The update job failed → skip button now appears (fail-gated).
    const skip = await screen.findByRole('button', { name: 'Ohne Update fortfahren' });
    expect(skip).toBeInTheDocument();
    // The POST used method POST against the same-origin proxy.
    const postCall = global.fetch.mock.calls.find(
      (c) => String(c[0]).includes('/api/system/update') && (c[1]?.method) === 'POST'
    );
    expect(postCall).toBeTruthy();

    // Skipping dismisses the modal.
    await userEvent.click(skip);
    await waitFor(() =>
      expect(screen.queryByText('Ein Update ist verfügbar!')).toBeNull()
    );
  });
});
