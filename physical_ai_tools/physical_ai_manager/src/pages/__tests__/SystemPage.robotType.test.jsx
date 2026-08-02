// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// WP-4 + WP-5 — the Orange Pi System tab's Robotertyp selector and the
// profile-aware „Umgebung starten" gate.
//
// The two are ONE change: the selector chooses the profile, and every gate on
// the page (start button, pill, Leader tile, hints) reads the SAME profile back
// off the agent's /status. Before this, the Pi hard-required BOTH arms, so
// `edu6_studio` AND the long-shipping `omx_follower` were unreachable there.
//
// Two properties carry the most weight and are asserted repeatedly:
//   1. an `omx_full` Pi renders and gates EXACTLY as it did (every existing
//      classroom rig), and
//   2. an agent from before WP-3 — which sends `robot_profiles`,
//      `robot_type` and `hardware_ready` not at all — degrades to that same
//      behaviour instead of crashing the Pi's only repair surface.

import React from 'react';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SystemPage from '../SystemPage';

const refreshAgentStatus = vi.fn(() => Promise.resolve(null));
let mockPi;
vi.mock('../../utils/piMode', () => ({
  __esModule: true,
  usePiMode: () => mockPi,
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

// The exact `robot_profiles` list agent.py::handle_status builds from
// pi_agent/constants.py::ROBOT_PROFILES (id + display_de + scan_requires_leader
// + camera_roles). `camera_roles` is carried verbatim, ORDER included — the two
// OMX rows deliberately disagree on order, which is what proves the dropdown
// takes its order from the SPA and not from the wire.
const PROFILES = [
  {
    id: 'omx_full',
    display_de: 'OMX – Voll',
    scan_requires_leader: true,
    camera_roles: ['gripper', 'scene'],
  },
  {
    id: 'omx_follower',
    display_de: 'OMX – Roboter Studio (nur Follower)',
    scan_requires_leader: false,
    camera_roles: ['scene', 'gripper'],
  },
  {
    id: 'edu6_studio',
    display_de: 'EduBotics 6-Achs – Roboter Studio',
    scan_requires_leader: false,
    camera_roles: ['scene'],
  },
];

function statusFixture(overrides = {}) {
  return {
    lan_ip: '192.168.1.7',
    hostname: 'edubotics-07',
    agent_ready: true,
    agent_version: '2.13.0',
    system_files_stale: false,
    system_files_version: null,
    manager_up: true,
    robot_tier_up: false,
    arms_identified: { leader: null, follower: null, both: false },
    robot_type: 'omx_full',
    robot_profiles: PROFILES,
    hardware_ready: false,
    cameras: [],
    follower_only: false,
    hf_token_saved: false,
    images: { age_days: null, is_stale: true },
    ...overrides,
  };
}

// A rig whose hardware satisfies the given profile: both arms for omx_full,
// the follower alone for the two leader-less profiles.
function readyFixture(robotType, overrides = {}) {
  const both = robotType === 'omx_full';
  return statusFixture({
    robot_type: robotType,
    hardware_ready: true,
    arms_identified: {
      leader: both ? '/dev/serial/by-id/usb-LEADER' : null,
      follower: '/dev/serial/by-id/usb-FOLLOWER',
      both,
    },
    ...overrides,
  });
}

function renderWith(status) {
  mockPi.agentStatus = status;
  return render(<SystemPage />);
}

let robotTypeResponse;

beforeEach(() => {
  refreshAgentStatus.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  robotTypeResponse = () =>
    jsonRes({ ok: true, robot_type: 'edu6_studio', message: 'Robotertyp gesetzt: X.' });
  mockPi = {
    piMode: true,
    piModeResolved: true,
    agentReachable: true,
    refreshAgentStatus,
    agentStatus: statusFixture(),
  };
  global.fetch = vi.fn((url) => {
    const u = String(url);
    if (u.includes('/robot-type')) return robotTypeResponse();
    if (u.includes('/cameras/preview/stop')) return jsonRes({ ok: true });
    if (u.includes('/scan-arms')) return jsonRes({ ok: true, message: 'ok' });
    return jsonRes({});
  });
});

// ── WP-4: the selector ──────────────────────────────────────────────────────

describe('SystemPage — Robotertyp selector', () => {
  it('renders every profile the agent offers, labelled in German', () => {
    renderWith(statusFixture());
    const select = screen.getByLabelText('Robotertyp');
    const labels = within(select)
      .getAllByRole('option')
      .map((o) => o.textContent);
    expect(labels).toEqual([
      'OMX – Voll',
      'OMX – Roboter Studio (nur Follower)',
      'EduBotics 6-Achs – Roboter Studio',
    ]);
    expect(select).toHaveValue('omx_full');
  });

  it('lives inside the existing „Modus" card — there is only ONE', () => {
    renderWith(statusFixture());
    expect(screen.getAllByText('Modus')).toHaveLength(1);
  });

  it('POSTs the chosen id to /api/system/robot-type and refreshes status', async () => {
    renderWith(statusFixture());
    const before = refreshAgentStatus.mock.calls.length;
    await userEvent.selectOptions(screen.getByLabelText('Robotertyp'), 'edu6_studio');

    await waitFor(() =>
      expect(mockToast.success).toHaveBeenCalledWith('Robotertyp gesetzt: X.')
    );
    const call = global.fetch.mock.calls.find((c) => String(c[0]).includes('/robot-type'));
    expect(call).toBeTruthy();
    expect(String(call[0])).toBe('/api/system/robot-type');
    expect(call[1].method).toBe('POST');
    expect(JSON.parse(call[1].body)).toEqual({ robot_type: 'edu6_studio' });
    expect(refreshAgentStatus.mock.calls.length).toBeGreaterThan(before);
  });

  it('is frozen while the robot tier runs, and says why', () => {
    renderWith(statusFixture({ robot_tier_up: true }));
    const select = screen.getByLabelText('Robotertyp');
    expect(select).toBeDisabled();
    expect(select).toHaveAttribute(
      'title',
      'Zum Wechseln zuerst die Roboter-Umgebung stoppen.'
    );
  });

  it('is frozen in Cloud-Modus (no robot → the type is irrelevant)', async () => {
    renderWith(statusFixture());
    expect(screen.getByLabelText('Robotertyp')).toBeEnabled();
    await userEvent.click(screen.getByRole('checkbox'));
    expect(screen.getByLabelText('Robotertyp')).toBeDisabled();
  });

  it('gives every disabled reason its OWN sentence', async () => {
    // The help paragraph explains the tier case only. Without a second title
    // the control is grey in Cloud-Modus while the one explanation on screen
    // names a condition that is already satisfied — a contradiction a teacher
    // reads as a bug.
    renderWith(statusFixture());
    await userEvent.click(screen.getByRole('checkbox'));
    expect(screen.getByLabelText('Robotertyp')).toHaveAttribute(
      'title',
      'Im Cloud-Modus wird kein Roboter verwendet — der Robotertyp spielt keine Rolle.'
    );
    cleanup();
    // The tier reason wins when both hold — it is the one the student can act on.
    mockPi.agentStatus = statusFixture({ robot_tier_up: true });
    render(<SystemPage />);
    await userEvent.click(screen.getByRole('checkbox'));
    expect(screen.getByLabelText('Robotertyp')).toHaveAttribute(
      'title',
      'Zum Wechseln zuerst die Roboter-Umgebung stoppen.'
    );
  });

  it('is frozen while its own POST is in flight', async () => {
    let release;
    robotTypeResponse = () =>
      new Promise((r) => {
        release = () => r({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
      });
    renderWith(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText('Robotertyp'), 'edu6_studio');
    await waitFor(() => expect(screen.getByLabelText('Robotertyp')).toBeDisabled());
    release();
    await waitFor(() => expect(screen.getByLabelText('Robotertyp')).toBeEnabled());
  });

  it('shows the chosen id while the POST is in flight (no snap-back flicker)', async () => {
    let release;
    robotTypeResponse = () =>
      new Promise((r) => {
        release = () => r({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
      });
    renderWith(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText('Robotertyp'), 'edu6_studio');
    // The agent's /status still says omx_full; the optimistic value holds.
    expect(screen.getByLabelText('Robotertyp')).toHaveValue('edu6_studio');
    release();
    await waitFor(() => expect(screen.getByLabelText('Robotertyp')).toBeEnabled());
  });

  it('reverts to the agent truth when the change is REFUSED', async () => {
    // agent.py answers 409 „nur wenn die Roboter-Umgebung gestoppt ist" — the
    // wizard must not keep showing a type this Pi never adopted.
    robotTypeResponse = () =>
      jsonRes(
        { ok: false, message: 'Der Robotertyp kann nur geändert werden, wenn die Roboter-Umgebung gestoppt ist.' },
        false,
        409
      );
    renderWith(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText('Robotertyp'), 'edu6_studio');
    await waitFor(() =>
      expect(mockToast.error).toHaveBeenCalledWith(
        'Der Robotertyp kann nur geändert werden, wenn die Roboter-Umgebung gestoppt ist.'
      )
    );
    await waitFor(() => expect(screen.getByLabelText('Robotertyp')).toHaveValue('omx_full'));
  });

  it('reverts when the agent is unreachable mid-change', async () => {
    robotTypeResponse = () => Promise.reject(new Error('offline'));
    renderWith(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText('Robotertyp'), 'edu6_studio');
    await waitFor(() =>
      expect(mockToast.error).toHaveBeenCalledWith('Der Agent ist nicht erreichbar.')
    );
    await waitFor(() => expect(screen.getByLabelText('Robotertyp')).toHaveValue('omx_full'));
  });

  it('clears the optimistic value only AFTER the refresh, never before it', async () => {
    // The ORDER inside the finally block is the whole point and nothing else
    // fenced it: clearing first makes both setStates flush before the await, so
    // the <select> visibly snaps back to the old id for one /status round-trip
    // on a change that SUCCEEDED. Held open by gating the refresh on a promise
    // this test resolves, so the window is observable rather than a race.
    let releaseRefresh;
    const refreshHeld = new Promise((r) => { releaseRefresh = r; });
    refreshAgentStatus
      .mockImplementationOnce(() => Promise.resolve(null))          // mount
      .mockImplementationOnce(() => refreshHeld.then(() => {        // the finally
        mockPi.agentStatus = statusFixture({ robot_type: 'edu6_studio' });
        return mockPi.agentStatus;
      }));
    renderWith(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText('Robotertyp'), 'edu6_studio');
    await waitFor(() => expect(refreshAgentStatus.mock.calls.length).toBeGreaterThan(1));
    // The refresh has NOT returned yet — the agent still says omx_full, so only
    // the optimistic value can be holding the display here.
    expect(screen.getByLabelText('Robotertyp')).toHaveValue('edu6_studio');
    releaseRefresh();
    await waitFor(() => expect(screen.getByLabelText('Robotertyp')).toBeEnabled());
  });

  it('still clears the optimistic value when the status refresh itself throws', async () => {
    // Call 1 is the mount effect; call 2 is the one inside the change
    // handler's finally block — that is the one that must not escape, or the
    // selector stays stuck on a value the agent never confirmed.
    refreshAgentStatus
      .mockImplementationOnce(() => Promise.resolve(null))
      .mockImplementationOnce(() => Promise.reject(new Error('boom')));
    renderWith(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText('Robotertyp'), 'edu6_studio');
    await waitFor(() => expect(refreshAgentStatus.mock.calls.length).toBeGreaterThan(1));
    await waitFor(() => expect(screen.getByLabelText('Robotertyp')).toHaveValue('omx_full'));
    expect(screen.getByLabelText('Robotertyp')).toBeEnabled();
  });

  it('never silently displays a different type than the rig runs', () => {
    // A hand-edited .env can hold an id the agent's list does not contain (or
    // /status can arrive before the list). Without the placeholder option the
    // <select> would render the FIRST profile while the Pi runs another.
    renderWith(statusFixture({ robot_type: 'irgendwas' }));
    const select = screen.getByLabelText('Robotertyp');
    expect(select).toHaveValue('irgendwas');
    // …and that placeholder is a REPORT, not an offer — POSTing it back would
    // just earn a German 400 („Kein Robotertyp angegeben" / „Unbekannter").
    expect(within(select).getByRole('option', { name: 'irgendwas' })).toBeDisabled();
  });

  it('names all THREE profiles worth of behaviour without enumerating them', () => {
    // gui_app.py's Modus help text still describes only the two OMX profiles
    // while its combobox lists three (FINDINGS D10). Deriving the sentence from
    // the SELECTED profile cannot go stale when a fourth is added.
    renderWith(statusFixture({ robot_type: 'omx_full' }));
    expect(
      screen.getByText(/Dieser Typ braucht beide Arme — Leader und Follower\./)
    ).toBeInTheDocument();
    cleanup();
    renderWith(statusFixture({ robot_type: 'edu6_studio' }));
    expect(
      screen.getByText(/Dieser Typ braucht nur einen Arm/)
    ).toBeInTheDocument();
  });
});

// ── graceful degrade: an agent from before WP-3 ─────────────────────────────

describe('SystemPage — an agent without the profile keys', () => {
  function oldAgentStatus(overrides = {}) {
    const s = statusFixture(overrides);
    delete s.robot_profiles;
    delete s.robot_type;
    delete s.hardware_ready;
    return s;
  }

  it('hides the selector instead of crashing', () => {
    renderWith(oldAgentStatus());
    expect(screen.queryByLabelText('Robotertyp')).toBeNull();
    // …and the page still renders.
    expect(screen.getByText('192.168.1.7')).toBeInTheDocument();
  });

  it('falls back to the both-arms gate it always had', () => {
    renderWith(oldAgentStatus());
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeDisabled();
    cleanup();
    renderWith(
      oldAgentStatus({
        arms_identified: { leader: '/dev/l', follower: '/dev/f', both: true },
      })
    );
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeEnabled();
    expect(screen.getByText('Beide Arme erkannt')).toBeInTheDocument();
    expect(screen.getByText('Leader')).toBeInTheDocument();
  });

  it('survives a malformed robot_profiles payload of every shape', () => {
    for (const bad of [null, 'omx_full', 42, {}, [null], [{ id: 'x' }], [{ display_de: 'y' }],
      [{ id: 3, display_de: 'y' }], [{ id: 'x', display_de: {} }], [{ id: '', display_de: 'y' }]]) {
      const status = statusFixture({ robot_profiles: bad });
      mockPi.agentStatus = status;
      expect(() => render(<SystemPage />)).not.toThrow();
      expect(screen.queryByLabelText('Robotertyp')).toBeNull();
      cleanup();
    }
  });

  it('survives a malformed robot_type SCALAR of every shape', () => {
    // Sibling of the robot_profiles case above, and until now the only one of
    // the pair that was not mutation-verified: removing the
    // `typeof … === 'string'` guard left the whole suite green.
    //
    // The crash is specific. `selectedRobotType` is rendered as a React CHILD
    // in the disabled placeholder <option> (`{selectedRobotType || '— … —'}`),
    // so a truthy non-string throws „Objects are not valid as a React child"
    // and white-screens the System tab — the Pi's ONLY repair surface, and the
    // one page a student reaches when nothing else works.
    for (const bad of [{}, { id: 'omx_full' }, ['omx_full'], 42, true]) {
      const status = statusFixture({ robot_type: bad });
      mockPi.agentStatus = status;
      expect(() => render(<SystemPage />)).not.toThrow();
      // The selector still renders (robot_profiles is fine) and reports „no
      // type" rather than a stale or invented one.
      expect(screen.getByLabelText('Robotertyp')).toHaveValue('');
      expect(screen.getByText('— Robotertyp —')).toBeInTheDocument();
      cleanup();
    }
  });

  it('treats a row with NO scan_requires_leader as needing a leader', () => {
    // Only an EXPLICIT false may hide the Leader tile — the same doctrine the
    // nav's capability gating uses. An agent that grows the list but omits the
    // flag must degrade to today's both-arms wizard, not to a leader-less one.
    renderWith(
      statusFixture({
        robot_type: 'neu',
        robot_profiles: [{ id: 'neu', display_de: 'Neuer Typ' }],
      })
    );
    expect(screen.getByText('Leader')).toBeInTheDocument();
    expect(screen.getByText('Follower')).toBeInTheDocument();
    expect(
      screen.getByText(/Dieser Typ braucht beide Arme — Leader und Follower\./)
    ).toBeInTheDocument();
  });

  it('ignores a partially-valid list only for the invalid rows', () => {
    renderWith(
      statusFixture({
        robot_profiles: [PROFILES[0], { id: 'kaputt' }, PROFILES[2]],
      })
    );
    const labels = within(screen.getByLabelText('Robotertyp'))
      .getAllByRole('option')
      .map((o) => o.textContent);
    expect(labels).toEqual(['OMX – Voll', 'EduBotics 6-Achs – Roboter Studio']);
  });
});

// ── WP-5: profile-aware start gating ────────────────────────────────────────

describe('SystemPage — follower-only start gating', () => {
  it.each(['omx_follower', 'edu6_studio'])(
    'starts %s with ONE arm — the gate that made both unreachable',
    (pid) => {
      renderWith(readyFixture(pid));
      expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeEnabled();
    }
  );

  it('still refuses a follower-only rig with no arm at all', () => {
    renderWith(statusFixture({ robot_type: 'edu6_studio', hardware_ready: false }));
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeDisabled();
  });

  it('refuses while the agent is not ready, on every profile', () => {
    for (const pid of ['omx_full', 'omx_follower', 'edu6_studio']) {
      renderWith(readyFixture(pid, { agent_ready: false }));
      expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeDisabled();
      cleanup();
    }
  });

  it('hides the Leader tile on a leader-less profile and renames the other', () => {
    renderWith(readyFixture('edu6_studio'));
    expect(screen.queryByText('Leader')).toBeNull();
    expect(screen.queryByText('Follower')).toBeNull();
    expect(screen.getByText('Roboterarm')).toBeInTheDocument();
    expect(screen.getByText('/dev/serial/by-id/usb-FOLLOWER')).toBeInTheDocument();
  });

  it('says „Arm erkannt", never „Beide Arme erkannt", on a leader-less rig', () => {
    renderWith(readyFixture('omx_follower'));
    expect(screen.getByText('Arm erkannt')).toBeInTheDocument();
    expect(screen.queryByText('Beide Arme erkannt')).toBeNull();
  });

  it('turns the pill GREEN on a ready leader-less rig, not just relabels it', () => {
    // `arms_identified.both` is false on such a rig by construction, so a pill
    // still toned off `both` would read „Arm erkannt" in grey — the neutral
    // tone the page uses for „Nicht erkannt". Tone and text must agree.
    renderWith(readyFixture('edu6_studio'));
    const pill = screen.getByText('Arm erkannt');
    expect(pill.className).toContain('--success-wash');
    cleanup();
    renderWith(statusFixture({ robot_type: 'edu6_studio', hardware_ready: false }));
    expect(screen.getByText('Nicht erkannt').className).toContain('--bg-sunk');
  });

  it('matches the hint and the button title to the profile', () => {
    renderWith(statusFixture({ robot_type: 'edu6_studio', hardware_ready: false }));
    expect(
      screen.getByText('Bitte zuerst den Roboterarm scannen (Schritt A) oder oben „Cloud-Modus" wählen.')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toHaveAttribute(
      'title',
      'Bitte zuerst den Roboterarm scannen (oder Cloud-Modus wählen).'
    );
    expect(
      screen.getByText('Startet den Roboterarm und die Kameras. Voraussetzung: Arm erkannt.')
    ).toBeInTheDocument();
  });

  it('keeps every both-arms sentence on omx_full', () => {
    renderWith(statusFixture({ robot_type: 'omx_full', hardware_ready: false }));
    expect(
      screen.getByText('Bitte zuerst beide Arme scannen (Schritt A) oder oben „Cloud-Modus" wählen.')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toHaveAttribute(
      'title',
      'Bitte zuerst beide Arme scannen (oder Cloud-Modus wählen).'
    );
    expect(
      screen.getByText('Startet Arme und Kameras. Voraussetzung: beide Arme erkannt.')
    ).toBeInTheDocument();
    expect(screen.getByText('Nicht erkannt')).toBeInTheDocument();
    expect(screen.getByText('Leader')).toBeInTheDocument();
    expect(screen.getByText('Follower')).toBeInTheDocument();
  });

  it('tells a leader-less rig to power ONE arm when a scan fails', async () => {
    // The mocked body is the agent's REAL wrong-arm 409 (agent.py
    // handle_scan_arms): an arm was found and it is the leader.
    const wrongArm =
      'Es wurde der Leader-Arm erkannt — dieser Robotertyp braucht den Follower-Arm. '
      + 'Bitte den Follower-Arm anschließen.';
    global.fetch = vi.fn((url) => {
      const u = String(url);
      if (u.includes('/scan-arms')) {
        return jsonRes({ ok: false, message: wrongArm }, false, 409);
      }
      return jsonRes({});
    });
    renderWith(statusFixture({ robot_type: 'edu6_studio', hardware_ready: false }));
    await userEvent.click(screen.getByRole('button', { name: 'Arm scannen' }));
    expect(
      await screen.findByText('Den Roboterarm über USB anschließen und einschalten (12-V-Netzteil).')
    ).toBeInTheDocument();
    expect(
      screen.queryByText('Beide Arme über USB anschließen und einschalten (12-V-Netzteil).')
    ).toBeNull();
    // The agent's own German wins over any generic fallback.
    expect(screen.getByText(wrongArm)).toBeInTheDocument();
  });

  it('gates on the PROFILE, not on „two arms are plugged in"', () => {
    // An omx_follower rig can physically have both arms attached; the agent
    // reports hardware_ready against the profile and the page must follow IT.
    renderWith(
      statusFixture({
        robot_type: 'omx_follower',
        hardware_ready: true,
        arms_identified: { leader: '/dev/l', follower: '/dev/f', both: true },
      })
    );
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeEnabled();
    expect(screen.getByText('Arm erkannt')).toBeInTheDocument();
    expect(screen.queryByText('Leader')).toBeNull();
  });

  it('Cloud-Modus starts regardless of profile or hardware', async () => {
    renderWith(statusFixture({ robot_type: 'edu6_studio', hardware_ready: false }));
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeDisabled();
    await userEvent.click(screen.getByRole('checkbox'));
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeEnabled();
  });
});

// ── omx_full regression fence ───────────────────────────────────────────────

describe('SystemPage — an omx_full Pi is unchanged', () => {
  it('gates the start button exactly on both arms + agent_ready', () => {
    renderWith(statusFixture({ robot_type: 'omx_full', hardware_ready: false }));
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeDisabled();
    cleanup();
    renderWith(readyFixture('omx_full'));
    expect(screen.getByRole('button', { name: 'Umgebung starten' })).toBeEnabled();
    expect(screen.getByText('Beide Arme erkannt')).toBeInTheDocument();
  });

  it('renders both arm tiles with their paths', () => {
    renderWith(readyFixture('omx_full'));
    expect(screen.getByText('Leader')).toBeInTheDocument();
    expect(screen.getByText('Follower')).toBeInTheDocument();
    expect(screen.getByText('/dev/serial/by-id/usb-LEADER')).toBeInTheDocument();
    expect(screen.getByText('/dev/serial/by-id/usb-FOLLOWER')).toBeInTheDocument();
  });

  it('keeps the both-arms scan help', async () => {
    global.fetch = vi.fn((url) =>
      String(url).includes('/scan-arms')
        ? jsonRes({ ok: false, message: 'Arm-Scan fehlgeschlagen.' }, false, 409)
        : jsonRes({})
    );
    renderWith(statusFixture({ robot_type: 'omx_full', hardware_ready: false }));
    await userEvent.click(screen.getByRole('button', { name: 'Arme scannen' }));
    expect(
      await screen.findByText('Beide Arme über USB anschließen und einschalten (12-V-Netzteil).')
    ).toBeInTheDocument();
  });
});

// ── the scan card's singular/plural ─────────────────────────────────────────

describe('Schritt A wording matches how many arms the profile has', () => {
  // DELIBERATE DIVERGENCE from the Windows GUI, which says „Arme scannen" on
  // every profile. The Pi card carries singular labels the GUI does not (a
  // „Roboterarm" tile, an „Arm erkannt" pill), so the plural read as a bug
  // beside them on a one-arm rig. `omx_full` must stay plural.

  const FOLLOWER_ONLY = ['omx_follower', 'edu6_studio'];

  it('is plural on omx_full — heading and button', () => {
    renderWith(statusFixture({ robot_type: 'omx_full' }));
    expect(screen.getByText('Schritt A — Arme scannen')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Arme scannen' })).toBeInTheDocument();
    expect(screen.queryByText('Schritt A — Arm scannen')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Arm scannen' })).toBeNull();
  });

  it.each(FOLLOWER_ONLY)('is singular on %s — heading and button', (robotType) => {
    renderWith(statusFixture({ robot_type: robotType }));
    expect(screen.getByText('Schritt A — Arm scannen')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Arm scannen' })).toBeInTheDocument();
    expect(screen.queryByText('Schritt A — Arme scannen')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Arme scannen' })).toBeNull();
  });

  it.each(FOLLOWER_ONLY)('agrees with the tile and pill it sits beside on %s', (robotType) => {
    // The three labels that made the plural look wrong are all singular now.
    renderWith(readyFixture(robotType));
    expect(screen.getByText('Schritt A — Arm scannen')).toBeInTheDocument();
    expect(screen.getByText('Roboterarm')).toBeInTheDocument();
    expect(screen.getByText('Arm erkannt')).toBeInTheDocument();
  });

  it('keeps the tile and pill untouched on omx_full', () => {
    renderWith(readyFixture('omx_full'));
    expect(screen.getByText('Leader')).toBeInTheDocument();
    expect(screen.getByText('Follower')).toBeInTheDocument();
    expect(screen.getByText('Beide Arme erkannt')).toBeInTheDocument();
    expect(screen.queryByText('Roboterarm')).toBeNull();
  });

  it('says „Wird gescannt …" on every profile while a scan runs', async () => {
    let release;
    global.fetch = vi.fn((url) =>
      String(url).includes('/scan-arms')
        ? new Promise((r) => {
            release = () => r({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
          })
        : jsonRes({})
    );
    renderWith(statusFixture({ robot_type: 'edu6_studio' }));
    await userEvent.click(screen.getByRole('button', { name: 'Arm scannen' }));
    expect(await screen.findByRole('button', { name: 'Wird gescannt …' })).toBeInTheDocument();
    release();
    expect(await screen.findByRole('button', { name: 'Arm scannen' })).toBeInTheDocument();
  });
});

// ── Schritt C: what the camera-role save actually POSTs ─────────────────────

describe('camera roles are only ever sent with an explicit role', () => {
  // LOAD-BEARING for the agent: handle_cameras_roles has NO lone-camera
  // default (it was unreachable code), so a role-less camera is a German 400
  // there. This filter is what guarantees the student never sees that 400 —
  // relaxing it would surface one on an ordinary save.

  function camFixture() {
    return statusFixture({
      robot_type: 'omx_full',
      cameras: [
        { path: '/dev/v4l/by-id/usb-CAM-A', role: 'gripper' },
        { path: '/dev/v4l/by-id/usb-CAM-B', role: '' },
      ],
    });
  }

  function rolesBody() {
    const call = global.fetch.mock.calls.find((c) => String(c[0]).includes('/cameras/roles'));
    expect(call).toBeTruthy();
    return JSON.parse(call[1].body);
  }

  async function scanThenSave() {
    await userEvent.click(screen.getByRole('button', { name: 'Kameras suchen' }));
    await screen.findByRole('button', { name: 'Zuordnung speichern' });
    await userEvent.click(screen.getByRole('button', { name: 'Zuordnung speichern' }));
    await waitFor(() =>
      expect(
        global.fetch.mock.calls.some((c) => String(c[0]).includes('/cameras/roles'))
      ).toBe(true)
    );
  }

  beforeEach(() => {
    global.fetch = vi.fn((url) => {
      const u = String(url);
      if (u.includes('/cameras/scan')) {
        return jsonRes({
          ok: true,
          cameras: [
            { path: '/dev/v4l/by-id/usb-CAM-A', name: 'CAM-A' },
            { path: '/dev/v4l/by-id/usb-CAM-B', name: 'CAM-B' },
          ],
        });
      }
      if (u.includes('/cameras/roles')) return jsonRes({ ok: true, message: 'ok' });
      return jsonRes({});
    });
  });

  it('drops a camera the student left unassigned', async () => {
    renderWith(camFixture());
    await scanThenSave();
    expect(rolesBody().cameras).toEqual([
      { path: '/dev/v4l/by-id/usb-CAM-A', role: 'gripper' },
    ]);
  });

  it('never sends a blank or absent role', async () => {
    renderWith(camFixture());
    await scanThenSave();
    rolesBody().cameras.forEach((c) => {
      expect(['gripper', 'scene']).toContain(c.role);
    });
  });

  it('sends an empty list rather than a lone role-less camera', async () => {
    // The camera MUST be seeded role-less into agentStatus.cameras, not merely
    // left out: with `cameras: []` the seed is {} and `roles` stays empty, so
    // the empty payload would come from there being nothing in the role map at
    // all — and the test would pass with the filter deleted. (It did; an
    // independent review caught it.) Seeded this way it fences the filter.
    renderWith(
      statusFixture({
        robot_type: 'edu6_studio',
        cameras: [{ path: '/dev/v4l/by-id/usb-CAM-A', role: '' }],
      })
    );
    await scanThenSave();
    expect(rolesBody().cameras).toEqual([]);
  });

  it('sends a lone „scene" camera — the only camera a follower-only kit has', async () => {
    // The filter is an ALLOWLIST, and until this test it was fenced in one
    // direction only: every case above supplies `gripper`, so narrowing it to
    // `r === 'gripper'` left the whole 46-file suite green. On an edu6 /
    // omx_follower kit `scene` is the ONLY role there is (a follower-only rig
    // has no gripper camera), so that narrowing would POST `{"cameras": []}`,
    // the agent would answer 200, the toast would say „Kameras zugeordnet."
    // — and the rig would then start with no camera at all.
    renderWith(
      statusFixture({
        robot_type: 'edu6_studio',
        cameras: [{ path: '/dev/v4l/by-id/usb-CAM-A', role: 'scene' }],
      })
    );
    await scanThenSave();
    expect(rolesBody().cameras).toEqual([
      { path: '/dev/v4l/by-id/usb-CAM-A', role: 'scene' },
    ]);
  });

  it('drops a role the agent reported that is not one of the two', async () => {
    // The other direction, and it is reachable rather than theoretical:
    // `agent.py::rehydrate_hardware` reads `CAMERA_NAME_i` VERBATIM out of the
    // .env with no validation, so one hand-edited line puts an arbitrary role
    // into /status.cameras, which seeds this map. `handle_cameras_roles` then
    // answers a German 400 („Ungültige Rolle für … (nur Greifer/Szene)") — the
    // exact 400 this filter's own comment, and the handler's docstring, say it
    // exists to keep off an ordinary save. Relaxing the allowlist to a
    // truthiness test would surface it.
    renderWith(
      statusFixture({
        robot_type: 'omx_full',
        cameras: [
          { path: '/dev/v4l/by-id/usb-CAM-A', role: 'wrist' },
          { path: '/dev/v4l/by-id/usb-CAM-B', role: 'scene' },
        ],
      })
    );
    await scanThenSave();
    expect(rolesBody().cameras).toEqual([
      { path: '/dev/v4l/by-id/usb-CAM-B', role: 'scene' },
    ]);
  });

  it('keeps both roles when a two-camera rig assigns both', async () => {
    // Belt against the opposite narrowing (`r === 'scene'`), and against a
    // filter that silently keeps only the first match.
    renderWith(
      statusFixture({
        robot_type: 'omx_full',
        cameras: [
          { path: '/dev/v4l/by-id/usb-CAM-A', role: 'gripper' },
          { path: '/dev/v4l/by-id/usb-CAM-B', role: 'scene' },
        ],
      })
    );
    await scanThenSave();
    expect(rolesBody().cameras).toEqual([
      { path: '/dev/v4l/by-id/usb-CAM-A', role: 'gripper' },
      { path: '/dev/v4l/by-id/usb-CAM-B', role: 'scene' },
    ]);
  });
});
