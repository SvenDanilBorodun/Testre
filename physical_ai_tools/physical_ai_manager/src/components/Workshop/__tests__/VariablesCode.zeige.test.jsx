/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Variablen of a Python/Java program (owner decisions O3, O8), end to end
// on the React side: the EXACT frames the server's `zeige` produces (the
// producing half is `test_code_rpc_zeige_vars.py::
// test_zeige_emits_the_var_sentinel_and_touches_nothing_else`, which asserts
// `[VAR:punkte=3]`, `[VAR:Anzahl Würfel=[1, 2]]`, `[VAR:text="Hallo"]`) go
// through the REAL `/workflow/status` branch of useRosTopicSubscription into
// the REAL workshop slice, and are read off the screen in the Debug panel's
// VariableInspector AND in the Sammlung drawer's Variablen tab over a code
// asset document — value, the last five, „Im Simulator zeigen" for a point,
// „Benutzt in" code lines, and no rename/delete (decision D7). Java says, in
// German, that its values come only through Robot.zeige(…) (A8).
//
// Only the transport is faked, as in VariableInspector.test.jsx.

import React from 'react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { render, screen, renderHook, act, within, fireEvent } from '@testing-library/react';

import workshopReducer from '../../../features/workshop/workshopSlice';
import studioAssetsReducer, { openDrawer } from '../../../features/workshop/studioAssetsSlice';
import rosReducer, { setRosbridgeUrl } from '../../../features/ros/rosSlice';
import { useRosTopicSubscription } from '../../../hooks/useRosTopicSubscription';
import VariableInspector from '../VariableInspector';
import SammlungDrawer from '../sammlung/SammlungDrawer';
import { createSammlungProvider } from '../sammlung/provider';
import { createDetachedDestinationStore } from '../sammlung/destinationStore';
import { createCodeAssetDocument } from '../code/codeAssetDocument';
import { DE } from '../blocks/messages_de';
import { CODE_DE } from '../code/codeMessagesDe';

const storeRef = vi.hoisted(() => ({ current: null }));
vi.mock('../../../store/store', () => ({
  __esModule: true,
  default: {
    dispatch: (action) => storeRef.current.dispatch(action),
    getState: () => storeRef.current.getState(),
  },
}));

vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.custom = vi.fn();
  fn.dismiss = vi.fn();
  return { __esModule: true, default: fn };
});

vi.mock('../../../services/workflowApi', () => ({
  renameTrajectory: vi.fn(async () => ({})),
  deleteTrajectory: vi.fn(async () => ({})),
  getTrajectory: vi.fn(async () => ({})),
  createTrajectory: vi.fn(async () => ({})),
  listTrajectories: vi.fn(async () => []),
}));

vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: {
    getConnection: vi.fn(() => Promise.resolve({ isConnected: true, on: () => {}, off: () => {} })),
  },
}));

const mockTopicSubscribe = vi.fn();
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.name = opts ? opts.name : undefined;
      this.subscribe = (cb) => { mockTopicSubscribe(this.name, cb); };
      this.unsubscribe = () => {};
    },
  },
}));

const PY = [
  'import robot',                                  // 1
  'punkte = 0',                                    // 2
  'for i in range(3):',                            // 3
  '    punkte += 1',                               // 4
  '    robot.zeige("punkte", punkte)',             // 5
  'robot.zeige("ziel", robot.ziel("Ablage"))',     // 6
  '',
].join('\n');

const JAVA = [
  'public class Main {',
  '  public static void main(String[] args) {',
  '    int punkte = 3;',
  '    Robot.zeige("punkte", punkte);',
  '  }',
  '}',
  '',
].join('\n');

async function wireUp({ language = 'python', files = { 'main.py': PY } } = {}) {
  const store = configureStore({
    reducer: { workshop: workshopReducer, studioAssets: studioAssetsReducer, ros: rosReducer },
  });
  store.dispatch(setRosbridgeUrl('ws://localhost:9090'));
  storeRef.current = store;
  const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
  const { result } = renderHook(() => useRosTopicSubscription(), { wrapper });
  await act(async () => { await result.current.subscribeToWorkflowStatus(); });
  await act(async () => { await Promise.resolve(); });
  const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === '/workflow/status');
  const cb = calls[calls.length - 1][1];

  const assetDoc = createCodeAssetDocument({
    language,
    store: createDetachedDestinationStore([]),
    getFiles: () => files,
    applyFiles: () => {},
    requestReveal: () => {},
    getCursor: () => null,
  });
  const provider = createSammlungProvider({
    capabilities: { drawer: true, previewVariables: true },
    trajectories: { status: 'ready', items: [] },
  });
  return {
    store, cb, assetDoc, provider,
  };
}

function renderBoth({
  store, assetDoc, provider, onPreview,
}) {
  // The page pushes the run's values into the provider (variableValues); the
  // test does the same from the store the wire writes.
  provider.setSnapshot({ variableValues: store.getState().workshop.variables });
  return render(
    <Provider store={store}>
      <VariableInspector />
      <SammlungDrawer
        assetDoc={assetDoc}
        provider={provider}
        accessToken="tok"
        workflowId="wf1"
        robotType="omx_f"
        onPreview={onPreview}
        saveWorkflowNow={vi.fn()}
        refetchTrajectories={vi.fn()}
      />
    </Provider>,
  );
}

// The hook arms its beep on the first user gesture (a document click), and
// jsdom has no AudioContext; a silent stand-in, for this file only.
const hadAudio = typeof window.AudioContext === 'function';
beforeAll(() => {
  if (!hadAudio) {
    window.AudioContext = function AudioContextStub() {
      this.state = 'running';
      this.resume = () => Promise.resolve();
      this.close = () => Promise.resolve();
    };
  }
});
afterAll(() => {
  if (!hadAudio) delete window.AudioContext;
});

beforeEach(() => {
  mockTopicSubscribe.mockClear();
  storeRef.current = null;
});

describe('a Python program’s zeige(…) reaches both Variablen views', () => {
  test('the Debug panel and the drawer show the value, its history and where it is used', async () => {
    const ctx = await wireUp();
    act(() => {
      ctx.cb({ log_message: '[VAR:punkte=1]', phase: 'running' });
      ctx.cb({ log_message: '[VAR:punkte=2]', phase: 'running' });
      ctx.cb({ log_message: '[VAR:punkte=3]', phase: 'running' });
    });
    ctx.store.dispatch(openDrawer({ tab: 'variablen', focusId: 'punkte' }));
    renderBoth(ctx);

    const panel = within(screen.getByRole('region', { name: DE.DEBUG_SECTION_VARIABLES }));
    expect(panel.getByText('punkte')).toBeInTheDocument();
    expect(panel.getByText('3')).toBeInTheDocument();

    const drawer = within(screen.getByRole('dialog', { name: DE.SAMMLUNG_TITLE }));
    expect(drawer.getAllByRole('definition').map((d) => d.textContent))
      .toEqual(expect.arrayContaining([expect.stringMatching(/^3 · vor \d+ s$/)]));
    const last = within(drawer.getByRole('region', { name: DE.DRAWER_LAST_VALUES }));
    expect(last.getAllByRole('listitem').map((li) => li.textContent.split(' · ')[0])).toEqual(['3', '2', '1']);
    const used = within(drawer.getByRole('region', { name: DE.DRAWER_USED_IN }));
    expect(used.getAllByRole('button').map((b) => b.textContent.split(' · ')[0]))
      .toEqual(['main.py:2', 'main.py:4', 'main.py:5']);
  });

  test('a variable of a code program is not renamed or deleted in the drawer (D7), and says why', async () => {
    const ctx = await wireUp();
    act(() => { ctx.cb({ log_message: '[VAR:punkte=3]', phase: 'running' }); });
    ctx.store.dispatch(openDrawer({ tab: 'variablen', focusId: 'punkte' }));
    renderBoth(ctx);
    const drawer = within(screen.getByRole('dialog', { name: DE.SAMMLUNG_TITLE }));
    expect(drawer.getByRole('heading', { name: 'punkte' })).toBeInTheDocument();
    expect(drawer.queryByRole('button', { name: DE.DRAWER_RENAME })).toBeNull();
    expect(drawer.queryByRole('button', { name: DE.DRAWER_DELETE_VARIABLE })).toBeNull();
    expect(drawer.getByText(CODE_DE.VARIABLES_EDIT_IN_CODE)).toBeInTheDocument();
    expect(drawer.queryByText(CODE_DE.VARIABLES_JAVA_NOTE)).toBeNull();
  });

  test('a point shown with zeige offers „Im Simulator zeigen"', async () => {
    const ctx = await wireUp();
    const onPreview = vi.fn();
    act(() => {
      ctx.cb({ log_message: '[VAR:ziel={"x": 0.12, "y": 0.04, "z": 0.0}]', phase: 'running' });
    });
    ctx.store.dispatch(openDrawer({ tab: 'variablen', focusId: 'ziel' }));
    renderBoth({ ...ctx, onPreview });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SHOW_POINT }));
    expect(onPreview).toHaveBeenCalledWith({ kind: 'variable', id: 'ziel', name: 'ziel' });
  });

  test('before any run the drawer lists the program’s own names, no value yet', async () => {
    const ctx = await wireUp();
    ctx.store.dispatch(openDrawer({ tab: 'variablen', focusId: null }));
    renderBoth(ctx);
    const drawer = within(screen.getByRole('dialog', { name: DE.SAMMLUNG_TITLE }));
    expect(drawer.getByRole('tab', { name: /Variablen/ }).textContent).toBe('Variablen 3');
    fireEvent.click(drawer.getByRole('button', { name: /^punkte/ }));
    expect(drawer.getByText(DE.DRAWER_NO_VALUE)).toBeInTheDocument();
  });
});

describe('a Java program', () => {
  test('says that its values come only through Robot.zeige(…)', async () => {
    const ctx = await wireUp({ language: 'java', files: { 'Main.java': JAVA } });
    act(() => { ctx.cb({ log_message: '[VAR:punkte=3]', phase: 'running' }); });
    ctx.store.dispatch(openDrawer({ tab: 'variablen', focusId: 'punkte' }));
    renderBoth(ctx);
    const drawer = within(screen.getByRole('dialog', { name: DE.SAMMLUNG_TITLE }));
    expect(drawer.getByText(CODE_DE.VARIABLES_JAVA_NOTE)).toBeInTheDocument();
    expect(CODE_DE.VARIABLES_JAVA_NOTE).toMatch(/Robot\.zeige/);
  });
});

describe('„Benutzt in: nirgends" speaks of code, not of blocks', () => {
  test('an unused Position points at „Einfügen"', async () => {
    const ctx = await wireUp();
    ctx.assetDoc.getStore().add({ name: 'Hoch', kind: 'pose', source: 'capture', x: 0.1, y: 0.1, z: 0.15 });
    ctx.store.dispatch(openDrawer({ tab: 'positionen', focusId: null }));
    renderBoth(ctx);
    fireEvent.click(screen.getByRole('button', { name: /^Hoch/ }));
    expect(screen.getByText(CODE_DE.USED_NOWHERE_INSERT)).toBeInTheDocument();
    expect(screen.queryByText(DE.DRAWER_USED_NOWHERE_POSE)).toBeNull();
  });
});
