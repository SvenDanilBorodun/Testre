/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Batch 2b — RecordPanel: start/stop a hand-guided recording, then save the
// recorded points to the cloud as a named trajectory on the current workflow.

import React from 'react';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RecordPanel from '../RecordPanel';

const POINTS = [
  [0, 0, 0, 0, 0, 0, 0.0],
  [0.1, 0, 0, 0, 0, 0, 0.033],
  [0.2, 0, 0, 0, 0, 0, 0.066],
];
const STOP_OK = {
  success: true,
  sample_count: 3,
  duration_s: 2.0,
  points_json: JSON.stringify({ fps: 30, points: POINTS }),
};

// react-redux: selector-aware stub over a mutable module-level state so the panel
// can read the rig identity (taskStatus.robotType) it tags a saved recording with.
let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

const mockRos = vi.hoisted(() => ({
  recordControl: vi.fn(),
  replayMotion: vi.fn(() => Promise.resolve({ success: true })),
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

const mockCreate = vi.hoisted(() => vi.fn(() => Promise.resolve({ id: 't1' })));
vi.mock('../../../services/workflowApi', () => ({
  __esModule: true,
  createTrajectory: mockCreate,
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

function setRecord(map) {
  mockRos.recordControl.mockImplementation((action) =>
    Promise.resolve(map[action] || { success: false }),
  );
}

beforeEach(() => {
  mockRos.recordControl.mockReset();
  mockRos.replayMotion.mockClear();
  mockCreate.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  // Default rig: an OMX follower (data_robot_type 'omx_f').
  mockState = { tasks: { taskStatus: { robotType: 'omx_f' } } };
  setRecord({ start: { success: true }, stop: STOP_OK, cancel: { success: true } });
});

describe('RecordPanel', () => {
  test('start begins recording and shows Stopp', async () => {
    render(<RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
    await waitFor(() => expect(mockRos.recordControl).toHaveBeenCalledWith('start'));
    expect(await screen.findByRole('button', { name: 'Stopp' })).toBeInTheDocument();
  });

  test('stop → review → save posts the parsed trajectory to the cloud', async () => {
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('Bewegung 1');
    render(<RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={false} />);

    await userEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Stopp' }));
    await waitFor(() => expect(mockRos.recordControl).toHaveBeenCalledWith('stop'));

    expect(await screen.findByText(/Aufgenommen: 3 Punkte/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Speichern' }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith('jwt-1', 'wf-1', {
        name: 'Bewegung 1',
        fps: 30,
        points: POINTS,
        duration_s: 2.0,
        // Tagged with the OMX rig identity (the default store).
        robot_profile: 'omx_f',
      }),
    );
    promptSpy.mockRestore();
  });

  test('tags the saved trajectory with the edu6 rig profile', async () => {
    mockState = { tasks: { taskStatus: { robotType: 'edu6_studio' } } };
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('Bewegung 1');
    // edu6 records 8-wide points; the panel forwards whatever the backend sent.
    setRecord({
      start: { success: true },
      stop: {
        success: true,
        sample_count: 2,
        duration_s: 1.0,
        points_json: JSON.stringify({
          fps: 25,
          points: [[0, 0, 0, 0, 0, 0, 0, 0.0], [0.1, 0, 0, 0, 0, 0, 0, 0.04]],
        }),
      },
      cancel: { success: true },
    });
    render(<RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={false} />);

    await userEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Stopp' }));
    expect(await screen.findByText(/Aufgenommen: 2 Punkte/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Speichern' }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith(
        'jwt-1',
        'wf-1',
        expect.objectContaining({ robot_profile: 'edu6_studio' }),
      ),
    );
    promptSpy.mockRestore();
  });

  test('omits robot_profile when the rig identity is unknown (falls back to OMX)', async () => {
    mockState = { tasks: { taskStatus: { robotType: '' } } };
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('Bewegung 1');
    render(<RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={false} />);

    await userEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Stopp' }));
    expect(await screen.findByText(/Aufgenommen: 3 Punkte/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Speichern' }));
    await waitFor(() => expect(mockCreate).toHaveBeenCalled());
    const payload = mockCreate.mock.calls[0][2];
    expect(payload).not.toHaveProperty('robot_profile');
    promptSpy.mockRestore();
  });

  test('a stop with zero samples returns to idle with a German hint', async () => {
    setRecord({ start: { success: true }, stop: { success: true, sample_count: 0, points_json: '' } });
    render(<RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Stopp' }));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.stringContaining('Keine Bewegung aufgenommen'),
        expect.anything(),
      ),
    );
    // Back to idle.
    expect(await screen.findByRole('button', { name: 'Bewegung aufnehmen' })).toBeInTheDocument();
  });

  test('Verwerfen during recording cancels the recording', async () => {
    render(<RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Verwerfen' }));
    await waitFor(() => expect(mockRos.recordControl).toHaveBeenCalledWith('cancel'));
    expect(await screen.findByRole('button', { name: 'Bewegung aufnehmen' })).toBeInTheDocument();
  });

  test('without a saved workflow the Speichern button is disabled', async () => {
    render(<RecordPanel accessToken="jwt-1" workflowId={null} disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Stopp' }));
    expect(await screen.findByText(/Aufgenommen: 3 Punkte/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Speichern' })).toBeDisabled();
    expect(mockCreate).not.toHaveBeenCalled();
  });

  test('a hand-guide conflict (disabled) locks Speichern in review (FE-3)', async () => {
    const { rerender } = render(
      <RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={false} />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Stopp' }));
    expect(await screen.findByText(/Aufgenommen: 3 Punkte/)).toBeInTheDocument();
    // With no conflict, Speichern is enabled …
    expect(screen.getByRole('button', { name: 'Speichern' })).not.toBeDisabled();
    // … then JogPanel opens a hand-guide session → the parent flips `disabled`.
    // Speichern must lock, else its closeManualSession() → handGuide(false) would
    // re-torque the arm out from under JogPanel while it shows „freigeschaltet".
    rerender(<RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={true} />);
    expect(screen.getByRole('button', { name: 'Speichern' })).toBeDisabled();
  });

  test('auto-stops recording at the RECORD_MAX_S cap (120 s)', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    try {
      render(<RecordPanel accessToken="jwt-1" workflowId="wf-1" disabled={false} />);
      // Start recording, then flush the async start (promise microtask + effects)
      // so the elapsed interval is armed before we advance the clock.
      fireEvent.click(screen.getByRole('button', { name: 'Bewegung aufnehmen' }));
      await act(async () => { await Promise.resolve(); });
      expect(screen.getByRole('button', { name: 'Stopp' })).toBeInTheDocument();
      mockRos.recordControl.mockClear();
      // Blow past the 120 s cap — the effect must auto-invoke Stopp exactly once.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(121000);
      });
      expect(mockRos.recordControl).toHaveBeenCalledWith('stop');
      expect(
        mockRos.recordControl.mock.calls.filter((c) => c[0] === 'stop'),
      ).toHaveLength(1);
      expect(mockToast).toHaveBeenCalledWith(
        expect.stringContaining('Maximale Aufnahmedauer erreicht'),
        expect.anything(),
      );
    } finally {
      vi.useRealTimers();
    }
  });
});
