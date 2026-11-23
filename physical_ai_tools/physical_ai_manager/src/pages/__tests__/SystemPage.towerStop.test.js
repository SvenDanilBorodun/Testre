// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// „Stoppen" on the Orange Pi's System tab and where a limp V1 falls (spec 40
// WP-HINT, 52 §5.3 (c) and §6; decision D56 (c)).
//
// Stopping the robot tier takes the torque off the arm. On the V1 under the
// camera tower the driver says where the arm would fall from its live pose
// (/edubotics/tower_state `fall`). Unless that is `forward`, the page asks
// first — „Abbrechen" or „Trotzdem stoppen" — BEFORE POST /environment/stop;
// no answer from the robot is `unknown` and asks too (52 §6.3). The dialog
// never blocks a stop. An OMX, or a V1 with the legacy camera, stops as today.

import React, { Profiler } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';

import SystemPage from '../SystemPage';
import { resetPiAuthForTests } from '../../features/piAuth/piAuth';

const TOWER_HINT_STOP_DE = 'Beim Stoppen wird der Arm weich. Aus dieser Lage würde er nach hinten '
  + 'zum Kameraturm fallen. Fahre ihn zuerst in die Grundstellung (Roboter Studio → Steuern → '
  + 'Grundstellung) oder halte ihn beim Stoppen fest.';
const TOWER_STATE_TOPIC = '/edubotics/tower_state';
const SCENE_CAMERA_PROFILE_TOPIC = '/edubotics/scene_camera_profile';

const refreshAgentStatus = vi.fn(() => Promise.resolve(null));
let mockPi;
vi.mock('../../utils/piMode', async () => {
  const actual = await vi.importActual('../../utils/piMode');
  return { ...actual, __esModule: true, usePiMode: () => mockPi };
});

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

vi.mock('../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { getConnection: vi.fn(() => Promise.resolve({ isConnected: true })) },
}));

const mockTopics = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.opts = opts;
      this.cb = null;
      this.unsubscribed = false;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => { this.unsubscribed = true; };
      mockTopics.push(this);
    },
  },
}));

const PROFILES = [
  { id: 'omx_full', display_de: 'OMX – Voll', scan_requires_leader: true, camera_roles: ['gripper', 'scene'] },
  { id: 'v1_studio', display_de: 'V1 – Roboter Studio', scan_requires_leader: false, camera_roles: ['scene'] },
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
    robot_tier_up: true,
    arms_identified: { leader: null, follower: '/dev/serial/by-id/x', both: false },
    robot_type: 'v1_studio',
    robot_profiles: PROFILES,
    hardware_ready: true,
    cameras: [],
    follower_only: true,
    images: { age_days: 1, is_stale: false },
    scene_camera_profile: 'tower_v1',
    scene_camera_hint: null,
    ...overrides,
  };
}

function jsonRes(body, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
}

function makeStore() {
  return configureStore({
    reducer: { ros: (state = { rosbridgeUrl: 'ws://localhost/rosbridge', rosHost: '', imageTopicList: [] }) => state },
  });
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve(); // eslint-disable-line no-await-in-loop
  });
}

async function renderWith(status) {
  mockPi.agentStatus = status;
  const view = render(<Provider store={makeStore()}><SystemPage /></Provider>);
  await flush();
  return view;
}

const towerTopics = () => mockTopics.filter((t) => t.opts.name === TOWER_STATE_TOPIC);
async function deliverFall(fall, glide = 'clear') {
  const live = towerTopics().filter((t) => !t.unsubscribed);
  expect(live).toHaveLength(1);
  await act(async () => { live[0].cb({ data: JSON.stringify({ v: 1, glide, fall, part: null, gen: 1 }) }); });
}

const statementTopics = () => mockTopics.filter((t) => t.opts.name === SCENE_CAMERA_PROFILE_TOPIC);
async function deliverStatement(profile) {
  const live = statementTopics().filter((t) => !t.unsubscribed);
  expect(live).toHaveLength(1);
  await act(async () => { live[0].cb({ data: JSON.stringify({ v: 1, profile }) }); });
}

const stopPosts = () => global.fetch.mock.calls.filter((c) => String(c[0]).includes('/environment/stop'));
const stopButton = () => screen.getByRole('button', { name: 'Stoppen' });

beforeEach(() => {
  mockTopics.length = 0;
  refreshAgentStatus.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  mockPi = {
    piMode: true,
    piModeResolved: true,
    agentReachable: true,
    refreshAgentStatus,
    agentStatus: statusFixture(),
  };
  global.fetch = vi.fn((url) => {
    const u = String(url);
    if (u.includes('/environment/stop')) return jsonRes({ ok: true, message: 'Roboter-Umgebung gestoppt.' });
    return jsonRes({});
  });
});

afterEach(() => {
  cleanup();
  resetPiAuthForTests();
});

describe('SystemPage — „Stoppen" on the V1 under the camera tower', () => {
  it.each(['back', 'unknown'])('fall %s: the dialog comes BEFORE POST /environment/stop', async (fall) => {
    await renderWith(statusFixture());
    await deliverFall(fall);
    await userEvent.click(stopButton());
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(screen.getByText(TOWER_HINT_STOP_DE)).toBeInTheDocument();
    // The limp-fall animation, rendered from the real V1 URDF.
    const pic = document.querySelector('[data-tower-hint="fall"]'); // eslint-disable-line testing-library/no-node-access
    expect(pic).not.toBeNull();
    expect(stopPosts()).toHaveLength(0);
  });

  it('„Abbrechen" stops nothing; „Trotzdem stoppen" stops', async () => {
    await renderWith(statusFixture());
    await deliverFall('back');
    await userEvent.click(stopButton());
    await userEvent.click(screen.getByRole('button', { name: 'Abbrechen' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(stopPosts()).toHaveLength(0);
    await userEvent.click(stopButton());
    await userEvent.click(screen.getByRole('button', { name: 'Trotzdem stoppen' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await flush();
    expect(stopPosts()).toHaveLength(1);
    expect(stopPosts()[0][1].method).toBe('POST');
    expect(mockToast.success).toHaveBeenCalledWith('Roboter-Umgebung gestoppt.');
  });

  it('focus starts on „Abbrechen" — a stray Enter never takes the torque off', async () => {
    await renderWith(statusFixture());
    await deliverFall('back');
    await userEvent.click(stopButton());
    expect(screen.getByRole('button', { name: 'Abbrechen' })).toHaveFocus();
  });

  it('no answer from the robot (no session, no topic) is unknown: the dialog', async () => {
    await renderWith(statusFixture());
    await userEvent.click(stopButton());
    expect(stopPosts()).toHaveLength(0);
    expect(screen.getByText(TOWER_HINT_STOP_DE)).toBeInTheDocument();
    expect(towerTopics()).toHaveLength(1);
  });

  it('fall forward: the stop runs as today, no dialog', async () => {
    await renderWith(statusFixture());
    await deliverFall('forward');
    await userEvent.click(stopButton());
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await flush();
    expect(stopPosts()).toHaveLength(1);
  });
});

describe('SystemPage — the 2 Hz verdict (S-1)', () => {
  it('a tower_state re-sent at 2 Hz commits nothing; a new fall does', async () => {
    mockPi.agentStatus = statusFixture();
    let commits = 0;
    render(
      <Provider store={makeStore()}>
        <Profiler id="system" onRender={() => { commits += 1; }}><SystemPage /></Profiler>
      </Provider>,
    );
    await flush();
    const live = towerTopics().filter((t) => !t.unsubscribed);
    expect(live).toHaveLength(1);
    const send = (fall, gen) => act(async () => {
      live[0].cb({ data: JSON.stringify({ v: 1, glide: 'clear', fall, part: null, gen }) });
    });
    await send('forward', 0);
    const before = commits;
    for (let gen = 1; gen <= 30; gen += 1) await send('forward', gen); // eslint-disable-line no-await-in-loop
    expect(commits).toBe(before);
    await send('back', 31);
    expect(commits).toBeGreaterThan(before);
  });
});

describe('SystemPage — the RUNNING tier decides, not the next start\'s profile (X-C5)', () => {
  // The agent's `scene_camera_profile` is the stored .env value: „Gilt ab dem nächsten „Umgebung starten“", so
  // a teacher may untick or tick the box while a tier runs. The dialog follows what runs: a tower_state message
  // (the V1 driver publishes it only under `tower_v1`), else the arm container's statement of its profile.
  it('a tower tier whose box was unticked since its start: fall back asks (C-5\'s repro)', async () => {
    await renderWith(statusFixture({ scene_camera_profile: 'legacy' }));
    await deliverFall('back');
    await userEvent.click(stopButton());
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(screen.getByText(TOWER_HINT_STOP_DE)).toBeInTheDocument();
    expect(stopPosts()).toHaveLength(0);
  });

  it('a running tier that states tower_v1, no fall yet: unknown asks', async () => {
    await renderWith(statusFixture({ scene_camera_profile: 'legacy' }));
    await deliverStatement('tower_v1');
    await userEvent.click(stopButton());
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(stopPosts()).toHaveLength(0);
  });

  it('a running tower tier whose limp arm falls forward stops at once', async () => {
    await renderWith(statusFixture({ scene_camera_profile: 'legacy' }));
    await deliverStatement('tower_v1');
    await deliverFall('forward');
    await userEvent.click(stopButton());
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await flush();
    expect(stopPosts()).toHaveLength(1);
  });

  it('a running tier that states legacy never asks, though the box was ticked since its start', async () => {
    await renderWith(statusFixture({ scene_camera_profile: 'tower_v1' }));
    await deliverStatement('legacy');
    await userEvent.click(stopButton());
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await flush();
    expect(stopPosts()).toHaveLength(1);
  });

  it('a running tier that has said nothing yet: the stored profile decides, as before', async () => {
    await renderWith(statusFixture({ scene_camera_profile: 'legacy' }));
    expect(statementTopics()).toHaveLength(1);
    expect(towerTopics()).toHaveLength(1);
    await userEvent.click(stopButton());
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await flush();
    expect(stopPosts()).toHaveLength(1);
  });
});

describe('SystemPage — every other rig stops as today', () => {
  it.each([
    ['OMX under the tower', { robot_type: 'omx_full', follower_only: false }],
    ['an agent without the scene-camera key', { scene_camera_profile: undefined }],
  ])('%s: no dialog, and nothing subscribes to the tower', async (_label, overrides) => {
    await renderWith(statusFixture(overrides));
    expect(towerTopics()).toHaveLength(0);
    expect(statementTopics()).toHaveLength(0);
    await userEvent.click(stopButton());
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await flush();
    expect(stopPosts()).toHaveLength(1);
  });

  it('the robot tier down (nothing is energised): nothing subscribes to the tower', async () => {
    await renderWith(statusFixture({ robot_tier_up: false }));
    expect(towerTopics()).toHaveLength(0);
    expect(statementTopics()).toHaveLength(0);
  });
});
