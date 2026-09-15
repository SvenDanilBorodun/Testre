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
 * The drawer's preview controls (▶ per recording version with the drawer's own
 * tempo, ▶ per Ziel/Position) and their result lines, against a real headless
 * Blockly workspace, the real destination store and the real studioAssets
 * reducer. The page's startPreview is the `onPreview` spy.
 */

import React from 'react';
import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, within, act } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import * as Blockly from 'blockly/core';
import 'blockly/blocks';
import * as De from 'blockly/msg/de';
import studioAssetsReducer, {
  openDrawer,
  previewFailed,
  previewStarted,
  previewUnreachable,
} from '../../../../features/workshop/studioAssetsSlice';
import { setWorkflowStatus } from '../../../../features/workshop/workshopSlice';
import { registerTrajectoryBlocks } from '../../blocks/trajectories';
import { registerDestinationBlocks } from '../../blocks/destinations';
import { DE } from '../../blocks/messages_de';
import { getDestinationStore, registerDestinationSerializer } from '../destinationStore';
import { createSammlungProvider } from '../provider';
import SammlungDrawer from '../SammlungDrawer';

vi.mock('../../../../services/workflowApi', () => ({
  renameTrajectory: vi.fn(async () => ({})),
  deleteTrajectory: vi.fn(async () => ({})),
  getTrajectory: vi.fn(async () => ({})),
  createTrajectory: vi.fn(async () => ({})),
  listTrajectories: vi.fn(async () => []),
}));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() }),
}));

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
];
const PIN = { name: 'Ablage', kind: 'pin', source: 'camera', robot_type: 'omx_f', x: 0.182, y: -0.064, z: 0.012 };

function setup({ tab = 'aufnahmen', focus = 'Winken', preview = true, previewPending = false } = {}) {
  ws = new Blockly.Workspace();
  ws.getToolbox = () => ({ getWidth: () => 118, clearSelection: vi.fn() });
  const store = getDestinationStore(ws);
  store.add(PIN);
  const pinId = store.getByName('Ablage').id;
  const provider = createSammlungProvider({
    capabilities: { hardware: true, drawer: true, preview, previewPending },
    robotType: 'omx_f',
    trajectories: { status: 'ready', items: ITEMS },
  });
  const redux = configureStore({ reducer: { studioAssets: studioAssetsReducer } });
  redux.dispatch(openDrawer({ tab, focusId: focus === 'pin' ? pinId : focus }));
  const onPreview = vi.fn();
  render(
    <Provider store={redux}>
      <SammlungDrawer
        workspace={ws}
        provider={provider}
        accessToken="tok"
        workflowId="wf1"
        robotType="omx_f"
        onPreview={onPreview}
        saveWorkflowNow={vi.fn(async () => ({ ok: true }))}
        refetchTrajectories={vi.fn()}
      />
    </Provider>,
  );
  return { redux, onPreview, pinId, provider };
}

describe('DrawerRecording — preview', () => {
  it('plays the newest version at the drawer tempo', () => {
    const { redux, onPreview } = setup();
    const section = screen.getByRole('region', { name: DE.PREVIEW_START });
    expect(within(section).getByRole('heading', { name: DE.PREVIEW_START })).toBeInTheDocument();
    fireEvent.click(within(section).getByRole('button', { name: DE.PREVIEW_PLAY }));
    expect(onPreview).toHaveBeenLastCalledWith(
      { kind: 'recording', id: 't3', name: 'Winken', robotProfile: 'omx_f' }, { tempo: 1.0 });

    const fast = within(section).getByRole('button', { name: DE.RUN_TEMPO_FAST });
    fireEvent.click(fast);
    expect(redux.getState().studioAssets.drawer.previewTempo).toBe(2.0);
    expect(fast).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(within(section).getByRole('button', { name: DE.PREVIEW_PLAY }));
    expect(onPreview).toHaveBeenLastCalledWith(
      { kind: 'recording', id: 't3', name: 'Winken', robotProfile: 'omx_f' }, { tempo: 2.0 });
  });

  it('an older version has its own ▶', () => {
    const { onPreview } = setup();
    fireEvent.click(screen.getByRole('button', { name: DE.RUN_TEMPO_SLOW }));
    const older = screen.getAllByRole('button', { name: DE.PREVIEW_START });
    expect(older).toHaveLength(1);
    fireEvent.click(older[0]);
    expect(onPreview).toHaveBeenLastCalledWith(
      { kind: 'recording', id: 't1', name: 'Winken', robotProfile: null }, { tempo: 0.5 });
  });

  it('shows a refusal of any version, and nothing for ok', () => {
    const { redux } = setup();
    act(() => { redux.dispatch(previewFailed({ key: 'rec:t1', message: 'Die Aufnahme ist beschädigt.' })); });
    expect(screen.getByText('Die Aufnahme ist beschädigt.')).toBeInTheDocument();
    act(() => {
      redux.dispatch(previewStarted({ key: 'rec:t3', kind: 'recording', name: 'Winken', workflowId: 'vorschau-aufnahme-t3' }));
      redux.dispatch(setWorkflowStatus({ workflow_id: 'vorschau-aufnahme-t3', phase: 'finished' }));
    });
    expect(redux.getState().studioAssets.lastPreviewResult['rec:t3'].status).toBe('ok');
    expect(screen.queryByText('Die Aufnahme ist beschädigt.')).toBeNull();
  });

  it('no preview controls without the capability', () => {
    setup({ preview: false });
    expect(screen.queryByRole('button', { name: DE.PREVIEW_PLAY })).toBeNull();
    expect(screen.queryByRole('button', { name: DE.PREVIEW_START })).toBeNull();
  });
});

describe('DrawerPlace — preview', () => {
  it('▶ Im Simulator ansehen plays the entry at the drawer tempo', () => {
    const { onPreview, pinId } = setup({ tab: 'ziele', focus: 'pin' });
    fireEvent.click(screen.getByRole('button', { name: `▶ ${DE.PREVIEW_START}` }));
    expect(onPreview).toHaveBeenLastCalledWith({ kind: 'pin', id: pinId, name: 'Ablage' }, { tempo: 1.0 });
  });

  it('a refusal shows its message; unreachable shows the chip text and the message', () => {
    const { redux, pinId } = setup({ tab: 'ziele', focus: 'pin' });
    const key = `dest:${pinId}`;
    act(() => { redux.dispatch(previewFailed({ key, message: 'Unbekanntes Ziel „Ablage".' })); });
    expect(screen.getByText('Unbekanntes Ziel „Ablage".')).toBeInTheDocument();
    act(() => {
      redux.dispatch(previewStarted({ key, kind: 'pin', name: 'Ablage', workflowId: `vorschau-ziel-${pinId}` }));
      redux.dispatch(previewUnreachable({ key, message: 'Ziel außerhalb des Arbeitsbereichs.' }));
      redux.dispatch(setWorkflowStatus({ workflow_id: `vorschau-ziel-${pinId}`, phase: 'finished' }));
    });
    expect(screen.getByText(`${DE.CHIP_UNREACHABLE}: Ziel außerhalb des Arbeitsbereichs.`)).toBeInTheDocument();
    expect(screen.queryByText('Unbekanntes Ziel „Ablage".')).toBeNull();
  });
});

// Owner decision 2026-09-15 (B1): until the leader-status bridge has answered
// once (WorkshopPage `previewPending`), every drawer ▶ is disabled with the
// German hint as its title; the answer (a provider snapshot) re-enables it.
describe('Drawer ▶ — waits for the first leader-status answer', () => {
  it('recording: ▶ Abspielen and an older version ▶ are disabled with the hint, then enabled', () => {
    const { onPreview, provider } = setup({ previewPending: true });
    const section = screen.getByRole('region', { name: DE.PREVIEW_START });
    const play = within(section).getByRole('button', { name: DE.PREVIEW_PLAY });
    const [older] = screen.getAllByRole('button', { name: DE.PREVIEW_START });
    for (const btn of [play, older]) {
      expect(btn).toBeDisabled();
      expect(btn).toHaveAttribute('title', 'Roboterstatus wird geprüft …');
      expect(btn).toHaveAccessibleDescription(DE.PREVIEW_BLOCK_LEADER_PENDING);
      fireEvent.click(btn);
    }
    expect(onPreview).not.toHaveBeenCalled();

    act(() => { provider.setSnapshot({ capabilities: { previewPending: false } }); });
    const playNow = within(screen.getByRole('region', { name: DE.PREVIEW_START }))
      .getByRole('button', { name: DE.PREVIEW_PLAY });
    const [olderNow] = screen.getAllByRole('button', { name: DE.PREVIEW_START });
    expect(playNow).toBeEnabled();
    expect(playNow).not.toHaveAttribute('title');
    expect(olderNow).toBeEnabled();
    expect(olderNow).toHaveAttribute('title', DE.PREVIEW_START);
    fireEvent.click(playNow);
    expect(onPreview).toHaveBeenLastCalledWith(
      { kind: 'recording', id: 't3', name: 'Winken', robotProfile: 'omx_f' }, { tempo: 1.0 });
  });

  it('Ziel: ▶ Im Simulator ansehen is disabled with the hint, then enabled', () => {
    const { onPreview, pinId, provider } = setup({ tab: 'ziele', focus: 'pin', previewPending: true });
    const btn = screen.getByRole('button', { name: `▶ ${DE.PREVIEW_START}` });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('title', DE.PREVIEW_BLOCK_LEADER_PENDING);
    fireEvent.click(btn);
    expect(onPreview).not.toHaveBeenCalled();
    act(() => { provider.setSnapshot({ capabilities: { previewPending: false } }); });
    const now = screen.getByRole('button', { name: `▶ ${DE.PREVIEW_START}` });
    expect(now).toBeEnabled();
    expect(now).not.toHaveAttribute('title');
    fireEvent.click(now);
    expect(onPreview).toHaveBeenLastCalledWith({ kind: 'pin', id: pinId, name: 'Ablage' }, { tempo: 1.0 });
  });
});
