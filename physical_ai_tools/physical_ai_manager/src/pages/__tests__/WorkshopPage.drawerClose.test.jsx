/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Sammlung drawer belongs to the editor it was opened over. Redux keeps
// `studioAssets.drawer.open` across the Galerie switch and a tab change, so the
// page must close it when the editor goes away — otherwise it reappears over a
// freshly mounted editor. Same mocking idiom as WorkshopPage.simToggle.test.jsx.

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WorkshopPage from '../WorkshopPage';
import { act } from '@testing-library/react';
import { closeDrawer, openDrawer } from '../../features/workshop/studioAssetsSlice';

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
const mockBlockly = vi.hoisted(() => ({ mountCount: 0, provider: null }));
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  // Named + capitalized so react-hooks/rules-of-hooks recognizes it as a component.
  default: function MockBlocklyWorkspace({ sammlungProvider }) {
    mockBlockly.provider = sammlungProvider;
    React.useEffect(() => {
      mockBlockly.mountCount += 1;
    }, []);
    return <div data-testid="blockly-workspace" />;
  },
}));

// ── Right-region components — recognizable stubs so we can assert the swap. ──
// RightDock additionally renders the control/record tab panels (the JogPanel/
// RecordPanel mocks below expose trigger buttons) so the sim-entry guard tests
// can flip the page's recording/hand-guide state through the real callbacks.
// Other tabs are NOT rendered — the '3d' tab would mount the real lazy UrdfTwin.
vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div data-testid="right-dock">
      {(tabs || [])
        .filter((t) => t.id === 'control' || t.id === 'record')
        .map((t) => (
          <div key={t.id}>{t.render()}</div>
        ))}
    </div>
  ),
}));
vi.mock('../../components/Workshop/sammlung/SammlungDrawer', () => ({
  __esModule: true,
  default: () => <div data-testid="sammlung-drawer" />,
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
// Unlike the other page tests, this stub renders its `leading` prop: that is
// where the Editor/Galerie switch lives.
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: ({ leading }) => <div data-testid="toolbar-buttons">{leading}</div>,
}));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div data-testid="gallery-tab" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({ __esModule: true, default: () => <div data-testid="version-history" /> }));
// JogPanel/RecordPanel stubs expose trigger buttons wired to the REAL page
// callbacks (onHandGuideChange/onRecordingChange), so the sim-entry guard tests
// drive the page state exactly like a live panel would.
vi.mock('../../components/Workshop/JogPanel', () => ({
  __esModule: true,
  default: function MockJogPanel({ onHandGuideChange }) {
    return (
      <div data-testid="jog-panel">
        <button
          type="button"
          data-testid="jog-hand-guide-on"
          onClick={() => onHandGuideChange && onHandGuideChange(true)}
        />
      </div>
    );
  },
}));
vi.mock('../../components/Workshop/RecordPanel', () => ({
  __esModule: true,
  default: function MockRecordPanel({ onRecordingChange }) {
    return (
      <div data-testid="record-panel">
        <button
          type="button"
          data-testid="record-panel-recording-on"
          onClick={() => onRecordingChange && onRecordingChange(true)}
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
    studioAssets: { drawer: { open: true, tab: 'ziele', focusId: null, previewTempo: 1 } },
    auth: { session: { access_token: 'jwt', user: { id: 'u1' } } },
    tasks: { heartbeatStatus: 'connected' },
  };
}

const closeCalls = () => mockDispatch.mock.calls.filter(([a]) => a && a.type === closeDrawer.type);

beforeEach(() => {
  mockState = baseState();
  mockBlockly.mountCount = 0;
  mockDispatch.mockClear();
  mockRos.getObjectCatalog.mockClear();
});

describe('WorkshopPage — the Sammlung drawer closes with its editor', () => {
  test('an open drawer renders over the editor and stays open while the editor stays', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    expect(screen.getByTestId('sammlung-drawer')).toBeInTheDocument();
    expect(closeCalls()).toHaveLength(0);
  });

  test('a card or „Alle verwalten …" action opens the drawer on its tab and item', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    expect(mockBlockly.provider.getSnapshot().capabilities.drawer).toBe(true);
    mockDispatch.mockClear();
    act(() => { mockBlockly.provider.dispatchAction({ type: 'manage', tab: 'positionen', focusId: 'd_1' }); });
    expect(mockDispatch).toHaveBeenCalledWith(openDrawer({ tab: 'positionen', focusId: 'd_1' }));
    act(() => { mockBlockly.provider.dispatchAction({ type: 'manage', tab: 'variablen', focusId: null }); });
    expect(mockDispatch).toHaveBeenCalledWith(openDrawer({ tab: 'variablen', focusId: null }));
  });

  test('switching to Galerie closes the Sammlung drawer', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    mockDispatch.mockClear();
    await userEvent.click(screen.getByRole('button', { name: 'Galerie' }));
    expect(mockDispatch).toHaveBeenCalledWith(closeDrawer());
  });

  test('leaving the tab (isActive false) closes it too', async () => {
    const { rerender } = render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    mockDispatch.mockClear();
    rerender(<WorkshopPage isActive={false} />);
    expect(mockDispatch).toHaveBeenCalledWith(closeDrawer());
  });
});
