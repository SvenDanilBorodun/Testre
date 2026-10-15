/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The studio frames its mode (WI-1.1): an orange border on the real robot, a
// blue one in the simulator, on the page's EXISTING root (`data-mode`), and one
// toast per switch — never on mount, whichever path made the switch (the header
// toggle, or a play button that enters the simulator on the student's behalf,
// R0-4). The switch must not remount the editor. Mocks in the style of
// WorkshopPage.simToggle.test.jsx.

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import toast from 'react-hot-toast';
import WorkshopPage from '../WorkshopPage';
import { MODE_DE } from '../../components/Workshop/modeCopy';
import { toastIcon } from '../../components/icons/toast';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockBlockly = vi.hoisted(() => ({ mountCount: 0, provider: null }));
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  default: function MockBlocklyWorkspace({ sammlungProvider }) {
    React.useEffect(() => {
      mockBlockly.mountCount += 1;
    }, []);
    mockBlockly.provider = sammlungProvider;
    return <div data-testid="blockly-workspace" />;
  },
}));
vi.mock('../../components/Workshop/RightDock', () => ({ __esModule: true, default: () => <div data-testid="right-dock" /> }));
vi.mock('../../components/Workshop/SimStage', () => ({ __esModule: true, default: () => <div data-testid="sim-stage" /> }));
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/RunControls', () => ({ __esModule: true, default: () => <div data-testid="run-controls" /> }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div data-testid="camera-feed" /> }));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div data-testid="template-picker" /> }));
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: ({ extra }) => <div data-testid="toolbar-buttons">{extra}</div>,
}));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div data-testid="gallery-tab" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({ __esModule: true, default: () => <div data-testid="version-history" /> }));
vi.mock('../../components/Workshop/teach/TeachHost', () => ({ __esModule: true, default: () => <div data-testid="teach-host" /> }));
vi.mock('../../hooks/useRsBridgeStatus', () => ({
  __esModule: true,
  default: () => ({ available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false }),
}));
vi.mock('../../components/Workshop/JogPanel', () => ({ __esModule: true, default: () => <div data-testid="jog-panel" /> }));
vi.mock('blockly/core', () => ({
  __esModule: true,
  svgResize: vi.fn(),
  Events: { SELECTED: 'selected' },
}));
vi.mock('../../components/Workshop/blocks/destinations', () => ({
  __esModule: true,
  setDriveToHandler: vi.fn(),
  applyPinnedCoordinates: vi.fn(),
}));
vi.mock('../../components/Workshop/blocks/perception', () => ({
  __esModule: true,
  setObjectCatalogOptions: vi.fn(),
  setWorkspaceAccessor: vi.fn(),
}));
const mockRos = vi.hoisted(() => ({
  getObjectCatalog: vi.fn(() => Promise.resolve({ success: true, type_names: ['wuerfel'], labels_de: ['Würfel'] })),
  capturePose: vi.fn(),
  jogArm: vi.fn(),
}));
vi.mock('../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));
vi.mock('../../hooks/useRosTopicSubscription', () => ({
  __esModule: true,
  useRosTopicSubscription: () => ({
    subscribeToWorkflowStatus: vi.fn(), subscribeToWorkflowSensors: vi.fn(), connected: true,
  }),
}));
vi.mock('../../components/Workshop/useAutosave', () => ({
  __esModule: true,
  useAutosave: () => ({}),
}));
vi.mock('../../services/workflowApi', () => ({
  __esModule: true,
  getWorkflow: vi.fn(() => Promise.resolve(null)),
  createWorkflow: vi.fn(),
  updateWorkflow: vi.fn(),
}));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() }),
  useToasterStore: () => ({ toasts: [] }),
}));

function baseState(over = {}, robotProfile = 'omx_full') {
  return {
    workshop: {
      hasIntrinsicScene: true,
      hasHandeyeScene: true,
      hasTableTouch: true,
      recalibrating: false,
      pendingVerify: false,
      selectedWorkflowId: null,
      unsavedBlocklyJson: null,
      restrictedBlocks: null,
      activeTutorialId: null,
      runState: 'idle',
      log: [],
      debuggerWarnings: [],
      ...over,
    },
    auth: { session: { access_token: 'jwt', user: { id: 'u1' } } },
    tasks: { heartbeatStatus: 'connected', taskStatus: { robotProfile } },
  };
}

beforeEach(() => {
  mockState = baseState();
  mockBlockly.mountCount = 0;
  mockDispatch.mockClear();
  toast.mockClear();
  toast.error.mockClear();
});

// The page root: the element that carries data-mode.
// eslint-disable-next-line testing-library/no-node-access
const root = () => document.querySelector('[data-mode]');
const modeToasts = () => toast.mock.calls.filter(([, opts]) => opts && opts.id === 'edubotics-mode');

describe('WorkshopPage — the mode frame', () => {
  test('real: data-mode="real" + the orange border; the simulator: data-mode="sim" + the blue one', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    expect(root()).toHaveAttribute('data-mode', 'real');
    expect(root().className).toContain('border-4');
    expect(root().className).toContain('border-[#EA580C]');
    expect(root().className).not.toContain('border-[#2563EB]');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    expect(root()).toHaveAttribute('data-mode', 'sim');
    expect(root().className).toContain('border-[#2563EB]');
    expect(root().className).not.toContain('border-[#EA580C]');
  });

  test('the frame is a class on the existing root — it carries no transform, filter, contain or z-index', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    const cls = root().className;
    ['transform', 'filter', 'contain', 'will-change', 'z-'].forEach((bad) => expect(cls).not.toContain(bad));
    expect(root().getAttribute('style')).toBeNull();
  });

  test('the header carries the mode badge after the title', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    expect(screen.getByTestId('mode-badge')).toHaveTextContent(MODE_DE.BADGE_REAL);
    // The real-robot badge draws the arm of the robot the student started with.
    // eslint-disable-next-line testing-library/no-node-access
    expect(screen.getByTestId('mode-badge').querySelector('[data-robot-glyph="omx_f"]')).not.toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    expect(screen.getByTestId('mode-badge')).toHaveTextContent(MODE_DE.BADGE_SIM);
  });

  test.each([
    ['omx_follower', 'omx_f'], ['edu6_studio', 'edu6'], ['edu1_studio', 'edu1'],
  ])('the badge follows the robot: %s draws the %s arm', async (profile, glyph) => {
    mockState = baseState({}, profile);
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    // eslint-disable-next-line testing-library/no-node-access
    expect(screen.getByTestId('mode-badge').querySelector(`[data-robot-glyph="${glyph}"]`)).not.toBeNull();
  });

  test('before the first status tick (profile unknown) the badge draws NO robot, only its words', async () => {
    mockState = baseState({}, '');
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    const badge = screen.getByTestId('mode-badge');
    expect(badge).toHaveTextContent(MODE_DE.BADGE_REAL);
    expect(badge.querySelector('[data-robot-glyph]')).toBeNull(); // eslint-disable-line testing-library/no-node-access
  });
});

describe('WorkshopPage — the switch toast', () => {
  test('never on mount; once per switch, both directions, with one id', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    expect(modeToasts()).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    expect(modeToasts()).toHaveLength(1);
    expect(modeToasts()[0][0]).toBe(MODE_DE.TOAST_SIM);
    await userEvent.click(screen.getByRole('button', { name: 'Simulator beenden' }));
    await screen.findByTestId('right-dock');
    expect(modeToasts()).toHaveLength(2);
    expect(modeToasts()[1][0]).toBe(MODE_DE.TOAST_REAL);
    modeToasts().forEach(([, opts]) => expect(opts.icon).toBeTruthy());
    // The real-mode toast draws the arm of the robot the student started with.
    expect(modeToasts()[1][1].icon).toBe(toastIcon('arm', { profile: 'omx_full' }));
  });

  test('the real-mode toast of an Edu:1 draws the Edu:1 arm, never an OMX', async () => {
    mockState = baseState({}, 'edu1_studio');
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    await userEvent.click(screen.getByRole('button', { name: 'Simulator beenden' }));
    await screen.findByTestId('right-dock');
    const icon = modeToasts()[1][1].icon;
    expect(icon).toBe(toastIcon('arm', { profile: 'edu1_studio' }));
    expect(icon).not.toBe(toastIcon('arm', { profile: 'omx_full' }));
  });

  test('with the profile unknown the real-mode toast has no robot picture (the simulator toast keeps its icon)', async () => {
    mockState = baseState({}, '');
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    await userEvent.click(screen.getByRole('button', { name: 'Simulator beenden' }));
    await screen.findByTestId('right-dock');
    expect(modeToasts()[0][1].icon).toBeTruthy();
    expect(modeToasts()[1][1].icon).toBeUndefined();
  });

  test('R0-4: a play button that enters the simulator for the student is announced too', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    // The variable path: previewAsset → ensureSimMode, no service call.
    mockBlockly.provider.dispatchAction({ type: 'preview', asset: { kind: 'variable', name: 'p' } });
    await screen.findByTestId('sim-stage');
    expect(modeToasts()).toHaveLength(1);
    expect(modeToasts()[0][0]).toBe(MODE_DE.TOAST_SIM);
  });

  test('switching modes never remounts the editor', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    const mounts = mockBlockly.mountCount;
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    await userEvent.click(screen.getByRole('button', { name: 'Simulator beenden' }));
    await screen.findByTestId('right-dock');
    expect(mockBlockly.mountCount).toBe(mounts);
  });
});
