/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-34 (React half) — Roboter Studio was the ONE student page without an
// on-screen toast cap.
//
// MEASURED: the output blocks inside „wiederhole fortlaufend" emit toasts at
// ~17.7/s, so a 15 s loop stacks ~265 of them — enough to cover the editor AND
// the run-control strip, the Stopp button included, leaving the student no way
// to end the run producing them. The other five student pages (ControlPanel,
// TrainingPage, EditDatasetPage, RecordPage, InferencePage) all run the same
// TOAST_LIMIT = 3 + useToasterStore dismissal loop; this asserts WorkshopPage
// now does too, with the SAME mechanism rather than a new one.
//
// The server-side emit rate limit is a separate change — this covers only the
// on-screen stack.

import React from 'react';
import { render } from '@testing-library/react';
import toast from 'react-hot-toast';
import WorkshopPage from '../WorkshopPage';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

// The toast stack the page reads. Mutable so a test can hand the page a
// deep stack and assert which ids get dismissed.
const mockToaster = vi.hoisted(() => ({ toasts: [] }));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    dismiss: vi.fn(),
  }),
  useToasterStore: () => ({ toasts: mockToaster.toasts }),
}));

// ── Children: import-safe no-op stubs (same set as WorkshopPage.simToggle). ──
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({ __esModule: true, default: () => <div data-testid="blockly-workspace" /> }));
vi.mock('../../components/Workshop/RightDock', () => ({ __esModule: true, default: () => <div data-testid="right-dock" /> }));
vi.mock('../../components/Workshop/SimStage', () => ({ __esModule: true, default: () => <div data-testid="sim-stage" /> }));
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
// Vormachen: TeachHost is a stub (the overlay has its own tests, and its
// leader-mode child would read `s.ros`, which these mock states lack), and the
// bridge probe is stubbed so no page test fetches localhost:8769.
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

// react-hot-toast hands the stack NEWEST-FIRST, and `visible` is false for a
// toast already on its way out.
function stack(n, { visible = true } = {}) {
  return Array.from({ length: n }, (_, i) => ({ id: `t${i}`, visible }));
}

beforeEach(() => {
  mockState = baseState();
  mockToaster.toasts = [];
  mockDispatch.mockClear();
  toast.dismiss.mockClear();
  mockRos.getObjectCatalog.mockClear();
});

describe('WorkshopPage — on-screen toast cap (RS-34)', () => {
  test('a stack at or below the limit of 3 dismisses nothing', () => {
    mockToaster.toasts = stack(3);
    render(<WorkshopPage isActive />);
    expect(toast.dismiss).not.toHaveBeenCalled();
  });

  test('everything past the 3 newest is dismissed', () => {
    mockToaster.toasts = stack(6);
    render(<WorkshopPage isActive />);

    const dismissed = toast.dismiss.mock.calls.map(([id]) => id);
    // Indices 0..2 are the three newest and survive; 3..5 are the overflow.
    expect(dismissed).toEqual(['t3', 't4', 't5']);
  });

  test('the 15 s runaway-loop case: ~265 toasts collapse to 3 on screen', () => {
    // 17.7 toasts/s x 15 s, the measured „wiederhole fortlaufend" rate.
    mockToaster.toasts = stack(265);
    render(<WorkshopPage isActive />);

    expect(toast.dismiss).toHaveBeenCalledTimes(262);
    // The three newest are never dismissed, so the Stopp button stays reachable.
    const dismissed = toast.dismiss.mock.calls.map(([id]) => id);
    expect(dismissed).not.toContain('t0');
    expect(dismissed).not.toContain('t1');
    expect(dismissed).not.toContain('t2');
  });

  test('already-invisible toasts are not counted or re-dismissed', () => {
    // `visible: false` is a toast already animating out — dismissing it again
    // would be a no-op, and counting it would evict a VISIBLE one early.
    mockToaster.toasts = [
      ...stack(2),
      { id: 'gone', visible: false },
      { id: 'keep', visible: true },
      { id: 'over', visible: true },
    ];
    render(<WorkshopPage isActive />);

    const dismissed = toast.dismiss.mock.calls.map(([id]) => id);
    expect(dismissed).not.toContain('gone');
    expect(dismissed).toEqual(['over']); // t0, t1, keep are the 3 visible newest
  });

  test('uses dismiss (keeps the exit animation), never remove', () => {
    mockToaster.toasts = stack(5);
    render(<WorkshopPage isActive />);
    expect(toast.dismiss).toHaveBeenCalled();
    expect(toast.remove).toBeUndefined(); // not on the mock, and not used
  });

  // THE case the cap exists for, and the one the five tests above cannot see.
  // All of them assign `mockToaster.toasts` BEFORE render(), so the effect's
  // dependency array is never exercised: a MOUNT-ONLY cap — `}, []);`, i.e.
  // exactly the broken version — passes every one of them, including the one
  // named „the 15 s runaway-loop case". A runaway loop starts AFTER mount.
  // Proven by mutation: with `[toasts]` emptied, this test fails and those five
  // still pass.
  test('a stack that grows AFTER mount is capped (the loop starts later)', () => {
    mockToaster.toasts = [];
    const { rerender } = render(<WorkshopPage isActive />);
    expect(toast.dismiss).not.toHaveBeenCalled();

    // The student presses Start; „wiederhole fortlaufend" begins stacking.
    mockToaster.toasts = stack(20);
    rerender(<WorkshopPage isActive />);

    const dismissed = toast.dismiss.mock.calls.map(([id]) => id);
    expect(dismissed).toHaveLength(17);
    expect(dismissed).not.toContain('t0');
    expect(dismissed).not.toContain('t1');
    expect(dismissed).not.toContain('t2');
  });
});
