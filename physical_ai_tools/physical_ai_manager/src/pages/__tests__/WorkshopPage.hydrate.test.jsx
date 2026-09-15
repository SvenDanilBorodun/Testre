/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Editor state across the two events that used to REMOUNT the Blockly editor
// under a working student, and the one save path.
//
// (1) The hydrate effect keyed on the access token's VALUE, and Supabase
//     rotates that string on every TOKEN_REFRESHED: the workflow was re-fetched
//     and the editor remounted from the cloud copy, dropping unsaved edits.
// (2) The first save of an unsaved workflow dispatched the new id, which
//     re-ran the same effect: a fetch of the document the editor already IS.
// (3) Saves never JOIN a save in flight: a joined caller was told `ok` for a
//     document it never sent. A later caller gets ONE coalesced follow-up that
//     serialises when it starts; the follow-up of a create is an update.
//
// Same mocking idiom as WorkshopPage.savePayload.test.jsx; `mountCount` counts
// BlocklyWorkspace mounts, so a remount is observable.

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
// Only the control tab renders — the '3d' tab would mount the real lazy
// UrdfTwin.
vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div data-testid="right-dock">
      {(tabs || [])
        .filter((t) => t.id === 'control')
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
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/RunControls', () => ({ __esModule: true, default: () => <div data-testid="run-controls" /> }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div data-testid="camera-feed" /> }));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div data-testid="template-picker" /> }));
// ToolbarButtons exposes the real `onSave` (= handleSave) as a button.
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: function MockToolbarButtons({ onSave }) {
    return (
      <button type="button" data-testid="save-button" onClick={() => onSave && onSave()} />
    );
  },
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
// JogPanel: an import-safe stub (the shared idiom's shape).
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
const mockApi = vi.hoisted(() => ({
  getWorkflow: vi.fn(() => Promise.resolve(null)),
  createWorkflow: vi.fn(() => Promise.resolve({ id: 'wf-new' })),
  updateWorkflow: vi.fn(() => Promise.resolve({})),
}));
vi.mock('../../services/workflowApi', () => ({ __esModule: true, ...mockApi }));
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

function baseState(over = {}, token = 'jwt') {
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
    auth: { session: { access_token: token, user: { id: 'u1' } } },
    tasks: { heartbeatStatus: 'connected' },
  };
}

const DOC = (tag) => ({
  blocks: { languageVersion: 0, blocks: [{ type: 'edubotics_home', id: tag }] },
});

// Let every pending promise callback and the effects they schedule run.
const settle = async () => {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const successToasts = () => toast.success.mock.calls.filter(([m]) => m === 'Gespeichert.').length;

beforeEach(() => {
  mockBlockly.mountCount = 0;
  mockDispatch.mockClear();
  mockApi.getWorkflow.mockReset();
  mockApi.getWorkflow.mockImplementation(() => Promise.resolve(null));
  mockApi.createWorkflow.mockReset();
  mockApi.createWorkflow.mockImplementation(() => Promise.resolve({ id: 'wf-new' }));
  mockApi.updateWorkflow.mockReset();
  mockApi.updateWorkflow.mockImplementation(() => Promise.resolve({}));
  toast.success.mockClear();
  toast.error.mockClear();
});

describe('WorkshopPage — hydration never remounts the editor under the student', () => {
  test('a Supabase token refresh (new access_token string) neither refetches nor remounts', async () => {
    mockState = baseState({ selectedWorkflowId: 'wf-1' });
    const { rerender } = render(<WorkshopPage isActive />);
    await waitFor(() => expect(mockApi.getWorkflow).toHaveBeenCalledTimes(1));
    await settle();
    const mounts = mockBlockly.mountCount;
    expect(mounts).toBeGreaterThan(0);

    mockState = baseState({ selectedWorkflowId: 'wf-1' }, 'jwt-refreshed');
    rerender(<WorkshopPage isActive />);
    await settle();
    expect(mockApi.getWorkflow).toHaveBeenCalledTimes(1);
    expect(mockBlockly.mountCount).toBe(mounts);
  });

  test('a token that first APPEARS (null → jwt) hydrates the selected workflow', async () => {
    mockState = baseState({ selectedWorkflowId: 'wf-1' }, null);
    const { rerender } = render(<WorkshopPage isActive />);
    await settle();
    expect(mockApi.getWorkflow).not.toHaveBeenCalled();

    mockState = baseState({ selectedWorkflowId: 'wf-1' }, 'jwt');
    rerender(<WorkshopPage isActive />);
    await waitFor(() => expect(mockApi.getWorkflow).toHaveBeenCalledTimes(1));
    expect(mockApi.getWorkflow).toHaveBeenCalledWith('jwt', 'wf-1');
  });

  test('the first save of an unsaved workflow does not re-fetch or remount the id it created', async () => {
    mockState = baseState({ unsavedBlocklyJson: DOC('a') });
    const { rerender } = render(<WorkshopPage isActive />);
    await settle();
    const mounts = mockBlockly.mountCount;

    await userEvent.click(await screen.findByTestId('save-button'));
    await waitFor(() => expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1));
    await settle();
    const selected = mockDispatch.mock.calls
      .map(([a]) => a)
      .filter((a) => a && a.type === 'workshop/setSelectedWorkflowId');
    expect(selected.map((a) => a.payload)).toEqual(['wf-new']);

    // The store answers the dispatch: the page now holds the id it created.
    mockState = baseState({ unsavedBlocklyJson: null, selectedWorkflowId: 'wf-new' });
    rerender(<WorkshopPage isActive />);
    await settle();
    expect(mockApi.getWorkflow).not.toHaveBeenCalledWith(expect.anything(), 'wf-new');
    expect(mockBlockly.mountCount).toBe(mounts);
  });
});

describe('WorkshopPage — one save path, never a joined save', () => {
  test('two clicks before the create resolves: ONE create, then ONE update of the new id', async () => {
    const create = deferred();
    mockApi.createWorkflow.mockImplementation(() => create.promise);
    mockState = baseState({ unsavedBlocklyJson: DOC('a') });
    render(<WorkshopPage isActive />);
    const button = await screen.findByTestId('save-button');

    await userEvent.click(button);
    await userEvent.click(button);
    await settle();
    expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1);
    expect(mockApi.updateWorkflow).not.toHaveBeenCalled();

    await act(async () => { create.resolve({ id: 'wf-new' }); });
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1));
    await settle();
    expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1);
    expect(mockApi.updateWorkflow.mock.calls[0][1]).toBe('wf-new');
    // One „Gespeichert." per COMPLETED save.
    expect(successToasts()).toBe(2);
  });

  test('three clicks during an update: two updates, the follow-up sends the document as of ITS start', async () => {
    const first = deferred();
    mockApi.updateWorkflow
      .mockImplementationOnce(() => first.promise)
      .mockImplementation(() => Promise.resolve({}));
    mockState = baseState({ selectedWorkflowId: 'wf-1', unsavedBlocklyJson: DOC('a') });
    const { rerender } = render(<WorkshopPage isActive />);
    const button = await screen.findByTestId('save-button');
    await settle();

    await userEvent.click(button);
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1));
    expect(mockApi.updateWorkflow.mock.calls[0][2].blockly_json).toEqual(DOC('a'));

    mockState = baseState({ selectedWorkflowId: 'wf-1', unsavedBlocklyJson: DOC('b') });
    rerender(<WorkshopPage isActive />);
    await userEvent.click(button);
    await userEvent.click(button);
    // The student keeps editing after the clicks, before the first save lands.
    mockState = baseState({ selectedWorkflowId: 'wf-1', unsavedBlocklyJson: DOC('c') });
    rerender(<WorkshopPage isActive />);
    await settle();
    expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1);

    await act(async () => { first.resolve({}); });
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(2));
    await settle();
    expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(2);
    expect(mockApi.updateWorkflow.mock.calls[1][1]).toBe('wf-1');
    expect(mockApi.updateWorkflow.mock.calls[1][2].blockly_json).toEqual(DOC('c'));
    expect(mockApi.createWorkflow).not.toHaveBeenCalled();
  });

  test('a failed save still lets the coalesced follow-up run', async () => {
    const first = deferred();
    mockApi.updateWorkflow
      .mockImplementationOnce(() => first.promise)
      .mockImplementation(() => Promise.resolve({}));
    mockState = baseState({ selectedWorkflowId: 'wf-1', unsavedBlocklyJson: DOC('a') });
    render(<WorkshopPage isActive />);
    const button = await screen.findByTestId('save-button');
    await userEvent.click(button);
    await userEvent.click(button);
    await act(async () => { first.reject(new Error('Netz weg')); });
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(2));
    expect(toast.error).toHaveBeenCalledWith('Speichern fehlgeschlagen: Netz weg');
  });
});
