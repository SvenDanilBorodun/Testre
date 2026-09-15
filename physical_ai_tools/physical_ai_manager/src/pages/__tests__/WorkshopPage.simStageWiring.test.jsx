/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// WorkshopPage → SimStage prop wiring, through the REAL SimStage. The sibling
// simToggle test mocks SimStage away, so the 11-prop boundary was untested — a
// renamed/dropped prop (catalogDims, showPath, debugOpen, …) would only surface
// on a rig. Here ONLY SimScene (the lazy-UrdfTwin owner) is mocked: the test
// enters sim mode and asserts the page's state actually crosses the real
// SimStage into SimScene (catalogDims from the real buildCatalogDims over the
// service response; showPath force-enabled on sim entry) and that RunControls'
// Debug toggle drives SimStage's debug strip (debugOpen).

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import toast from 'react-hot-toast';
import WorkshopPage from '../WorkshopPage';

// ── react-redux: selector-aware stub over a mutable module-level state. ──
let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

// ── SimScene: the ONLY sim-surface mock — captures the props that crossed the
//    real SimStage boundary. (SimStage itself is deliberately NOT mocked.) ──
const mockSimScene = vi.hoisted(() => vi.fn());
vi.mock('../../components/Workshop/SimScene', () => ({
  __esModule: true,
  default: (props) => {
    mockSimScene(props);
    return <div data-testid="sim-scene" data-layout={props.layout} />;
  },
}));

// ── BlocklyWorkspace: hands the page a FAKE workspace through the real
//    onWorkspaceReady callback (the destination store is keyed by it) and
//    exposes the page's Sammlung provider so a test can dispatch a card action. ──
const mockBlockly = vi.hoisted(() => ({ workspace: null, provider: null }));
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  // Named + capitalized so react-hooks/rules-of-hooks recognizes it as a component.
  default: function MockBlocklyWorkspace({ onWorkspaceReady, sammlungProvider }) {
    mockBlockly.provider = sammlungProvider || null;
    React.useEffect(() => {
      if (onWorkspaceReady && mockBlockly.workspace) onWorkspaceReady(mockBlockly.workspace);
    }, [onWorkspaceReady]);
    return <div data-testid="blockly-workspace" />;
  },
}));

// ── The document's destination store: spies over a mutable entry list, with
//    the synchronous subscribe the page's marker state listens to. ──
const mockStore = vi.hoisted(() => {
  const store = { entries: [], listeners: [] };
  store.getEntries = vi.fn(() => store.entries);
  store.add = vi.fn();
  store.rename = vi.fn();
  store.subscribe = vi.fn((fn) => {
    store.listeners.push(fn);
    return () => { store.listeners = store.listeners.filter((l) => l !== fn); };
  });
  return store;
});
vi.mock('../../components/Workshop/sammlung/destinationStore', () => ({
  __esModule: true,
  MAX_DESTINATION_ENTRIES: 64,
  getDestinationStore: () => mockStore,
  nextAutoName: () => 'Ziel 1',
  takenDestinationNames: () => ['Ablage'],
}));
vi.mock('../../components/Workshop/sammlung/SammlungDrawer', () => ({
  __esModule: true,
  default: () => <div data-testid="sammlung-drawer" />,
}));
// The „3D-Ansicht" dock tab mounts the lazy UrdfTwin: capture its props.
const mockTwin = vi.hoisted(() => vi.fn());
vi.mock('../../components/UrdfTwin', () => ({
  __esModule: true,
  default: (props) => {
    mockTwin(props);
    return <div data-testid="urdf-twin" />;
  },
}));

// ── RunControls: exposes the Debug toggle so the test can drive simDebugOpen
//    exactly like the real Debug button does. ──
vi.mock('../../components/Workshop/RunControls', () => ({
  __esModule: true,
  default: function MockRunControls({ onToggleDebug, debugOpen }) {
    return (
      <button
        type="button"
        data-testid="run-controls-debug"
        data-debug-open={String(!!debugOpen)}
        onClick={onToggleDebug}
      />
    );
  },
}));

// ── Remaining children: import-safe no-op stubs (mirrors the simToggle test —
//    a vi.mock factory is hoisted above imports, so no shared helper). ──
// RightDock renders ONLY the „3D-Ansicht" tab, so its twin's props are observable.
vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div data-testid="right-dock">
      {(tabs || []).filter((t) => t.id === '3d').map((t) => <div key={t.id}>{t.render()}</div>)}
    </div>
  ),
}));
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div data-testid="camera-feed" /> }));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div data-testid="template-picker" /> }));
vi.mock('../../components/Workshop/ToolbarButtons', () => ({ __esModule: true, default: () => <div data-testid="toolbar-buttons" /> }));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div data-testid="gallery-tab" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({ __esModule: true, default: () => <div data-testid="version-history" /> }));
// Vormachen: TeachHost is a stub (the overlay has its own tests, and its
// leader-mode child would read `s.ros`, which these mock states lack), and the
// bridge probe is stubbed so no page test fetches localhost:8769.
vi.mock('../../components/Workshop/teach/TeachHost', () => ({ __esModule: true, default: () => <div data-testid="teach-host" /> }));
vi.mock('../../hooks/useRsBridgeStatus', () => ({
  __esModule: true,
  default: () => ({ available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false }),
}));
vi.mock('../../components/Workshop/JogPanel', () => ({ __esModule: true, default: () => <div data-testid="jog-panel" /> }));

// ── Blockly + the block-registration modules (they pull in blockly/core). ──
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

// ── Hooks + services. The catalog response feeds the REAL buildCatalogDims —
//    the expected catalogDims below is its real output over these arrays. ──
const mockRos = vi.hoisted(() => ({
  getObjectCatalog: vi.fn(() =>
    Promise.resolve({
      success: true,
      type_names: ['wuerfel'],
      labels_de: ['Würfel'],
      object_height_m: [0.03],
      object_width_m: [0.03],
      color_hex: ['#f59e0b'],
      max_instances: [2],
    }),
  ),
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
vi.mock('../../components/Workshop/useAutosave', () => ({
  __esModule: true,
  useAutosave: () => ({ lastSavedAt: null }),
}));
vi.mock('../../services/workflowApi', () => ({
  __esModule: true,
  getWorkflow: vi.fn(() => Promise.resolve(null)),
  createWorkflow: vi.fn(),
  updateWorkflow: vi.fn(),
}));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  }),
  // RS-34: WorkshopPage now runs the shared TOAST_LIMIT dismissal loop, so
  // the store hook has to exist on the mock (same idiom as
  // TrainingPage.gate.test.js). An empty stack means the loop is a no-op
  // here; the cap itself is covered by WorkshopPage.toastCap.test.jsx.
  useToasterStore: () => ({ toasts: [] }),
}));

function baseState() {
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
    },
    auth: { session: { access_token: 'jwt', user: { id: 'u1' } } },
    tasks: { heartbeatStatus: 'connected', taskStatus: { robotType: 'omx_f' } },
  };
}

function fakeWorkspace() {
  return { addChangeListener: vi.fn(), getBlockById: vi.fn(() => null) };
}

beforeEach(() => {
  mockState = baseState();
  mockBlockly.workspace = fakeWorkspace();
  mockBlockly.provider = null;
  mockStore.entries = [];
  mockStore.listeners = [];
  mockStore.add.mockReset();
  mockStore.add.mockImplementation((input) => ({ ok: true, entry: { id: 'd_00000009', ...input } }));
  mockTwin.mockClear();
  toast.success.mockClear();
  toast.error.mockClear();
  mockDispatch.mockClear();
  mockSimScene.mockClear();
  mockRos.getObjectCatalog.mockClear();
});

const lastSimSceneProps = () =>
  mockSimScene.mock.calls[mockSimScene.mock.calls.length - 1][0];

describe('WorkshopPage — SimStage wiring (real SimStage, mocked SimScene)', () => {
  test('sim entry threads catalogDims/showPath/scene through the real SimStage into SimScene', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');

    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));

    // The REAL SimStage rendered (its top bar), hosting the mocked SimScene.
    expect(await screen.findByTestId('sim-scene')).toBeInTheDocument();
    expect(screen.getByText('Simulator')).toBeInTheDocument();

    // ONE assertion inside waitFor (testing-library/no-wait-for-multiple-
    // assertions), and it MUST be the genuinely async one: catalogDims starts
    // as useState({}) in WorkshopPage and is filled from the mocked
    // getObjectCatalog promise, so it needs settling. `layout` is a hardcoded
    // literal on SimStage — present on SimScene's very first render, which the
    // findByTestId above already awaited — so waiting on it gates on a constant
    // and proves nothing about the state-derived props. Once catalogDims has
    // crossed, the rest asserts on that settled snapshot.
    await waitFor(() => {
      expect(lastSimSceneProps().catalogDims).toEqual({
        wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 },
      });
    });
    const props = lastSimSceneProps();
    // SimStage pins the mount-time literals.
    expect(props.layout).toBe('split');
    expect(props.showShadows).toBe(true);
    expect(props.showReach).toBe(true);
    // Sim entry force-enables the trail; the page's showPath state crossed
    // SimStage → SimScene.
    expect(props.showPath).toBe(true);
    // The empty scene reached SimScene too.
    expect(props.scene).toEqual(expect.objectContaining({ objects: expect.any(Array) }));
    // The real SimStage's trail toggle mirrors showPath=true.
    expect(screen.getByRole('button', { name: 'Bahn verbergen' })).toBeInTheDocument();
  });

  test('RunControls\' Debug toggle drives the real SimStage debug strip (debugOpen)', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-scene');

    // Closed by default — SimStage renders no debug strip.
    expect(screen.queryByTestId('debug-panel')).toBeNull();

    // Toggle via RunControls (exactly the real Debug button's wiring).
    await userEvent.click(screen.getByTestId('run-controls-debug'));
    expect(await screen.findByTestId('debug-panel')).toBeInTheDocument();

    // Toggle again → the strip closes.
    await userEvent.click(screen.getByTestId('run-controls-debug'));
    await waitFor(() => expect(screen.queryByTestId('debug-panel')).toBeNull());
  });
});

describe('WorkshopPage — Ziel/Position markers and „Ziel setzen"', () => {
  const PIN = { id: 'd_00000001', name: 'Ablage', kind: 'pin', x: 0.18, y: -0.06, z: 0.012, source: 'camera' };
  const POSE = { id: 'd_00000002', name: 'Oben', kind: 'pose', x: 0.14, y: 0.1, z: 0.12, source: 'capture' };

  async function enterSim() {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-scene');
  }

  test('SimStage receives the store as markers (pins on the virtual table) and onCreateDestination', async () => {
    mockStore.entries = [PIN, POSE];
    await enterSim();
    await waitFor(() => expect(lastSimSceneProps().markers).toHaveLength(2));
    const props = lastSimSceneProps();
    expect(props.markers).toEqual([
      { id: PIN.id, label: 'Ablage', kind: 'pin', x: 0.18, y: -0.06, z: 0, highlighted: false },
      { id: POSE.id, label: 'Oben', kind: 'pose', x: 0.14, y: 0.1, z: 0.12, highlighted: false },
    ]);
    expect(typeof props.onCreateDestination).toBe('function');
    expect(mockStore.subscribe).toHaveBeenCalled();
  });

  test('onCreateDestination adds a sim pin at z 0 through the store and names it', async () => {
    await enterSim();
    act(() => { lastSimSceneProps().onCreateDestination({ x: 0.135, y: 0 }); });
    expect(mockStore.add).toHaveBeenCalledTimes(1);
    expect(mockStore.add.mock.calls[0][0]).toEqual({
      name: 'Ziel 1', kind: 'pin', source: 'sim', x: 0.135, y: 0, z: 0, robot_type: 'omx_f',
    });
    expect(toast.success).toHaveBeenCalledWith('Ziel „Ziel 1" auf den Sim-Tisch gesetzt.');
  });

  test('a full store refuses the Ziel with the German „full" sentence', async () => {
    mockStore.entries = Array.from({ length: 64 }, (_, i) => ({ ...PIN, id: `d_${i}`, name: `Ziel ${i + 1}` }));
    await enterSim();
    act(() => { lastSimSceneProps().onCreateDestination({ x: 0.1, y: 0 }); });
    expect(mockStore.add).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith('In diesem Workflow gibt es schon 64 Ziele und Positionen.');
  });

  test('a store change reaches the markers without a re-render from outside', async () => {
    await enterSim();
    await waitFor(() => expect(mockStore.listeners.length).toBeGreaterThan(0));
    act(() => { mockStore.listeners.forEach((fn) => fn([PIN])); });
    await waitFor(() => expect(lastSimSceneProps().markers.map((m) => m.id)).toEqual([PIN.id]));
  });

  test('the „3D-Ansicht" twin gets the same markers, with the real-rig pin label', async () => {
    mockStore.entries = [PIN];
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await waitFor(() => expect(mockTwin).toHaveBeenCalled());
    await waitFor(() => {
      expect(mockTwin.mock.calls[mockTwin.mock.calls.length - 1][0].markers).toEqual([
        { id: PIN.id, label: 'Ablage (z ≈)', kind: 'pin', x: 0.18, y: -0.06, z: 0.012, highlighted: false },
      ]);
    });
  });

  test('the Sammlung highlight marks its marker; a card hover dispatches setHighlight', async () => {
    mockStore.entries = [PIN, POSE];
    mockState.studioAssets = { highlight: { kind: 'pose', id: POSE.id } };
    await enterSim();
    await waitFor(() => expect(lastSimSceneProps().markers).toHaveLength(2));
    expect(lastSimSceneProps().markers.map((m) => m.highlighted)).toEqual([false, true]);
    mockDispatch.mockClear();
    act(() => { mockBlockly.provider.dispatchAction({ type: 'highlight', asset: { kind: 'pin', id: PIN.id } }); });
    expect(mockDispatch).toHaveBeenCalledWith({
      type: 'studioAssets/setHighlight', payload: { kind: 'pin', id: PIN.id },
    });
    act(() => { mockBlockly.provider.dispatchAction({ type: 'highlight', asset: null }); });
    expect(mockDispatch).toHaveBeenLastCalledWith({ type: 'studioAssets/setHighlight', payload: null });
  });

  test('a Ziel focused in the drawer highlights its marker', async () => {
    mockState.studioAssets = { drawer: { open: true, tab: 'ziele', focusId: PIN.id, previewTempo: 1 } };
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await waitFor(() => expect(mockDispatch).toHaveBeenCalledWith({
      type: 'studioAssets/setHighlight', payload: { kind: 'pin', id: PIN.id },
    }));
  });

  test('the provider offers pinSim; from the editor it enters the simulator WITHOUT the trail and asks for „Ziel setzen"', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await waitFor(() => expect(mockBlockly.provider).not.toBeNull());
    expect(mockBlockly.provider.getSnapshot().capabilities.pinSim).toBe(true);
    expect(screen.queryByTestId('sim-scene')).toBeNull();
    act(() => { mockBlockly.provider.dispatchAction({ type: 'pinSim' }); });
    expect(await screen.findByTestId('sim-scene')).toBeInTheDocument();
    // ensureSimMode({ showPath: false }) never turns the trail on.
    expect(lastSimSceneProps().showPath).toBe(false);
    await waitFor(
      () => expect(lastSimSceneProps().requestedMode).toEqual({ mode: 'ziel', token: expect.any(Number) }),
      { timeout: 2000 },
    );
    expect(lastSimSceneProps().showPath).toBe(false);
    expect(screen.getByRole('button', { name: 'Bahn anzeigen' })).toBeInTheDocument();
  });
});
