// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Start page's contract, after the „Lagebericht" rebuild.
//
// WHAT HAPPENED TO THE OLD SUITE. It pinned the hero's „Aufnahme starten"
// button against `capabilities.recordable`, so that a student on a
// follower-only rig could not slip into the (hidden) Aufnahme tab through it.
// That button no longer exists: the page reports and does not dispatch, and
// navigation is the rail's job. The invariant is NOT dropped — it is now
// satisfied structurally, and the first block below asserts exactly that, on
// the same four capability fixtures the old suite used. The tab-level gate it
// backed up lives in utils/navGating (`isCapabilityVisible`) with its own
// tests.
//
// The rest of the file pins the reason the page was rebuilt: every value is
// real or absent. Unknown renders „—", never 0, never green, and never the
// SPA's own package version dressed up as the product's.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';

import tasksReducer, { setTaskStatus, setHeartbeatStatus } from '../../features/tasks/taskSlice';
import authReducer, { setProfile } from '../../features/auth/authSlice';
import rosReducer from '../../features/ros/rosSlice';
import uiReducer from '../../features/ui/uiSlice';
import HomePage from '../HomePage';

// three.js has no WebGL in jsdom, and the twin has its own suite. The hero
// lazy-loads it, so this stub also keeps the lazy boundary honest.
vi.mock('../../components/UrdfTwin', () => ({
  __esModule: true,
  default: () => <div data-testid="urdf-twin" />,
}));

// The joint-liveness probe is a rosbridge subscription with its own contract;
// HomePage's job is to own it once and hand the same value to both surfaces.
let mockJointsLive = null;
vi.mock('../../hooks/useJointLiveness', () => ({
  __esModule: true,
  default: () => mockJointsLive,
}));

// The two ROS calls the Health-Check makes. Both read a value that ONLY
// another page populates in Redux, so the card asks for itself — see the note
// at the top of HealthCard.jsx.
let mockCalibrationResult = null;
let mockCalibrationThrows = false;
let mockTopicListResult = null;
// NAMED export, mirroring the real module. A `default:` mock here compiled
// fine under vitest and let a broken production import ship — the bundler
// caught it, the suite did not. Mock the shape that exists.
vi.mock('../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({
    getCalibrationStatus: () => (mockCalibrationThrows
      ? Promise.reject(new Error('kein Dienst'))
      : Promise.resolve(mockCalibrationResult)),
    getImageTopicList: () => (mockTopicListResult
      ? Promise.resolve(mockTopicListResult)
      : Promise.reject(new Error('kein Dienst'))),
  }),
}));

// Cloud reads for „Deine Arbeit". Default: everything fails, which is the
// state that must render „—" rather than „0".
let mockDatasets = null;
let mockTrainings = null;
let mockWorkflows = null;
let mockQuota = null;
const reject = () => Promise.reject(new Error('offline'));
vi.mock('../../services/datasetsApi', () => ({
  __esModule: true,
  listDatasets: () => (mockDatasets ? Promise.resolve(mockDatasets) : reject()),
}));
vi.mock('../../services/workflowApi', () => ({
  __esModule: true,
  listWorkflows: () => (mockWorkflows ? Promise.resolve(mockWorkflows) : reject()),
}));
vi.mock('../../services/cloudTrainingApi', () => ({
  __esModule: true,
  getTrainingJobs: () => (mockTrainings ? Promise.resolve(mockTrainings) : reject()),
  getQuota: () => (mockQuota ? Promise.resolve(mockQuota) : reject()),
}));

function makeStore() {
  return configureStore({
    reducer: { tasks: tasksReducer, auth: authReducer, ros: rosReducer, ui: uiReducer },
  });
}

// setTaskStatus adopts only COMPLETE six-boolean manifests, so fixtures must
// ship the whole server-contract key set — an incomplete one is IGNORED and
// the page behaves as if capabilities were unknown.
function fullCaps(overrides = {}) {
  return {
    recordable: true, editable: true, trainable: true,
    inferable: true, roboter_studio: true, has_leader: true,
    ...overrides,
  };
}

function renderHome({ caps = fullCaps(), connected = true, status = {}, profile = null } = {}) {
  const store = makeStore();
  store.dispatch(setHeartbeatStatus(connected ? 'connected' : 'disconnected'));
  store.dispatch(setTaskStatus({ robotType: 'omx_f', capabilities: caps, ...status }));
  if (profile) store.dispatch(setProfile(profile));
  render(<Provider store={store}><HomePage /></Provider>);
  return store;
}

beforeEach(() => {
  mockJointsLive = null;
  mockCalibrationResult = null;
  mockCalibrationThrows = false;
  mockTopicListResult = null;
  mockDatasets = null;
  mockTrainings = null;
  mockWorkflows = null;
  mockQuota = null;
  try { localStorage.clear(); } catch { /* jsdom always has it */ }
});

// ── the old suite's invariant, kept ────────────────────────────────────────

describe('HomePage — no recording entry point, on any profile', () => {
  const cases = [
    ['omx_full (recordable true)', fullCaps()],
    ['omx_follower (recordable false)', fullCaps({ recordable: false, has_leader: false })],
    ['unknown capabilities (null)', null],
    ['a PARTIAL manifest, which is never adopted', { recordable: false }],
  ];

  it.each(cases)('offers no „Aufnahme starten" control on %s', (_label, caps) => {
    renderHome({ caps });
    // The gate the old suite protected is now structural: there is nothing to
    // gate. A future hero CTA would fail here and would have to be re-argued
    // against `capabilities.recordable`, which is the point of keeping this.
    expect(screen.queryByRole('button', { name: /Aufnahme starten/ })).toBeNull();
    expect(screen.queryByText(/Aufnahme starten/)).toBeNull();
  });
});

// ── identity ───────────────────────────────────────────────────────────────

describe('HomePage — the robot names itself in German', () => {
  it('shows display_de from the capability manifest', () => {
    renderHome({ caps: fullCaps({ display_de: 'Edu:1 – Roboter Studio' }) });
    expect(screen.getByText('Edu:1 – Roboter Studio')).toBeInTheDocument();
  });

  it('renders help_de under the name when the server sent one', () => {
    renderHome({
      caps: fullCaps({
        display_de: 'OMX – Voll',
        help_de: 'Beide Arme: mit dem Leader-Arm führst du.',
      }),
    });
    expect(screen.getByText(/Beide Arme: mit dem Leader-Arm führst du\./))
      .toBeInTheDocument();
  });

  it('falls back to the profile id on an older server image', () => {
    // Not pretty, but true — and it is what the page showed before the key
    // existed, so an un-updated rig is no worse off.
    renderHome({ caps: fullCaps(), status: { robotProfile: 'edu6_studio' } });
    // Twice on purpose in this fallback case: once as the hero's name, once in
    // the footer, where the profile id is a permanent diagnostic a support call
    // can quote. With `display_de` present the two differ, which is the point.
    expect(screen.getAllByText('edu6_studio').length).toBeGreaterThan(0);
  });

  it('says the robot is being detected rather than showing an empty heading', () => {
    const store = makeStore();
    store.dispatch(setHeartbeatStatus('connected'));
    render(<Provider store={store}><HomePage /></Provider>);
    expect(screen.getByText('Roboter wird erkannt …')).toBeInTheDocument();
  });
});

// ── the version that used to be wrong ──────────────────────────────────────

describe('HomePage — version', () => {
  it('never prints the SPA package version as the product version', () => {
    // The old page rendered „EduBotics v0.9.0" twice while the product was at
    // 2.17.0. Without a `?_v=` release the honest answer is to say nothing.
    renderHome({});
    expect(screen.queryByText(/0\.9\.0/)).toBeNull();
    expect(screen.queryByText(/EduBotics v/)).toBeNull();
  });
});

// ── health ─────────────────────────────────────────────────────────────────

describe('HomePage — Roboter-Zustand', () => {
  it('is „Nicht bereit" with a German remedy when the bridge is down', async () => {
    renderHome({ connected: false });
    expect(screen.getByText('Nicht bereit')).toBeInTheDocument();
    expect(screen.getByText('Kein Kontakt zum Roboter')).toBeInTheDocument();
    expect(screen.getByText(/Umgebung starten/)).toBeInTheDocument();
  });

  it('is „Alles bereit" only once every row has actually answered', async () => {
    mockJointsLive = true;
    mockCalibrationResult = {
      has_scene_intrinsics: true, has_scene_handeye: true, has_table_plane: true,
    };
    mockTopicListResult = {
      success: true,
      image_topic_list: ['/gripper/image_raw/compressed', '/scene/image_raw/compressed'],
    };
    renderHome({ caps: fullCaps({ camera_roles: ['gripper', 'scene'] }) });

    expect(await screen.findByText('Alles bereit')).toBeInTheDocument();
    expect(screen.getByText('2 von 2')).toBeInTheDocument();
    expect(screen.getByText('vollständig')).toBeInTheDocument();
  });

  it('shows „—" for cameras until the service answers, never „keine erkannt"', async () => {
    // `state.ros.imageTopicList` is populated ONLY by ImageGrid on the Aufnahme
    // tab and starts as [], which is indistinguishable from "asked, and there
    // are none". Reading it here reported „keine erkannt" on every fresh
    // session — a claim about the rig from an initial value.
    mockJointsLive = true;
    renderHome({});
    await screen.findByText('Kameras');
    const row = screen.getAllByRole('listitem')
      .find((li) => li.textContent.includes('Kameras'));
    expect(row).toHaveTextContent('—');
    expect(screen.queryByText('keine erkannt')).toBeNull();
  });

  it('shows „—" for calibration while /calibration/status has not answered', async () => {
    // The bug this exists for: the Redux calibration flags are hydrated only by
    // CalibrationWizard, so reading them here would report „0 von 3" about a
    // rig nobody asked.
    mockJointsLive = true;
    mockCalibrationThrows = true;
    renderHome({});
    await screen.findByText('Kalibrierung');
    const row = screen.getAllByRole('listitem')
      .find((li) => li.textContent.includes('Kalibrierung'));
    expect(row).toHaveTextContent('—');
    expect(screen.queryByText('0 von 3')).toBeNull();
  });

  it('surfaces a server [FEHLER], which the page never used to render', async () => {
    renderHome({ status: { error: '[FEHLER] Kamera nicht gefunden' } });
    expect(await screen.findByText('Der Roboter meldet einen Fehler')).toBeInTheDocument();
    // The severity prefix drives the dot; the student reads the sentence.
    expect(screen.getByText('Kamera nicht gefunden')).toBeInTheDocument();
  });
});

// ── work ───────────────────────────────────────────────────────────────────

describe('HomePage — Deine Arbeit', () => {
  it('renders „—" when the cloud could not be asked, never „0"', async () => {
    renderHome({});
    await screen.findByText('Aufnahmen');
    // A network failure is not an empty portfolio, and the empty-state sentence
    // must not fire on unknown counts either.
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThan(0);
    expect(screen.queryByText(/Noch nichts aufgenommen/)).toBeNull();
  });

  it('labels credits and models as the group\'s when the student is in one', async () => {
    // `/trainings/list` returns the group's rows and get_remaining_credits
    // returns the shared pool; both numbers are correct, and presenting them
    // unlabelled as personal would be the lie.
    renderHome({
      profile: {
        role: 'student', username: 'anna', full_name: 'Anna Müller',
        classroom_id: 'c1', workgroup_id: 'g1', workgroup_name: 'Rot',
        training_credits: 10,
      },
    });
    expect(await screen.findByText('Credits der Gruppe')).toBeInTheDocument();
    expect(screen.getByText(/Training und Credits teilst du mit der Gruppe Rot/))
      .toBeInTheDocument();
    expect(screen.getByText('in der Gruppe')).toBeInTheDocument();
  });

  it('greets the student by first name', () => {
    renderHome({
      profile: {
        role: 'student', username: 'anna', full_name: 'Anna Müller',
        classroom_id: 'c1', training_credits: 0,
      },
    });
    expect(screen.getByText(/, Anna\./)).toBeInTheDocument();
  });
});
