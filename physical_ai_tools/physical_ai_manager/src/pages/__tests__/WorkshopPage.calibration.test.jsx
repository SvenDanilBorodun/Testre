/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Roboter Studio on the real robot and its calibration (spec 40 S8): entering
// the page asks the robot for a mat check once; the editor — and with it
// RunControls, the only way to START a real program — is mounted outside the
// simulator only while the robot's calibration state is `ok`, otherwise the
// calibration panel is; a real program already running keeps its editor (and
// its Stopp) until it ends. The dock's 3D twin draws the tower kit only while
// the arm container states the tower camera.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WorkshopPage from '../WorkshopPage';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  default: () => <div data-testid="blockly-workspace" />,
}));
// The dock renders its 3D tab, so the twin's props can be read.
vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div data-testid="right-dock">
      {(tabs || []).filter((t) => t.id === '3d').map((t) => <div key={t.id}>{t.render()}</div>)}
    </div>
  ),
}));
const mockTwin = vi.hoisted(() => ({ props: null }));
vi.mock('../../components/UrdfTwin', () => ({
  __esModule: true,
  default: (props) => {
    mockTwin.props = props;
    return <div data-testid="urdf-twin" />;
  },
}));
vi.mock('../../components/Workshop/SimStage', () => ({ __esModule: true, default: () => <div data-testid="sim-stage" /> }));
const mockPanel = vi.hoisted(() => ({ props: null }));
vi.mock('../../components/Workshop/calibration/CalibrationPanel', () => ({
  __esModule: true,
  default: (props) => {
    mockPanel.props = props;
    return <div data-testid="calib-panel" />;
  },
}));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/RunControls', () => ({ __esModule: true, default: () => <div data-testid="run-controls" /> }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: ({ extra }) => <div data-testid="toolbar-buttons">{extra}</div>,
}));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/teach/TeachHost', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../hooks/useRsBridgeStatus', () => ({
  __esModule: true,
  default: () => ({ available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false }),
}));
vi.mock('../../components/Workshop/JogPanel', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('blockly/core', () => ({ __esModule: true, svgResize: vi.fn(), Events: { SELECTED: 'selected' } }));
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
  startCalibration: vi.fn(() => Promise.resolve({ success: true, message: '' })),
  getObjectCatalog: vi.fn(() => Promise.resolve({ success: false })),
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
    subscribeToWorkflowStatus: vi.fn(),
    subscribeToWorkflowSensors: vi.fn(),
    connected: true,
  }),
}));
vi.mock('../../components/Workshop/useAutosave', () => ({ __esModule: true, useAutosave: () => ({ lastSavedAt: null }) }));
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

const OMX_CAPS = { urdf_asset_id: 'omx_f', arm_joints: 5, reach_inner_m: 0.1, reach_outer_m: 0.25 };

function baseState({ calibration = { state: 'ok' }, workshop = {}, tasks = {} } = {}) {
  return {
    workshop: {
      calibration,
      // The old status flags say „calibrated": they no longer unlock anything.
      hasIntrinsicScene: true,
      hasHandeyeScene: true,
      hasTableTouch: true,
      sceneCameraProfile: null,
      selectedWorkflowId: null,
      unsavedBlocklyJson: null,
      restrictedBlocks: null,
      activeTutorialId: null,
      runState: 'idle',
      paused: false,
      log: [],
      debuggerWarnings: [],
      ...workshop,
    },
    auth: { session: { access_token: 'jwt', user: { id: 'u1' } } },
    tasks: { heartbeatStatus: 'connected', taskStatus: { capabilities: OMX_CAPS }, ...tasks },
  };
}

beforeEach(() => {
  mockState = baseState();
  mockDispatch.mockClear();
  mockRos.startCalibration.mockClear();
  mockTwin.props = null;
  mockPanel.props = null;
});

const matChecks = () => mockRos.startCalibration.mock.calls.filter(([, step]) => step === 'mat_check');

describe('WorkshopPage — only the state ok opens the editor on the real robot', () => {
  test.each([
    [null], [{ state: 'unknown' }], [{ state: 'blocked', code: 'too_dark' }],
    [{ state: 'needs_arm_check', code: 'needs_arm_check/none' }], [{ state: 'arm_check_running' }],
    [{ state: 'confirm_target_removed' }], [{ state: 'handheld_running' }],
  ])('calibration %j: the calibration panel, no RunControls, no editor', async (calibration) => {
    mockState = baseState({ calibration });
    render(<WorkshopPage isActive />);
    expect(await screen.findByTestId('calib-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('run-controls')).toBeNull();
    expect(screen.queryByTestId('blockly-workspace')).toBeNull();
  });

  test('state ok: the editor and RunControls', async () => {
    render(<WorkshopPage isActive />);
    expect(await screen.findByTestId('run-controls')).toBeInTheDocument();
    expect(screen.getByTestId('blockly-workspace')).toBeInTheDocument();
    expect(screen.queryByTestId('calib-panel')).toBeNull();
  });

  test('in the simulator RunControls is mounted whatever the calibration says', async () => {
    mockState = baseState({ calibration: { state: 'needs_arm_check', code: 'needs_arm_check/none' } });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('calib-panel');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    expect(await screen.findByTestId('run-controls')).toBeInTheDocument();
    expect(screen.queryByTestId('calib-panel')).toBeNull();
  });

  test('a real program running keeps its editor (and its Stopp) when the state leaves ok, until it ends', async () => {
    const { rerender } = render(<WorkshopPage isActive />);
    await screen.findByTestId('run-controls');
    mockState = baseState({ workshop: { runState: 'running' } });
    rerender(<WorkshopPage isActive />);
    mockState = baseState({ calibration: { state: 'blocked', code: 'tags_hidden' }, workshop: { runState: 'running' } });
    rerender(<WorkshopPage isActive />);
    expect(screen.getByTestId('run-controls')).toBeInTheDocument();
    mockState = baseState({ calibration: { state: 'blocked', code: 'tags_hidden' } });
    rerender(<WorkshopPage isActive />);
    expect(await screen.findByTestId('calib-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('run-controls')).toBeNull();
  });

  test('a program running when the page is entered on a non-ok state never shows the editor', async () => {
    mockState = baseState({ calibration: { state: 'arm_check_running' }, workshop: { runState: 'running' } });
    render(<WorkshopPage isActive />);
    expect(await screen.findByTestId('calib-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('run-controls')).toBeNull();
  });
});

describe('WorkshopPage — the mat check on entering', () => {
  test('entering Roboter Studio on the real robot calls the mat check once', async () => {
    const { rerender } = render(<WorkshopPage isActive />);
    await screen.findByTestId('run-controls');
    rerender(<WorkshopPage isActive />);
    rerender(<WorkshopPage isActive />);
    expect(matChecks()).toEqual([['scene', 'mat_check']]);
    // Leaving and entering again is a new entry.
    rerender(<WorkshopPage isActive={false} />);
    rerender(<WorkshopPage isActive />);
    await screen.findByTestId('run-controls');
    expect(matChecks()).toHaveLength(2);
  });

  test('without a robot link the check waits for it, then runs once', async () => {
    mockState = baseState({ tasks: { heartbeatStatus: 'disconnected' } });
    const { rerender } = render(<WorkshopPage isActive />);
    await screen.findByTestId('run-controls');
    expect(matChecks()).toHaveLength(0);
    mockState = baseState();
    rerender(<WorkshopPage isActive />);
    rerender(<WorkshopPage isActive />);
    expect(matChecks()).toHaveLength(1);
  });

  test('not while a program runs (its own start gate checks the mat)', async () => {
    mockState = baseState({ workshop: { runState: 'running' } });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('run-controls');
    expect(matChecks()).toHaveLength(0);
  });

  test('an inactive page asks nothing', () => {
    render(<WorkshopPage isActive={false} />);
    expect(mockRos.startCalibration).not.toHaveBeenCalled();
  });
});

describe('WorkshopPage — the toolbar opens the panel', () => {
  test('„Kalibrierung" opens the panel from the editor; its onClose returns to the editor', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('run-controls');
    await userEvent.click(screen.getByRole('button', { name: 'Kalibrierung' }));
    expect(await screen.findByTestId('calib-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('run-controls')).toBeNull();
    act(() => { mockPanel.props.onClose(); });
    expect(await screen.findByTestId('run-controls')).toBeInTheDocument();
  });

  test('the panel the page shows on its own offers no „back": the state decides', async () => {
    mockState = baseState({ calibration: { state: 'needs_arm_check', code: 'needs_arm_check/none' } });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('calib-panel');
    expect(mockPanel.props.onClose).toBeFalsy();
  });
});

describe('WorkshopPage — the dock\'s 3D twin and the tower kit', () => {
  test('tower_v1: the twin gets the fixture (raised by 7.2 mm, plate and tower)', async () => {
    mockState = baseState({ workshop: { sceneCameraProfile: 'tower_v1' } });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('urdf-twin');
    expect(mockTwin.props.fixture).toBeTruthy();
    expect(mockTwin.props.fixture.liftM).toBe(0.0072);
    expect(mockTwin.props.fixture.boxes.map((b) => b.name)).toContain('column');
  });

  test.each([['legacy'], [null]])('%s: no fixture, the twin renders as before', async (profile) => {
    mockState = baseState({ workshop: { sceneCameraProfile: profile } });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('urdf-twin');
    expect(mockTwin.props.fixture).toBeUndefined();
  });
});
