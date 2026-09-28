/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Owner decision R2-O3 (review round 2, mi7): while a program runs or stands
// at a breakpoint, no action may replace the open document — gallery pick,
// „Öffnen", „Neu", a version restore, an opening clone — for a Blockly and a
// code program alike. Each is disabled with the German reason „Stoppe zuerst
// dein Programm.", and the page's handlers refuse a choice that arrives
// anyway. Opening another program goes through `openWorkflow`, which also
// retires the values of an UNSAVED document (the reducer half is in
// features/workshop/__tests__/workshopSlice.variables.test.js).
//
// The harness is WorkshopPage.codeLanguage.test.jsx's, with the three pickers
// replaced by prop-capturing stubs.
//
// Review round 3: the lock holds only while the robot link is alive (mb1,
// like utils/signOut::logoutBlockReason); a refusal is observed after React
// flushed (MB2b — asserted synchronously it could not see a restore that
// DID land) and on the „Neu" menu left open when the lock came (MB2c); and no
// run or preview starts while a version restore is on its way (nb2).

import React from 'react';
import { render, screen, act } from '@testing-library/react';
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
  onSave: null, assetDoc: null, host: null, teachHost: null,
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
  default: ({
    codeLanguage, codeFiles, destinationStore, startBlockedReason,
  }) => (
    <div
      data-testid="run-controls"
      data-start-blocked={startBlockedReason || ''}
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
const mockPickers = vi.hoisted(() => ({ template: null, gallery: null, history: null }));
vi.mock('../../components/Workshop/TemplatePicker', () => ({
  __esModule: true,
  default: (props) => {
    mockPickers.template = props;
    return (
      <button type="button" data-testid="pick-other" onClick={() => props.onPicked({ id: 'wf-other' })}>
        anderes
      </button>
    );
  },
}));
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
vi.mock('../../components/Workshop/GalleryTab', () => ({
  __esModule: true,
  default: (props) => {
    mockPickers.gallery = props;
    return <div data-testid="gallery-tab" />;
  },
}));
vi.mock('../../components/Workshop/SkillmapPlayer', () => ({ __esModule: true, default: () => <div data-testid="skillmap" /> }));
vi.mock('../../components/Workshop/VersionHistoryDropdown', () => ({
  __esModule: true,
  default: (props) => {
    mockPickers.history = props;
    return <div data-testid="version-history" />;
  },
}));
vi.mock('../../components/Workshop/teach/TeachHost', () => ({
  __esModule: true,
  default: (props) => {
    mockPage.teachHost = props;
    return <div data-testid="teach-host" />;
  },
}));
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


const REASON = 'Stoppe zuerst dein Programm.';
const openWorkflowActions = () => mockDispatch.mock.calls
  .map(([a]) => a).filter((a) => a && a.type === 'workshop/openWorkflow');

describe.each([
  ['a Blockly program', { selectedWorkflowId: null }, 'blockly-workspace'],
  ['a Python program', { selectedWorkflowId: 'wf-py' }, 'code-workspace'],
])('while %s runs — no document switch (owner decision R2-O3)', (_label, over, editorTestId) => {
  const running = [['running', { runState: 'running' }], ['paused at a breakpoint', { runState: 'running', paused: true }],
    ['paused (runState idle, paused flag)', { runState: 'idle', paused: true }]];

  test.each(running)('%s: „Öffnen", „Neu", the history and the gallery say why and do nothing', async (_s, runFlags) => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ ...over, ...runFlags });
    render(<WorkshopPage isActive />);
    await screen.findByTestId(editorTestId);
    const open = screen.getByRole('button', { name: /Öffnen/ });
    expect(open).toBeDisabled();
    expect(open.getAttribute('title')).toBe(REASON);
    const neu = screen.getByRole('button', { name: /^Neu/ });
    expect(neu).toBeDisabled();
    expect(neu.getAttribute('title')).toBe(REASON);
    expect(mockPickers.history.lockedReason).toBe(REASON);
    await userEvent.click(screen.getByRole('button', { name: 'Galerie' }));
    await screen.findByTestId('gallery-tab');
    expect(mockPickers.gallery.lockedReason).toBe(REASON);
  });

  test('a choice made anyway — a popover opened before the run, a late clone — is refused in German', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ ...over });
    const { rerender } = render(<WorkshopPage isActive />);
    await screen.findByTestId(editorTestId);
    await userEvent.click(screen.getByRole('button', { name: /Öffnen/ }));
    mockState = baseState({ ...over, runState: 'running' });
    rerender(<WorkshopPage isActive />);
    expect(mockPickers.template.lockedReason).toBe(REASON);
    mockDispatch.mockClear();
    await userEvent.click(screen.getByTestId('pick-other'));
    expect(openWorkflowActions()).toEqual([]);
    expect(mockToast.error).toHaveBeenCalledWith(REASON);
    // A version the cloud already restored is not swapped in either —
    // observed after React flushed (MB2b: asserted synchronously, the check
    // could not see a restore that DID land). It is a version of the OPEN
    // program (its row's id): since review round 5 (MD3) a restore result
    // applies only to the document it was asked for, so the positive control
    // below restores that one.
    const restored = {
      id: over.selectedWorkflowId || undefined, code_language: 'python', code_files: { 'main.py': 'x = 9\n' },
    };
    await act(async () => {
      mockPickers.history.onRestore(restored);
      await Promise.resolve();
    });
    expect(screen.queryByText(/x = 9/)).toBeNull();
    // The positive control: unlocked, the same restore DOES show.
    mockState = baseState({ ...over });
    rerender(<WorkshopPage isActive />);
    await act(async () => {
      mockPickers.history.onRestore(restored);
      await Promise.resolve();
    });
    expect(screen.getByText(/x = 9/)).toBeInTheDocument();
  });

  test('MB2c: a „Neu" menu opened before the run is refused in German, and nothing changes', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ ...over });
    const { rerender } = render(<WorkshopPage isActive />);
    await screen.findByTestId(editorTestId);
    await userEvent.click(screen.getByRole('button', { name: /^Neu/ }));
    const java = screen.getByRole('button', { name: /Java/ });
    mockState = baseState({ ...over, runState: 'running' });
    rerender(<WorkshopPage isActive />);
    mockDispatch.mockClear();
    await userEvent.click(java);
    expect(mockToast.error).toHaveBeenCalledWith(REASON);
    expect(screen.getByTestId(editorTestId)).toBeInTheDocument();
    expect(screen.queryByTestId('code-workspace')?.getAttribute('data-language') ?? 'none').not.toBe('java');
    expect(mockDispatch.mock.calls.map(([a]) => a && a.type)).not.toContain('workshop/clearVariables');
  });

  test('mb1: with the robot link gone, the lock is released — a dead link cannot retire a run', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ ...over, runState: 'running' });
    mockState.tasks = { heartbeatStatus: 'disconnected' };
    render(<WorkshopPage isActive />);
    await screen.findByTestId(editorTestId);
    const open = screen.getByRole('button', { name: /Öffnen/ });
    expect(open).toBeEnabled();
    expect(open.getAttribute('title')).not.toBe(REASON);
    expect(screen.getByRole('button', { name: /^Neu/ })).toBeEnabled();
    expect(mockPickers.history.lockedReason).toBeNull();
    await userEvent.click(open);
    mockDispatch.mockClear();
    await userEvent.click(screen.getByTestId('pick-other'));
    expect(openWorkflowActions()).toEqual([{ type: 'workshop/openWorkflow', payload: 'wf-other' }]);
    await userEvent.click(screen.getByRole('button', { name: 'Galerie' }));
    await screen.findByTestId('gallery-tab');
    expect(mockPickers.gallery.lockedReason).toBeNull();
  });

  test('when nothing runs, every control is live and a pick opens through openWorkflow', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ ...over });
    render(<WorkshopPage isActive />);
    await screen.findByTestId(editorTestId);
    expect(screen.getByRole('button', { name: /^Neu/ })).toBeEnabled();
    expect(mockPickers.history.lockedReason).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /Öffnen/ }));
    mockDispatch.mockClear();
    await userEvent.click(screen.getByTestId('pick-other'));
    expect(openWorkflowActions()).toEqual([{ type: 'workshop/openWorkflow', payload: 'wf-other' }]);
  });
});

describe('nb2: no run and no preview starts while a version restore is on its way', () => {
  test('the Start button says why while the restore runs, and is free again after', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    const runControls = () => screen.getByTestId('run-controls');
    expect(runControls().getAttribute('data-start-blocked')).toBe('');
    act(() => { mockPickers.history.onRestoringChange(true); });
    expect(runControls().getAttribute('data-start-blocked'))
      .toBe('Eine frühere Version wird gerade wiederhergestellt – bitte kurz warten.');
    // A preview is a run too: refused with the same reason.
    mockToast.error.mockClear();
    act(() => {
      mockPage.host.provider.dispatchAction({
        type: 'preview', asset: { kind: 'pin', id: 'p1', name: 'Ablage' },
      });
    });
    expect(mockToast.error).toHaveBeenCalledWith(
      'Eine frühere Version wird gerade wiederhergestellt – bitte kurz warten.',
    );
    act(() => { mockPickers.history.onRestoringChange(false); });
    expect(runControls().getAttribute('data-start-blocked')).toBe('');
  });
});

describe('mc10: no run and no preview while the opened program is still loading', () => {
  test('Start and ▶ say why until the new program arrived; then they are free', async () => {
    mockApi.getWorkflow.mockImplementation(() => Promise.resolve(PYTHON_ROW));
    mockState = baseState({ selectedWorkflowId: 'wf-py' });
    const { rerender } = render(<WorkshopPage isActive />);
    await screen.findByTestId('code-workspace');
    const runControls = () => screen.getByTestId('run-controls');
    expect(runControls().getAttribute('data-start-blocked')).toBe('');
    const { provider } = mockPage.host;
    // Another program is opened; its row is still on its way.
    let arrive;
    mockApi.getWorkflow.mockImplementation(() => new Promise((r) => {
      arrive = () => r({ ...PYTHON_ROW, id: 'wf-2', code_files: { 'main.py': 'import robot\n' } });
    }));
    mockState = baseState({ selectedWorkflowId: 'wf-2' });
    rerender(<WorkshopPage isActive />);
    // The old program's files and Ziele are still the page's — Start must
    // not run them under the new id.
    expect(runControls().getAttribute('data-files')).toBe('main.py,hilfe.py');
    expect(runControls().getAttribute('data-start-blocked'))
      .toBe('Das Programm wird noch geladen – bitte kurz warten.');
    mockToast.error.mockClear();
    act(() => {
      provider.dispatchAction({ type: 'preview', asset: { kind: 'pin', id: 'p1', name: 'Ablage' } });
    });
    expect(mockToast.error).toHaveBeenCalledWith('Das Programm wird noch geladen – bitte kurz warten.');
    await act(async () => { arrive(); });
    expect(runControls().getAttribute('data-start-blocked')).toBe('');
    expect(runControls().getAttribute('data-files')).toBe('main.py');
  });
});
