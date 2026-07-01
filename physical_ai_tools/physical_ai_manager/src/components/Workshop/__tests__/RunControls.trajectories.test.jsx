/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Batch 2b — CONTRACT C: RunControls collects the trajectory names referenced by
// „spiele Bewegung ab" replay blocks, fetches each from the cloud, and injects a
// top-level `trajectories` sibling { name: { fps, points } } into the
// /workflow/start payload (both sim + real). A missing trajectory aborts the
// start with a German toast.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunControls from '../RunControls';

const POINTS = [
  [0, 0, 0, 0, 0, 0, 0.0],
  [0.1, 0, 0, 0, 0, 0, 0.033],
];

// react-redux: selector-aware stub + dispatch spy.
let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

// ROS service caller — capture the /workflow/start payload.
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

const mockGetTrajectory = vi.hoisted(() => vi.fn());
vi.mock('../../../services/workflowApi', () => ({
  __esModule: true,
  getTrajectoryByName: mockGetTrajectory,
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

function baseState() {
  return {
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
}

const REPLAY_JSON = {
  blocks: { blocks: [{ type: 'edubotics_replay_trajectory', fields: { NAME: 'Bewegung 1' } }] },
};

beforeEach(() => {
  mockState = baseState();
  mockDispatch.mockClear();
  mockRos.callService.mockClear();
  mockGetTrajectory.mockReset();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  // probeRsStatus → no bridge (fails open, Start allowed).
  global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
});

describe('RunControls — CONTRACT C trajectories injection', () => {
  test('fetches referenced trajectories and injects them into /workflow/start', async () => {
    mockGetTrajectory.mockResolvedValue({ fps: 30, points: POINTS });
    render(<RunControls workflowId="wf-1" blocklyJson={REPLAY_JSON} simMode={false} simScene={null} />);

    await userEvent.click(screen.getByRole('button', { name: /Start/ }));

    await waitFor(() => expect(mockRos.callService).toHaveBeenCalled());
    expect(mockGetTrajectory).toHaveBeenCalledWith('jwt-1', 'wf-1', 'Bewegung 1');

    const [name, , payload] = mockRos.callService.mock.calls[0];
    expect(name).toBe('/workflow/start');
    const parsed = JSON.parse(payload.workflow_json);
    expect(parsed.trajectories).toEqual({ 'Bewegung 1': { fps: 30, points: POINTS } });
  });

  test('a program without replay blocks injects an empty trajectories map', async () => {
    render(
      <RunControls
        workflowId="wf-1"
        blocklyJson={{ blocks: { blocks: [{ type: 'edubotics_home' }] } }}
        simMode={false}
        simScene={null}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalled());
    expect(mockGetTrajectory).not.toHaveBeenCalled();
    const parsed = JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json);
    expect(parsed.trajectories).toEqual({});
  });

  test('a missing trajectory aborts the start with a German toast', async () => {
    mockGetTrajectory.mockRejectedValue(new Error('not found'));
    render(<RunControls workflowId="wf-1" blocklyJson={REPLAY_JSON} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() =>
      expect(mockToast.error).toHaveBeenCalledWith(
        expect.stringContaining('Bewegung konnte nicht geladen werden'),
      ),
    );
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('replay blocks with an unsaved workflow abort with a German toast', async () => {
    render(<RunControls workflowId={null} blocklyJson={REPLAY_JSON} simMode={false} simScene={null} />);
    await userEvent.click(screen.getByRole('button', { name: /Start/ }));
    await waitFor(() =>
      expect(mockToast.error).toHaveBeenCalledWith(
        expect.stringContaining('Bitte zuerst den Workflow speichern'),
      ),
    );
    expect(mockGetTrajectory).not.toHaveBeenCalled();
    expect(mockRos.callService).not.toHaveBeenCalled();
  });
});
