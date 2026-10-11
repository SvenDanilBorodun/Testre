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
  teach: null, autosave: null, sim: null, codeDocArgs: null,
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
vi.mock('../../components/Workshop/SimStage', () => ({
  __esModule: true,
  default: (props) => {
    mockPage.sim = props;
    return <div data-testid="sim-stage" />;
  },
}));
// The page's document serializer (m-d: a scene change must not re-serialize
// the canvas), wrapped, not replaced.
const mockSlim = vi.hoisted(() => ({ calls: 0 }));
vi.mock('../../utils/blocklyPayload', async (importOriginal) => {
  const real = await importOriginal();
  return {
    ...real,
    slimSavePayload: (json) => {
      mockSlim.calls += 1;
      return real.slimSavePayload(json);
    },
  };
});
// The code asset document, built for real; its arguments are recorded.
vi.mock('../../components/Workshop/code/codeAssetDocument', async (importOriginal) => {
  const real = await importOriginal();
  return {
    ...real,
    createCodeAssetDocument: (args) => {
      mockPage.codeDocArgs = args;
      return real.createCodeAssetDocument(args);
    },
  };
});
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div data-testid="camera-feed" /> }));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/teach/TeachHost', () => ({
  __esModule: true,
  default: (props) => {
    mockPage.teach = props;
    return <div data-testid="teach-host" />;
  },
}));
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
  useAutosave: (opts) => {
    mockPage.autosave = opts;
    return {};
  },
  autosaveSessionScope: () => 'session-1',
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
    // No version of either program is restored meanwhile.
    expect(mockPage.history.lockedReason).toBe(DE.DOCUMENT_LOADING);
    await act(async () => { pending[b].resolve(ROWS[b]); });
    await screen.findByTestId(testId);
    await act(async () => { pressCtrlS(); await Promise.resolve(); });
    // The program that arrived, never the one that was open before.
    expect(saves()).toEqual([[b, content(ROWS[b])]]);
  });

  test('a program that failed to load holds no cloud row: a save creates one and overwrites neither', async () => {
    const { rerender } = await openFirst(a, testId);
    edit(testId, 'A-bearbeitet');
    pending[b] = true;
    // What the editor falls back to after a failed load: the store's copy of
    // the last canvas edit (for a code program, a leftover from earlier).
    const leftover = { blocks: { blocks: [{ type: 'edubotics_home', id: 'Rest' }] } };
    mockState = baseState({ selectedWorkflowId: b, unsavedBlocklyJson: leftover });
    rerender(<WorkshopPage isActive />);
    await act(async () => { pending[b].reject(new Error('offline')); });
    // Nothing that writes to the cloud names either row.
    expect(mockPage.run.workflowId).toBeNull();
    expect(mockPage.history.workflowId).toBeNull();
    expect(mockPage.teach.workflowId).toBeNull();
    await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
    expect(mockApi.updateWorkflow).not.toHaveBeenCalled();
    expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(mockApi.createWorkflow.mock.calls[0][1].blockly_json)).toContain('Rest');
  });

  test('„Neu" while a saved program is open: the new document holds no row, and its save creates one', async () => {
    await openFirst(a, testId);
    await userEvent.click(screen.getByRole('button', { name: /^Neu/ }));
    await userEvent.click(screen.getByRole('button', { name: /Python/ }));
    await screen.findByTestId('code-workspace');
    expect(mockPage.run.workflowId).toBeNull();
    await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
    expect(mockApi.updateWorkflow).not.toHaveBeenCalled();
    expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1);
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
    edit(testId, 'B-bearbeitet');
    await act(async () => { mockPage.history.onRestore(restoredB); await Promise.resolve(); });
    expect(screen.getByTestId(testId).textContent).toContain('B-alt');
    // …and a Start afterwards runs the restored version, not the edit before it
    // (a code run sends its files, which the editor above shows).
    const runs = testId === 'blockly-workspace' ? JSON.stringify(mockPage.run.blocklyJson) : JSON.stringify(mockPage.code.files);
    expect(runs).toContain('B-alt');
  });

  test('a restore result that names another row is dropped', async () => {
    await openFirst(a, testId);
    const shownA = screen.getByTestId(testId).textContent;
    const other = testId === 'code-workspace'
      ? { id: 'wf-fremd', code_language: 'python', blockly_json: {}, code_files: { 'main.py': 'import robot\nrobot.log("fremd")\n' } }
      : { id: 'wf-fremd', code_language: '', blockly_json: { blocks: { blocks: [{ type: 'edubotics_home', id: 'fremd' }] } } };
    await act(async () => { mockPage.history.onRestore(other); await Promise.resolve(); });
    expect(screen.getByTestId(testId).textContent).toBe(shownA);
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

test('a „Neu" document opened while the unsaved one is being created does not receive its new id', async () => {
  // An unsaved Blockly document: the first save creates its row.
  mockState = baseState({ unsavedBlocklyJson: { blocks: { blocks: [{ type: 'edubotics_home', id: 'X' }] } } });
  render(<WorkshopPage isActive />);
  await screen.findByTestId('blockly-workspace');
  let created;
  mockApi.createWorkflow.mockImplementationOnce(() => new Promise((r) => { created = r; }));
  await act(async () => { pressCtrlS(); await Promise.resolve(); });
  expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1);
  // Meanwhile the student starts a new Python program — also unsaved.
  await userEvent.click(screen.getByRole('button', { name: /^Neu/ }));
  await userEvent.click(screen.getByRole('button', { name: /Python/ }));
  await screen.findByTestId('code-workspace');
  mockDispatch.mockClear();
  await act(async () => { created({ id: 'wf-x' }); await Promise.resolve(); await Promise.resolve(); });
  // X's row exists, but the Python program is not it.
  const selected = mockDispatch.mock.calls.map(([x]) => x)
    .filter((x) => x && x.type === 'workshop/setSelectedWorkflowId');
  expect(selected).toEqual([]);
  expect(mockPage.run.workflowId).toBeNull();
  await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
  expect(mockApi.updateWorkflow).not.toHaveBeenCalled();
  expect(mockApi.createWorkflow).toHaveBeenCalledTimes(2);
});

test('a cleared selection leaves the editor holding no row', async () => {
  const { rerender } = await openFirst('wf-blk', 'blockly-workspace');
  expect(mockPage.run.workflowId).toBe('wf-blk');
  // The store stops naming a program (it was deleted, the student signed out).
  mockState = baseState({ selectedWorkflowId: null });
  rerender(<WorkshopPage isActive />);
  await waitFor(() => expect(mockPage.run.workflowId).toBeNull());
  expect(mockPage.history.workflowId).toBeNull();
});

test('Blockly: a crash-recovery draft replaces the last edit — Start runs the draft that is shown', async () => {
  mockState = baseState();
  render(<WorkshopPage isActive />);
  await screen.findByTestId('blockly-workspace');
  // The canvas reported an edit before the draft arrived (the read is async).
  edit('blockly-workspace', 'vor-dem-Entwurf');
  const draft = { blocks: { blocks: [{ type: 'edubotics_home', id: 'Entwurf' }] } };
  act(() => { mockPage.autosave.onRestore(draft); });
  await waitFor(() => expect(screen.getByTestId('blockly-workspace').textContent).toContain('Entwurf'));
  expect(mockPage.run.blocklyJson).toEqual(draft);
});

test('a save queued behind another, then another program opens: the caller is told, never handed that program', async () => {
  const { rerender } = await openFirst('wf-py', 'code-workspace');
  let finish;
  mockApi.updateWorkflow.mockImplementationOnce(() => new Promise((r) => { finish = r; }));
  let queued;
  await act(async () => {
    mockPage.teach.saveWorkflowNow({ toastOnSuccess: false });
    await Promise.resolve();
  });
  await act(async () => {
    // Vormachen's keep: it needs the id of the program it saves.
    queued = mockPage.teach.saveWorkflowNow({ toastOnSuccess: false });
    await Promise.resolve();
  });
  mockState = baseState({ selectedWorkflowId: 'wf-py2' });
  rerender(<WorkshopPage isActive />);
  await waitFor(() => expect(screen.getByTestId('code-workspace').textContent).toContain('B'));
  await act(async () => { finish({}); });
  const result = await queued;
  expect(result.ok).toBe(false);
  expect(saves().map(([id]) => id)).toEqual(['wf-py']);
});

// ---------------------------------------------------------------------------
// R0-2: the toolbar's save status is the CLOUD save of the open document,
// judged by CONTENT (the real ToolbarButtons renders it). M1a: a simulator
// scene change is an edit. M1b: after a save, an onChange carrying the saved
// content (Blockly's late events of a rename) leaves „Gespeichert".
// ---------------------------------------------------------------------------

const statusText = () => screen.getByTestId('save-status').textContent.trim();
const SCENE_A = { version: 1, objects: [{ type: 'wuerfel', tag_id: 0, x: 0.2, y: 0, yaw: 0 }], zones: [] };
// The same content again, as Blockly's late event or a code keystroke that
// changes nothing would report it.
function editSame(testId, marker) { edit(testId, marker); }

describe.each(NOTATIONS)('R0-2 — the save status of %s', (_l, a, b, testId) => {
  test('an opened row with no edit is not „Nicht gespeichert"; an edit is; a save reads „gerade eben"', async () => {
    await openFirst(a, testId);
    expect(statusText()).toBe('Noch nicht gespeichert');
    edit(testId, 'A-1');
    expect(statusText()).toBe('Nicht gespeichert');
    await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
    expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1);
    expect(statusText()).toBe('Gespeichert: gerade eben');
  });

  test('an opened row shows its updated_at', async () => {
    const at = new Date(Date.now() - 3 * 60 * 1000).toISOString();
    mockApi.getWorkflow.mockImplementation((_t, id) => Promise.resolve({ ...ROWS[id], updated_at: at }));
    await openFirst(a, testId);
    expect(statusText()).toBe('Gespeichert: vor 3 Min.');
  });

  test('an edit DURING the save stays „Nicht gespeichert"', async () => {
    await openFirst(a, testId);
    edit(testId, 'A-1');
    let finish;
    mockApi.updateWorkflow.mockImplementation(() => new Promise((r) => { finish = r; }));
    await act(async () => { pressCtrlS(); await Promise.resolve(); });
    edit(testId, 'A-2');
    await act(async () => { finish({}); await Promise.resolve(); });
    expect(statusText()).toBe('Nicht gespeichert');
  });

  test('M1b: after a save, the saved content reported again (late events) stays „Gespeichert"; undoing back clears an edit', async () => {
    await openFirst(a, testId);
    edit(testId, 'A-1');
    await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
    expect(statusText()).toBe('Gespeichert: gerade eben');
    editSame(testId, 'A-1');
    expect(statusText()).toBe('Gespeichert: gerade eben');
    edit(testId, 'A-2');
    expect(statusText()).toBe('Nicht gespeichert');
    edit(testId, 'A-1');
    expect(statusText()).toBe('Gespeichert: gerade eben');
  });

  test('M1a: a simulator scene change is unsaved; saving it clears that', async () => {
    await openFirst(a, testId);
    await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
    expect(statusText()).toBe('Gespeichert: gerade eben');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    act(() => { mockPage.sim.onChange(SCENE_A); });
    expect(statusText()).toBe('Nicht gespeichert');
    await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
    expect(mockApi.updateWorkflow.mock.calls[mockApi.updateWorkflow.mock.calls.length - 1][2].sim_scene)
      .toEqual(SCENE_A);
    expect(statusText()).toBe('Gespeichert: gerade eben');
  });

  test('m-d: ten scene changes in a row serialize the document zero times, and end unsaved', async () => {
    await openFirst(a, testId);
    await act(async () => { pressCtrlS(); await Promise.resolve(); await Promise.resolve(); });
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    const before = mockSlim.calls;
    act(() => {
      for (let i = 0; i < 10; i += 1) {
        mockPage.sim.onChange({ ...SCENE_A, objects: [{ ...SCENE_A.objects[0], x: 0.2 + i * 0.001 }] });
      }
    });
    expect(mockSlim.calls).toBe(before);
    expect(statusText()).toBe('Nicht gespeichert');
  });

  test('opening another program resets the status', async () => {
    const { rerender } = await openFirst(a, testId);
    edit(testId, 'A-1');
    expect(statusText()).toBe('Nicht gespeichert');
    mockState = baseState({ selectedWorkflowId: b });
    rerender(<WorkshopPage isActive />);
    await waitFor(() => expect(screen.getByTestId(testId).textContent).toContain('B'));
    expect(statusText()).toBe('Noch nicht gespeichert');
  });
});

describe('R0-2 — Blockly: only a content change is an edit', () => {
  test('a selection or flyout event (isUiEvent) does not dirty', async () => {
    await openFirst('wf-blk', 'blockly-workspace');
    act(() => {
      mockPage.blockly.onChange(ROWS['wf-blk'].blockly_json, { isUiEvent: true, type: 'selected' });
    });
    expect(statusText()).toBe('Noch nicht gespeichert');
    act(() => {
      mockPage.blockly.onChange({ blocks: { blocks: [{ type: 'edubotics_home', id: 'neu' }] } }, { isUiEvent: false });
    });
    expect(statusText()).toBe('Nicht gespeichert');
  });
});

describe('R0-2 / K-1 — the code document\'s wiring', () => {
  test('K-1: CodeWorkspace gets onSave, and calling it (the editor\'s Strg+S) saves like the button', async () => {
    await openFirst('wf-py', 'code-workspace');
    edit('code-workspace', 'P-1');
    expect(typeof mockPage.code.onSave).toBe('function');
    await act(async () => { mockPage.code.onSave(); await Promise.resolve(); await Promise.resolve(); });
    expect(saves()).toEqual([['wf-py', JSON.stringify({ 'main.py': 'import robot\nrobot.log("P-1")\n' })]]);
    expect(statusText()).toBe('Gespeichert: gerade eben');
  });

  test('onFilesChange is the SAME function across re-renders, and it is the asset document\'s applyFiles', async () => {
    await openFirst('wf-py', 'code-workspace');
    const first = mockPage.code.onFilesChange;
    edit('code-workspace', 'P-1');
    edit('code-workspace', 'P-2');
    expect(mockPage.code.onFilesChange).toBe(first);
    expect(mockPage.codeDocArgs.applyFiles).toBe(first);
  });

  test('a rename or insertion through the asset document is an edit too', async () => {
    await openFirst('wf-py', 'code-workspace');
    act(() => { mockPage.codeDocArgs.applyFiles({ 'main.py': 'import robot\nrobot.log("umbenannt")\n' }); });
    expect(statusText()).toBe('Nicht gespeichert');
  });

  test('the toolbar is in code mode and the undo handle and the sidebar width ref are handed down', async () => {
    await openFirst('wf-py', 'code-workspace');
    expect(screen.queryByRole('button', { name: DE.TOOLBAR_ZOOM_FIT })).toBeNull();
    expect(mockPage.code.commandsRef).toEqual({ current: null });
    const undo = vi.fn();
    mockPage.code.commandsRef.current = { undo, redo: vi.fn() };
    await userEvent.click(screen.getByRole('button', { name: DE.TOOLBAR_UNDO }));
    expect(undo).toHaveBeenCalledTimes(1);
    // The drawer's anchor: the APPLIED width CodeWorkspace measures (m-3).
    expect(mockPage.code.sidebarWidthRef).toEqual({ current: null });
    mockPage.code.sidebarWidthRef.current = 222;
    expect(mockPage.codeDocArgs.getSidebarWidth()).toBe(222);
  });

  test('a code Ziel added to the open document is an edit', async () => {
    await openFirst('wf-py', 'code-workspace');
    act(() => {
      mockPage.codeDocArgs.store.add({ name: 'Ablage', kind: 'pin', source: 'sim', x: 0.2, y: 0, z: 0 });
    });
    expect(statusText()).toBe('Nicht gespeichert');
  });
});
