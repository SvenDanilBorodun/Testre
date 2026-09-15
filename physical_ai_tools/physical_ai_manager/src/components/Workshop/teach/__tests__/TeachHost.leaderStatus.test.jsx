/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// R7 (fixed 2026-09-15), end to end: the REAL TeachHost and TeachOverlay over the
// REAL studioAssets reducer. A Vormachen request on a rig that may have a leader,
// while the leader-status bridge cannot report, opens with a German notice and
// no teaching; the bridge's later answer resolves the session to hand or leader
// mode exactly as an open with that answer would have. Only the ROS services,
// sounds, the glide provider and the activation hook are doubles.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import studioAssetsReducer, { requestTeach } from '../../../../features/workshop/studioAssetsSlice';
import TeachHost from '../TeachHost';
import { DE } from '../../blocks/messages_de';

vi.mock('react-hot-toast', () => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return { __esModule: true, default: t };
});
const mockRos = vi.hoisted(() => ({
  handGuide: vi.fn(), recordControl: vi.fn(), capturePose: vi.fn(), replayMotion: vi.fn(),
}));
vi.mock('../../../../hooks/useRosServiceCaller', () => ({ __esModule: true, useRosServiceCaller: () => mockRos }));
vi.mock('../../HomeGlidePrompt', () => ({
  __esModule: true,
  useHomeGlide: () => ({ offerHomeGlide: vi.fn(), homeGlideDialog: null, homeGlideActive: false }),
}));
vi.mock('../teachSounds', () => ({
  __esModule: true,
  createTeachSounds: () => ({ tick() {}, start() {}, stop() {}, capture() {}, dispose() {} }),
}));
vi.mock('../../../../utils/rosConnectionManager', () => ({ __esModule: true, default: { ros: null } }));
vi.mock('roslib', () => ({
  __esModule: true,
  default: { Topic: function Topic() { this.subscribe = () => {}; this.unsubscribe = () => {}; } },
}));
vi.mock('../../../../hooks/useRobotActivation', async (importOriginal) => ({
  ...(await importOriginal()),
  default: () => ({ status: null, activate: vi.fn(), calling: false, error: null }),
}));

const PENDING = { available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false, probed: false };
const DOWN = { ...PENDING, probed: true };
const FOLLOWER = { available: true, followerOnly: true, hasLeader: true, busy: false, leaderOn: false, probed: true };
const LEADER_ON = { ...FOLLOWER, followerOnly: false, leaderOn: true };

function mount(rsBridge, caps = { has_leader: true }) {
  const store = configureStore({
    reducer: {
      studioAssets: studioAssetsReducer,
      tasks: (s = { collision: { active: false } }) => s,
    },
  });
  const props = {
    isActive: true,
    workspace: { hideChaff: vi.fn() },
    accessToken: 'jwt',
    workflowId: 'wf-1',
    robotType: 'omx_f',
    caps,
    heartbeatStatus: 'connected',
    runState: 'idle',
    paused: false,
    simMode: false,
    jogHandGuideOn: false,
    previewActive: false,
    saveWorkflowNow: vi.fn(),
    refetchTrajectories: vi.fn(),
  };
  const view = render(
    <Provider store={store}><TeachHost {...props} rsBridge={rsBridge} /></Provider>,
  );
  const answer = (next, nextCaps = caps) => view.rerender(
    <Provider store={store}><TeachHost {...props} caps={nextCaps} rsBridge={next} /></Provider>,
  );
  act(() => { store.dispatch(requestTeach({ focus: null })); });
  return { store, answer, view };
}

const notice = () => screen.queryByTestId('teach-leader-status');
const recButton = () => screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_REC) });

beforeEach(() => {
  Object.values(mockRos).forEach((fn) => fn.mockReset());
});

describe('Vormachen on a leader rig whose bridge cannot report (R7)', () => {
  test('opens with the pending notice, then the answer „follower only" resolves to HAND mode', () => {
    const { store, answer } = mount(PENDING);
    expect(store.getState().studioAssets.teach).toMatchObject({ open: true, mode: null });
    expect(screen.getByTestId('teach-overlay')).toBeInTheDocument();
    expect(notice()).toHaveTextContent('Roboterstatus wird geprüft …');
    expect(recButton()).toBeDisabled();
    expect(screen.queryByText(DE.TEACH_MODE_HAND)).toBeNull();

    answer(DOWN);
    expect(store.getState().studioAssets.teach.mode).toBeNull();
    expect(notice()).toHaveTextContent(DE.TEACH_LEADER_STATUS_UNKNOWN);
    expect(recButton()).toBeDisabled();

    answer(FOLLOWER);
    expect(store.getState().studioAssets.teach.mode).toBe('hand');
    expect(notice()).toBeNull();
    expect(screen.getByText(DE.TEACH_MODE_HAND)).toBeInTheDocument();
    expect(screen.getByText(DE.TEACH_STATE_LOCKED)).toBeInTheDocument();
    expect(recButton()).toBeEnabled();
    expect(mockRos.handGuide).not.toHaveBeenCalled();
  });

  test('the answer „leader on" resolves to LEADER mode', () => {
    const { store, answer } = mount(DOWN);
    expect(store.getState().studioAssets.teach.mode).toBeNull();
    expect(notice()).toHaveTextContent(DE.TEACH_LEADER_STATUS_UNKNOWN);
    answer(LEADER_ON);
    expect(store.getState().studioAssets.teach.mode).toBe('leader');
    expect(notice()).toBeNull();
    expect(screen.getByText(DE.TEACH_MODE_LEADER)).toBeInTheDocument();
    expect(screen.getByText(DE.TEACH_STATE_LEADER_READY)).toBeInTheDocument();
    expect(screen.queryByText(DE.TEACH_LEADER_TURNED_ON)).toBeNull();
    expect(recButton()).toBeEnabled();
  });

  test('a leader-less profile opens HAND mode at once, with no notice, whatever the bridge says', () => {
    const { store } = mount(PENDING, { has_leader: false });
    expect(store.getState().studioAssets.teach).toMatchObject({ open: true, mode: 'hand' });
    expect(notice()).toBeNull();
    expect(recButton()).toBeEnabled();
  });

  test('„Fertig" closes an unresolved session without a service call', () => {
    const { store } = mount(PENDING);
    act(() => { screen.getByRole('button', { name: `${DE.TEACH_DONE} (Esc)` }).click(); });
    expect(store.getState().studioAssets.teach).toMatchObject({ open: false, mode: null });
    expect(screen.queryByTestId('teach-overlay')).toBeNull();
    Object.values(mockRos).forEach((fn) => expect(fn).not.toHaveBeenCalled());
  });
});
