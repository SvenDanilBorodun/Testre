// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Orange Pi „System"-Fenster drives the pi-agent through the same-origin
// /api/system proxy. These lock the load-bearing bits: the Pi-IP-Anzeige, the
// Arme-scannen POST → refresh, and the Netzwerk-Check green/red rendering.

import React from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
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

// ── System-files drift banner ───────────────────────────────────────────────
//
// `system_files_stale` is set at exactly ONE place in agent.py: the branch where
// an automatic repair FAILED. A refused/failed repair leaves the previous,
// working file byte-intact, so the rig is FINE — hence an informational
// `role="status"`, not an assertive `role="alert"`, and wording that says the Pi
// keeps running rather than that it is broken. These pin the four render states
// (stale off / versions equal / versions differ / older agent sending nulls) and
// the two sentences that were removed for being false or unperformable.

// The full shape agent.py::handle_status returns, so the banner is judged
// against a realistic payload rather than a hand-picked pair of keys.
function agentStatusFixture(overrides = {}) {
  return {
    lan_ip: '192.168.1.7',
    hostname: 'edubotics-07',
    agent_ready: true,
    agent_version: '2.14.0',
    system_files_stale: false,
    system_files_version: null,
    manager_up: true,
    robot_tier_up: false,
    container_status: { physical_ai_manager: 'running' },
    gateway_bound: true,
    arms_identified: { leader: null, follower: null, both: false },
    cameras: [],
    follower_only: false,
    hf_token_saved: false,
    images: { age_days: null, is_stale: true },
    ...overrides,
  };
}

const BANNER_HEADING = 'Eine Systemdatei-Aktualisierung wurde nicht übernommen';
const BANNER_BODY =
  'Der Pi läuft normal weiter — mit der zuletzt funktionierenden Konfiguration. ' +
  'Eine Änderung aus einer neueren Version konnte nicht übernommen werden; ' +
  'den Grund nennt das Protokoll. Beim nächsten Neustart versucht der Pi es ' +
  'automatisch erneut.';

function renderWithStatus(overrides) {
  mockPi.agentStatus = agentStatusFixture(overrides);
  return render(<SystemPage />);
}

describe('SystemPage — Systemdateien drift banner', () => {
  it('does not render at all while system_files_stale is false', () => {
    renderWithStatus({ system_files_stale: false, system_files_version: '2.13.0' });
    expect(screen.queryByText(BANNER_HEADING)).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('renders the approved German heading + body when a repair failed', () => {
    renderWithStatus({ system_files_stale: true, system_files_version: '2.13.0' });
    expect(screen.getByText(BANNER_HEADING)).toBeInTheDocument();
    expect(screen.getByText(BANNER_BODY)).toBeInTheDocument();
  });

  it('is a polite status region, never an assertive alert', () => {
    renderWithStatus({ system_files_stale: true, system_files_version: '2.13.0' });
    // The rig is running on its last-known-good config; announcing this
    // assertively interrupts a screen reader for a non-urgent notice.
    expect(screen.queryByRole('alert')).toBeNull();
    const banner = screen.getByRole('status');
    expect(banner).toHaveTextContent(BANNER_HEADING);
    // …and it must not look like an emergency either.
    expect(banner.className).not.toMatch(/amber|red/);
  });

  it('names the origin version when it differs from the agent version', () => {
    renderWithStatus({
      system_files_stale: true,
      system_files_version: '2.13.0',
      agent_version: '2.14.0',
    });
    const origin = screen.getByText(/Die installierten Systemdateien stammen aus Version/);
    expect(origin).toHaveTextContent('Die installierten Systemdateien stammen aus Version 2.13.0.');
  });

  it('suppresses the origin version when it equals the agent version', () => {
    // Reachable whenever setup.sh last ran at the version now running (a bench
    // re-provision). Naming 2.14.0 twice reads as a contradiction — same rule
    // agent.py applies to its Protokoll lines.
    renderWithStatus({
      system_files_stale: true,
      system_files_version: '2.14.0',
      agent_version: '2.14.0',
    });
    expect(screen.getByText(BANNER_HEADING)).toBeInTheDocument();
    expect(screen.queryByText(/stammen aus Version/)).toBeNull();
    expect(screen.getByRole('status')).not.toHaveTextContent('2.14.0');
  });

  it('suppresses the origin version when the Pi was provisioned before the stamp', () => {
    // An old Pi can be drifted AND unstamped — system_files_version is null.
    renderWithStatus({ system_files_stale: true, system_files_version: null });
    expect(screen.getByText(BANNER_HEADING)).toBeInTheDocument();
    expect(screen.queryByText(/stammen aus Version/)).toBeNull();
  });

  it('still renders cleanly when an older agent omits the version keys entirely', () => {
    const status = agentStatusFixture({ system_files_stale: true });
    delete status.system_files_version;
    delete status.agent_version;
    mockPi.agentStatus = status;
    render(<SystemPage />);
    expect(screen.getByText(BANNER_HEADING)).toBeInTheDocument();
    expect(screen.getByText(BANNER_BODY)).toBeInTheDocument();
    expect(screen.queryByText(/stammen aus Version/)).toBeNull();
    // No stray em-dash placeholder where a missing version used to be printed.
    expect(screen.getByRole('status')).not.toHaveTextContent('—.');
  });

  it('never leaks or throws on a malformed system_files_version', () => {
    // The agent's stamp is Optional[str] (agent.py::_read_system_files_stamp),
    // so none of these is reachable today — but the System tab is the Pi's ONLY
    // repair surface, and the two failure modes are not equal in cost. A bare
    // `{value && <p/>}` renders a literal `0`/`NaN` into the banner (measured),
    // and a non-string throws „Objects are not valid as a React child", which
    // white-screens the one page that could fix the Pi. The ternary this
    // replaced was leak-safe for 0/NaN; the guard must not be a regression.
    for (const bad of [0, NaN, '', false, 2.14, ['2.13.0'], {}, { v: 1 }]) {
      const status = agentStatusFixture({
        system_files_stale: true,
        system_files_version: bad,
        agent_version: '2.14.0',
      });
      mockPi.agentStatus = status;
      expect(() => render(<SystemPage />)).not.toThrow();
      const banner = screen.getByRole('status');
      // The heading is always there; nothing else may be.
      expect(banner).toHaveTextContent(BANNER_HEADING);
      expect(banner).not.toHaveTextContent(/stammen aus Version/);
      expect(banner.textContent.trim()).toMatch(/automatisch erneut\.$/);
      cleanup();
    }
  });

  it('no longer claims updates skip these files, nor sends a classroom to setup.sh', () => {
    renderWithStatus({ system_files_stale: true, system_files_version: '2.13.0' });
    const banner = screen.getByRole('status');
    // FALSE since the units/udev/compose joined the self-updated, self-repaired
    // set — that is exactly what this branch built.
    expect(banner).not.toHaveTextContent(/Aktualisierungen erneuern nur den Agenten/);
    // A remedy that needs a source checkout no classroom has.
    expect(banner).not.toHaveTextContent(/setup\.sh/);
    expect(banner).not.toHaveTextContent(/veraltet/);
    // The drift is benign and the data is irreplaceable.
    expect(banner).not.toHaveTextContent(/SD-Karte|neu aufsetzen|flashen/i);
  });
});
