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
import toast from 'react-hot-toast';
import { DE } from '../../components/Workshop/blocks/messages_de';
import { setDriveToHandler } from '../../components/Workshop/blocks/destinations';

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
// RightDock additionally renders the control tab panel (the JogPanel mock
// below exposes a trigger button) so the sim-entry guard tests can flip the
// page's hand-guide state through the real callback, and the camera tab (its
// feed is a stub) so the retired „Position merken" row can be asserted absent.
// `data-open-ids` exposes the dock layout the page read from storage.
// Other tabs are NOT rendered — the '3d' tab would mount the real lazy UrdfTwin.
vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: ({ tabs, openIds }) => (
    <div data-testid="right-dock" data-open-ids={JSON.stringify(openIds)}>
      {(tabs || [])
        .filter((t) => t.id === 'control' || t.id === 'camera')
        .map((t) => (
          <div key={t.id}>{t.render()}</div>
        ))}
    </div>
  ),
}));
vi.mock('../../components/Workshop/SimStage', () => ({
  __esModule: true,
  default: () => <div data-testid="sim-stage" />,
}));

// ── Remaining children: import-safe no-op stubs (inlined — a vi.mock factory is
//    hoisted above imports, so it can't reference a top-level helper). ──
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
// LeaderToggle exposes the lock reason the page hands it.
vi.mock('../../components/Workshop/LeaderToggle', () => ({
  __esModule: true,
  default: ({ lockedReason }) => <div data-testid="leader-toggle" data-locked-reason={lockedReason || ''} />,
}));
vi.mock('../../components/Workshop/RunControls', () => ({ __esModule: true, default: () => <div data-testid="run-controls" /> }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div data-testid="camera-feed" /> }));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div data-testid="template-picker" /> }));
// ToolbarButtons renders its `extra` slot, where the Vormachen button lives.
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: ({ extra }) => <div data-testid="toolbar-buttons">{extra}</div>,
}));
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
// The JogPanel stub exposes a trigger button wired to the REAL page
// callback (onHandGuideChange), so the sim-entry guard tests
// drive the page state exactly like a live panel would.
vi.mock('../../components/Workshop/JogPanel', () => ({
  __esModule: true,
  default: function MockJogPanel({ onHandGuideChange, disabled }) {
    return (
      <div data-testid="jog-panel" data-disabled={String(!!disabled)}>
        <button
          type="button"
          data-testid="jog-hand-guide-on"
          onClick={() => onHandGuideChange && onHandGuideChange(true)}
        />
      </div>
    );
  },
}));

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

  test('sim entry is BLOCKED while Vormachen is open (it owns the REAL arm under the student\'s hand)', async () => {
    mockState = { ...baseState(), studioAssets: { teach: { open: true } } };
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    const btn = screen.getByRole('button', { name: 'Test im Simulator' });
    expect(btn).toBeDisabled();
    expect(btn.getAttribute('title')).toBe(DE.TEACH_SIM_ENTRY_BLOCKED);
    await userEvent.click(btn);
    expect(screen.queryByTestId('sim-stage')).toBeNull();
  });

  test('the Kamera tab is only the feed — the retired „Position merken" button is gone', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    expect(screen.getAllByTestId('camera-feed').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /Position merken/ })).toBeNull();
    expect(screen.queryByText('Speichert die aktuelle Armposition als Ziel.')).toBeNull();
  });

  test('a stored dock layout naming the retired „record" tab heals to the known tabs', async () => {
    window.localStorage.setItem('edubotics_workshop_dock_open', JSON.stringify(['camera', 'record']));
    try {
      render(<WorkshopPage isActive />);
      const dock = await screen.findByTestId('right-dock');
      expect(JSON.parse(dock.getAttribute('data-open-ids'))).toEqual(['camera']);
    } finally {
      window.localStorage.removeItem('edubotics_workshop_dock_open');
    }
  });

  test('sim entry is BLOCKED while the arm is hand-guided (entering sim would unmount JogPanel mid-session)', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    const btn = screen.getByRole('button', { name: 'Test im Simulator' });
    expect(btn).toBeEnabled();

    // JogPanel reports a live hand-guide via the real onHandGuideChange.
    await userEvent.click(screen.getByTestId('jog-hand-guide-on'));

    expect(btn).toBeDisabled();
    expect(btn.getAttribute('title')).toContain('freigeschaltet');
    await userEvent.click(btn);
    expect(screen.queryByTestId('sim-stage')).toBeNull();
  });
});

describe('WorkshopPage — Vormachen wiring', () => {
  test('the toolbar „✋ Vormachen" requests Vormachen with no focus', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    const btn = screen.getByRole('button', { name: DE.TOOLBAR_TEACH });
    expect(btn).toBeEnabled();
    expect(btn.getAttribute('title')).toBe(DE.TOOLBAR_TEACH_TITLE);
    mockDispatch.mockClear();
    await userEvent.click(btn);
    const requested = mockDispatch.mock.calls.map((c) => c[0]).filter((a) => a && a.type === 'studioAssets/requestTeach');
    expect(requested).toHaveLength(1);
    expect(requested[0].payload).toEqual({ focus: null });
  });

  test('offline the toolbar button is disabled and names the reason', async () => {
    mockState = { ...baseState(), tasks: { heartbeatStatus: 'disconnected' } };
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    const btn = screen.getByRole('button', { name: DE.TOOLBAR_TEACH });
    expect(btn).toBeDisabled();
    expect(btn.getAttribute('title')).toBe(DE.TEACH_BLOCK_OFFLINE);
  });

  test('while Vormachen is open: the button, the LeaderToggle and the drive-to are locked', async () => {
    mockState = { ...baseState(), studioAssets: { teach: { open: true } } };
    setDriveToHandler.mockClear();
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    expect(screen.getByRole('button', { name: DE.TOOLBAR_TEACH })).toBeDisabled();
    expect(screen.getByTestId('leader-toggle').getAttribute('data-locked-reason')).toBe(DE.TEACH_BLOCK_UI_LOCKED);
    expect(screen.getByTestId('jog-panel').getAttribute('data-disabled')).toBe('true');
    const handler = setDriveToHandler.mock.calls.map((c) => c[0]).filter(Boolean).pop();
    toast.error.mockClear();
    await handler({ name: 'A', x: 0.2, y: 0, z: 0.05 });
    expect(toast.error).toHaveBeenCalledWith(DE.TEACH_DRIVE_BLOCKED);
    expect(mockRos.jogArm).not.toHaveBeenCalled();
  });

  test('with Vormachen closed the LeaderToggle carries no lock reason', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    expect(screen.getByTestId('leader-toggle').getAttribute('data-locked-reason')).toBe('');
    expect(screen.getByTestId('jog-panel').getAttribute('data-disabled')).toBe('false');
  });
});
