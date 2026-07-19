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
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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

// ── BlocklyWorkspace: inert stub (the editor is not under test). ──
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  default: () => <div data-testid="blockly-workspace" />,
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
vi.mock('../../components/Workshop/RightDock', () => ({ __esModule: true, default: () => <div data-testid="right-dock" /> }));
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div data-testid="camera-feed" /> }));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div data-testid="template-picker" /> }));
vi.mock('../../components/Workshop/ToolbarButtons', () => ({ __esModule: true, default: () => <div data-testid="toolbar-buttons" /> }));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div data-testid="gallery-tab" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({ __esModule: true, default: () => <div data-testid="version-history" /> }));
vi.mock('../../components/Workshop/JogPanel', () => ({ __esModule: true, default: () => <div data-testid="jog-panel" /> }));
vi.mock('../../components/Workshop/RecordPanel', () => ({ __esModule: true, default: () => <div data-testid="record-panel" /> }));

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
  default: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
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
    tasks: { heartbeatStatus: 'connected' },
  };
}

beforeEach(() => {
  mockState = baseState();
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
    // assertions): once layout has crossed SimStage → SimScene the same render
    // delivered every sibling prop, so the rest asserts on a settled snapshot.
    await waitFor(() => {
      expect(lastSimSceneProps().layout).toBe('split');
    });
    const props = lastSimSceneProps();
    // SimStage pins the mount-time literals.
    expect(props.showShadows).toBe(true);
    expect(props.showReach).toBe(true);
    // Sim entry force-enables the trail; the page's showPath state crossed
    // SimStage → SimScene…
    expect(props.showPath).toBe(true);
    // …and the page's catalogDims (the REAL buildCatalogDims output over the
    // service arrays) arrived intact.
    expect(props.catalogDims).toEqual({
      wuerfel: { height_m: 0.03, width_m: 0.03, color: '#f59e0b', max_instances: 2 },
    });
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
