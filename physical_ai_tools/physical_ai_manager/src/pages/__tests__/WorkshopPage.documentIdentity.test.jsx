/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Owner decision R5-O1 (review round 5, MD2 / MD3 / MD5): three PRE-EXISTING
// ways one program's content reached another, for Blockly and code alike.
//
//   * MD2 — a save while the next program is still loading wrote the OLD
//     program into the NEW one's row: the selected id had already moved, the
//     document had not. A save is refused while a program loads (the toolbar
//     button and Strg+S go through the same save), and a save always targets
//     the document the editor HOLDS — a failed load holds no cloud row.
//   * MD3 — a version restore of A that landed after B was opened replaced
//     B's editor, and the next save wrote A into B. A restore result is
//     dropped unless it belongs to the document open when it was asked for,
//     and no document switch starts while a restore is on its way.
//   * MD5 — Start awaits the breakpoints and the recordings before the run is
//     marked running; a switch in that window started A's run while B was
//     shown. From Start until the run is marked running (or failed) every
//     document-changing action is refused, and a run whose document changed
//     anyway is not started.
//
// The harness is WorkshopPage.switchLock.test.jsx's, with two differences: the
// REAL ToolbarButtons (so Strg+S is the real window binding) over the real
// `blockly/core`, and stubs that expose what the page hands the editors and
// RunControls.

import React from 'react';
import {
  render, screen, act, waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import WorkshopPage from '../WorkshopPage';
import { DE } from '../../components/Workshop/blocks/messages_de';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockPage = vi.hoisted(() => ({
  blockly: null, code: null, run: null, history: null, template: null, gallery: null,
}));
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  default: function MockBlocklyWorkspace(props) {
    mockPage.blockly = props;
    return <pre data-testid="blockly-workspace">{JSON.stringify(props.initialJson)}</pre>;
  },
}));
vi.mock('../../components/Workshop/code/CodeWorkspace', () => ({
  __esModule: true,
  default: function MockCodeWorkspace(props) {
    mockPage.code = props;
    return <pre data-testid="code-workspace">{JSON.stringify(props.files)}</pre>;
  },
}));
vi.mock('../../components/Workshop/RunControls', () => ({
  __esModule: true,
  default: (props) => {
    mockPage.run = props;
    return <div data-testid="run-controls" />;
  },
}));
vi.mock('../../components/Workshop/TemplatePicker', () => ({
  __esModule: true,
  default: (props) => {
    mockPage.template = props;
    return (
      <button type="button" data-testid="pick-other" onClick={() => props.onPicked({ id: 'wf-other' })}>
        anderes
      </button>
    );
  },
}));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({
  __esModule: true,
  default: (props) => {
    mockPage.history = props;
    return <div data-testid="version-history" />;
  },
}));
vi.mock('../../components/Workshop/GalleryTab', () => ({
  __esModule: true,
  default: (props) => {
    mockPage.gallery = props;
    return <div data-testid="gallery-tab" />;
  },
}));
vi.mock('../../components/Workshop/RightDock', () => ({ __esModule: true, default: () => <div data-testid="right-dock" /> }));
vi.mock('../../components/Workshop/SimStage', () => ({ __esModule: true, default: () => <div data-testid="sim-stage" /> }));
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div data-testid="camera-feed" /> }));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/teach/TeachHost', () => ({ __esModule: true, default: () => <div data-testid="teach-host" /> }));
vi.mock('../../components/Workshop/JogPanel', () => ({ __esModule: true, default: () => <div data-testid="jog-panel" /> }));
vi.mock('../../hooks/useRsBridgeStatus', () => ({
  __esModule: true,
  default: () => ({ available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false }),
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
// ONE object for the file: the page's catalog effect depends on the
// function's identity, and a fresh one per render re-fetched forever.
const mockRos = vi.hoisted(() => ({
  getObjectCatalog: vi.fn(() => Promise.resolve({ success: true, type_names: ['wuerfel'], labels_de: ['Würfel'] })),
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
    subscribeToWorkflowStatus: vi.fn(), subscribeToWorkflowSensors: vi.fn(), connected: true,
  }),
}));
vi.mock('../../components/Workshop/useAutosave', () => ({
  __esModule: true,
  useAutosave: () => ({ lastSavedAt: null }),
  autosaveSessionScope: () => 'session-1',
  formatAutosaveAge: () => '',
}));
vi.mock('idb-keyval', () => ({
  get: vi.fn(async () => undefined), set: vi.fn(async () => undefined), del: vi.fn(async () => undefined),
}));
const mockApi = vi.hoisted(() => ({
  getWorkflow: vi.fn(),
  createWorkflow: vi.fn(() => Promise.resolve({ id: 'wf-new' })),
  updateWorkflow: vi.fn(() => Promise.resolve({})),
  submitWorkflow: vi.fn(() => Promise.resolve({ id: 'sub-1' })),
}));
vi.mock('../../services/workflowApi', () => ({ __esModule: true, ...mockApi }));
const mockToast = vi.hoisted(() => Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() }));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: mockToast,
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

const SCENE = { version: 1, objects: [], zones: [] };
const ROWS = {
  'wf-py': {
    id: 'wf-py', blockly_json: {}, sim_scene: SCENE, code_language: 'python',
    code_files: { 'main.py': 'import robot\nrobot.log("A")\n' },
  },
  'wf-py2': {
    id: 'wf-py2', blockly_json: {}, sim_scene: SCENE, code_language: 'python',
    code_files: { 'main.py': 'import robot\nrobot.log("B")\n' },
  },
  'wf-blk': {
    id: 'wf-blk', blockly_json: { blocks: { blocks: [{ type: 'edubotics_home', id: 'A' }] } },
    sim_scene: SCENE, code_language: '', code_files: {},
  },
  'wf-blk2': {
    id: 'wf-blk2', blockly_json: { blocks: { blocks: [{ type: 'edubotics_home', id: 'B' }] } },
    sim_scene: SCENE, code_language: '', code_files: {},
  },
};
const NOTATIONS = [
  ['a Python program', 'wf-py', 'wf-py2', 'code-workspace'],
  ['a Blockly program', 'wf-blk', 'wf-blk2', 'blockly-workspace'],
];
// The save body a notation's content lives in: a code program's files, a
// Blockly program's blocks.
const content = (body) => JSON.stringify(body.code_language ? body.code_files : body.blockly_json);

let pending;
beforeEach(() => {
  mockState = baseState();
  mockDispatch.mockClear();
  pending = {};
  mockApi.getWorkflow.mockReset();
  // A row in `pending` arrives only when the test lets it.
  mockApi.getWorkflow.mockImplementation((_t, id) => {
    if (pending[id]) return new Promise((resolve, reject) => { pending[id] = { resolve, reject }; });
    return Promise.resolve(ROWS[id]);
  });
  mockApi.createWorkflow.mockClear();
  mockApi.updateWorkflow.mockClear();
  mockApi.updateWorkflow.mockImplementation(() => Promise.resolve({}));
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  Object.keys(mockPage).forEach((k) => { mockPage[k] = null; });
  try { window.localStorage.clear(); } catch (_) { /* jsdom */ }
});

async function openFirst(id, testId) {
  mockState = baseState({ selectedWorkflowId: id });
  const utils = render(<WorkshopPage isActive />);
  await screen.findByTestId(testId);
  return utils;
}

// The student edits the open document: code through the host's onFilesChange,
// blocks through the canvas's onChange (what BlocklyWorkspace reports).
function edit(testId, marker) {
  act(() => {
    if (testId === 'code-workspace') {
      mockPage.code.onFilesChange({ 'main.py': `import robot\nrobot.log("${marker}")\n` });
    } else {
      mockPage.blockly.onChange({ blocks: { blocks: [{ type: 'edubotics_home', id: marker }] } });
    }
  });
}

// Strg+S as the browser delivers it: a keydown on the window, which the REAL
// ToolbarButtons binds to the same save as the button.
const pressCtrlS = () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 's', ctrlKey: true })); };
const saves = () => mockApi.updateWorkflow.mock.calls.map(([, id, body]) => [id, content(body)]);

describe.each(NOTATIONS)('MD2 — no save while %s loads, and a save goes to the program the editor holds', (_l, a, b, testId) => {
  test('the button and Strg+S are refused in German while the next program loads; after it arrived they save IT', async () => {
    const { rerender } = await openFirst(a, testId);
    edit(testId, 'A-bearbeitet');
    pending[b] = true;
    mockState = baseState({ selectedWorkflowId: b });
    rerender(<WorkshopPage isActive />);
    await userEvent.click(screen.getByRole('button', { name: DE.TOOLBAR_SAVE }));
    await act(async () => { pressCtrlS(); await Promise.resolve(); });
    expect(mockApi.updateWorkflow).not.toHaveBeenCalled();
    expect(mockApi.createWorkflow).not.toHaveBeenCalled();
    expect(mockToast.error).toHaveBeenCalledWith(DE.DOCUMENT_LOADING);
    expect(mockToast.error.mock.calls.filter(([m]) => m === DE.DOCUMENT_LOADING).length).toBe(2);
    await act(async () => { pending[b].resolve(ROWS[b]); });
    await screen.findByTestId(testId);
    await act(async () => { pressCtrlS(); await Promise.resolve(); });
    // The program that arrived, never the one that was open before.
    expect(saves()).toEqual([[b, content(ROWS[b])]]);
  });

  test('a program that failed to load holds no cloud row: a save creates one and never overwrites it', async () => {
    const { rerender } = await openFirst(a, testId);
    edit(testId, 'A-bearbeitet');
    pending[b] = true;
    mockState = baseState({ selectedWorkflowId: b });
    rerender(<WorkshopPage isActive />);
    await act(async () => { pending[b].reject(new Error('offline')); });
    await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
    expect(mockApi.updateWorkflow.mock.calls.map(([, id]) => id)).not.toContain(b);
  });
});

describe.each(NOTATIONS)('MD2 — a save the moment %s arrived, before React drew it', (_l, a, b, testId) => {
  test('writes the program that arrived, never the previous one\'s last edit', async () => {
    const { rerender } = await openFirst(a, testId);
    edit(testId, 'A-bearbeitet');
    pending[b] = true;
    // The store still holds A's last edit, as the real one does until the
    // page replaces it (the mocked store never changes on its own).
    const lastEditOfA = { blocks: { blocks: [{ type: 'edubotics_home', id: 'A-bearbeitet' }] } };
    mockState = baseState({ selectedWorkflowId: b, unsavedBlocklyJson: lastEditOfA });
    rerender(<WorkshopPage isActive />);
    // Inside ONE act: the row arrives, and Strg+S lands before React renders.
    await act(async () => {
      pending[b].resolve(ROWS[b]);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      pressCtrlS();
    });
    expect(saves()).toEqual([[b, content(ROWS[b])]]);
  });
});

test('Blockly: a canvas still showing the previous program is never what a save writes', async () => {
  const { rerender } = await openFirst('wf-blk', 'blockly-workspace');
  // A live canvas holding A — mounted for A, then B was opened. (The real
  // canvas is unmounted a render later; until then it is still A's.)
  const canvasOfA = new Blockly.Workspace();
  Blockly.serialization.workspaces.load(
    { blocks: { languageVersion: 0, blocks: [{ type: 'text', id: 'A-canvas', x: 0, y: 0, fields: { TEXT: 'A' } }] } },
    canvasOfA,
  );
  act(() => { mockPage.blockly.onWorkspaceReady(canvasOfA); });
  mockState = baseState({ selectedWorkflowId: 'wf-blk2' });
  rerender(<WorkshopPage isActive />);
  await waitFor(() => expect(screen.getByTestId('blockly-workspace').textContent).toContain('"B"'));
  await act(async () => { pressCtrlS(); await Promise.resolve(); });
  expect(saves()).toEqual([['wf-blk2', content(ROWS['wf-blk2'])]]);
  // The positive control: a canvas mounted for the open program IS saved.
  mockApi.updateWorkflow.mockClear();
  act(() => { mockPage.blockly.onWorkspaceReady(canvasOfA); });
  await act(async () => { pressCtrlS(); await Promise.resolve(); });
  expect(saves().map(([id, body]) => [id, body.includes('A-canvas')])).toEqual([['wf-blk2', true]]);
  canvasOfA.dispose();
});

describe.each(NOTATIONS)('MD3 — a version restore of %s that lands after another program was opened', (_l, a, b, testId) => {
  test('is dropped: the open program stays, and the next save writes the open program', async () => {
    const { rerender } = await openFirst(a, testId);
    const restoreOfA = mockPage.history.onRestore;
    mockState = baseState({ selectedWorkflowId: b });
    rerender(<WorkshopPage isActive />);
    await waitFor(() => expect(screen.getByTestId(testId).textContent).toContain('B'));
    const shownB = screen.getByTestId(testId).textContent;
    const restoredA = testId === 'code-workspace'
      ? { id: a, code_language: 'python', blockly_json: {}, code_files: { 'main.py': 'import robot\nrobot.log("A-alt")\n' } }
      : { id: a, code_language: '', blockly_json: { blocks: { blocks: [{ type: 'edubotics_home', id: 'A-alt' }] } } };
    await act(async () => { restoreOfA(restoredA); await Promise.resolve(); });
    expect(screen.getByTestId(testId).textContent).toBe(shownB);
    await act(async () => { pressCtrlS(); await Promise.resolve(); });
    expect(saves()).toEqual([[b, content(ROWS[b])]]);
    // The positive control: a restore of the OPEN program does show.
    const restoredB = testId === 'code-workspace'
      ? { id: b, code_language: 'python', blockly_json: {}, code_files: { 'main.py': 'import robot\nrobot.log("B-alt")\n' } }
      : { id: b, code_language: '', blockly_json: { blocks: { blocks: [{ type: 'edubotics_home', id: 'B-alt' }] } } };
    await act(async () => { mockPage.history.onRestore(restoredB); await Promise.resolve(); });
    expect(screen.getByTestId(testId).textContent).toContain('B-alt');
  });

  test('while a restore is on its way no program is opened, created or restored, and nothing is saved', async () => {
    await openFirst(a, testId);
    // The „Öffnen" list is open from before the restore began.
    await userEvent.click(screen.getByRole('button', { name: /Öffnen/ }));
    act(() => { mockPage.history.onRestoringChange(true); });
    const open = screen.getByRole('button', { name: /Öffnen/ });
    expect(open).toBeDisabled();
    expect(open.getAttribute('title')).toBe(DE.VERSION_RESTORE_IN_FLIGHT);
    const neu = screen.getByRole('button', { name: /^Neu/ });
    expect(neu).toBeDisabled();
    expect(neu.getAttribute('title')).toBe(DE.VERSION_RESTORE_IN_FLIGHT);
    // A choice made anyway (a popover opened before) is refused in German.
    mockDispatch.mockClear();
    act(() => { mockPage.template.onPicked({ id: 'wf-other' }); });
    expect(mockDispatch.mock.calls.map(([x]) => x && x.type)).not.toContain('workshop/openWorkflow');
    expect(mockToast.error).toHaveBeenCalledWith(DE.VERSION_RESTORE_IN_FLIGHT);
    await act(async () => { pressCtrlS(); await Promise.resolve(); });
    expect(mockApi.updateWorkflow).not.toHaveBeenCalled();
    act(() => { mockPage.history.onRestoringChange(false); });
    expect(screen.getByRole('button', { name: /Öffnen/ })).toBeEnabled();
  });

  test('while a save is on its way the history says why and takes no restore', async () => {
    await openFirst(a, testId);
    let finish;
    mockApi.updateWorkflow.mockImplementation(() => new Promise((r) => { finish = r; }));
    await act(async () => { pressCtrlS(); await Promise.resolve(); });
    expect(mockPage.history.lockedReason).toBe(DE.SAVE_IN_FLIGHT);
    await act(async () => { finish({}); });
    expect(mockPage.history.lockedReason).toBeNull();
  });
});

describe.each(NOTATIONS)('MD5 — from Start until %s runs, nothing replaces the document', (_l, a, b, testId) => {
  test('every document-changing action says „Das Programm startet gerade …" and does nothing', async () => {
    await openFirst(a, testId);
    // The „Öffnen" list is open from before Start was pressed.
    await userEvent.click(screen.getByRole('button', { name: /Öffnen/ }));
    act(() => { mockPage.run.onStartingChange(true); });
    const open = screen.getByRole('button', { name: /Öffnen/ });
    expect(open).toBeDisabled();
    expect(open.getAttribute('title')).toBe(DE.PROGRAM_STARTING);
    expect(screen.getByRole('button', { name: /^Neu/ })).toBeDisabled();
    expect(mockPage.history.lockedReason).toBe(DE.PROGRAM_STARTING);
    mockDispatch.mockClear();
    act(() => { mockPage.template.onPicked({ id: 'wf-other' }); });
    expect(mockDispatch.mock.calls.map(([x]) => x && x.type)).not.toContain('workshop/openWorkflow');
    expect(mockToast.error).toHaveBeenCalledWith(DE.PROGRAM_STARTING);
    await userEvent.click(screen.getByRole('button', { name: 'Galerie' }));
    await screen.findByTestId('gallery-tab');
    expect(mockPage.gallery.lockedReason).toBe(DE.PROGRAM_STARTING);
    act(() => { mockPage.run.onStartingChange(false); });
    expect(mockPage.gallery.lockedReason).toBeNull();
  });

  test('the run is tied to the open document: RunControls gets its id and a token that moves when it is replaced', async () => {
    const { rerender } = await openFirst(a, testId);
    expect(mockPage.run.workflowId).toBe(a);
    const before = mockPage.run.getDocumentToken();
    mockState = baseState({ selectedWorkflowId: b });
    rerender(<WorkshopPage isActive />);
    await waitFor(() => expect(mockPage.run.workflowId).toBe(b));
    expect(mockPage.run.getDocumentToken()).not.toBe(before);
  });
});

test('Blockly: after another program opened, Start runs THAT program, never the last edit of the one before', async () => {
  mockState = baseState({ selectedWorkflowId: 'wf-blk' });
  const { rerender } = render(<WorkshopPage isActive />);
  await screen.findByTestId('blockly-workspace');
  edit('blockly-workspace', 'A-bearbeitet');
  expect(JSON.stringify(mockPage.run.blocklyJson)).toContain('A-bearbeitet');
  mockState = baseState({ selectedWorkflowId: 'wf-blk2' });
  rerender(<WorkshopPage isActive />);
  await waitFor(() => expect(screen.getByTestId('blockly-workspace').textContent).toContain('"B"'));
  expect(mockPage.run.blocklyJson).toEqual(ROWS['wf-blk2'].blockly_json);
});
