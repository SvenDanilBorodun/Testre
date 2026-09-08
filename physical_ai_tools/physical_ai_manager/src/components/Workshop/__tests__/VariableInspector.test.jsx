/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-49 — the OUTCOME test. Everything else in this area asserts one hop:
// the predicate, the dispatched action, the reducer. Each of those passed while
// the student saw „Noch keine Variablen." on a program that had just assigned
// three variables, because the two hops were gated by two DIFFERENT regexes and
// no test ever joined them up.
//
// So this file joins them up. It drives a real `/workflow/status` message
// through the REAL `interceptToken` branch of `useRosTopicSubscription`, into a
// REAL `workshopSlice` reducer, into the REAL `VariableInspector`, and asserts
// the student can read the name off the screen. Only the transport is faked
// (roslib + the rosbridge connection) — nothing between the wire and the DOM.
//
// REAL-WORLD TRIGGER: „Variablen" → „Variable erstellen …" → the student types
// „meine Zahl" → „setze meine Zahl auf 7" → „Start" → Debug-Panel → „Variablen".
// The producing half is verified separately: executing the real server-side
// `Interpreter` over a workspace whose variable table maps that id to
// „meine Zahl" emits exactly `[VAR:meine Zahl=7.0]`.

import React from 'react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { render, screen, renderHook, act } from '@testing-library/react';

import workshopReducer from '../../../features/workshop/workshopSlice';
import rosReducer, { setRosbridgeUrl } from '../../../features/ros/rosSlice';
import { useRosTopicSubscription } from '../../../hooks/useRosTopicSubscription';
import VariableInspector from '../VariableInspector';
import { DE } from '../blocks/messages_de';
import { BLOCKLY_REAL_NAMES } from '../../../utils/__tests__/blocklyVariableNames.fixture';

// The hook's [VAR:] branch dispatches on the imported SINGLETON store, not on
// its own `useDispatch`. Point that singleton at the per-test store so the
// sentinel really reaches the reducer under test.
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

vi.mock('../../../utils/rosConnectionManager', () => ({
  __esModule: true,
  default: {
    getConnection: vi.fn(() =>
      Promise.resolve({ isConnected: true, on: () => {}, off: () => {} })
    ),
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

function workflowStatusCallback() {
  const calls = mockTopicSubscribe.mock.calls.filter((c) => c[0] === '/workflow/status');
  return calls.length ? calls[calls.length - 1][1] : null;
}

// Mount the hook against the SAME store the panel renders from, subscribe to
// /workflow/status, and hand back the topic callback the server would drive.
async function wireUp() {
  const store = configureStore({
    reducer: { workshop: workshopReducer, ros: rosReducer },
  });
  store.dispatch(setRosbridgeUrl('ws://localhost:9090'));
  storeRef.current = store;

  const wrapper = ({ children }) => <Provider store={store}>{children}</Provider>;
  const { result } = renderHook(() => useRosTopicSubscription(), { wrapper });
  await act(async () => { await result.current.subscribeToWorkflowStatus(); });
  await act(async () => { await Promise.resolve(); });

  return { store, wrapper, cb: workflowStatusCallback() };
}

function renderPanel(store) {
  return render(
    <Provider store={store}><VariableInspector /></Provider>
  );
}

beforeEach(() => {
  mockTopicSubscribe.mockClear();
  storeRef.current = null;
});

describe('a variable the student named reaches the Variablen panel', () => {
  test('„meine Zahl" — wire → store → screen', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    // Before the run there is nothing to show.
    expect(screen.getByText(DE.DEBUG_NO_VARIABLES)).toBeInTheDocument();

    act(() => cb({ log_message: '[VAR:meine Zahl=7]', phase: 'running' }));

    expect(screen.queryByText(DE.DEBUG_NO_VARIABLES)).toBeNull();
    expect(screen.getByText('meine Zahl')).toBeInTheDocument();
    expect(screen.getByText('7')).toBeInTheDocument();
  });

  test.each(BLOCKLY_REAL_NAMES)('%j is shown', async (name) => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: `[VAR:${name}=42]`, phase: 'running' }));
    expect(screen.getByText(name)).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
  });

  test('three variables from one program all show', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => {
      cb({ log_message: '[VAR:meine Zahl=7]', phase: 'running' });
      cb({ log_message: '[VAR:Anzahl Würfel=3]', phase: 'running' });
      cb({ log_message: '[VAR:Öl-Stand="voll"]', phase: 'running' });
    });
    expect(screen.getByText('meine Zahl')).toBeInTheDocument();
    expect(screen.getByText('Anzahl Würfel')).toBeInTheDocument();
    expect(screen.getByText('Öl-Stand')).toBeInTheDocument();
    // Strings are rendered JSON-quoted so „voll" cannot be mistaken for a name.
    expect(screen.getByText('"voll"')).toBeInTheDocument();
  });

  test('a later assignment replaces the value in the same row', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: '[VAR:meine Zahl=7]', phase: 'running' }));
    act(() => cb({ log_message: '[VAR:meine Zahl=8]', phase: 'running' }));
    expect(screen.getAllByText('meine Zahl')).toHaveLength(1);
    expect(screen.getByText('8')).toBeInTheDocument();
    expect(screen.queryByText('7')).toBeNull();
  });
});

describe('what must NOT reach the panel', () => {
  test('a control character in the name shows nothing', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: '[VAR:a\u0000b=1]', phase: 'running' }));
    expect(screen.getByText(DE.DEBUG_NO_VARIABLES)).toBeInTheDocument();
  });

  test('__proto__ shows nothing and leaves the prototype alone', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: '[VAR:__proto__={"pwned":true}]', phase: 'running' }));
    expect(screen.getByText(DE.DEBUG_NO_VARIABLES)).toBeInTheDocument();
    expect(Object.getPrototypeOf(store.getState().workshop.variables))
      .toBe(Object.prototype);
  });
});
