/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code sidebar's Sammlung (owner decision O4) and the cursor it keeps for
// „Einfügen" (O6): the four counts that open the drawer, „+ Neu" through the
// page's provider actions, the last cursor line per file, a reveal request
// that switches file, and the asset names handed to the editor (O7).
//
// The editor is mocked (as in CodeWorkspace.test.jsx); the mock records the
// props it was given and exposes the cursor callback.

import React from 'react';
import { render, screen, within, act, fireEvent } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import CodeWorkspace from '../CodeWorkspace';
import { CODE_DE } from '../codeMessagesDe';
import { DE } from '../../blocks/messages_de';
import { createCodeAssetDocument } from '../codeAssetDocument';
import { createDetachedDestinationStore } from '../../sammlung/destinationStore';
import { createSammlungProvider } from '../../sammlung/provider';
import workshopReducer from '../../../../features/workshop/workshopSlice';
import studioAssetsReducer, { selectDrawer } from '../../../../features/workshop/studioAssetsSlice';

vi.mock('../../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ setWorkflowBreakpoints: vi.fn(() => Promise.resolve({})) }),
}));

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

const editor = vi.hoisted(() => ({ props: null }));
vi.mock('../CodeEditor', () => ({
  __esModule: true,
  default: function MockCodeEditor(props) {
    editor.props = props;
    return <div data-testid="code-editor" data-path={props.path} />;
  },
}));

const FILES = Object.freeze({
  'main.py': 'import robot\npunkte = 0\nrobot.replay("Winken")\nrobot.move_to("Ablage")\n',
  'hilfe.py': '# Hilfe\n',
});

async function mount({ files = FILES, capabilities = {}, readOnly = false, withSammlung = true } = {}) {
  const store = createDetachedDestinationStore([]);
  store.add({ name: 'Ablage', kind: 'pin', source: 'camera', x: 0.2, y: 0, z: 0 });
  store.add({ name: 'Hoch', kind: 'pose', source: 'capture', x: 0.1, y: 0.1, z: 0.15 });
  const assetDoc = createCodeAssetDocument({
    language: 'python',
    store,
    getFiles: () => files,
    applyFiles: () => {},
    requestReveal: () => {},
    getCursor: () => null,
  });
  const provider = createSammlungProvider({
    capabilities: {
      hardware: true, teach: true, drawer: true, pinCamera: true, pinSim: true, ...capabilities,
    },
    trajectories: {
      status: 'ready',
      items: [{ id: 't1', name: 'Winken', duration_s: 2, created_at: '2026-09-27T10:00:00Z' }],
    },
  });
  const dispatchAction = vi.spyOn(provider, 'dispatchAction');
  const onCursorChange = vi.fn();
  const redux = configureStore({ reducer: { workshop: workshopReducer, studioAssets: studioAssetsReducer } });
  const props = {
    language: 'python',
    files,
    onFilesChange: vi.fn(),
    readOnly,
    onCursorChange,
    objectTypes: ['wuerfel'],
    ...(withSammlung ? { assetDoc, provider } : {}),
  };
  const utils = render(
    <CodeWorkspace {...props} />,
    { wrapper: ({ children }) => <Provider store={redux}>{children}</Provider> },
  );
  await screen.findByTestId('code-editor');
  return {
    ...utils,
    store,
    redux,
    provider,
    dispatchAction,
    onCursorChange,
    show: (next) => utils.rerender(<CodeWorkspace {...props} {...next} />),
  };
}

const activePath = () => screen.getByTestId('code-editor').getAttribute('data-path');
const sammlung = () => screen.getByRole('region', { name: DE.SAMMLUNG_TITLE });

beforeEach(() => {
  editor.props = null;
  try {
    window.localStorage.clear();
  } catch (_) { /* no storage — the component tolerates it */ }
});

describe('the Sammlung section', () => {
  test('counts the program’s Variablen, Aufnahmen, Ziele and Positionen and opens the drawer on that tab', async () => {
    const { redux } = await mount();
    const rows = within(sammlung()).getAllByRole('button', { name: /^(Variablen|Aufnahmen|Ziele|Positionen) \d+$/ });
    expect(rows.map((b) => b.textContent)).toEqual(['Aufnahmen 1', 'Ziele 1', 'Positionen 1', 'Variablen 1']);
    fireEvent.click(within(sammlung()).getByRole('button', { name: 'Ziele 1' }));
    expect(selectDrawer(redux.getState())).toMatchObject({ open: true, tab: 'ziele', focusId: null });
  });

  test('follows the Ziele store', async () => {
    const { store } = await mount();
    act(() => { store.add({ name: 'Rand', kind: 'pin', source: 'sim', x: 0.15, y: 0.05, z: 0 }); });
    expect(within(sammlung()).getByRole('button', { name: 'Ziele 2' })).toBeInTheDocument();
  });

  test('is absent without an asset document (a read-only viewer, an older page)', async () => {
    await mount({ withSammlung: false });
    expect(screen.queryByRole('region', { name: DE.SAMMLUNG_TITLE })).toBeNull();
  });
});

describe('„+ Neu"', () => {
  test('offers what the rig can do and dispatches it through the page’s provider', async () => {
    const { dispatchAction } = await mount();
    fireEvent.click(within(sammlung()).getByRole('button', { name: `+ ${CODE_DE.SAMMLUNG_NEW}` }));
    const menu = screen.getByRole('menu', { name: CODE_DE.SAMMLUNG_NEW_MENU });
    expect(within(menu).getAllByRole('menuitem').map((m) => m.textContent)).toEqual([
      DE.FLY_TEACH_RECORDING, DE.FLY_TEACH_ZIEL, DE.FLY_PIN_CAMERA, DE.FLY_PIN_SIM, DE.FLY_TEACH_POSE,
    ]);
    fireEvent.click(within(menu).getByRole('menuitem', { name: DE.FLY_TEACH_ZIEL }));
    expect(dispatchAction).toHaveBeenCalledWith({ type: 'teach', focus: 'ziel' });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  test('is not offered in a read-only editor', async () => {
    await mount({ readOnly: true });
    expect(within(sammlung()).queryByRole('button', { name: `+ ${CODE_DE.SAMMLUNG_NEW}` })).toBeNull();
  });

  test('is not offered when the rig can do nothing', async () => {
    await mount({ capabilities: { hardware: false, pinCamera: false, pinSim: false } });
    expect(within(sammlung()).queryByRole('button', { name: `+ ${CODE_DE.SAMMLUNG_NEW}` })).toBeNull();
  });
});

describe('the cursor for „Einfügen"', () => {
  test('reports the editor’s line with its file, and remembers it per file', async () => {
    const { onCursorChange } = await mount();
    act(() => { editor.props.onCursorChange(3); });
    expect(onCursorChange).toHaveBeenLastCalledWith({ file: 'main.py', line: 3 });

    fireEvent.click(screen.getByRole('option', { name: /hilfe\.py$/ }));
    expect(activePath()).toBe('hilfe.py');
    expect(onCursorChange).toHaveBeenLastCalledWith(null);
    act(() => { editor.props.onCursorChange(1); });
    expect(onCursorChange).toHaveBeenLastCalledWith({ file: 'hilfe.py', line: 1 });

    fireEvent.click(screen.getByRole('option', { name: /main\.py$/ }));
    expect(onCursorChange).toHaveBeenLastCalledWith({ file: 'main.py', line: 3 });
    expect(editor.props.revealRequest).toMatchObject({ line: 3 });
  });

  test('a reveal request switches to its file and puts the caret there', async () => {
    const { show, onCursorChange } = await mount();
    show({ revealRequest: { file: 'hilfe.py', line: 1, nonce: 7 } });
    expect(activePath()).toBe('hilfe.py');
    expect(editor.props.revealRequest).toMatchObject({ line: 1 });
    expect(onCursorChange).toHaveBeenLastCalledWith({ file: 'hilfe.py', line: 1 });
    const first = editor.props.revealRequest.nonce;
    show({ revealRequest: { file: 'main.py', line: 4, nonce: 8 } });
    expect(activePath()).toBe('main.py');
    expect(editor.props.revealRequest).toMatchObject({ line: 4 });
    expect(editor.props.revealRequest.nonce).not.toBe(first);
  });

  test('a reveal for a file the program does not have changes nothing', async () => {
    const { show, onCursorChange } = await mount();
    show({ revealRequest: { file: 'weg.py', line: 1, nonce: 9 } });
    expect(activePath()).toBe('main.py');
    expect(onCursorChange).not.toHaveBeenCalled();
  });
});

describe('what the editor knows', () => {
  test('the Sammlung’s names, the program’s own, the catalog’s object types', async () => {
    const { store } = await mount();
    const { assets } = editor.props;
    expect(assets.recordings.map((r) => r.name)).toEqual(['Winken']);
    expect(assets.recordingsStatus).toBe('ready');
    expect(assets.places.map((e) => e.name)).toEqual(['Ablage', 'Hoch']);
    expect(assets.objects).toEqual(['wuerfel']);
    act(() => { store.add({ name: 'Rand', kind: 'pin', source: 'sim', x: 0.15, y: 0.05, z: 0 }); });
    expect(editor.props.assets.places.map((e) => e.name)).toEqual(['Ablage', 'Hoch', 'Rand']);
  });
});

describe('a keystroke never undoes an edit the page applied a moment before (review n8)', () => {
  test('the content change is built from the page’s latest files, not the rendered prop', async () => {
    // A page-like owner: it applies a change to its ref at once (a drawer
    // rename rewrote hilfe.py) while React has not re-rendered yet.
    let latest = { ...FILES };
    const onFilesChange = vi.fn((next) => {
      latest = typeof next === 'function' ? next(latest) : next;
    });
    const redux = configureStore({ reducer: { workshop: workshopReducer, studioAssets: studioAssetsReducer } });
    render(
      <CodeWorkspace language="python" files={FILES} onFilesChange={onFilesChange} />,
      { wrapper: ({ children }) => <Provider store={redux}>{children}</Provider> },
    );
    await screen.findByTestId('code-editor');
    latest = { ...latest, 'hilfe.py': '# umbenannt\n' };      // applied, not yet rendered
    act(() => { editor.props.onChange(`${FILES['main.py']}x = 1\n`); });
    expect(latest['hilfe.py']).toBe('# umbenannt\n');
    expect(latest['main.py']).toBe(`${FILES['main.py']}x = 1\n`);
  });
});

describe('a file switch without a remembered line reveals nothing (review m5)', () => {
  test('the caret request of another file is not carried over', async () => {
    const { show, onCursorChange } = await mount();
    show({ revealRequest: { file: 'hilfe.py', line: 1, nonce: 7 } });
    expect(editor.props.revealRequest).toMatchObject({ line: 1 });
    fireEvent.click(screen.getByRole('option', { name: /main\.py$/ }));
    expect(activePath()).toBe('main.py');
    expect(editor.props.revealRequest).toBeNull();
    expect(onCursorChange).toHaveBeenLastCalledWith(null);
  });
});
