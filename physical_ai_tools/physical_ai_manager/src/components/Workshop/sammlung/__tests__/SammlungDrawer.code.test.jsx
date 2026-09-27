/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/*
 * The Sammlung drawer over a PYTHON program (owner decisions O4–O6): the same
 * drawer, fed a code asset document. What only code has: rows are inserted
 * into the program („Einfügen", or dragged into the editor), uses are code
 * lines, a rename rewrites the program's string arguments, and — since a code
 * program has no flyout cards — the drawer itself offers „Neu".
 */

import React from 'react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import studioAssetsReducer, { openDrawer } from '../../../../features/workshop/studioAssetsSlice';
import { DE } from '../../blocks/messages_de';
import { CODE_DE, formatCode } from '../../code/codeMessagesDe';
import { createDetachedDestinationStore } from '../destinationStore';
import { createCodeAssetDocument } from '../../code/codeAssetDocument';
import { createSammlungProvider } from '../provider';
import SammlungDrawer, { SNIPPET_MIME } from '../SammlungDrawer';
import * as workflowApi from '../../../../services/workflowApi';

vi.mock('../../../../services/workflowApi', () => ({
  renameTrajectory: vi.fn(async () => ({})),
  deleteTrajectory: vi.fn(async () => ({})),
  getTrajectory: vi.fn(async () => ({})),
  createTrajectory: vi.fn(async () => ({})),
  listTrajectories: vi.fn(async () => []),
}));

const mockToast = vi.hoisted(() => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  fn.dismiss = vi.fn();
  return fn;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

afterEach(() => { vi.clearAllMocks(); });

const MAIN = [
  'import robot',
  'robot.replay("Winken")',
  'robot.move_to("Ablage")',
  '',
].join('\n');

const ITEMS = [
  { id: 't1', name: 'Winken', point_count: 50, duration_s: 2, fps: 25, robot_profile: 'omx_f', created_at: '2026-09-27T10:00:00Z' },
];

function setup({ drawer = {}, capabilities = {} } = {}) {
  let files = { 'main.py': MAIN };
  const store = createDetachedDestinationStore([]);
  store.add({ name: 'Ablage', kind: 'pin', source: 'camera', x: 0.2, y: 0, z: 0 });
  store.add({ name: 'Hoch', kind: 'pose', source: 'capture', x: 0.1, y: 0.1, z: 0.15 });
  const reveals = [];
  const assetDoc = createCodeAssetDocument({
    language: 'python',
    store,
    getFiles: () => files,
    applyFiles: (next) => { files = next; },
    requestReveal: (at) => reveals.push(at),
    getCursor: () => null,
  });
  const provider = createSammlungProvider({
    capabilities: {
      hardware: true, drawer: true, teach: true, pinCamera: true, pinSim: true, ...capabilities,
    },
    robotType: 'omx_f',
    trajectories: { status: 'ready', items: ITEMS },
  });
  const dispatchAction = vi.spyOn(provider, 'dispatchAction');
  const redux = configureStore({ reducer: { studioAssets: studioAssetsReducer } });
  redux.dispatch(openDrawer({ tab: 'aufnahmen', focusId: null, ...drawer }));
  const saveWorkflowNow = vi.fn(async () => ({ ok: true }));
  render(
    <Provider store={redux}>
      <SammlungDrawer
        assetDoc={assetDoc}
        provider={provider}
        accessToken="tok"
        workflowId="wf1"
        robotType="omx_f"
        onPreview={vi.fn()}
        saveWorkflowNow={saveWorkflowNow}
        refetchTrajectories={vi.fn()}
      />
    </Provider>,
  );
  return {
    files: () => files, store, reveals, redux, provider, dispatchAction, saveWorkflowNow,
  };
}

describe('SammlungDrawer over a Python program', () => {
  it('counts the code’s assets and sits beside the file sidebar', () => {
    setup();
    expect(screen.getAllByRole('tab').map((t) => t.textContent))
      .toEqual(['Variablen 0', 'Aufnahmen 1', 'Ziele 1', 'Positionen 1']);
    expect(screen.getByRole('dialog', { name: DE.SAMMLUNG_TITLE }).style.left).toBe('176px');
  });

  it('„Einfügen" writes the call into the program and says where', () => {
    const { files, reveals } = setup({ drawer: { tab: 'positionen' } });
    fireEvent.click(screen.getByRole('button', { name: `${CODE_DE.SAMMLUNG_INSERT}: Hoch` }));
    expect(files()['main.py'].split('\n')[3]).toBe('robot.move_to("Hoch")');
    expect(reveals).toEqual([{ file: 'main.py', line: 4 }]);
    expect(mockToast.success).toHaveBeenCalledWith(formatCode(CODE_DE.INSERTED_AT, 'main.py', 4));
  });

  it('a row can be dragged into the editor as a snippet', () => {
    setup();
    const item = screen.getAllByRole('listitem')
      .find((li) => within(li).queryByRole('button', { name: /^Winken/ }));
    expect(item).toHaveAttribute('draggable', 'true');
    const dataTransfer = { setData: vi.fn(), effectAllowed: '' };
    fireEvent.dragStart(item, { dataTransfer });
    expect(dataTransfer.setData).toHaveBeenCalledWith(
      SNIPPET_MIME, JSON.stringify({ kind: 'recording', name: 'Winken' }),
    );
  });

  it('offers „Neu" per tab through the page’s actions, gated by the capabilities', () => {
    const { dispatchAction } = setup({ drawer: { tab: 'ziele' } });
    const group = screen.getByRole('group', { name: CODE_DE.SAMMLUNG_NEW });
    fireEvent.click(within(group).getByRole('button', { name: DE.FLY_TEACH_ZIEL }));
    fireEvent.click(within(group).getByRole('button', { name: DE.FLY_PIN_CAMERA }));
    fireEvent.click(within(group).getByRole('button', { name: DE.FLY_PIN_SIM }));
    expect(dispatchAction.mock.calls.map((c) => c[0])).toEqual([
      { type: 'teach', focus: 'ziel' }, { type: 'pinCamera' }, { type: 'pinSim' },
    ]);
    fireEvent.click(screen.getByRole('tab', { name: /Aufnahmen/ }));
    fireEvent.click(within(screen.getByRole('group', { name: CODE_DE.SAMMLUNG_NEW }))
      .getByRole('button', { name: DE.FLY_TEACH_RECORDING }));
    expect(dispatchAction).toHaveBeenLastCalledWith({ type: 'teach', focus: 'recording' });
  });

  it('hides a „Neu" the rig cannot do (the simulator: no Vormachen, no camera)', () => {
    setup({ drawer: { tab: 'ziele' }, capabilities: { simMode: true, pinCamera: false } });
    const group = screen.getByRole('group', { name: CODE_DE.SAMMLUNG_NEW });
    expect(within(group).queryByRole('button', { name: DE.FLY_TEACH_ZIEL })).toBeNull();
    expect(within(group).queryByRole('button', { name: DE.FLY_PIN_CAMERA })).toBeNull();
    expect(within(group).getByRole('button', { name: DE.FLY_PIN_SIM })).toBeInTheDocument();
  });

  it('„Benutzt in" lists code lines and jumps to them', () => {
    const { reveals } = setup({ drawer: { focusId: 'Winken' } });
    const section = screen.getByRole('region', { name: DE.DRAWER_USED_IN });
    const row = within(section).getByRole('button');
    expect(row.textContent).toBe('main.py:2 · robot.replay("Winken")');
    fireEvent.click(row);
    expect(reveals).toEqual([{ file: 'main.py', line: 2 }]);
  });

  it('renaming a Ziel rewrites the program’s string arguments', () => {
    const { files, store } = setup({ drawer: { tab: 'ziele' } });
    fireEvent.click(screen.getByRole('button', { name: /^Ablage/ }));
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_RENAME }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Tisch' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    expect(store.getByName('Tisch')).toBeTruthy();
    expect(files()['main.py']).toContain('robot.move_to("Tisch")');
  });

  it('renaming a recording renames the cloud rows, rewrites the code, then saves', async () => {
    const { files, saveWorkflowNow } = setup({ drawer: { focusId: 'Winken' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_RENAME }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Gruss' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    await waitFor(() => expect(saveWorkflowNow).toHaveBeenCalledWith({ toastOnSuccess: false, toastOnError: false }));
    expect(workflowApi.renameTrajectory).toHaveBeenCalledWith('tok', 'wf1', 't1', 'Gruss');
    expect(files()['main.py']).toContain('robot.replay("Gruss")');
  });
});
