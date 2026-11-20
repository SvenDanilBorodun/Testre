// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// WP-07 — the Orange Pi System tab's „Turm-Kamera (Turm-Kit)" box and the
// start pre-flight's hint (spec 40 S1, decisions C6 and D32).
//
// The box is the Pi's half of the Windows setup checkbox: it shows the agent's
// /status `scene_camera_profile` (`tower_v1` | `legacy`) and POSTs a change to
// /api/system/scene-camera-profile. It is a maintenance setting, so a STUDENT
// robot session sees it frozen with the page's MAINTENANCE_ONLY_DE (the agent
// answers such a session 403 anyway). A profile without a `scene` camera role
// has no tower camera, so the box is not drawn; nor is it for an agent from
// before the key (graceful degrade: no key, no route). The legacy start's hint
// (`scene_camera_hint`) is shown whenever /status carries it.

import React from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SystemPage, { MAINTENANCE_ONLY_DE } from '../SystemPage';
import {
  resetPiAuthForTests, setPiAccessToken, startPiAuth,
} from '../../features/piAuth/piAuth';

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

const BOX = 'Turm-Kamera (Turm-Kit)';
const HINT = 'Die Szenen-Kamera meldet das Aufnahmeformat (640 × 480 mit 30 Bildern pro '
  + 'Sekunde) nicht. Ist es die Turm-Kamera, bitte in der Einrichtung „Turm-Kamera“ auswählen.';

const PROFILES = [
  { id: 'omx_full', display_de: 'OMX – Voll', scan_requires_leader: true,
    camera_roles: ['gripper', 'scene'] },
  { id: 'v1_studio', display_de: 'V1 – Roboter Studio', scan_requires_leader: false,
    camera_roles: ['scene'] },
  // A profile with no scene camera (none ships today): it has no tower camera.
  { id: 'gripper_only', display_de: 'Nur Greifer', scan_requires_leader: false,
    camera_roles: ['gripper'] },
];

function statusFixture(overrides = {}) {
  return {
    lan_ip: '192.168.1.7',
    hostname: 'edubotics-07',
    agent_ready: true,
    agent_version: '2.41.0',
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
    images: { age_days: null, is_stale: true },
    scene_camera_profile: 'legacy',
    scene_camera_hint: null,
    ...overrides,
  };
}

function renderWith(status) {
  mockPi.agentStatus = status;
  return render(<SystemPage />);
}

let profileResponse;

beforeEach(() => {
  refreshAgentStatus.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  profileResponse = (body) => jsonRes({
    ok: true, scene_camera_profile: body.profile, message: 'Turm-Kamera gespeichert.',
  });
  mockPi = {
    piMode: true,
    piModeResolved: true,
    agentReachable: true,
    refreshAgentStatus,
    agentStatus: statusFixture(),
  };
  global.fetch = vi.fn((url, opts) => {
    const u = String(url);
    if (u.includes('/scene-camera-profile')) return profileResponse(JSON.parse(opts.body));
    if (u.includes('/cameras/preview/stop')) return jsonRes({ ok: true });
    return jsonRes({});
  });
});

afterEach(() => {
  cleanup();
  resetPiAuthForTests();
});

function profilePosts() {
  return global.fetch.mock.calls.filter((c) => String(c[0]).includes('/scene-camera-profile'));
}

describe('SystemPage — „Turm-Kamera (Turm-Kit)"', () => {
  it('reflects the status: unticked for legacy, ticked for tower_v1', () => {
    renderWith(statusFixture({ scene_camera_profile: 'legacy' }));
    expect(screen.getByLabelText(BOX, { exact: false })).not.toBeChecked();
    cleanup();
    renderWith(statusFixture({ scene_camera_profile: 'tower_v1' }));
    expect(screen.getByLabelText(BOX, { exact: false })).toBeChecked();
  });

  it('POSTs tower_v1 when ticked and refreshes the status', async () => {
    renderWith(statusFixture({ scene_camera_profile: 'legacy' }));
    const before = refreshAgentStatus.mock.calls.length;
    await userEvent.click(screen.getByLabelText(BOX, { exact: false }));
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith('Turm-Kamera gespeichert.'));
    const posts = profilePosts();
    expect(posts).toHaveLength(1);
    expect(String(posts[0][0])).toBe('/api/system/scene-camera-profile');
    expect(posts[0][1].method).toBe('POST');
    expect(JSON.parse(posts[0][1].body)).toEqual({ profile: 'tower_v1' });
    expect(refreshAgentStatus.mock.calls.length).toBeGreaterThan(before);
  });

  it('POSTs legacy when cleared', async () => {
    renderWith(statusFixture({ scene_camera_profile: 'tower_v1' }));
    await userEvent.click(screen.getByLabelText(BOX, { exact: false }));
    await waitFor(() => expect(profilePosts()).toHaveLength(1));
    expect(JSON.parse(profilePosts()[0][1].body)).toEqual({ profile: 'legacy' });
  });

  it('shows the agent refusal and falls back to the status', async () => {
    profileResponse = () => jsonRes({ ok: false, message: 'Ein anderer Vorgang läuft gerade.' },
      false, 503);
    renderWith(statusFixture({ scene_camera_profile: 'legacy' }));
    await userEvent.click(screen.getByLabelText(BOX, { exact: false }));
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(
      'Ein anderer Vorgang läuft gerade.'));
    await waitFor(() => expect(screen.getByLabelText(BOX, { exact: false })).not.toBeChecked());
  });

  it('is frozen for a student robot session, and says why', async () => {
    const agent = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, auth: 1, claimed: true, session: true, role: 'student',
        expires_in: 900, pi_id: 'q8V0f2Lr3nC1sZtA9xY7wB' }),
    }));
    setPiAccessToken('jwt-s');
    startPiAuth({ fetch: agent, cloudApiUrl: 'https://cloud.test' });
    renderWith(statusFixture({ scene_camera_profile: 'tower_v1' }));
    const box = screen.getByLabelText(BOX, { exact: false });
    await waitFor(() => expect(box).toBeDisabled());
    expect(box).toBeChecked();
    expect(box).toHaveAttribute('title', MAINTENANCE_ONLY_DE);
    expect(screen.getByTestId('maintenance-only-scene-camera'))
      .toHaveTextContent(MAINTENANCE_ONLY_DE);
    await userEvent.click(box);
    expect(profilePosts()).toHaveLength(0);
  });

  it('is open to a teacher session', async () => {
    const agent = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, auth: 1, claimed: true, session: true, role: 'teacher',
        expires_in: 900, pi_id: 'q8V0f2Lr3nC1sZtA9xY7wB' }),
    }));
    setPiAccessToken('jwt-t');
    startPiAuth({ fetch: agent, cloudApiUrl: 'https://cloud.test' });
    renderWith(statusFixture());
    await waitFor(() => expect(agent).toHaveBeenCalled());
    expect(screen.getByLabelText(BOX, { exact: false })).toBeEnabled();
    expect(screen.queryByTestId('maintenance-only-scene-camera')).toBeNull();
  });

  it('is absent for a profile without a scene camera role', () => {
    renderWith(statusFixture({ robot_type: 'gripper_only' }));
    expect(screen.queryByLabelText(BOX, { exact: false })).toBeNull();
  });

  it('is present for the V1 (a scene camera only)', () => {
    renderWith(statusFixture({ robot_type: 'v1_studio' }));
    expect(screen.getByLabelText(BOX, { exact: false })).toBeInTheDocument();
  });

  it('is absent for an agent from before the key', () => {
    const status = statusFixture();
    delete status.scene_camera_profile;
    delete status.scene_camera_hint;
    renderWith(status);
    expect(screen.queryByLabelText(BOX, { exact: false })).toBeNull();
    expect(screen.getAllByRole('checkbox')).toHaveLength(1);
  });

  it('is absent in Cloud-Modus (no cameras)', async () => {
    renderWith(statusFixture());
    await userEvent.click(screen.getByLabelText('Cloud-Modus (ohne Roboter)', { exact: false }));
    expect(screen.queryByLabelText(BOX, { exact: false })).toBeNull();
  });
});

describe('SystemPage — the scene camera hint', () => {
  it('shows the hint when the status carries it', () => {
    renderWith(statusFixture({ scene_camera_hint: HINT }));
    expect(screen.getByText(HINT)).toBeInTheDocument();
  });

  it('shows no hint without one', () => {
    for (const hint of [null, undefined, '', 7]) {
      renderWith(statusFixture({ scene_camera_hint: hint }));
      expect(screen.queryByTestId('scene-camera-hint')).toBeNull();
      cleanup();
    }
  });
});
