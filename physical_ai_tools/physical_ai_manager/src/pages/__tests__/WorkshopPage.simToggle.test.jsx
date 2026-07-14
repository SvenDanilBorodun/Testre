/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// T6 DoD lock: entering/leaving the simulator swaps ONLY the right region
// (RightDock ↔ SimStage) while the LEFT Blockly editor keeps its mount — the
// workspace is a positional sibling of the swapped region and is keyed only by
// editorKey, so the toggle must NOT remount it (a remount would drop undo history
// / re-seed initialJson). The heavy children + ROS/cloud hooks are stubbed so the
// test exercises only the layout swap.

import React from 'react';
import { render, screen } from '@testing-library/react';
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

// ── BlocklyWorkspace: a mount counter. A []-deps effect bumps once per MOUNT, so
//    a remount (editorKey change) is observable. ──
const mockBlockly = vi.hoisted(() => ({ mountCount: 0 }));
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  // Named + capitalized so react-hooks/rules-of-hooks recognizes it as a component.
  default: function MockBlocklyWorkspace() {
    React.useEffect(() => {
      mockBlockly.mountCount += 1;
    }, []);
    return <div data-testid="blockly-workspace" />;
  },
}));

// ── Right-region components — recognizable stubs so we can assert the swap. ──
vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: () => <div data-testid="right-dock" />,
}));
vi.mock('../../components/Workshop/SimStage', () => ({
  __esModule: true,
  default: () => <div data-testid="sim-stage" />,
}));

// ── Remaining children: import-safe no-op stubs (inlined — a vi.mock factory is
//    hoisted above imports, so it can't reference a top-level helper). ──
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/RunControls', () => ({ __esModule: true, default: () => <div data-testid="run-controls" /> }));
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

// ── Hooks + services. ──
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

function baseState(over = {}) {
  return {
    workshop: {
      // calibrated so the editor (not the wizard) renders; sim needs no calib.
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
    tasks: { heartbeatStatus: 'connected' },
  };
}

beforeEach(() => {
  mockState = baseState();
  mockBlockly.mountCount = 0;
  mockDispatch.mockClear();
  mockRos.getObjectCatalog.mockClear();
});

describe('WorkshopPage — sim toggle swaps only the right region', () => {
  test('toggling the simulator does NOT remount the Blockly workspace and swaps RightDock ↔ SimStage', async () => {
    render(<WorkshopPage isActive />);

    // Editor + dock render; SimStage absent initially.
    await screen.findByTestId('blockly-workspace');
    expect(await screen.findByTestId('right-dock')).toBeInTheDocument();
    expect(screen.queryByTestId('sim-stage')).toBeNull();

    // Capture the settled mount count (initial mount + the one editorKey-bump
    // remount the hydrate effect always does on load — both already happened).
    const mountsBeforeToggle = mockBlockly.mountCount;
    expect(mountsBeforeToggle).toBeGreaterThanOrEqual(1);

    // Enter the simulator.
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));

    await screen.findByTestId('sim-stage');
    // The dock is gone; the workspace was NOT remounted by the swap.
    expect(screen.queryByTestId('right-dock')).toBeNull();
    expect(screen.getByTestId('blockly-workspace')).toBeInTheDocument();
    expect(mockBlockly.mountCount).toBe(mountsBeforeToggle);

    // Leave the simulator — dock returns, workspace still not remounted.
    await userEvent.click(screen.getByRole('button', { name: 'Simulator beenden' }));
    await screen.findByTestId('right-dock');
    expect(screen.queryByTestId('sim-stage')).toBeNull();
    expect(mockBlockly.mountCount).toBe(mountsBeforeToggle);
  });

  test('sim entry is BLOCKED while a tutorial is active (button disabled)', async () => {
    mockState = baseState({ activeTutorialId: 'tut-1' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    const btn = screen.getByRole('button', { name: 'Test im Simulator' });
    expect(btn).toBeDisabled();
    // Clicking a disabled button is a no-op → no SimStage.
    await userEvent.click(btn);
    expect(screen.queryByTestId('sim-stage')).toBeNull();
  });
});
