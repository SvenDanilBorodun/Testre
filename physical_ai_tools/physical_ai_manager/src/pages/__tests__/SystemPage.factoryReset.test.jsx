// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The factory reset needs a TYPED confirmation (owner decision 5) and returns
// the Pi to unclaimed with a new setup code, which the page hands to the claim
// screen (robot login, 2026-11-10).

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SystemPage, { factoryResetWordMatches, FACTORY_RESET_WORD } from '../SystemPage';

const refreshAgentStatus = vi.fn(() => Promise.resolve(null));
vi.mock('../../utils/piMode', () => ({
  __esModule: true,
  usePiMode: () => ({
    piMode: true,
    piModeResolved: true,
    agentReachable: true,
    refreshAgentStatus,
    agentStatus: {
      lan_ip: '192.168.1.7',
      hostname: 'edubotics-07',
      agent_ready: true,
      manager_up: true,
      robot_tier_up: false,
      arms_identified: { leader: null, follower: null, both: false },
      cameras: [],
      images: { age_days: null, is_stale: true },
    },
  }),
}));

const setPiResetCode = vi.fn();
const noteUnauthorized = vi.fn();
vi.mock('../../features/piAuth/piAuth', () => ({
  __esModule: true,
  setPiResetCode: (...a) => setPiResetCode(...a),
  noteUnauthorized: (...a) => noteUnauthorized(...a),
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

function jsonRes(body, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
}

let resetBody = null;
beforeEach(() => {
  resetBody = null;
  setPiResetCode.mockClear();
  noteUnauthorized.mockClear();
  global.fetch = vi.fn((url, opts = {}) => {
    const u = String(url);
    if (u.includes('/factory-reset')) {
      resetBody = JSON.parse(opts.body);
      return jsonRes({
        ok: true,
        setup_code: 'WXYZ-2345',
        message: '2 Volumes gelöscht. Der Roboter ist jetzt nicht mehr eingerichtet.',
      });
    }
    if (u.includes('/status')) return jsonRes({ ok: false, code: 'login_required' }, false, 401);
    return jsonRes({});
  });
});

async function openFinalStep() {
  render(<SystemPage />);
  await userEvent.click(screen.getByRole('button', { name: /Daten zurücksetzen/ }));
  await userEvent.click(screen.getByRole('button', { name: 'Fortfahren' }));
}

describe('the typed confirmation', () => {
  it('matches the word case-insensitively and nothing else', () => {
    expect(FACTORY_RESET_WORD).toBe('ZURÜCKSETZEN');
    expect(factoryResetWordMatches('zurücksetzen')).toBe(true);
    expect(factoryResetWordMatches('  ZURÜCKSETZEN ')).toBe(true);
    expect(factoryResetWordMatches('ZURUECKSETZEN')).toBe(false);
    expect(factoryResetWordMatches('ja')).toBe(false);
    expect(factoryResetWordMatches(undefined)).toBe(false);
  });

  it('keeps the final button disabled until the word is typed', async () => {
    await openFinalStep();
    const final = screen.getByRole('button', { name: 'Endgültig löschen' });
    expect(final).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Zur Bestätigung/), 'zurück');
    expect(final).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Zur Bestätigung/), 'setzen');
    expect(final).toBeEnabled();
  });

  it('sends the typed word and hands the new setup code to the claim screen', async () => {
    await openFinalStep();
    await userEvent.type(screen.getByLabelText(/Zur Bestätigung/), 'ZURÜCKSETZEN');
    await userEvent.click(screen.getByRole('button', { name: 'Endgültig löschen' }));
    await waitFor(() => expect(setPiResetCode).toHaveBeenCalledWith('WXYZ-2345'));
    expect(resetBody).toEqual({ confirm: true, confirm_again: true, confirm_text: 'ZURÜCKSETZEN' });
    expect(mockToast.success).toHaveBeenCalled();
  });
});

describe('a refused agent call', () => {
  it('a 401 tells the robot login to re-check its session', async () => {
    global.fetch = vi.fn(() => jsonRes({ ok: false, code: 'login_required', message: 'x' }, false, 401));
    render(<SystemPage />);
    await userEvent.click(screen.getByRole('button', { name: "Netzwerk prüfen" }));
    await waitFor(() => expect(noteUnauthorized).toHaveBeenCalled());
  });
});
