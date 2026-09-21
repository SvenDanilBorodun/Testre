/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// One workflow is one language, chosen when it is created and immutable after.
// A code workflow never mounts the Blockly canvas, saves `code_language` +
// `code_files` as two fields on the same PATCH/POST (never inside
// `blockly_json`, whose SAVE allowlist would silently delete them), and its
// PATCH echoes the document's own language — never a different one.
//
// Same mocking idiom as WorkshopPage.savePayload.test.jsx.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import WorkshopPage from '../WorkshopPage';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockBlockly = vi.hoisted(() => ({ mountCount: 0 }));
vi.mock('../../components/Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  default: function MockBlocklyWorkspace() {
    React.useEffect(() => {
      mockBlockly.mountCount += 1;
    }, []);
    return <div data-testid="blockly-workspace" />;
  },
}));

// The code editor host: a stub that exposes what the page handed it and lets a
// test edit the entry file through the page's own onChange.
vi.mock('../../components/Workshop/code/CodeWorkspace', () => ({
  __esModule: true,
  default: function MockCodeWorkspace({ language, files, onFilesChange }) {
    return (
      <div data-testid="code-workspace" data-language={language}>
        <pre data-testid="code-files">{JSON.stringify(files)}</pre>
        <button
          type="button"
          data-testid="code-edit"
          onClick={() => onFilesChange({ ...files, 'main.py': 'import robot\nrobot.log("geändert")\n' })}
        />
      </div>
    );
  },
}));

vi.mock('../../components/Workshop/RightDock', () => ({ __esModule: true, default: () => <div data-testid="right-dock" /> }));
vi.mock('../../components/Workshop/SimStage', () => ({ __esModule: true, default: () => <div data-testid="sim-stage" /> }));
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/RunControls', () => ({
  __esModule: true,
  default: ({ codeLanguage, codeFiles }) => (
    <div data-testid="run-controls" data-language={codeLanguage || ''} data-files={codeFiles ? Object.keys(codeFiles).join(',') : ''} />
  ),
}));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({ __esModule: true, default: () => <div data-testid="camera-feed" /> }));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div data-testid="template-picker" /> }));
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: function MockToolbarButtons({ onSave, leading }) {
    return (
      <div>
        {leading}
        <button type="button" data-testid="save-button" onClick={() => onSave && onSave()} />
      </div>
    );
  },
}));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div data-testid="gallery-tab" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({ __esModule: true, default: () => <div data-testid="version-history" /> }));
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
    subscribeToWorkflowStatus: vi.fn(),
    subscribeToWorkflowSensors: vi.fn(),
    connected: true,
  }),
}));
vi.mock('../../components/Workshop/useAutosave', () => ({
  __esModule: true,
  useAutosave: () => ({ lastSavedAt: null }),
  autosaveSessionScope: () => 'session-1',
}));
// NOT mocked: `code/useCodeAutosave`. The crash-recovery draft of a CODE
// document is the thing the last describe below measures, and it measures it
// the way a student loses work — through the bucket that is or is not written.
const idb = vi.hoisted(() => ({
  get: vi.fn(async () => undefined),
  set: vi.fn(async () => undefined),
  del: vi.fn(async () => undefined),
}));
vi.mock('idb-keyval', () => ({ get: idb.get, set: idb.set, del: idb.del }));
const mockApi = vi.hoisted(() => ({
  getWorkflow: vi.fn(() => Promise.resolve(null)),
  createWorkflow: vi.fn(() => Promise.resolve({ id: 'wf-new' })),
  updateWorkflow: vi.fn(() => Promise.resolve({})),
  submitWorkflow: vi.fn(() => Promise.resolve({ id: 'sub-1' })),
}));
vi.mock('../../services/workflowApi', () => ({ __esModule: true, ...mockApi }));
const mockToast = vi.hoisted(() => Object.assign(vi.fn(), {
  success: vi.fn(),
  error: vi.fn(),
  dismiss: vi.fn(),
}));
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

const PYTHON_ROW = {
  id: 'wf-py',
  name: 'Mein Python',
  blockly_json: {},
  sim_scene: { version: 1, objects: [], zones: [] },
  code_language: 'python',
  code_files: { 'main.py': 'import robot\nrobot.home()\n', 'hilfe.py': 'x = 1\n' },
};

beforeEach(() => {
  mockState = baseState();
  mockBlockly.mountCount = 0;
  mockDispatch.mockClear();
  mockApi.getWorkflow.mockReset();
  mockApi.getWorkflow.mockImplementation(() => Promise.resolve(null));
  mockApi.createWorkflow.mockClear();
  mockApi.updateWorkflow.mockClear();
  mockApi.submitWorkflow.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  idb.get.mockReset();
  idb.get.mockImplementation(async () => undefined);
  idb.set.mockClear();
  idb.del.mockClear();
  try { window.localStorage.clear(); } catch (_) { /* jsdom */ }
});

/** The `{state, ts}` a code-draft bucket was last written with, or null. */
const lastCodeDraft = () => {
  const call = idb.set.mock.calls.filter(([k]) => String(k).includes('code-autosave')).pop();
  return call ? { key: call[0], ...call[1] } : null;
};

describe('WorkshopPage — a code workflow', () => {
  test('a python workflow never mounts the Blockly canvas and saves the code fields', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    const editor = await screen.findByTestId('code-workspace');
    expect(editor.getAttribute('data-language')).toBe('python');
    expect(JSON.parse(screen.getByTestId('code-files').textContent)).toEqual(PYTHON_ROW.code_files);
    expect(screen.queryByTestId('blockly-workspace')).toBeNull();
    expect(mockBlockly.mountCount).toBe(0);
    // RunControls receives the language and the files, not a blockly document.
    const rc = screen.getByTestId('run-controls');
    expect(rc.getAttribute('data-language')).toBe('python');
    expect(rc.getAttribute('data-files')).toBe('main.py,hilfe.py');

    await userEvent.click(screen.getByTestId('code-edit'));
    await userEvent.click(screen.getByTestId('save-button'));
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1));
    const [token, id, body] = mockApi.updateWorkflow.mock.calls[0];
    expect(token).toBe('jwt');
    expect(id).toBe('wf-py');
    // Two fields on the PATCH — and NO blockly_json beside them (the cloud
    // refuses a PATCH that mixes the two, and the SAVE allowlist would have
    // deleted the files had they ridden inside blockly_json).
    expect(Object.keys(body).sort()).toEqual(['code_files', 'code_language', 'sim_scene']);
    expect(body.code_language).toBe('python');
    expect(body.code_files['main.py']).toContain('geändert');
    expect(body.code_files['hilfe.py']).toBe('x = 1\n');
    expect(mockApi.createWorkflow).not.toHaveBeenCalled();
  });

  test('„Neu → Python" creates the document with the language, the starter file and an EMPTY blockly_json', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await userEvent.click(screen.getByRole('button', { name: /^Neu/ }));
    await userEvent.click(screen.getByRole('button', { name: /Python/ }));
    const editor = await screen.findByTestId('code-workspace');
    expect(editor.getAttribute('data-language')).toBe('python');
    expect(screen.queryByTestId('blockly-workspace')).toBeNull();

    await userEvent.click(screen.getByTestId('save-button'));
    await waitFor(() => expect(mockApi.createWorkflow).toHaveBeenCalledTimes(1));
    const body = mockApi.createWorkflow.mock.calls[0][1];
    expect(Object.keys(body).sort())
      .toEqual(['blockly_json', 'code_files', 'code_language', 'description', 'name', 'sim_scene']);
    expect(body.blockly_json).toEqual({});
    expect(body.code_language).toBe('python');
    expect(Object.keys(body.code_files)).toEqual(['main.py']);
    expect(body.code_files['main.py']).toMatch(/^import robot$/m);
    expect(mockApi.updateWorkflow).not.toHaveBeenCalled();
  });

  test('„Neu → Java" starts a Java project with Main.java', async () => {
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await userEvent.click(screen.getByRole('button', { name: /^Neu/ }));
    await userEvent.click(screen.getByRole('button', { name: /Java/ }));
    const editor = await screen.findByTestId('code-workspace');
    expect(editor.getAttribute('data-language')).toBe('java');
    expect(JSON.parse(screen.getByTestId('code-files').textContent)['Main.java']).toMatch(/class Main/);
  });

  test('„Neu → Blöcke" returns to a blank Blockly canvas', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await userEvent.click(screen.getByRole('button', { name: /^Neu/ }));
    await userEvent.click(screen.getByRole('button', { name: /Blöcke/ }));
    await screen.findByTestId('blockly-workspace');
    expect(screen.queryByTestId('code-workspace')).toBeNull();
  });

  test('a Blockly workflow’s PATCH never carries the code fields', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve({
      id: 'wf-b', blockly_json: { blocks: { blocks: [] } }, code_language: '', code_files: {},
    }));
    mockState = baseState({ selectedWorkflowId: 'wf-b', unsavedBlocklyJson: { blocks: { blocks: [] } } });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await userEvent.click(screen.getByTestId('save-button'));
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1));
    expect(Object.keys(mockApi.updateWorkflow.mock.calls[0][2]).sort()).toEqual(['blockly_json', 'sim_scene']);
  });
});

describe('WorkshopPage — „Abgeben"', () => {
  test('saves first, then posts the submission and confirms in German', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await userEvent.click(screen.getByRole('button', { name: /Abgeben/ }));
    await waitFor(() => expect(mockApi.submitWorkflow).toHaveBeenCalledTimes(1));
    expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1);
    expect(mockApi.submitWorkflow.mock.calls[0][0]).toBe('jwt');
    expect(mockApi.submitWorkflow.mock.calls[0][1]).toBe('wf-py');
    expect(mockToast.success).toHaveBeenCalledWith('Abgegeben.');
    window.confirm.mockRestore();
  });

  test('a declined confirmation submits nothing', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await userEvent.click(screen.getByRole('button', { name: /Abgeben/ }));
    expect(mockApi.submitWorkflow).not.toHaveBeenCalled();
    expect(mockApi.updateWorkflow).not.toHaveBeenCalled();
    window.confirm.mockRestore();
  });
});

// The crash-recovery draft. `useAutosave` is keyed on the BLOCKLY workspace —
// `save()` returns at `if (!enabled || !workspace) return;` — and a code
// workflow renders CodeWorkspace instead of BlocklyWorkspace, whose unmount
// hands the page `onWorkspaceReady(null)`. So a Python student's edits reached
// no local draft at all: a reload, a WebView2 crash, „Neu ▾" or picking another
// workflow (neither confirms) lost every edit since the last „Speichern", while
// a Blockly student kept a 750 ms-debounced one.
describe('WorkshopPage — the code document is autosaved locally', () => {
  test('an edit reaches a code-autosave bucket namespaced by the student', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');

    await userEvent.click(screen.getByTestId('code-edit'));

    await waitFor(() => expect(lastCodeDraft()).not.toBeNull(), { timeout: 4000 });
    const draft = lastCodeDraft();
    // The user id, never a shared bare name — one PC, one WebView2 profile,
    // many students (utils/sessionScope).
    expect(draft.key).toBe('edubotics:workshop:code-autosave:u1');
    expect(draft.state.language).toBe('python');
    expect(draft.state.files['main.py']).toContain('geändert');
    expect(draft.state.files['hilfe.py']).toBe('x = 1\n');
    expect(typeof draft.ts).toBe('number');
  });

  test('a stored draft reopens the unsaved program, and a saved workflow wins over it', async () => {
    idb.get.mockImplementation(async (key) => (String(key).includes('code-autosave')
      ? { state: { language: 'java', files: { 'Main.java': 'class Main {}\n' } }, ts: 5 }
      : undefined));

    // (a) nothing open: the draft becomes the editor's document.
    const view = render(<WorkshopPage isActive />);
    const editor = await screen.findByTestId('code-workspace');
    expect(editor.getAttribute('data-language')).toBe('java');
    expect(JSON.parse(screen.getByTestId('code-files').textContent))
      .toEqual({ 'Main.java': 'class Main {}\n' });
    view.unmount();

    // (b) a cloud workflow is selected: it takes precedence, exactly as the
    // Blockly restore does — the draft must never clobber a server document.
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    const second = await screen.findByTestId('code-workspace');
    expect(second.getAttribute('data-language')).toBe('python');
  });

  test('choosing „Neu → Blöcke" drops the code draft instead of resurrecting it', async () => {
    // The bucket exists only while the open document is code. Without that a
    // student who moved on to blocks would be pulled back into their old
    // Python program on the next reload.
    render(<WorkshopPage isActive />);
    await screen.findByTestId('blockly-workspace');
    await userEvent.click(screen.getByRole('button', { name: /^Neu/ }));
    await userEvent.click(screen.getByRole('button', { name: /Blöcke/ }));

    await waitFor(() => expect(
      idb.del.mock.calls.some(([k]) => String(k).includes('code-autosave')),
    ).toBe(true));
    expect(lastCodeDraft()).toBeNull();
  });
});
