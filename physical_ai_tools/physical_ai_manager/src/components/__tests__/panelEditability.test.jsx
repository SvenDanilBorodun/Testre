// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// D8 companion #2: InfoPanel + InferencePanel derive form editability from the
// EXPLICIT phase/running signal, not the old 1 s /task/status SILENCE detector.
// With the new ~0.5 Hz idle identity tick, a silence detector would oscillate-
// lock every field (~1 s per tick, focus/keystroke loss). Contract (updated by
// audit fix 5): the form is LOCKED until the FIRST real /task/status tick lands
// (taskStatus.topicReceived) — the initialState is READY/not-running, so
// without that gate a reload MID-TASK painted a phantom-editable window; a
// stream of READY / not-running ticks then keeps the form EDITABLE; a
// RECORDING tick LOCKS it. The „✏️ Bearbeitungsmodus" / „🔒 Nur lesen"
// indicator (present in both panels, keyed on isEditable) is the assertion
// target.

import React from 'react';
import { render, screen, act } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import tasksReducer, { setTaskStatus } from '../../features/tasks/taskSlice';
import uiReducer from '../../features/ui/uiSlice';
import rosReducer from '../../features/ros/rosSlice';
import trainingReducer from '../../features/training/trainingSlice';
import editDatasetReducer from '../../features/editDataset/editDatasetSlice';
import authReducer from '../../features/auth/authSlice';
import workshopReducer from '../../features/workshop/workshopSlice';
import jetsonReducer from '../../store/jetsonSlice';
import TaskPhase from '../../constants/taskPhases';
import InfoPanel from '../InfoPanel';
import InferencePanel from '../InferencePanel';

// Heavy data hooks the panels mount — stubbed so the panels render standalone.
// (vi.mock is hoisted above these imports by vitest, so the stubs still apply.)
vi.mock('../../hooks/useHfUserList', () => ({
  __esModule: true,
  useHfUserList: () => ({ hfUserList: [], reload: () => Promise.resolve([]) }),
}));
vi.mock('../../hooks/useSupabaseTrainings', () => ({
  __esModule: true,
  default: () => ({ jobs: [] }),
}));

function makeStore() {
  return configureStore({
    reducer: {
      tasks: tasksReducer,
      ui: uiReducer,
      ros: rosReducer,
      training: trainingReducer,
      editDataset: editDatasetReducer,
      auth: authReducer,
      workshop: workshopReducer,
      jetson: jetsonReducer,
    },
  });
}

function editable() {
  return screen.queryByText(/Bearbeitungsmodus/) !== null;
}

function locked() {
  return screen.queryByText(/Nur lesen/) !== null;
}

describe.each([
  ['InfoPanel', InfoPanel],
  ['InferencePanel', InferencePanel],
])('%s — editability derives from phase/running (D8 companion #2)', (name, Panel) => {
  it('locks before the first tick, stays editable through idle READY ticks, locks on RECORDING', () => {
    const store = makeStore();
    render(
      <Provider store={store}>
        <Panel />
      </Provider>
    );

    // PRE-FIRST-TICK: locked (audit fix 5). The initialState is READY/not-
    // running, so without the topicReceived gate a reload MID-TASK would show a
    // phantom-editable form until the first /task/status tick lands.
    expect(editable()).toBe(false);
    expect(locked()).toBe(true);

    // First real idle READY tick (topicReceived stamped like the production
    // /task/status handler does) → editable.
    act(() => {
      store.dispatch(
        setTaskStatus({ phase: TaskPhase.READY, running: false, topicReceived: true })
      );
    });
    expect(editable()).toBe(true);
    expect(locked()).toBe(false);

    // A burst of idle identity ticks (each a NEW taskStatus reference) must NOT
    // flip editability off — the oscillation the silence detector caused.
    act(() => {
      for (let i = 0; i < 5; i += 1) {
        store.dispatch(
          setTaskStatus({ phase: TaskPhase.READY, running: false, topicReceived: true, usedCpu: i })
        );
      }
    });
    expect(editable()).toBe(true);
    expect(locked()).toBe(false);

    // A real RECORDING tick locks the form.
    act(() => {
      store.dispatch(
        setTaskStatus({ phase: TaskPhase.RECORDING, running: true, topicReceived: true })
      );
    });
    expect(editable()).toBe(false);
    expect(locked()).toBe(true);
  });

  it('a mid-task reload stays LOCKED on the first tick when that tick reports running', () => {
    // The exact phantom-editable scenario: browser reloads while RECORDING is
    // in flight. The very first tick already reports the running phase — the
    // form must go straight from "locked (no tick yet)" to "locked (running)"
    // with no editable window in between.
    const store = makeStore();
    render(
      <Provider store={store}>
        <Panel />
      </Provider>
    );
    expect(editable()).toBe(false);

    act(() => {
      store.dispatch(
        setTaskStatus({ phase: TaskPhase.RECORDING, running: true, topicReceived: true })
      );
    });
    expect(editable()).toBe(false);
    expect(locked()).toBe(true);
  });
});
