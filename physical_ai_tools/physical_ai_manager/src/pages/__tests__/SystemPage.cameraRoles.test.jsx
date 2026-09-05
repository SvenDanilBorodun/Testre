// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The camera-role seam: which roles Schritt C OFFERS, and which it SENDS.
//
// The defect this fences: `edu6_studio` declares exactly ONE camera topic
// server-side (`scene:/scene/image_raw/compressed`), but this page rendered a
// hardcoded gripper/scene pair on every profile, so a student could name the
// only camera „Greifer". That publishes /gripper/image_raw/compressed, which
// nothing subscribes to — while the opi compose healthcheck greps the topic the
// student just NAMED, so the container goes healthy, „Umgebung starten" reports
// success in German, and the failure surfaces later as an empty Roboter Studio.
//
// The fix is deliberately redundant: the agent refuses the role with a German
// 400 AND the wizard stops offering it. This file covers the wizard half. Three
// properties carry the weight:
//   1. BOTH OMX profiles are behaviourally unchanged (they allow both roles),
//   2. an agent that sends no `camera_roles` degrades to exactly that — never
//      to an empty <select>, because on a Pi this page is the only repair
//      surface there is, and
//   3. the payload can never contain a role the profile does not allow.

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

// Verbatim from pi_agent/constants.py::ROBOT_PROFILES, camera_roles ORDER
// included. omx_follower's ('scene', 'gripper') is not a typo — the tuple is
// ordered for the WINDOWS lone-camera default (`camera_roles[0]`), and the Pi
// tests membership only.
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
  {
    id: 'edu1_studio',
    display_de: 'Edu:1 – Roboter Studio',
    scan_requires_leader: false,
    camera_roles: ['scene'],
  },
];

// Derived, never restated: the degrade sweep below was a hardcoded triple and
// stopped covering `edu1_studio` the day it was added, silently.
const ALL_PROFILE_IDS = PROFILES.map((p) => [p.id]);

const CAM = { path: '/dev/video0', name: 'Innomaker' };
const CAM2 = { path: '/dev/video2', name: 'Innomaker 2' };

function statusFixture(overrides = {}) {
  return {
    lan_ip: '192.168.1.7',
    hostname: 'edubotics-07',
    agent_ready: true,
    agent_version: '2.13.0',
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

let scannedCameras;
let rolesPosts;

beforeEach(() => {
  refreshAgentStatus.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  scannedCameras = [CAM];
  rolesPosts = [];
  mockPi = {
    piMode: true,
    piModeResolved: true,
    agentReachable: true,
    refreshAgentStatus,
    agentStatus: statusFixture(),
  };
  global.fetch = vi.fn((url, opts) => {
    const u = String(url);
    if (u.includes('/cameras/scan')) return jsonRes({ ok: true, cameras: scannedCameras });
    if (u.includes('/cameras/preview/stop')) return jsonRes({ ok: true });
    if (u.includes('/cameras/roles')) {
      rolesPosts.push(JSON.parse(opts.body));
      return jsonRes({ ok: true, cameras: [], message: 'ok' });
    }
    return jsonRes({});
  });
});

afterEach(cleanup);

// Render, then run the „Kameras suchen" round-trip so the role selects exist.
async function renderWithCameras(status) {
  mockPi.agentStatus = status;
  render(<SystemPage />);
  await scanCameras();
}

// user-event v13 has no `.setup()` — the module-level API is the whole API.
async function scanCameras() {
  await userEvent.click(screen.getByRole('button', { name: 'Kameras suchen' }));
  await screen.findByLabelText(`Rolle für ${CAM.name}`);
}

function roleOptions(camName = CAM.name) {
  const sel = screen.getByLabelText(`Rolle für ${camName}`);
  return within(sel)
    .getAllByRole('option')
    .map((o) => o.textContent);
}

// ── the behavioural delta: BOTH Feetech arms ────────────────────────────────

describe('Schritt C offers only the roles the profile declares', () => {
  it.each([['edu6_studio'], ['edu1_studio']])(
    'offers „Szene" alone on %s — never „Greifer"',
    async (id) => {
      await renderWithCameras(statusFixture({ robot_type: id }));
      expect(roleOptions()).toEqual(['— Rolle —', 'Szene']);
    }
  );

  it('cannot POST „gripper" on edu6_studio even if the state still holds it', async () => {
    // The reachable sequence: assign Greifer on omx_full, THEN switch type.
    // `roles` is component state and survives the switch; the filter is what
    // stops the stale value reaching the agent.
    await renderWithCameras(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText(`Rolle für ${CAM.name}`), 'gripper');

    mockPi.agentStatus = statusFixture({ robot_type: 'edu6_studio' });
    await scanCameras();

    await userEvent.click(screen.getByRole('button', { name: 'Zuordnung speichern' }));
    await waitFor(() => expect(rolesPosts).toHaveLength(1));
    expect(rolesPosts[0]).toEqual({ cameras: [] });
  });

  it('shows a role the profile lost as UNASSIGNED, not as a blank box', async () => {
    // Pins the END STATE, and deliberately does NOT credit our code for it:
    // the <select> keeps `value="gripper"` with no matching <option>, and
    // HTML's selectedness algorithm selects the first one instead. MEASURED in
    // this jsdom — a normalising `value=` expression is indistinguishable
    // (both give value '' / selectedIndex 0), so adding one would be an
    // unfenceable no-op. Recorded here so nobody adds it believing otherwise.
    await renderWithCameras(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText(`Rolle für ${CAM.name}`), 'gripper');
    expect(screen.getByLabelText(`Rolle für ${CAM.name}`)).toHaveValue('gripper');

    mockPi.agentStatus = statusFixture({ robot_type: 'edu6_studio' });
    await scanCameras();
    const sel = screen.getByLabelText(`Rolle für ${CAM.name}`);
    await waitFor(() => expect(sel).toHaveValue(''));
    expect(sel.selectedIndex).toBe(0);
    expect(sel.options[0].textContent).toBe('— Rolle —');
  });
});

// ── property 1: both OMX profiles are unchanged ─────────────────────────────

describe('an OMX Pi renders and posts exactly as it did', () => {
  it.each([['omx_full'], ['omx_follower']])('offers both roles on %s', async (id) => {
    await renderWithCameras(statusFixture({ robot_type: id }));
    expect(roleOptions()).toEqual(['— Rolle —', 'Greifer', 'Szene']);
  });

  it('keeps Greifer before Szene on omx_follower, whose wire order is reversed', async () => {
    // The registry sends ['scene', 'gripper'] here. Rendering in wire order
    // would silently flip the dropdown between two profiles offering the same
    // two roles — the order belongs to the SPA, the membership to the agent.
    const p = PROFILES.find((x) => x.id === 'omx_follower');
    expect(p.camera_roles).toEqual(['scene', 'gripper']);
    await renderWithCameras(statusFixture({ robot_type: 'omx_follower' }));
    expect(roleOptions()).toEqual(['— Rolle —', 'Greifer', 'Szene']);
  });

  it.each([['omx_full'], ['omx_follower']])(
    'posts both assigned roles verbatim on %s',
    async (id) => {
      scannedCameras = [CAM, CAM2];
      await renderWithCameras(statusFixture({ robot_type: id }));
      await userEvent.selectOptions(screen.getByLabelText(`Rolle für ${CAM.name}`), 'gripper');
      await userEvent.selectOptions(screen.getByLabelText(`Rolle für ${CAM2.name}`), 'scene');
      await userEvent.click(screen.getByRole('button', { name: 'Zuordnung speichern' }));
      await waitFor(() => expect(rolesPosts).toHaveLength(1));
      expect(rolesPosts[0]).toEqual({
        cameras: [
          { path: CAM.path, role: 'gripper' },
          { path: CAM2.path, role: 'scene' },
        ],
      });
    }
  );

  it('still drops an UNASSIGNED camera, on every profile', async () => {
    scannedCameras = [CAM, CAM2];
    await renderWithCameras(statusFixture());
    await userEvent.selectOptions(screen.getByLabelText(`Rolle für ${CAM2.name}`), 'scene');
    await userEvent.click(screen.getByRole('button', { name: 'Zuordnung speichern' }));
    await waitFor(() => expect(rolesPosts).toHaveLength(1));
    expect(rolesPosts[0]).toEqual({ cameras: [{ path: CAM2.path, role: 'scene' }] });
  });
});

// ── property 2: an older agent degrades to the old dropdown ─────────────────

describe('an agent that sends no camera_roles degrades to both roles', () => {
  const stripped = PROFILES.map(({ camera_roles, ...rest }) => rest); // eslint-disable-line no-unused-vars

  it.each(ALL_PROFILE_IDS)(
    'offers both roles on %s when the key is absent entirely',
    async (id) => {
      await renderWithCameras(
        statusFixture({ robot_type: id, robot_profiles: stripped })
      );
      expect(roleOptions()).toEqual(['— Rolle —', 'Greifer', 'Szene']);
    }
  );

  it('offers both roles when the whole profile list is missing', async () => {
    await renderWithCameras(statusFixture({ robot_profiles: undefined }));
    expect(roleOptions()).toEqual(['— Rolle —', 'Greifer', 'Szene']);
  });

  it('offers both roles when robot_type names no known profile', async () => {
    await renderWithCameras(statusFixture({ robot_type: 'something_new' }));
    expect(roleOptions()).toEqual(['— Rolle —', 'Greifer', 'Szene']);
  });

  it.each([
    ['a string instead of a list', 'scene'],
    ['an empty list', []],
    ['null', null],
    ['a list of unknown roles only', ['phone', 'wrist']],
    ['a list of non-strings', [1, {}, null]],
    ['a nested list', [['scene']]],
  ])('never renders an empty <select> — %s', async (_label, roles) => {
    await renderWithCameras(
      statusFixture({
        robot_type: 'edu6_studio',
        robot_profiles: PROFILES.map((p) =>
          p.id === 'edu6_studio' ? { ...p, camera_roles: roles } : p
        ),
      })
    );
    expect(roleOptions()).toEqual(['— Rolle —', 'Greifer', 'Szene']);
  });

  it('honours a PARTIALLY valid list — the known roles only', async () => {
    await renderWithCameras(
      statusFixture({
        robot_type: 'edu6_studio',
        robot_profiles: PROFILES.map((p) =>
          p.id === 'edu6_studio' ? { ...p, camera_roles: ['scene', 'phone', 7] } : p
        ),
      })
    );
    expect(roleOptions()).toEqual(['— Rolle —', 'Szene']);
  });
});

// ── property 3: the payload can only ever carry allowed roles ───────────────

describe('the POSTed payload is filtered by the same allowlist that is rendered', () => {
  // The closing of the loop: whatever the dropdown ended up OFFERING, picking
  // it and saving must produce a role the registry declares. Nothing here
  // names a role literally — the assertion is derived from the same fixture
  // the agent's own registry is copied from, so it moves with a new profile.
  it.each(PROFILES.map((p) => [p.id, p.camera_roles]))(
    'sends only a declared role on %s',
    async (id, declared) => {
      await renderWithCameras(statusFixture({ robot_type: id }));
      const offered = roleOptions().filter((t) => t !== '— Rolle —');
      expect(offered.length).toBeGreaterThan(0);
      const role = offered[0] === 'Greifer' ? 'gripper' : 'scene';
      await userEvent.selectOptions(screen.getByLabelText(`Rolle für ${CAM.name}`), role);
      await userEvent.click(screen.getByRole('button', { name: 'Zuordnung speichern' }));
      await waitFor(() => expect(rolesPosts).toHaveLength(1));
      expect(declared).toContain(rolesPosts[0].cameras[0].role);
    }
  );
});
