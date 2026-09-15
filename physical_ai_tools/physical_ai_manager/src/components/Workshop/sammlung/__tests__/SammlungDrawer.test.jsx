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
 * The Sammlung drawer against a real headless Blockly workspace (real blocks,
 * real destination store) and the real studioAssets reducer. jumpToBlock and
 * the cloud API are the only mocks.
 */

import React from 'react';
import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, within, act, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import studioAssetsReducer, { openDrawer, setRenameSplit } from '../../../../features/workshop/studioAssetsSlice';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { DE, formatDe } from '../../blocks/messages_de';
import { getDestinationStore, registerDestinationSerializer } from '../destinationStore';
import { createSammlungProvider } from '../provider';
import SammlungDrawer from '../SammlungDrawer';
import { jumpToBlock } from '../blockUsage';
import * as workflowApi from '../../../../services/workflowApi';

vi.mock('../blockUsage', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, jumpToBlock: vi.fn() };
});

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

let ws;

beforeAll(() => {
  Blockly.setLocale(De);
  registerTrajectoryBlocks();
  registerDestinationBlocks();
  registerDestinationSerializer();
});

afterEach(() => {
  vi.clearAllMocks();
  if (ws) ws.dispose();
  ws = null;
});

const ITEMS = [
  { id: 't3', name: 'Winken', point_count: 105, duration_s: 4.2, fps: 25, robot_profile: 'omx_f', created_at: '2026-09-13T10:30:00Z' },
  { id: 't1', name: 'Winken', point_count: 80, duration_s: 3.1, fps: 25, robot_profile: null, created_at: '2026-09-13T10:00:00Z' },
  { id: 't9', name: 'Tanz', point_count: 10, duration_s: 0.4, fps: 25, robot_profile: 'omx_f', created_at: '2026-09-13T09:00:00Z' },
];

const replay = (name, id) => ({ type: 'edubotics_replay_trajectory', id, fields: { NAME: name } });
const ref = (name, id) => ({ type: 'edubotics_destination_ref', id, fields: { NAME: name } });

function setup({ blocks = [], variables, drawer = {}, toolboxWidth = 118, pins = [] } = {}) {
  ws = new Blockly.Workspace();
  Blockly.serialization.workspaces.load({
    blocks: { languageVersion: 0, blocks: blocks.map((b, i) => ({ x: 0, y: i * 80, ...b })) },
    ...(variables ? { variables } : {}),
  }, ws);
  const toolbox = { getWidth: () => toolboxWidth, clearSelection: vi.fn() };
  ws.getToolbox = () => toolbox;
  const listeners = [];
  const realAdd = ws.addChangeListener.bind(ws);
  ws.addChangeListener = (fn) => { listeners.push(fn); return realAdd(fn); };
  const store = getDestinationStore(ws);
  pins.forEach((p) => store.add(p));
  const provider = createSammlungProvider({
    capabilities: { hardware: true, drawer: true },
    robotType: 'omx_f',
    trajectories: { status: 'ready', items: ITEMS },
  });
  const redux = configureStore({ reducer: { studioAssets: studioAssetsReducer } });
  redux.dispatch(openDrawer({ tab: 'aufnahmen', focusId: null, ...drawer }));
  const saveWorkflowNow = vi.fn(async () => ({ ok: true }));
  const refetchTrajectories = vi.fn();
  const utils = render(
    <Provider store={redux}>
      <SammlungDrawer
        workspace={ws}
        provider={provider}
        accessToken="tok"
        workflowId="wf1"
        robotType="omx_f"
        onPreview={vi.fn()}
        saveWorkflowNow={saveWorkflowNow}
        refetchTrajectories={refetchTrajectories}
      />
    </Provider>,
  );
  return { ...utils, redux, toolbox, listeners, store, saveWorkflowNow, refetchTrajectories };
}

const PIN = { name: 'Ablage', kind: 'pin', source: 'camera', robot_type: 'omx_f', x: 0.182, y: -0.064, z: 0.012 };
const POSE = { name: 'Über Kiste', kind: 'pose', source: 'capture', robot_type: 'omx_f', x: 0.1, y: 0.1, z: 0.118 };

describe('SammlungDrawer', () => {
  it('renders four tabs with their counts and the Aufnahmen list', () => {
    setup({ pins: [PIN, POSE], variables: [{ name: 'Anzahl', id: 'v1' }] });
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual(['Variablen 1', 'Aufnahmen 2', 'Ziele 1', 'Positionen 1']);
    expect(screen.getByRole('tab', { name: /Aufnahmen/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('button', { name: /Winken/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: /Positionen/ }));
    expect(screen.getByRole('button', { name: /Über Kiste/ })).toBeInTheDocument();
  });

  it('an empty tab says so', () => {
    setup({ drawer: { tab: 'ziele' } });
    expect(screen.getByText(DE.DRAWER_EMPTY_TAB)).toBeInTheDocument();
  });

  it('sits beside the toolbox: style.left is the toolbox width', () => {
    setup();
    const aside = screen.getByRole('dialog', { name: DE.SAMMLUNG_TITLE });
    expect(aside.style.left).toBe('118px');
    expect(aside.className).toContain('absolute');
    expect(aside.className).toContain('z-20');
  });

  it('opening the drawer closes an open flyout', () => {
    const { toolbox } = setup();
    expect(toolbox.clearSelection).toHaveBeenCalled();
  });

  it('focus shows the recording detail with its older versions', () => {
    setup({ drawer: { focusId: 'Winken' } });
    expect(screen.getByText(DE.DRAWER_DURATION)).toBeInTheDocument();
    expect(screen.getByText(formatDe(DE.DRAWER_DURATION_VALUE, '4,2', 105, '25'))).toBeInTheDocument();
    expect(screen.getByText('OpenMANIPULATOR-X (omx_f)')).toBeInTheDocument();
    expect(screen.getByText(DE.DRAWER_OLDER_VERSIONS)).toBeInTheDocument();
    expect(screen.getByText(/3,1 s · wird nicht abgespielt$/)).toBeInTheDocument();
    expect(screen.getByText(DE.DRAWER_USED_NOWHERE_RECORDING)).toBeInTheDocument();
  });

  it('a usage row click jumps to the block', () => {
    setup({ blocks: [replay('Winken', 'b1')], drawer: { focusId: 'Winken' } });
    const section = screen.getByRole('region', { name: DE.DRAWER_USED_IN });
    const row = within(section).getByRole('button');
    expect(row.textContent).toBe(ws.getBlockById('b1').toString());
    fireEvent.click(row);
    expect(jumpToBlock).toHaveBeenCalledWith(ws, 'b1');
  });

  it('a disabled usage row carries the suffix', () => {
    setup({ blocks: [replay('Winken', 'b1')], drawer: { focusId: 'Winken' } });
    act(() => { ws.getBlockById('b1').setDisabledReason(true, 'test'); });
    fireEvent.click(screen.getByRole('tab', { name: /Variablen/ }));
    fireEvent.click(screen.getByRole('tab', { name: /Aufnahmen/ }));
    const section = screen.getByRole('region', { name: DE.DRAWER_USED_IN });
    expect(within(section).getByRole('button').textContent.endsWith(` ${DE.DRAWER_DISABLED_SUFFIX}`)).toBe(true);
  });
});

describe('SammlungDrawer: closing', () => {
  it('Esc closes the drawer, but not while typing in an input', () => {
    const { redux } = setup({ drawer: { focusId: 'Winken' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_RENAME }));
    fireEvent.keyDown(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { key: 'Escape' });
    expect(redux.getState().studioAssets.drawer.open).toBe(true);
    fireEvent.keyDown(screen.getByRole('tab', { name: /Ziele/ }), { key: 'Escape' });
    expect(redux.getState().studioAssets.drawer.open).toBe(false);
  });

  it('the ✕ closes the drawer', () => {
    const { redux } = setup();
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_CLOSE }));
    expect(redux.getState().studioAssets.drawer.open).toBe(false);
  });

  it('a selected toolbox category closes it; the empty selection of clearSelection does not', () => {
    const { redux, listeners } = setup();
    expect(listeners).toHaveLength(1);
    act(() => { listeners[0]({ type: Blockly.Events.TOOLBOX_ITEM_SELECT, newItem: undefined, isUiEvent: true }); });
    expect(redux.getState().studioAssets.drawer.open).toBe(true);
    act(() => { listeners[0]({ type: Blockly.Events.TOOLBOX_ITEM_SELECT, newItem: 'sammlung-ziele', isUiEvent: true }); });
    expect(redux.getState().studioAssets.drawer.open).toBe(false);
  });

  it('the rename-split banner shows its exact text while renameSplit is set', () => {
    const { redux } = setup();
    expect(screen.queryByRole('alert')).toBeNull();
    act(() => { redux.dispatch(setRenameSplit({ cloudName: 'Tanz' })); });
    expect(screen.getByRole('alert').textContent).toBe(
      'Achtung: Die Aufnahme heißt jetzt „Tanz", der Workflow ist aber noch nicht gespeichert. Bitte erneut speichern.',
    );
  });
});

describe('SammlungDrawer: rename and delete', () => {
  it('a destination rename input sanitises live and caps at 24', () => {
    setup({ pins: [PIN], drawer: { tab: 'ziele' } });
    fireEvent.click(screen.getByRole('button', { name: /Ablage/ }));
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_RENAME }));
    const input = screen.getByRole('textbox', { name: DE.DRAWER_NAME });
    expect(input).toHaveAttribute('maxLength', '24');
    fireEvent.change(input, { target: { value: 'Ab!lage#' } });
    expect(input.value).toBe('Ablage');
    fireEvent.change(input, { target: { value: 'x'.repeat(30) } });
    expect(input.value).toBe('x'.repeat(24));
  });

  it('a Position detail names its captured gripper only when the S3 joints classify', () => {
    const names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'];
    const { unmount } = setup({
      pins: [{ ...POSE, joints: [0, -0.9, 1.1, 0.3, 0, -0.3], joint_names: names }],
      drawer: { tab: 'positionen' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Über Kiste/ }));
    expect(screen.getByText(DE.DRAWER_STATE)).toBeInTheDocument();
    expect(screen.getByText(DE.TEACH_GRIPPER_CLOSED)).toBeInTheDocument();
    unmount();
    ws.dispose();
    // An older server's Position (no joints): no gripper line at all.
    setup({ pins: [POSE], drawer: { tab: 'positionen' } });
    fireEvent.click(screen.getByRole('button', { name: /Über Kiste/ }));
    expect(screen.getByText(DE.DRAWER_SOURCE)).toBeInTheDocument();
    expect(screen.queryByText(DE.DRAWER_STATE)).toBeNull();
    expect(screen.queryByText(DE.TEACH_GRIPPER_OPEN)).toBeNull();
  });

  it('a destination rename rewrites the reference blocks', () => {
    const { store } = setup({ pins: [PIN], blocks: [ref('Ablage', 'r1')], drawer: { tab: 'ziele' } });
    fireEvent.click(screen.getByRole('button', { name: /Ablage/ }));
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_RENAME }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Tisch' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    expect(store.getEntries()[0].name).toBe('Tisch');
    expect(ws.getBlockById('r1').getFieldValue('NAME')).toBe('Tisch');
  });

  it('a recording rename input caps at 40', () => {
    setup({ drawer: { focusId: 'Winken' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_RENAME }));
    expect(screen.getByRole('textbox', { name: DE.DRAWER_NAME })).toHaveAttribute('maxLength', '40');
  });

  it('deleting a place used once asks with the ONE sentence, keeps the block, and deletes on „Löschen"', () => {
    const { store } = setup({ pins: [PIN], blocks: [ref('Ablage', 'r1')], drawer: { tab: 'ziele' } });
    fireEvent.click(screen.getByRole('button', { name: /Ablage/ }));
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_DELETE_PLACE }));
    const dialog = screen.getByRole('alertdialog');
    expect(within(dialog).getByText(
      '„Ablage" wird in 1 Block benutzt. Trotzdem löschen? Der Block bleibt stehen und zeigt danach eine Warnung.',
    )).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: DE.CONFIRM_YES_DELETE }));
    expect(store.getEntries()).toEqual([]);
    expect(ws.getBlockById('r1')).not.toBeNull();
  });

  it('deleting a recording used twice asks with the MANY sentence; „Abbrechen" deletes nothing', async () => {
    setup({ blocks: [replay('Winken', 'b1'), replay('Winken', 'b2')], drawer: { focusId: 'Winken' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_DELETE_RECORDING }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(
      '„Winken" wird in 2 Blöcken benutzt. Trotzdem löschen? Die Blöcke bleiben stehen und zeigen danach eine Warnung.',
    )).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: DE.DRAWER_CANCEL }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull());
    expect(workflowApi.getTrajectory).not.toHaveBeenCalled();
    expect(workflowApi.deleteTrajectory).not.toHaveBeenCalled();
  });

  it('deleting an older version asks first and offers NO „Rückgängig" (a restore would make it the played take)', async () => {
    const { refetchTrajectories } = setup({ drawer: { focusId: 'Winken' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_DELETE_VERSION }));
    let dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(DE.CONFIRM_DELETE_VERSION)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: DE.DRAWER_CANCEL }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull());
    expect(workflowApi.deleteTrajectory).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_DELETE_VERSION }));
    dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: DE.CONFIRM_YES_DELETE }));
    await waitFor(() => expect(refetchTrajectories).toHaveBeenCalled());
    expect(workflowApi.deleteTrajectory).toHaveBeenCalledTimes(1);
    expect(workflowApi.deleteTrajectory).toHaveBeenCalledWith('tok', 'wf1', 't1');
    expect(mockToast.success).toHaveBeenCalledWith(formatDe(DE.TOAST_DELETED, 'Winken'));
    expect(mockToast).not.toHaveBeenCalled();
  });

  it('deleting every version of an unused recording offers „Rückgängig"', async () => {
    const { refetchTrajectories } = setup({ drawer: { focusId: 'Winken' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_DELETE_RECORDING }));
    await waitFor(() => expect(refetchTrajectories).toHaveBeenCalled());
    expect(workflowApi.deleteTrajectory.mock.calls.map((c) => c[2])).toEqual(['t3', 't1']);
    expect(mockToast).toHaveBeenCalledTimes(1);
    expect(mockToast.success).not.toHaveBeenCalled();
  });

  it('a rename onto an existing recording asks „Ersetzen?" inline and replaces on the button', async () => {
    const { saveWorkflowNow, refetchTrajectories } = setup({
      blocks: [replay('Winken', 'b1')], drawer: { focusId: 'Winken' },
    });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_RENAME }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Tanz' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText('Eine Bewegung „Tanz" gibt es schon. Ersetzen?')).toBeInTheDocument();
    expect(workflowApi.deleteTrajectory).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: DE.CONFIRM_YES_REPLACE }));
    await waitFor(() => expect(saveWorkflowNow).toHaveBeenCalledWith({ toastOnSuccess: false }));
    expect(workflowApi.deleteTrajectory).toHaveBeenCalledWith('tok', 'wf1', 't9');
    expect(workflowApi.renameTrajectory).toHaveBeenCalledWith('tok', 'wf1', 't3', 'Tanz');
    expect(ws.getBlockById('b1').getFieldValue('NAME')).toBe('Tanz');
    await waitFor(() => expect(refetchTrajectories).toHaveBeenCalled());
  });
});
