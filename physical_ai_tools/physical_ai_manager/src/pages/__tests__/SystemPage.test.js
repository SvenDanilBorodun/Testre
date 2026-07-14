// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Orange Pi „System"-Fenster drives the pi-agent through the same-origin
// /api/system proxy. These lock the load-bearing bits: the Pi-IP-Anzeige, the
// Arme-scannen POST → refresh, and the Netzwerk-Check green/red rendering.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SystemPage from '../SystemPage';

// usePiMode — controllable snapshot.
const refreshAgentStatus = vi.fn(() => Promise.resolve(null));
let mockPi;
vi.mock('../../utils/piMode', () => ({
  __esModule: true,
  usePiMode: () => mockPi,
}));

// react-hot-toast — callable default with success/error.
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

const NETCHECKS = [
  { key: 'cloud', label: 'Cloud-Dienst erreichbar', ok: true, hint: '' },
  {
    key: 'tls',
    label: 'Zertifikate echt (keine TLS-Inspektion)',
    ok: false,
    hint: 'TLS-Inspektion erkannt — IT: Ausnahme für das Robotik-VLAN nötig.',
  },
];

beforeEach(() => {
  refreshAgentStatus.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  mockPi = {
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
      hf_token_saved: false,
      images: { age_days: null, is_stale: true },
    },
  };
  global.fetch = vi.fn((url, opts = {}) => {
    const u = String(url);
    if (u.includes('/netzwerk-check')) return jsonRes({ ok: true, checks: NETCHECKS });
    if (u.includes('/scan-arms')) {
      return jsonRes({
        ok: true,
        leader: '/dev/serial/by-id/leader',
        follower: '/dev/serial/by-id/follower',
        message: 'Beide Arme erkannt und gespeichert.',
      });
    }
    if (u.includes('/cameras/preview/stop')) return jsonRes({ ok: true });
    return jsonRes({});
  });
});

describe('SystemPage', () => {
  it('shows the Pi LAN IP prominently (Pi-IP-Anzeige)', () => {
    render(<SystemPage />);
    expect(screen.getByText('192.168.1.7')).toBeInTheDocument();
    expect(screen.getByText('edubotics-07.local')).toBeInTheDocument();
    // Refreshes the agent status on mount.
    expect(refreshAgentStatus).toHaveBeenCalled();
  });

  it('scans arms via POST /api/system/scan-arms and refreshes status', async () => {
    render(<SystemPage />);
    await userEvent.click(screen.getByRole('button', { name: 'Arme scannen' }));

    await waitFor(() =>
      expect(mockToast.success).toHaveBeenCalledWith('Beide Arme erkannt und gespeichert.')
    );
    const scanCall = global.fetch.mock.calls.find((c) => String(c[0]).includes('/scan-arms'));
    expect(scanCall).toBeTruthy();
    expect(scanCall[1].method).toBe('POST');
    expect(String(scanCall[0])).toContain('/api/system/scan-arms');
    // finally-block refresh (mount + post-scan).
    expect(refreshAgentStatus.mock.calls.length).toBeGreaterThan(1);
  });

  it('runs the Netzwerk-Check and renders green/red German lines with hints', async () => {
    render(<SystemPage />);
    await userEvent.click(screen.getByRole('button', { name: 'Netzwerk prüfen' }));

    expect(await screen.findByText('Cloud-Dienst erreichbar')).toBeInTheDocument();
    expect(screen.getByText('Zertifikate echt (keine TLS-Inspektion)')).toBeInTheDocument();
    // The failing check surfaces its one-line German hint.
    expect(
      screen.getByText(/TLS-Inspektion erkannt/)
    ).toBeInTheDocument();
  });

  it('gates „Umgebung starten" until both arms are identified', () => {
    render(<SystemPage />);
    // arms.both is false in the base snapshot → start disabled.
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeDisabled();
  });
});
