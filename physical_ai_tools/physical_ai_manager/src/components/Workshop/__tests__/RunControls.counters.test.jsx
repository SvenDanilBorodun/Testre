/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// RS-50 — RETIREMENT, half one: „Start" clears the „Zähler" section.
//
// CLAUDE.md, „Editor UI invariants": every server-pushed Roboter-Studio field
// needs an event that RETIRES it. `[CNT:]` is pushed only when a counter is
// WRITTEN, so nothing retires it on its own — a program whose „setze Zähler auf
// 0" sits behind a condition, or one that only READS a counter, would show the
// PREVIOUS run's tally for the whole of the new run. The sign-out half is in
// features/workshop/__tests__/workshopSlice.counters.test.js.
//
// Modelled on RunControls.staleError.test.jsx, which pinned the same property
// for `workflowError` — including its third case, that the clear happens BEFORE
// any of handleStart's abort paths can return.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunControls from '../RunControls';
import { clearCounters, clearVariables } from '../../../features/workshop/workshopSlice';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockRos = vi.hoisted(() => ({
  callService: vi.fn(() =>
    Promise.resolve({
      success: true,
      message: 'gestartet',
      unreachable_block_ids: [],
      unreachable_messages: [],
    }),
  ),
  pauseWorkflow: vi.fn(),
  stepWorkflow: vi.fn(),
  continueWorkflow: vi.fn(),
  setWorkflowBreakpoints: vi.fn(),
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));
vi.mock('../../../services/workflowApi', () => ({
  __esModule: true,
  getTrajectoryByName: vi.fn(),
}));
const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

const PROGRAM = { blocks: { blocks: [{ type: 'edubotics_home' }] } };

beforeEach(() => {
  mockState = {
    workshop: {
      runState: 'idle',
      phase: '',
      currentBlockId: null,
      paused: false,
      log: [],
      workflowError: null,
      debuggerVisible: false,
      debuggerWarnings: [],
      breakpoints: [],
    },
    auth: { session: { access_token: 'jwt-1' } },
  };
  mockDispatch.mockClear();
  mockRos.callService.mockClear();
  mockToast.error.mockClear();
  global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
});

const dispatchedType = (type) =>
  mockDispatch.mock.calls.some(([a]) => a && a.type === type);

describe('RunControls — a new run does not inherit the last one’s Zähler', () => {
  test('Start clears the counters', async () => {
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalled());
    expect(dispatchedType(clearCounters().type)).toBe(true);
  });

  test('it is cleared next to the variables, not instead of them', async () => {
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalled());
    expect(dispatchedType(clearVariables().type)).toBe(true);
    expect(dispatchedType(clearCounters().type)).toBe(true);
  });

  test('it is cleared BEFORE the start can abort, not after it succeeds', async () => {
    // Every abort path (refused service call, missing trajectory, empty
    // program) returns above the `setRunState('running')` at the bottom of the
    // handler. Clearing there would leave a dead run's tally over a start that
    // never happened — exactly what a student hits pressing Start again after a
    // failure.
    mockRos.callService.mockResolvedValueOnce({ success: false, message: 'Roboter belegt.' });
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith('Roboter belegt.'));
    expect(dispatchedType(clearCounters().type)).toBe(true);
  });
});
