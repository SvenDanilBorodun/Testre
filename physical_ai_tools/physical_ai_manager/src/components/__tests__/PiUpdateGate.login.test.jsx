// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The forced update gate and the robot login (2026-11-10): /update is a
// session-only route, so the gate arms only with a robot session — or for an
// agent from before the login, whose open /update is the way out of that skew.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import PiUpdateGate from '../PiUpdateGate';

vi.mock('../../utils/piMode', () => ({
  __esModule: true,
  usePiMode: () => ({ piMode: true, piModeResolved: true }),
}));

let mockPhase = 'locked';
vi.mock('../../features/piAuth/piAuth', () => ({
  __esModule: true,
  usePiAuth: () => ({ phase: mockPhase }),
  noteUnauthorized: vi.fn(),
  PI_AUTH_PHASE: { OFF: 'off', LEGACY: 'legacy', SESSION: 'session' },
}));

beforeEach(() => {
  global.fetch = vi.fn((url) => {
    if (String(url).includes('/update/check')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ update_available: true, latest_version: '9.9.9', current_version: '1.0.0' }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  });
});

const probed = () => global.fetch.mock.calls.some((c) => String(c[0]).includes('/update/check'));

describe('PiUpdateGate and the robot login', () => {
  it.each(['locked', 'unclaimed', 'probing', 'exchanging', 'error'])(
    'does not arm without a robot session (%s)',
    async (phase) => {
      mockPhase = phase;
      render(<PiUpdateGate />);
      await Promise.resolve();
      expect(probed()).toBe(false);
      expect(screen.queryByText('Ein Update ist verfügbar!')).toBeNull();
    },
  );

  it.each(['session', 'legacy'])('arms with a session or for a legacy agent (%s)', async (phase) => {
    mockPhase = phase;
    render(<PiUpdateGate />);
    expect(await screen.findByText('Ein Update ist verfügbar!')).toBeInTheDocument();
    await waitFor(() => expect(probed()).toBe(true));
  });
});
