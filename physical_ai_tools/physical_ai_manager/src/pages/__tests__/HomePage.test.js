// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Start page „Aufnahme starten" hero button must stay in lockstep with the
// (hidden) Aufnahme tab: on a follower-only rig (capabilities.recordable ===
// false) it is disabled, so a student can't slip into Aufnahme through the hero
// button. It is enabled once a robot identity is present, the bridge is up, and
// recording is allowed.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import tasksReducer, {
  setTaskStatus,
  setHeartbeatStatus,
} from '../../features/tasks/taskSlice';
import authReducer from '../../features/auth/authSlice';
import rosReducer from '../../features/ros/rosSlice';
import uiReducer from '../../features/ui/uiSlice';
import HomePage from '../HomePage';

function makeStore() {
  return configureStore({
    reducer: { tasks: tasksReducer, auth: authReducer, ros: rosReducer, ui: uiReducer },
  });
}

// setTaskStatus adopts only COMPLETE six-boolean manifests (audit fix 3), so
// the fixtures must ship the full server-contract key set.
function fullCaps(overrides = {}) {
  return {
    recordable: true, editable: true, trainable: true,
    inferable: true, roboter_studio: true, has_leader: true,
    ...overrides,
  };
}

function renderWith({ caps }) {
  const store = makeStore();
  store.dispatch(setHeartbeatStatus('connected'));
  store.dispatch(setTaskStatus({ robotType: 'omx_f', capabilities: caps }));
  render(
    <Provider store={store}>
      <HomePage />
    </Provider>
  );
  return store;
}

function recordButton() {
  return screen.getByRole('button', { name: /Aufnahme starten/ });
}

beforeEach(() => {
  try { localStorage.clear(); } catch { /* jsdom always has it */ }
});

describe('HomePage — „Aufnahme starten" capability gate', () => {
  it('is ENABLED on omx_full (recordable true) with the bridge connected', () => {
    renderWith({ caps: fullCaps() });
    expect(recordButton().disabled).toBe(false);
  });

  it('is DISABLED on omx_follower (recordable false)', () => {
    renderWith({ caps: fullCaps({ recordable: false, has_leader: false }) });
    expect(recordButton().disabled).toBe(true);
  });

  it('is ENABLED when caps are unknown (null) — recordable only gates on an explicit false', () => {
    renderWith({ caps: null });
    expect(recordButton().disabled).toBe(false);
  });

  it('a PARTIAL manifest is not adopted (fail-closed) — button behaves as unknown caps', () => {
    // setTaskStatus ignores incomplete manifests (audit fix 3): caps stay null,
    // and null caps gate nothing.
    renderWith({ caps: { recordable: false } });
    expect(recordButton().disabled).toBe(false);
  });
});
