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
// Migration 041 (owner decisions O1/O2): a code program keeps its Ziele and
// Positionen where a Blockly program keeps them — `blockly_json`'s
// `edubotics-destinations` key, and nothing else there. So a code save now
// sends `blockly_json` too: `{}` without Ziele, the serializer state with them.
// Every path that opens a code document (hydrate, „Neu", a draft, a version
// restore) loads them into ONE store per document (openCodeDocument).
//
// Same mocking idiom as WorkshopPage.savePayload.test.jsx.

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
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
// test edit the entry file through the page's own onChange — and, through the
// asset document the page hands it, rename a Ziel and save IN THE SAME TICK
// (the drawer's rename does exactly that: rewrite the code, then await a save).
const mockPage = vi.hoisted(() => ({
  onSave: null, assetDoc: null, host: null,
}));
vi.mock('../../components/Workshop/code/CodeWorkspace', () => ({
  __esModule: true,
  default: function MockCodeWorkspace(props) {
    const {
      language, files, onFilesChange, assetDoc,
    } = props;
    mockPage.assetDoc = assetDoc;
    mockPage.host = props;
    return (
      <div data-testid="code-workspace" data-language={language}>
        <pre data-testid="code-files">{JSON.stringify(files)}</pre>
        <pre data-testid="code-ziele">
          {JSON.stringify(assetDoc ? assetDoc.getStore().getEntries().map((e) => e.name) : null)}
        </pre>
        <button
          type="button"
          data-testid="code-edit"
          onClick={() => onFilesChange({ ...files, 'main.py': 'import robot\nrobot.log("geändert")\n' })}
        />
        <button
          type="button"
          data-testid="code-rename-and-save"
          onClick={() => {
            const entry = assetDoc.getStore().getByName('Ablage');
            assetDoc.renamePlace(entry.id, 'Tisch');
            mockPage.onSave();
          }}
        />
      </div>
    );
  },
}));

// RightDock renders ONLY the camera tab, so the page's CameraFeedOverlay wiring
// is reachable (the camera click-to-mark of a code program).
vi.mock('../../components/Workshop/RightDock', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div data-testid="right-dock">
      {(tabs || []).filter((t) => t.id === 'camera').map((t) => <div key={t.id}>{t.render()}</div>)}
    </div>
  ),
}));
const mockSimStage = vi.hoisted(() => ({ props: null }));
vi.mock('../../components/Workshop/SimStage', () => ({
  __esModule: true,
  default: (props) => {
    mockSimStage.props = props;
    return <div data-testid="sim-stage" data-language={props.codeLanguage} />;
  },
}));
vi.mock('../../components/Workshop/CalibrationWizard', () => ({ __esModule: true, default: () => <div data-testid="calib-wizard" /> }));
vi.mock('../../components/Workshop/LeaderToggle', () => ({ __esModule: true, default: () => <div data-testid="leader-toggle" /> }));
vi.mock('../../components/Workshop/RunControls', () => ({
  __esModule: true,
  default: ({ codeLanguage, codeFiles, destinationStore }) => (
    <div
      data-testid="run-controls"
      data-language={codeLanguage || ''}
      data-files={codeFiles ? Object.keys(codeFiles).join(',') : ''}
      data-ziele={destinationStore ? destinationStore.getEntries().map((e) => e.name).join(',') : '-'}
    />
  ),
}));
const mockOverlay = vi.hoisted(() => ({ props: null }));
vi.mock('../../components/Workshop/CameraFeedOverlay', () => ({
  __esModule: true,
  default: (props) => {
    mockOverlay.props = props;
    return <div data-testid="camera-feed" />;
  },
}));
vi.mock('../../components/Workshop/TemplatePicker', () => ({ __esModule: true, default: () => <div data-testid="template-picker" /> }));
vi.mock('../../components/Workshop/ToolbarButtons', () => ({
  __esModule: true,
  default: function MockToolbarButtons({ onSave, leading, extra }) {
    mockPage.onSave = onSave;
    return (
      <div>
        {leading}
        {extra}
        <button type="button" data-testid="save-button" onClick={() => onSave && onSave()} />
      </div>
    );
  },
}));
vi.mock('../../components/Workshop/DebugPanel', () => ({ __esModule: true, default: () => <div data-testid="debug-panel" /> }));
vi.mock('../../components/Workshop/GalleryTab', () => ({ __esModule: true, default: () => <div data-testid="gallery-tab" /> }));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
const mockHistory = vi.hoisted(() => ({ onRestore: null }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({
  __esModule: true,
  default: ({ onRestore }) => {
    mockHistory.onRestore = onRestore;
    return <div data-testid="version-history" />;
  },
}));
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
    // The code fields on the PATCH — and, since migration 041, the document's
    // Ziele beside them as blockly_json: `{}` for a program without any (never
    // the files: the SAVE allowlist would have deleted them had they ridden
    // inside blockly_json, and the cloud refuses any other key there).
    expect(Object.keys(body).sort()).toEqual(['blockly_json', 'code_files', 'code_language', 'sim_scene']);
    expect(body.blockly_json).toEqual({});
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

// Migration 041: a code program keeps its Ziele/Positionen.
const ZIELE_STATE = {
  version: 1,
  entries: [
    { id: 'd_0000aaaa', name: 'Ablage', kind: 'pin', x: 0.2, y: 0, z: 0, source: 'camera' },
    { id: 'd_0000bbbb', name: 'Hoch', kind: 'pose', x: 0.1, y: 0.1, z: 0.15, source: 'capture' },
  ],
};
const PYTHON_ROW_ZIELE = {
  ...PYTHON_ROW,
  blockly_json: { 'edubotics-destinations': ZIELE_STATE },
  code_files: { 'main.py': 'import robot\nrobot.move_to("Ablage")\n' },
};

describe('WorkshopPage — a code program’s Ziele (migration 041)', () => {
  test('hydrate loads them into the document’s store, the run gets them, the save sends them back', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW_ZIELE));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    expect(JSON.parse(screen.getByTestId('code-ziele').textContent)).toEqual(['Ablage', 'Hoch']);
    expect(screen.getByTestId('run-controls').getAttribute('data-ziele')).toBe('Ablage,Hoch');

    await userEvent.click(screen.getByTestId('save-button'));
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1));
    const body = mockApi.updateWorkflow.mock.calls[0][2];
    expect(Object.keys(body.blockly_json)).toEqual(['edubotics-destinations']);
    expect(body.blockly_json['edubotics-destinations'].entries.map((e) => e.name)).toEqual(['Ablage', 'Hoch']);
  });

  test('a rename that rewrites the code and saves in the SAME tick saves the rewritten code', async () => {
    // applyCodeFiles sets the ref runSave reads synchronously — a ref updated
    // in an effect would still hold the old text when the save starts.
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW_ZIELE));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await userEvent.click(screen.getByTestId('code-rename-and-save'));
    await waitFor(() => expect(mockApi.updateWorkflow).toHaveBeenCalledTimes(1));
    const body = mockApi.updateWorkflow.mock.calls[0][2];
    expect(body.code_files['main.py']).toBe('import robot\nrobot.move_to("Tisch")\n');
    expect(body.blockly_json['edubotics-destinations'].entries.map((e) => e.name)).toEqual(['Tisch', 'Hoch']);
  });

  test('„Neu → Python" starts with an EMPTY store, never the previous program’s', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW_ZIELE));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await userEvent.click(screen.getByRole('button', { name: /^Neu/ }));
    await userEvent.click(screen.getByRole('button', { name: /Python/ }));
    await waitFor(() => expect(JSON.parse(screen.getByTestId('code-ziele').textContent)).toEqual([]));
  });

  test('a version restore of a code program brings its Ziele back', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    expect(JSON.parse(screen.getByTestId('code-ziele').textContent)).toEqual([]);
    act(() => { mockHistory.onRestore(PYTHON_ROW_ZIELE); });
    await waitFor(() => expect(JSON.parse(screen.getByTestId('code-ziele').textContent)).toEqual(['Ablage', 'Hoch']));
    expect(JSON.parse(screen.getByTestId('code-files').textContent)).toEqual(PYTHON_ROW_ZIELE.code_files);
  });

  test('the local draft carries the Ziele and a restored draft brings them back', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW_ZIELE));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    const view = render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await userEvent.click(screen.getByTestId('code-edit'));
    await waitFor(() => expect(lastCodeDraft()).not.toBeNull(), { timeout: 4000 });
    expect(lastCodeDraft().state.destinations.entries.map((e) => e.name)).toEqual(['Ablage', 'Hoch']);
    view.unmount();

    idb.get.mockImplementation(async (key) => (String(key).includes('code-autosave')
      ? { state: { language: 'python', files: { 'main.py': 'import robot\n' }, destinations: ZIELE_STATE }, ts: 5 }
      : undefined));
    mockState = baseState();
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await waitFor(() => expect(JSON.parse(screen.getByTestId('code-ziele').textContent)).toEqual(['Ablage', 'Hoch']));
  });

  test('an older draft without Ziele still restores', async () => {
    idb.get.mockImplementation(async (key) => (String(key).includes('code-autosave')
      ? { state: { language: 'python', files: { 'main.py': 'import robot\n' } }, ts: 5 }
      : undefined));
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    expect(JSON.parse(screen.getByTestId('code-ziele').textContent)).toEqual([]);
  });
});

describe('WorkshopPage — the simulator of a code program (O9)', () => {
  test('SimStage receives the open program’s language for its Debug panel', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    const stage = await screen.findByTestId('sim-stage');
    expect(stage.getAttribute('data-language')).toBe('python');
  });
});

describe('WorkshopPage — creating Ziele in a code program (the three silent no-ops)', () => {
  const PY_PINNED = {
    ...PYTHON_ROW_ZIELE,
    code_files: { 'main.py': 'import robot\nrobot.pin("Ziel 1", 0.1, 0.0, 0.0)\n' },
  };

  test('a camera click makes a new Ziel in the document’s store, named past the code’s own pins', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PY_PINNED));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await waitFor(() => expect(mockOverlay.props).not.toBeNull());
    const resolved = mockOverlay.props.resolveMarkLabel();
    expect(resolved).toEqual({ label: 'Ziel 2', target: 'store' });
    let marked;
    act(() => {
      marked = mockOverlay.props.onMark({ ...resolved, world_x: 0.15, world_y: 0.02, world_z: 0.01 });
    });
    expect(marked).toMatchObject({ name: 'Ziel 2' });
    await waitFor(() => expect(JSON.parse(screen.getByTestId('code-ziele').textContent))
      .toEqual(['Ablage', 'Hoch', 'Ziel 2']));
    expect(screen.getByTestId('run-controls').getAttribute('data-ziele')).toBe('Ablage,Hoch,Ziel 2');
    // …and the overlay's inline rename renames it in that same store.
    act(() => { mockOverlay.props.onRenameMark(marked.entryId, 'Tischkante'); });
    await waitFor(() => expect(JSON.parse(screen.getByTestId('code-ziele').textContent))
      .toEqual(['Ablage', 'Hoch', 'Tischkante']));
  });

  test('„Ziel setzen" on the sim table adds a pin at z 0 to the code document', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PY_PINNED));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await userEvent.click(screen.getByRole('button', { name: 'Test im Simulator' }));
    await screen.findByTestId('sim-stage');
    act(() => { mockSimStage.props.onCreateDestination({ x: 0.2, y: -0.1 }); });
    await waitFor(() => expect(JSON.parse(screen.getByTestId('code-ziele').textContent))
      .toEqual(['Ablage', 'Hoch', 'Ziel 2']));
    const pin = mockPage.assetDoc.getStore().getByName('Ziel 2');
    expect(pin).toMatchObject({ kind: 'pin', source: 'sim', z: 0 });
    // The sim stage's markers are the code document's entries.
    expect(mockSimStage.props.markers.map((m) => m.label)).toEqual(expect.arrayContaining(['Ablage', 'Hoch', 'Ziel 2']));
  });
});

describe('WorkshopPage — the code editor host gets the Sammlung (O4–O7)', () => {
  test('the catalog’s object types, the page’s provider, and the cursor „Einfügen" writes below', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    await waitFor(() => expect(mockPage.host.objectTypes).toEqual(['wuerfel']));
    expect(typeof mockPage.host.provider.dispatchAction).toBe('function');

    act(() => { mockPage.host.onCursorChange({ file: 'hilfe.py', line: 1 }); });
    act(() => { mockPage.assetDoc.insertSnippet({ kind: 'recording', name: 'Winken' }); });
    const files = JSON.parse(screen.getByTestId('code-files').textContent);
    expect(files['hilfe.py']).toBe('x = 1\nrobot.replay("Winken")\n');
    expect(mockPage.host.revealRequest).toMatchObject({ file: 'hilfe.py', line: 2 });
  });
});
