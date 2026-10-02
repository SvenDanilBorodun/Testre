// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Review V2-R2-1: on the Aufnahme page Space means „Aufnahme starten". A
// sidebar or bottom-navigation button clicked with the MOUSE used to keep the
// focus, so the next Space pressed „Aufnahme" again and started nothing. The
// nav buttons now give the focus back after a pointer click; a keyboard click
// (Enter/Space, `detail === 0`) keeps it, so Tab navigation and its focus ring
// are unchanged. StudentApp is rendered for real over a real store; the pages,
// the ROS/Supabase plumbing and the global overlays are stubs.

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

import tasksReducer from '../../../features/tasks/taskSlice';
import uiReducer from '../../../features/ui/uiSlice';
import rosReducer from '../../../features/ros/rosSlice';
import trainingReducer from '../../../features/training/trainingSlice';
import authReducer, { setSession, setIsLoading } from '../../../features/auth/authSlice';
import workshopReducer from '../../../features/workshop/workshopSlice';
import jetsonReducer from '../../../store/jetsonSlice';
import { setHeartbeatStatus, setTaskStatus } from '../../../features/tasks/taskSlice';
import { moveToPage } from '../../../features/ui/uiSlice';
import PageType from '../../../constants/pageType';
import TaskPhase from '../../../constants/taskPhases';
import StudentApp from '../../../StudentApp';

vi.mock('../../../pages/HomePage', () => ({ __esModule: true, default: () => <div data-testid="page-home" /> }));
vi.mock('../../../pages/RecordPage', () => ({ __esModule: true, default: () => <div data-testid="page-record" /> }));
vi.mock('../../../pages/InferencePage', () => ({ __esModule: true, default: () => <div data-testid="page-inference" /> }));
vi.mock('../../../pages/TrainingPage', () => ({ __esModule: true, default: () => <div data-testid="page-training" /> }));
vi.mock('../../../pages/EditDatasetPage', () => ({ __esModule: true, default: () => <div data-testid="page-data" /> }));
vi.mock('../../../pages/WorkshopPage', () => ({ __esModule: true, default: () => <div data-testid="page-workshop" /> }));
vi.mock('../../../pages/SystemPage', () => ({ __esModule: true, default: () => <div data-testid="page-system" /> }));
vi.mock('../../../components/StartupGate', () => ({ __esModule: true, default: ({ children }) => children }));
vi.mock('../../../components/PiUpdateGate', () => ({ __esModule: true, default: () => null }));
vi.mock('../../../components/CollisionModal', () => ({ __esModule: true, default: () => null }));
vi.mock('../../../components/LoginForm', () => ({ __esModule: true, default: () => <div data-testid="login" /> }));
vi.mock('../../../hooks/useRosTopicSubscription', () => ({
  useRosTopicSubscription: () => ({ initializeSubscriptions: () => {} }),
}));
vi.mock('../../../hooks/useHfUserList', () => ({ useHfUserList: () => ({ reload: () => Promise.resolve([]) }) }));
vi.mock('../../../hooks/useHeartbeatWatchdog', () => ({ useHeartbeatWatchdog: () => {} }));
vi.mock('../../../hooks/useMeProfile', () => ({
  __esModule: true,
  useMeProfile: () => ({}),
  default: () => ({}),
}));
vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: { setOnConnected: () => {}, disconnect: () => {}, getConnection: () => Promise.resolve({}) },
}));
vi.mock('../../../lib/supabaseClient', () => ({
  supabase: {
    auth: {
      getSession: () => new Promise(() => {}),
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: () => {} } } }),
    },
  },
}));

const CAPS = {
  recordable: true, editable: true, trainable: true, inferable: true, roboter_studio: true, has_leader: true,
};

function mountApp() {
  const store = configureStore({
    reducer: {
      tasks: tasksReducer, ui: uiReducer, ros: rosReducer, training: trainingReducer,
      auth: authReducer, workshop: workshopReducer, jetson: jetsonReducer,
    },
  });
  store.dispatch(setSession({ user: { id: 'u1', email: 'schueler@schule.de' }, access_token: 't' }));
  store.dispatch(setIsLoading(false));
  store.dispatch(setHeartbeatStatus('connected'));
  store.dispatch(setTaskStatus({
    robotType: 'omx_f', robotProfile: 'omx_full', capabilities: CAPS, phase: TaskPhase.READY,
    running: false, topicReceived: true,
  }));
  store.dispatch(moveToPage(PageType.HOME));
  render(<Provider store={store}><StudentApp /></Provider>);
  return store;
}

// The desktop rail's buttons carry the label as their title; the mobile
// bottom navigation's carry it as text only.
const railButton = (label) => screen.getByTitle(label);
const bottomNavButton = (label) => screen.getAllByRole('button', { name: label })
  .find((b) => !b.hasAttribute('title'));

describe('StudentApp — a nav button clicked with the mouse gives the focus back (V2-R2-1)', () => {
  it('sidebar „Aufnahme": the page opens and the focus is not left on the nav', async () => {
    const store = mountApp();
    const nav = railButton('Aufnahme');
    nav.focus();
    fireEvent.click(nav, { detail: 1 });
    await waitFor(() => expect(store.getState().ui.currentPage).toBe(PageType.RECORD));
    expect(screen.getByTestId('page-record')).toBeInTheDocument();
    expect(nav).not.toHaveFocus();
  });

  it('bottom navigation: the same', async () => {
    const store = mountApp();
    const nav = bottomNavButton('Training');
    nav.focus();
    fireEvent.click(nav, { detail: 1 });
    await waitFor(() => expect(store.getState().ui.currentPage).toBe(PageType.TRAINING));
    expect(nav).not.toHaveFocus();
  });

  it('a keyboard click keeps the focus on the nav (Tab navigation unchanged)', async () => {
    const store = mountApp();
    const nav = railButton('Aufnahme');
    nav.focus();
    fireEvent.click(nav, { detail: 0 });
    await waitFor(() => expect(store.getState().ui.currentPage).toBe(PageType.RECORD));
    expect(nav).toHaveFocus();
  });
});
