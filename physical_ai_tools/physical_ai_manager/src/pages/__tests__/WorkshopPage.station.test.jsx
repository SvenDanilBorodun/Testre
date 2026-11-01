/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Roboter Studio on a camera station (station spec v6.13, §9.2): the page
// gates read the station, never the manual calibration.
//
//   * showEditor = simMode || station_configured; an unconfigured station shows
//     the station card where CalibrationWizard would be;
//   * once configured, a compact station strip under the header, a keyed
//     sibling OUTSIDE <main>: appearing and every snapshot change never remount
//     the editor;
//   * a real run needs `verified` (RunControls' start reason); the simulator
//     needs nothing; pinCamera needs `verified`; the camera tab shows the
//     station stream with `source_ok`; CalibrationWizard and „Kalibrierung neu
//     starten" are not rendered;
//   * the visit is reported to the controller (§9.1 step 1), with the leader;
//   * manual rigs keep every gate exactly as before.
//
// Same mocking idiom as WorkshopPage.hydrate.test.jsx; the station controller
// is mocked at its module boundary.

import React from 'react';
import { act, render, screen, within } from '@testing-library/react';
import WorkshopPage from '../WorkshopPage';
import { STATION_DE } from '../../features/station/stationCopy';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockStation = vi.hoisted(() => {
  const listeners = new Set();
  const box = { snap: null };
  return {
    box,
    set(patch) {
      box.snap = { ...box.snap, ...patch };
      listeners.forEach((fn) => fn());
    },
    api: {
      subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
      getSnapshot: () => box.snap,
      setWorkshop: vi.fn(),
    },
  };
});
vi.mock('../../features/station/controller', () => ({ __esModule: true, ...mockStation.api }));

const mockStationPanel = vi.hoisted(() => ({ mounts: 0, props: [] }));
vi.mock('../../components/Station/StationPanel', () => ({
  __esModule: true,
  default: function MockStationPanel(props) {
    mockStationPanel.props.push(props);
    // eslint-disable-next-line react-hooks/rules-of-hooks
    React.useEffect(() => { mockStationPanel.mounts += 1; }, []);
    return <div data-testid={props.compact ? 'station-strip' : 'station-card'} />;
  },
}));
vi.mock('../../components/Station/StationCommissionWizard', () => ({
  __esModule: true,
  default: () => <div data-testid="station-wizard" />,
}));

const mockBlockly = vi.hoisted(() => ({ mountCount: 0 }));
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  default: function MockBlocklyWorkspace() {
    React.useEffect(() => {
      mockBlockly.mountCount += 1;
    }, []);
    return <div data-testid="blockly-workspace" />;
  },
}));

vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div data-testid="right-dock">
      {(tabs || [])
        .filter((t) => t.id === 'camera')
        .map((t) => (
          <div key={t.id}>{t.render()}</div>
        ))}
    </div>
  ),
}));
vi.mock('../../components/Workshop/SimStage', () => ({ __esModule: true, default: () => <div data-testid="sim-stage" /> }));
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
const mockRunControls = vi.hoisted(() => ({ props: null }));
vi.mock('../../components/Workshop/RunControls', () => ({
  __esModule: true,
  default: (props) => {
    mockRunControls.props = props;
    return <div data-testid="run-controls" />;
  },
}));
const mockCamera = vi.hoisted(() => ({ props: null }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({
  __esModule: true,
  default: (props) => {
    mockCamera.props = props;
    return <div data-testid="camera-feed" />;
  },
}));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: ({ extra }) => <div data-testid="toolbar">{extra}</div>,
}));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/Workshop/teach/TeachHost', () => ({ __esModule: true, default: () => <div /> }));
const mockBridge = vi.hoisted(() => ({
  value: { available: true, followerOnly: true, hasLeader: true, busy: false, leaderOn: false, probed: true },
}));
vi.mock('../../hooks/useRsBridgeStatus', () => ({ __esModule: true, default: () => mockBridge.value }));
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
  getObjectCatalog: vi.fn(() => Promise.resolve({ success: true, type_names: ['wuerfel'], labels_de: ['Würfel'] })),
  capturePose: vi.fn(),
  jogArm: vi.fn(),
}));
vi.mock('../../hooks/useRosServiceCaller', () => ({ __esModule: true, useRosServiceCaller: () => mockRos }));
vi.mock('../../hooks/useRosTopicSubscription', () => ({
  __esModule: true,
  useRosTopicSubscription: () => ({ subscribeToWorkflowStatus: vi.fn(), subscribeToWorkflowSensors: vi.fn(), connected: true }),
}));
const mockAutosave = vi.hoisted(() => ({ calls: [] }));
vi.mock('../../components/Workshop/useAutosave', () => ({
  __esModule: true,
  useAutosave: (o) => { mockAutosave.calls.push(o); return { lastSavedAt: null }; },
}));
vi.mock('../../services/workflowApi', () => ({
  __esModule: true,
  getWorkflow: vi.fn(() => Promise.resolve(null)),
  createWorkflow: vi.fn(() => Promise.resolve({ id: 'wf-new' })),
  updateWorkflow: vi.fn(() => Promise.resolve({})),
  listTrajectories: vi.fn(() => Promise.resolve([])),
}));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() }),
  useToasterStore: () => ({ toasts: [] }),
}));
// The Sammlung provider: what the flyouts are told about the rig (pinCamera).
const mockProvider = vi.hoisted(() => ({ snapshots: [] }));
vi.mock('../../components/Workshop/sammlung/provider', async () => {
  const actual = await vi.importActual('../../components/Workshop/sammlung/provider');
  return {
    ...actual,
    createSammlungProvider: () => {
      const p = actual.createSammlungProvider();
      const setSnapshot = p.setSnapshot.bind(p);
      p.setSnapshot = (snap) => { mockProvider.snapshots.push(snap); setSnapshot(snap); };
      return p;
    },
  };
});

const STATION_CAPS = {
  recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true,
  station_protocol: 6,
};

function baseState({ caps = STATION_CAPS, manualCalibrated = false } = {}) {
  return {
    workshop: {
      hasIntrinsicScene: manualCalibrated,
      hasHandeyeScene: manualCalibrated,
      hasTableTouch: manualCalibrated,
      recalibrating: false,
      pendingVerify: false,
      selectedWorkflowId: null,
      unsavedBlocklyJson: null,
      restrictedBlocks: null,
      activeTutorialId: null,
      runState: 'idle',
      log: [],
      debuggerWarnings: [],
    },
    auth: { session: { access_token: 'jwt', user: { id: 'u1' } } },
    tasks: { heartbeatStatus: 'connected', taskStatus: { capabilities: caps, robotType: 'omx_f', robotProfile: 'omx_full' } },
  };
}

const SNAP = (over = {}) => ({
  active: true, stationRig: true, phase: 'held', held: true, sessionId: 's1',
  stationConfigured: true, sourceOk: true, cameraOk: false, robotOk: false, verified: false,
  workflowActive: false, startOffered: false, leaderBlocked: false, notice: '', streamError: '',
  checking: '', cameraCheck: null, robotCheck: null, resolveJob: null,
  ...over,
});

const settle = async () => {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
};

beforeEach(() => {
  mockBlockly.mountCount = 0;
  mockStationPanel.mounts = 0;
  mockStationPanel.props = [];
  mockRunControls.props = null;
  mockCamera.props = null;
  mockAutosave.calls = [];
  mockStation.api.setWorkshop.mockReset();
  mockStation.box.snap = SNAP();
  mockBridge.value = { available: true, followerOnly: true, hasLeader: true, busy: false, leaderOn: false, probed: true };
});

describe('WorkshopPage on a camera station', () => {
  it('an unconfigured station shows the station card where the manual wizard would be, never the editor', async () => {
    mockStation.box.snap = SNAP({ stationConfigured: false });
    mockState = baseState({ manualCalibrated: true });
    render(<WorkshopPage isActive />);
    await settle();
    expect(screen.getByTestId('station-card')).toBeInTheDocument();
    expect(screen.queryByTestId('calib-wizard')).toBe(null);
    expect(screen.queryByTestId('blockly-workspace')).toBe(null);
    expect(screen.queryByTestId('station-strip')).toBe(null);
  });

  it('opens the editor once station_configured, with the strip under the header and no manual wizard', async () => {
    mockState = baseState({ manualCalibrated: false });
    render(<WorkshopPage isActive />);
    await settle();
    expect(screen.getByTestId('blockly-workspace')).toBeInTheDocument();
    expect(screen.getByTestId('station-strip')).toBeInTheDocument();
    expect(screen.queryByTestId('calib-wizard')).toBe(null);
    // „Kalibrierung neu starten" is not rendered on a station rig.
    expect(screen.queryByText('Kalibrierung neu starten')).toBe(null);
    // The strip is a sibling of <main>, never inside it.
    expect(within(screen.getByRole('main')).queryByTestId('station-strip')).toBe(null);
  });

  it('the strip never remounts the editor: the strip itself appearing, verification and a revocation', async () => {
    // The editor is open (a calibrated manual-looking rig whose capabilities
    // have not said „station" yet); then the station manifest arrives and the
    // strip appears above the editor row.
    mockState = baseState({ caps: { ...STATION_CAPS, station_protocol: undefined }, manualCalibrated: true });
    const { rerender } = render(<WorkshopPage isActive />);
    await settle();
    expect(screen.queryByTestId('station-strip')).toBe(null);
    const mounts = mockBlockly.mountCount;
    expect(mounts).toBeGreaterThan(0);
    mockState = baseState({ manualCalibrated: true });
    rerender(<WorkshopPage isActive />);
    await settle();
    expect(screen.getByTestId('station-strip')).toBeInTheDocument();
    expect(mockBlockly.mountCount).toBe(mounts);
    for (const patch of [
      { verified: true, cameraOk: true, robotOk: true },
      { verified: false, cameraOk: false },
      { sourceOk: false },
      { notice: 'Verbindung zum Roboter unterbrochen – die Kamerastation wird beendet.' },
      { held: false, phase: 'idle', startOffered: true },
    ]) {
      act(() => mockStation.set(patch));
      rerender(<WorkshopPage isActive />);
      // eslint-disable-next-line no-await-in-loop
      await settle();
    }
    expect(mockBlockly.mountCount).toBe(mounts);
  });

  it('a real run needs `verified`; the simulator needs nothing', async () => {
    mockState = baseState();
    const { rerender } = render(<WorkshopPage isActive />);
    await settle();
    expect(mockRunControls.props.startBlockedReason).toBe(STATION_DE.notVerified);
    act(() => mockStation.set({ verified: true }));
    rerender(<WorkshopPage isActive />);
    await settle();
    expect(mockRunControls.props.startBlockedReason).toBe(null);
  });

  it('the camera tab shows the station stream, clickable (click-to-Ziel) only once verified, with source_ok', async () => {
    mockState = baseState();
    const { rerender } = render(<WorkshopPage isActive />);
    await settle();
    expect(mockCamera.props.camera).toBe('station_scene');
    expect(mockCamera.props.clickable).toBe(false);
    expect(mockCamera.props.sourceOk).toBe(true);
    act(() => mockStation.set({ verified: true, sourceOk: false }));
    rerender(<WorkshopPage isActive />);
    await settle();
    expect(mockCamera.props.clickable).toBe(true);
    expect(mockCamera.props.sourceOk).toBe(false);
  });

  it('a camera pin needs the verified session (click-to-Ziel is D4-gated)', async () => {
    mockState = baseState({ manualCalibrated: true });
    const { rerender } = render(<WorkshopPage isActive />);
    await settle();
    const last = () => mockProvider.snapshots[mockProvider.snapshots.length - 1].capabilities.pinCamera;
    expect(last()).toBe(false);
    act(() => mockStation.set({ verified: true }));
    rerender(<WorkshopPage isActive />);
    await settle();
    expect(last()).toBe(true);
  });

  it('the autosave unlocks with station_configured, not with the manual calibration', async () => {
    mockState = baseState({ manualCalibrated: false });
    render(<WorkshopPage isActive />);
    await settle();
    expect(mockAutosave.calls[mockAutosave.calls.length - 1].enabled).toBe(true);
  });

  it('reports the visit and the leader to the controller (§9.1 step 1), and the end of it', async () => {
    mockBridge.value = { ...mockBridge.value, followerOnly: false, leaderOn: true };
    mockState = baseState();
    const { unmount } = render(<WorkshopPage isActive />);
    await settle();
    expect(mockStation.api.setWorkshop).toHaveBeenCalledWith({ open: true, leaderOn: true, leaderProbed: true });
    unmount();
    expect(mockStation.api.setWorkshop).toHaveBeenLastCalledWith({ open: false });
  });
});

describe('WorkshopPage on a manual rig keeps every gate', () => {
  it('reads the manual calibration, renders no station UI and the scene camera', async () => {
    mockStation.box.snap = SNAP({ active: false, stationRig: false, stationConfigured: null, held: false });
    mockState = baseState({ caps: { ...STATION_CAPS, station_protocol: undefined }, manualCalibrated: true });
    render(<WorkshopPage isActive />);
    await settle();
    expect(screen.getByTestId('blockly-workspace')).toBeInTheDocument();
    expect(screen.queryByTestId('station-strip')).toBe(null);
    expect(screen.getByText('Kalibrierung neu starten')).toBeInTheDocument();
    expect(mockCamera.props.camera).toBe('scene');
    expect(mockCamera.props.clickable).toBe(true);
    expect(mockRunControls.props.startBlockedReason).toBe(null);
  });

  it('an uncalibrated manual rig shows the manual wizard', async () => {
    mockStation.box.snap = SNAP({ active: false, stationRig: false, stationConfigured: null, held: false });
    mockState = baseState({ caps: { ...STATION_CAPS, station_protocol: undefined }, manualCalibrated: false });
    render(<WorkshopPage isActive />);
    await settle();
    expect(screen.getByTestId('calib-wizard')).toBeInTheDocument();
    expect(screen.queryByTestId('station-card')).toBe(null);
  });
});
