/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Audit `docs/plans/audit-G10-G12.md` §11.2 / §11.3 — the CLOUD SAVE path.
//
// `handleSave` shipped the FULL serializer output into `workflows.blockly_json`.
// Two measured consequences: `suggested-blocks` grows ~16 bytes per drag and is
// NEVER trimmed, so a long-lived document eventually crosses
// MAX_BLOCKLY_JSON_BYTES (256 KiB) and becomes unsaveable behind a German 413
// the student cannot act on; and `backpack` — one student's private clipboard —
// rode into a row that group siblings read, `clone_workflow` copies and a
// teacher can publish as a class template.
//
// The pure allowlist is covered in utils/__tests__/blocklyPayload.test.js. THIS
// file is the CALL SITE: what `createWorkflow` / `updateWorkflow` actually
// receive. A pure-function test alone cannot see the call site still passing
// `json`.

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
// page's hand-guide state through the real callback.
// Other tabs are NOT rendered — the '3d' tab would mount the real lazy UrdfTwin.
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
// The JogPanel stub exposes a trigger button wired to the REAL page
// callback (onHandGuideChange), so the sim-entry guard tests
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
    auth: { session: { access_token: 'jwt', user: { id: 'u1' } } },
    tasks: { heartbeatStatus: 'connected' },
  };
}

// Exactly the shape the editor hands over with the shipped plugin set loaded:
// three CORE serializer keys plus the two plugin ones.
const FULL_EDITOR_JSON = {
  blocks: { languageVersion: 0, blocks: [{ type: 'edubotics_home', id: 'b1' }] },
  variables: [{ name: 'Punkte', id: 'v1' }],
  workspaceComments: [{ id: 'c1', text: 'Erst greifen, dann ablegen' }],
  'suggested-blocks': {
    recentlyUsedBlocks: ['edubotics_home', 'edubotics_home'],
    defaultJsonForBlockLookup: { edubotics_home: { type: 'edubotics_home' } },
  },
  backpack: ['<block type="edubotics_grasp_object"/>'],
};

beforeEach(() => {
  mockState = baseState({ unsavedBlocklyJson: FULL_EDITOR_JSON });
  mockBlockly.mountCount = 0;
  mockDispatch.mockClear();
  mockRos.getObjectCatalog.mockClear();
  mockApi.createWorkflow.mockClear();
  mockApi.updateWorkflow.mockClear();
});

describe('WorkshopPage — what the cloud SAVE actually ships', () => {
  test('a NEW workflow carries the document and neither editor-plugin key', async () => {
    render(<WorkshopPage isActive />);
    await userEvent.click(await screen.findByTestId('save-button'));
    await waitFor(() => expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1));

    // The WHOLE request body, not just its blockly_json. Reading one field left
    // three mutations green — dropping `sim_scene` here, dropping it on update,
    // and dropping `name` on create — because a field this test never looks at
    // can vanish without a failure. The no-go zones and the sim scene live in
    // `sim_scene`; losing it on save loses the student's drawn zones silently.
    const body = mockApi.createWorkflow.mock.calls[0][1];
    expect(Object.keys(body).sort())
      .toEqual(['blockly_json', 'description', 'name', 'sim_scene']);
    expect(body.name).toBe('Neuer Workflow');

    const sent = body.blockly_json;
    expect(Object.keys(sent).sort())
      .toEqual(['blocks', 'variables', 'workspaceComments']);
    expect('backpack' in sent).toBe(false);
    expect('suggested-blocks' in sent).toBe(false);
  });

  test("the student's canvas notes SURVIVE — dropping them would destroy work", async () => {
    render(<WorkshopPage isActive />);
    await userEvent.click(await screen.findByTestId('save-button'));
    await waitFor(() => expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1));

    const sent = mockApi.createWorkflow.mock.calls[0][1].blockly_json;
    expect(sent.workspaceComments)
      .toEqual([{ id: 'c1', text: 'Erst greifen, dann ablegen' }]);
    expect(sent.blocks).toEqual(FULL_EDITOR_JSON.blocks);
    expect(sent.variables).toEqual(FULL_EDITOR_JSON.variables);
  });

  test('an UPDATE of an existing workflow is slimmed the same way', async () => {
    mockState = baseState({
      unsavedBlocklyJson: FULL_EDITOR_JSON,
      selectedWorkflowId: 'wf-1',
    });
    render(<WorkshopPage isActive />);
    await userEvent.click(await screen.findByTestId('save-button'));
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1));

    const body = mockApi.updateWorkflow.mock.calls[0][2];
    expect(Object.keys(body).sort()).toEqual(['blockly_json', 'sim_scene']);

    const sent = body.blockly_json;
    expect(Object.keys(sent).sort())
      .toEqual(['blocks', 'variables', 'workspaceComments']);
    expect(mockApi.createWorkflow).not.toHaveBeenCalled();
  });

  test('the Ziele/Positionen (`edubotics-destinations`) are part of the saved document', async () => {
    // The document serializer from the Sammlung round: the store of named
    // places travels in the workflow row, so a reload or a clone keeps them.
    const withDestinations = {
      ...FULL_EDITOR_JSON,
      'edubotics-destinations': {
        version: 1,
        entries: [{ id: 'd_00000001', name: 'Ablage', kind: 'pin', x: 0.18, y: -0.06, z: 0.01, source: 'camera' }],
      },
    };
    mockState = baseState({ unsavedBlocklyJson: withDestinations });
    render(<WorkshopPage isActive />);
    await userEvent.click(await screen.findByTestId('save-button'));
    await waitFor(() => expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1));

    const sent = mockApi.createWorkflow.mock.calls[0][1].blockly_json;
    expect(Object.keys(sent).sort())
      .toEqual(['blocks', 'edubotics-destinations', 'variables', 'workspaceComments']);
    expect(sent['edubotics-destinations']).toEqual(withDestinations['edubotics-destinations']);
  });

  test('the object the editor and AUTOSAVE share is not mutated', async () => {
    const before = JSON.stringify(FULL_EDITOR_JSON);
    render(<WorkshopPage isActive />);
    await userEvent.click(await screen.findByTestId('save-button'));
    await waitFor(() => expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(FULL_EDITOR_JSON)).toBe(before);
  });
});
