// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The station card / strip (station spec v6.13, §9.2) and the robot check
// (§7.2), against a mocked station controller and a mocked hand-guide service.
//
// Pinned: nothing renders off a station; an unconfigured station offers only
// „Station einrichten (Lehrkraft)"; „Station verlassen" (or „Station starten"
// after leaving); the leader-first sentence (D7); „Kamera prüfen" repeats the
// check; MOVED offers the re-solve behind „Nur nach Rücksprache mit der
// Lehrkraft"; the robot check frees the arm through the existing hand-guide,
// keeps it alive while the student guides the tip, ends it after the check and
// offers the home glide only once the session is verified.

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import StationPanel from '../StationPanel';
import { STATION_DE } from '../../../features/station/stationCopy';

const mockStation = vi.hoisted(() => {
  const listeners = new Set();
  const box = { snap: null };
  return {
    box,
    set(patch) {
      box.snap = { ...box.snap, ...patch };
      listeners.forEach((fn) => fn());
    },
    api: {
      subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
      getSnapshot: () => box.snap,
      leave: vi.fn(),
      startSession: vi.fn(),
      runCameraCheck: vi.fn(),
      runRobotCheck: vi.fn(),
      resolveCamera: vi.fn(),
    },
  };
});
vi.mock('../../../features/station/controller', () => ({ __esModule: true, ...mockStation.api }));

const mockHandGuide = vi.hoisted(() => vi.fn());
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({ handGuide: mockHandGuide }),
}));
const mockGlide = vi.hoisted(() => ({ offer: vi.fn() }));
vi.mock('../../Workshop/HomeGlidePrompt', () => ({
  __esModule: true,
  useHomeGlide: () => ({ offerHomeGlide: mockGlide.offer, homeGlideDialog: null, homeGlideActive: false }),
}));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.success = vi.fn();
  fn.error = vi.fn();
  return { __esModule: true, default: fn };
});

const HELD = {
  active: true,
  stationRig: true,
  phase: 'held',
  held: true,
  sessionId: 's1',
  epoch: 1,
  serverState: 'ACTIVE',
  stationConfigured: true,
  sourceOk: true,
  cameraOk: false,
  robotOk: false,
  verified: false,
  workflowActive: false,
  startOffered: false,
  leaderBlocked: false,
  notice: '',
  streamError: '',
  checking: '',
  cameraCheck: null,
  robotCheck: null,
  resolveJob: null,
};

beforeEach(() => {
  mockStation.box.snap = { ...HELD };
  Object.values(mockStation.api).forEach((f) => (typeof f.mockReset === 'function' ? f.mockReset() : null));
  mockStation.api.leave.mockResolvedValue({ left: true, message: '' });
  mockHandGuide.mockReset();
  mockHandGuide.mockResolvedValue({ success: true, message: '' });
  mockGlide.offer.mockReset();
});

describe('StationPanel', () => {
  it('renders nothing while the controller is inactive (no station, Pi, a claimed Jetson)', () => {
    mockStation.box.snap = { ...HELD, active: false };
    render(<StationPanel onOpenCommission={() => {}} />);
    expect(screen.queryByRole('region', { name: STATION_DE.title })).toBe(null);
    expect(screen.queryByTestId('station-card')).toBe(null);
  });

  it('an unconfigured station offers only „Station einrichten (Lehrkraft)"', async () => {
    mockStation.box.snap = { ...HELD, stationConfigured: false };
    const onOpen = vi.fn();
    render(<StationPanel onOpenCommission={onOpen} />);
    expect(screen.queryByText(STATION_DE.stepCamera)).toBe(null);
    expect(screen.queryByText(STATION_DE.stepRobot)).toBe(null);
    await userEvent.click(screen.getByRole('button', { name: STATION_DE.commission }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('says it is still asking while the robot has not answered', () => {
    mockStation.box.snap = { ...HELD, held: false, phase: 'idle', stationConfigured: null };
    render(<StationPanel onOpenCommission={() => {}} />);
    expect(screen.getByText(STATION_DE.checkingConfig)).toBeInTheDocument();
  });

  it('„Station verlassen" leaves; it is disabled while a real program runs', async () => {
    const { rerender } = render(<StationPanel onOpenCommission={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: STATION_DE.leave }));
    expect(mockStation.api.leave).toHaveBeenCalledTimes(1);
    act(() => mockStation.set({ workflowActive: true }));
    rerender(<StationPanel onOpenCommission={() => {}} />);
    const btn = screen.getByRole('button', { name: STATION_DE.leave });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('title', STATION_DE.stopProgramFirst);
  });

  it('offers „Station starten" after leaving', async () => {
    mockStation.box.snap = { ...HELD, held: false, phase: 'idle', startOffered: true };
    render(<StationPanel onOpenCommission={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: STATION_DE.start }));
    expect(mockStation.api.startSession).toHaveBeenCalledTimes(1);
  });

  it('shows the leader-first sentence while the leader blocks the session (D7)', () => {
    mockStation.box.snap = { ...HELD, held: false, phase: 'idle', leaderBlocked: true };
    render(<StationPanel onOpenCommission={() => {}} />);
    expect(screen.getByText(STATION_DE.leaderFirst)).toBeInTheDocument();
  });

  it('shows the server end reason and the GUI sentence', () => {
    mockStation.box.snap = { ...HELD, streamError: STATION_DE.guiUnreachable };
    render(<StationPanel onOpenCommission={() => {}} />);
    expect(screen.getByTestId('station-notice')).toHaveTextContent(STATION_DE.guiUnreachable);
  });

  it('„Kamera prüfen" repeats the check; it needs the station frames', async () => {
    const { rerender } = render(<StationPanel onOpenCommission={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: STATION_DE.stepCamera }));
    expect(mockStation.api.runCameraCheck).toHaveBeenCalledTimes(1);
    act(() => mockStation.set({ sourceOk: false }));
    rerender(<StationPanel onOpenCommission={() => {}} />);
    expect(screen.getByRole('button', { name: STATION_DE.stepCamera })).toBeDisabled();
  });

  it('a MOVED result offers the re-solve behind the teacher confirmation', async () => {
    mockStation.box.snap = {
      ...HELD,
      cameraCheck: { result: 'MOVED', message: 'Die Kamera wurde seit der Einrichtung bewegt.', rmsMm: 4, maxMm: 30, actionDe: '' },
    };
    render(<StationPanel onOpenCommission={() => {}} />);
    // max > 25 mm: „clearly misaligned" (§7.1).
    expect(screen.getByText(STATION_DE.cameraClearlyMoved)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: STATION_DE.stepResolve }));
    const dialog = screen.getByRole('dialog', { name: STATION_DE.resolveConfirmTitle });
    expect(dialog).toHaveTextContent('Nur nach Rücksprache mit der Lehrkraft');
    await userEvent.click(screen.getByRole('button', { name: STATION_DE.resolveGo }));
    expect(mockStation.api.resolveCamera).toHaveBeenCalledTimes(1);
  });

  it('no re-solve without a MOVED result', () => {
    render(<StationPanel onOpenCommission={() => {}} />);
    expect(screen.queryByRole('button', { name: STATION_DE.stepResolve })).toBe(null);
  });
});

describe('the robot check (§7.2)', () => {
  it('frees the arm, keeps it alive while guiding, ends it after the check, offers the glide when verified', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const onHg = vi.fn();
      mockStation.api.runRobotCheck.mockResolvedValue({ result: 'OK', verified: true });
      render(<StationPanel onOpenCommission={() => {}} onHandGuideChange={onHg} />);
      await act(async () => { screen.getByTestId('station-robot-check').click(); });
      expect(mockHandGuide).toHaveBeenLastCalledWith(true);
      expect(onHg).toHaveBeenLastCalledWith(true);
      expect(screen.getByText(STATION_DE.robotIntro)).toBeInTheDocument();
      // The hand-guide keepalive while the student guides the tip.
      await act(async () => { await vi.advanceTimersByTimeAsync(15100); });
      expect(mockHandGuide.mock.calls.filter((c) => c[0] === true).length).toBe(2);
      await act(async () => { screen.getByRole('button', { name: STATION_DE.robotCheckGo }).click(); });
      await waitFor(() => expect(mockGlide.offer).toHaveBeenCalledTimes(1));
      expect(mockStation.api.runRobotCheck).toHaveBeenCalledTimes(1);
      expect(mockHandGuide).toHaveBeenLastCalledWith(false);
      expect(onHg).toHaveBeenLastCalledWith(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a failed check still ends the hand-guide, and offers no glide', async () => {
    mockStation.api.runRobotCheck.mockResolvedValue({ result: 'NO_DOT', verified: false });
    render(<StationPanel onOpenCommission={() => {}} />);
    await act(async () => { screen.getByTestId('station-robot-check').click(); });
    await act(async () => { screen.getByRole('button', { name: STATION_DE.robotCheckGo }).click(); });
    await waitFor(() => expect(mockHandGuide).toHaveBeenLastCalledWith(false));
    expect(mockGlide.offer).not.toHaveBeenCalled();
  });

  it('re-locks the arm when the panel unmounts mid-guide', async () => {
    const { unmount } = render(<StationPanel onOpenCommission={() => {}} />);
    await act(async () => { screen.getByTestId('station-robot-check').click(); });
    unmount();
    expect(mockHandGuide).toHaveBeenLastCalledWith(false);
  });

  it('is disabled while another surface holds the arm or a program runs', () => {
    const { rerender } = render(<StationPanel onOpenCommission={() => {}} armBusy />);
    expect(screen.getByTestId('station-robot-check')).toBeDisabled();
    mockStation.box.snap = { ...HELD, workflowActive: true };
    rerender(<StationPanel onOpenCommission={() => {}} />);
    expect(screen.getByTestId('station-robot-check')).toBeDisabled();
  });
});
