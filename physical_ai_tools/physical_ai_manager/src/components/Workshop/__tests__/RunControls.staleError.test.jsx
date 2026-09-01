/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// 2026-09-01 latching-state pass. `handleStart` cleared three pieces of
// previous-run state (log, variables, debuggerWarnings) and skipped the fourth
// — `workflowError`, the only one the student SEES, as a red role="alert" that
// also force-opens the Protokoll drawer. The server emits no further
// WorkflowStatus after a terminal `error` phase, so nothing else retired it.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunControls from '../RunControls';
import { clearWorkflowError } from '../../../features/workshop/workshopSlice';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockRos = vi.hoisted(() => ({
  callService: vi.fn(() =>
    Promise.resolve({ success: true, message: 'gestartet', unreachable_block_ids: [], unreachable_messages: [] }),
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

const DEAD_RUN_ERROR = 'Zielpunkt liegt außerhalb des Arbeitsbereichs.';

function baseState(workflowError) {
  return {
    workshop: {
      runState: 'idle',
      phase: '',
      currentBlockId: null,
      paused: false,
      log: [],
      workflowError,
      debuggerVisible: false,
      debuggerWarnings: [],
      breakpoints: [],
    },
    auth: { session: { access_token: 'jwt-1' } },
  };
}

const PROGRAM = { blocks: { blocks: [{ type: 'edubotics_home' }] } };

beforeEach(() => {
  mockState = baseState(DEAD_RUN_ERROR);
  mockDispatch.mockClear();
  mockRos.callService.mockClear();
  mockToast.error.mockClear();
  global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
});

const clearWasDispatched = () =>
  mockDispatch.mock.calls.some(([a]) => a && a.type === clearWorkflowError().type);

describe('RunControls — a new run does not inherit the last one’s error', () => {
  test('the previous run’s alert really is on screen before Start', () => {
    // Not vacuous: without this the test below could pass against a component
    // that never renders workflowError at all.
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    expect(screen.getByRole('alert')).toHaveTextContent(DEAD_RUN_ERROR);
  });

  test('Start clears it', async () => {
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalled());
    expect(clearWasDispatched()).toBe(true);
  });

  test('it is cleared BEFORE the start can abort, not after it succeeds', async () => {
    // The abort paths (refused service call, missing trajectory, empty program)
    // all `return` above the setRunState('running') at the bottom of the
    // handler. Clearing there would leave a dead run's alert over a start that
    // never happened — the exact case a student hits when they press Start
    // again after a failure.
    mockRos.callService.mockResolvedValueOnce({ success: false, message: 'Roboter belegt.' });
    render(<RunControls workflowId="wf-1" blocklyJson={PROGRAM} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith('Roboter belegt.'));
    expect(clearWasDispatched()).toBe(true);
  });
});
