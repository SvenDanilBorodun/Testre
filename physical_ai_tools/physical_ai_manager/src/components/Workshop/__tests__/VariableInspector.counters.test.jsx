/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-50 — the OUTCOME test for the „Zähler" section. Everything else in this
// area asserts one hop: the contract, the dispatched action, the reducer. RS-49
// shipped with every one of those green and the panel still empty, so this file
// joins them up: a real `/workflow/status` message goes through the REAL
// `interceptToken` branch of `useRosTopicSubscription`, into a REAL
// `workshopSlice` reducer, into the REAL `VariableInspector`, and the student
// reads the number off the screen. Only the transport is faked.
//
// REAL-WORLD TRIGGER (the canonical points lesson): „Zähler" → „setze Zähler
// Punkte auf 0" → inside „Solange sichtbar", „erhöhe Zähler Punkte um 1" →
// „wenn Zähler Punkte größer als 3" → „Start" → Debug-Panel → „Variablen".
// Before this change the panel said „Noch keine Variablen." while the program
// was visibly counting, because the counter handlers wrote `ctx.counters` and
// emitted no sentinel at all.
//
// The producing half is verified server-side in
// physical_ai_server/test/test_counter_sentinel.py, which drives the real
// `handlers.counters` and asserts the exact `[CNT:Punkte=1]` bytes.

import React from 'react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { render, screen, renderHook, act, within } from '@testing-library/react';

import workshopReducer from '../../../features/workshop/workshopSlice';
import rosReducer, { setRosbridgeUrl } from '../../../features/ros/rosSlice';
import { signedOut } from '../../../features/session/sessionActions';
import { useRosTopicSubscription } from '../../../hooks/useRosTopicSubscription';
import VariableInspector from '../VariableInspector';
import { DE } from '../blocks/messages_de';
import {
  COUNTER_NAMES_SHOWN,
  COUNTER_NAMES_HIDDEN,
} from '../../../utils/__tests__/counterNames.fixture';

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

  return { store, cb: workflowStatusCallback() };
}

function renderPanel(store) {
  return render(<Provider store={store}><VariableInspector /></Provider>);
}

// The two sections are `<section aria-label=…>`, i.e. role="region" with an
// accessible name. Scoping every query is not decoration: the „Zähler" heading
// is literally the text „Zähler", which is also a perfectly ordinary variable
// name — telling one „Punkte" from the other is the whole point of the design.
const counters = () => within(screen.getByRole('region', { name: DE.DEBUG_SECTION_COUNTERS }));
const variables = () => within(screen.getByRole('region', { name: DE.DEBUG_SECTION_VARIABLES }));

beforeEach(() => {
  mockTopicSubscribe.mockClear();
  storeRef.current = null;
});

describe('a counter the student named reaches the „Zähler" section', () => {
  test('„Punkte" — wire → store → screen', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    expect(counters().getByText(DE.DEBUG_NO_COUNTERS)).toBeInTheDocument();

    act(() => cb({ log_message: '[CNT:Punkte=1]', phase: 'running' }));

    expect(counters().queryByText(DE.DEBUG_NO_COUNTERS)).toBeNull();
    expect(counters().getByText('Punkte')).toBeInTheDocument();
    expect(counters().getByText('1')).toBeInTheDocument();
  });

  test.each(COUNTER_NAMES_SHOWN)('%j is shown', async (name) => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: `[CNT:${name}=12]`, phase: 'running' }));
    expect(counters().getByText(name)).toBeInTheDocument();
    expect(counters().getByText('12')).toBeInTheDocument();
  });

  test('the points lesson counts up in place: 0 → 1 → 2 → 3', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: '[CNT:Punkte=0]', phase: 'running' }));
    act(() => cb({ log_message: '[CNT:Punkte=1]', phase: 'running' }));
    act(() => cb({ log_message: '[CNT:Punkte=2]', phase: 'running' }));
    act(() => cb({ log_message: '[CNT:Punkte=3]', phase: 'running' }));
    expect(counters().getAllByText('Punkte')).toHaveLength(1);
    expect(counters().getByText('3')).toBeInTheDocument();
    expect(counters().queryByText('2')).toBeNull();
  });

  test('two counters from one program both show', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => {
      cb({ log_message: '[CNT:Punkte=3]', phase: 'running' });
      cb({ log_message: '[CNT:Fehlversuche=1]', phase: 'running' });
    });
    expect(counters().getByText('Punkte')).toBeInTheDocument();
    expect(counters().getByText('Fehlversuche')).toBeInTheDocument();
  });
});

describe('a VARIABLE „Punkte" and a ZÄHLER „Punkte" both display, correctly', () => {
  // The specific failure the cheaper prefix design was rejected to avoid.
  // „Punkte" is pre-filled in all four Zähler blocks, so most students have a
  // counter by that name; a student naming a variable „Punkte" as well is the
  // ordinary case, not a contrived one.
  test('both rows are on screen, in their own sections, with their own values', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => {
      cb({ log_message: '[VAR:Punkte="noch nichts"]', phase: 'running' });
      cb({ log_message: '[CNT:Punkte=7]', phase: 'running' });
    });

    // Two rows named „Punkte", one per section — neither overwrote the other.
    expect(screen.getAllByText('Punkte')).toHaveLength(2);
    expect(variables().getByText('Punkte')).toBeInTheDocument();
    expect(counters().getByText('Punkte')).toBeInTheDocument();

    // …and each shows ITS OWN value, in ITS OWN section.
    expect(variables().getByText('"noch nichts"')).toBeInTheDocument();
    expect(variables().queryByText('7')).toBeNull();
    expect(counters().getByText('7')).toBeInTheDocument();
    expect(counters().queryByText('"noch nichts"')).toBeNull();
  });

  test('order does not matter — the counter first, then the variable', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => {
      cb({ log_message: '[CNT:Punkte=7]', phase: 'running' });
      cb({ log_message: '[VAR:Punkte="noch nichts"]', phase: 'running' });
    });
    expect(counters().getByText('7')).toBeInTheDocument();
    expect(variables().getByText('"noch nichts"')).toBeInTheDocument();
  });

  test('counting on does not disturb the variable, and vice versa', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => {
      cb({ log_message: '[VAR:Punkte=100]', phase: 'running' });
      cb({ log_message: '[CNT:Punkte=1]', phase: 'running' });
      cb({ log_message: '[CNT:Punkte=2]', phase: 'running' });
    });
    expect(variables().getByText('100')).toBeInTheDocument();
    expect(counters().getByText('2')).toBeInTheDocument();
    expect(store.getState().workshop.variables.Punkte.value).toBe(100);
    expect(store.getState().workshop.counters.Punkte.value).toBe(2);
  });
});

describe('what must NOT reach the „Zähler" section', () => {
  test.each(COUNTER_NAMES_HIDDEN)('%j shows nothing', async (name) => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: `[CNT:${name}=1]`, phase: 'running' }));
    expect(counters().getByText(DE.DEBUG_NO_COUNTERS)).toBeInTheDocument();
  });

  test('__proto__ shows nothing and leaves the prototype alone', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: '[CNT:__proto__=1]', phase: 'running' }));
    expect(counters().getByText(DE.DEBUG_NO_COUNTERS)).toBeInTheDocument();
    expect(Object.getPrototypeOf(store.getState().workshop.counters))
      .toBe(Object.prototype);
  });
});

describe('RETIREMENT on screen', () => {
  test('the section empties on sign-out — the next student sees no tally', async () => {
    const { store, cb } = await wireUp();
    renderPanel(store);
    act(() => cb({ log_message: '[CNT:Punkte=9]', phase: 'running' }));
    expect(counters().getByText('9')).toBeInTheDocument();

    act(() => { store.dispatch(signedOut()); });
    expect(counters().getByText(DE.DEBUG_NO_COUNTERS)).toBeInTheDocument();
    expect(counters().queryByText('Punkte')).toBeNull();
  });
});

describe('the empty state', () => {
  test('both sections are labelled and both say so before a run', async () => {
    const { store } = await wireUp();
    renderPanel(store);
    expect(screen.getByRole('region', { name: DE.DEBUG_SECTION_VARIABLES }))
      .toBeInTheDocument();
    expect(screen.getByRole('region', { name: DE.DEBUG_SECTION_COUNTERS }))
      .toBeInTheDocument();
    expect(variables().getByText(DE.DEBUG_NO_VARIABLES)).toBeInTheDocument();
    expect(counters().getByText(DE.DEBUG_NO_COUNTERS)).toBeInTheDocument();
  });

  test('the German is exactly what the panel promises', () => {
    expect(DE.DEBUG_SECTION_COUNTERS).toBe('Zähler');
    expect(DE.DEBUG_NO_COUNTERS).toBe('Noch keine Zähler.');
    // Same shape as the sibling it sits next to.
    expect(DE.DEBUG_NO_VARIABLES).toBe('Noch keine Variablen.');
  });
});
