/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Batch 2b — JogPanel („Roboter steuern (Tippbetrieb)"): per-joint / cartesian
// nudges call jogArm with the right mode+index+delta, and the hand-guide pair
// calls handGuide(true/false) and disables jog while the arm is freigeschaltet.

import React from 'react';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import JogPanel from '../JogPanel';

const DEG2RAD = Math.PI / 180;

// Stable hook identities (the real hook returns stable useCallbacks).
const mockRos = vi.hoisted(() => ({
  jogArm: vi.fn(() => Promise.resolve({ success: true })),
  handGuide: vi.fn(() => Promise.resolve({ success: true })),
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

const mockToast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

beforeEach(() => {
  mockRos.jogArm.mockClear();
  mockRos.handGuide.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
});

describe('JogPanel', () => {
  test('a joint nudge calls jogArm(joint, index, +step-in-radians)', async () => {
    render(<JogPanel disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Gelenk 1 erhöhen' }));
    await waitFor(() => expect(mockRos.jogArm).toHaveBeenCalled());
    expect(mockRos.jogArm).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: 'joint',
        index: 0,
        delta: expect.closeTo(5 * DEG2RAD, 6), // default „mittel" preset = 5°
      }),
    );
  });

  test('the gripper rotation is joint index 5, decreasing sends a negative delta', async () => {
    render(<JogPanel disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Greifer-Drehung verringern' }));
    await waitFor(() => expect(mockRos.jogArm).toHaveBeenCalled());
    expect(mockRos.jogArm).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: 'joint',
        index: 5,
        delta: expect.closeTo(-5 * DEG2RAD, 6),
      }),
    );
  });

  test('a cartesian X nudge calls jogArm(cartesian, index 0, +step-in-metres)', async () => {
    render(<JogPanel disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'X erhöhen' }));
    await waitFor(() => expect(mockRos.jogArm).toHaveBeenCalled());
    expect(mockRos.jogArm).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: 'cartesian',
        index: 0,
        delta: expect.closeTo(0.01, 6), // „mittel" cartesian step = 10 mm
      }),
    );
  });

  test('freischalten calls handGuide(true) and disables jog; festsetzen calls handGuide(false)', async () => {
    render(<JogPanel disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Arm freischalten' }));
    await waitFor(() => expect(mockRos.handGuide).toHaveBeenCalledWith(true));

    // While freigeschaltet a jog nudge is disabled → no jogArm call.
    const jointBtn = screen.getByRole('button', { name: 'Gelenk 1 erhöhen' });
    expect(jointBtn).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: 'Arm festsetzen' }));
    await waitFor(() => expect(mockRos.handGuide).toHaveBeenCalledWith(false));
  });

  test('disabled hides jogging: every nudge + freischalten is disabled', () => {
    render(<JogPanel disabled={true} />);
    expect(screen.getByRole('button', { name: 'Gelenk 1 erhöhen' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'X erhöhen' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Arm freischalten' })).toBeDisabled();
  });

  test('a pagehide while freigeschaltet re-fixes the arm (limp-arm safety)', async () => {
    render(<JogPanel disabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Arm freischalten' }));
    // Wait until the arm is actually freigeschaltet (button flips) so the
    // handGuideOn state the listener reads has committed + its ref flushed.
    await screen.findByRole('button', { name: 'Arm festsetzen' });
    mockRos.handGuide.mockClear();
    // Tab-close / navigation fires pagehide, but NOT a React unmount cleanup —
    // the listener must still re-torque the follower.
    window.dispatchEvent(new Event('pagehide'));
    await waitFor(() => expect(mockRos.handGuide).toHaveBeenCalledWith(false));
  });

  test('keeps an active hand-guide session alive with a periodic handGuide(true) (FE-4)', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    try {
      render(<JogPanel disabled={false} />);
      // Freischalten opens the session (handGuide(true) once), then flush the
      // async toggle so handGuideOn commits + the keepalive interval arms.
      fireEvent.click(screen.getByRole('button', { name: 'Arm freischalten' }));
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      expect(screen.getByRole('button', { name: 'Arm festsetzen' })).toBeInTheDocument();
      mockRos.handGuide.mockClear();
      // 15 s of no manual call → the keepalive re-sends handGuide(true) so the
      // backend idle watchdog does not stiffen the actively hand-guided arm.
      await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
      expect(mockRos.handGuide).toHaveBeenCalledWith(true);
    } finally {
      vi.useRealTimers();
    }
  });

  test('reports the hand-guide state up via onHandGuideChange', async () => {
    const onHandGuideChange = vi.fn();
    render(<JogPanel disabled={false} onHandGuideChange={onHandGuideChange} />);
    // Mount reports the initial closed state so the parent starts consistent.
    expect(onHandGuideChange).toHaveBeenCalledWith(false);
    await userEvent.click(screen.getByRole('button', { name: 'Arm freischalten' }));
    await waitFor(() => expect(onHandGuideChange).toHaveBeenCalledWith(true));
    await userEvent.click(screen.getByRole('button', { name: 'Arm festsetzen' }));
    await waitFor(() => {
      const calls = onHandGuideChange.mock.calls.map((c) => c[0]);
      expect(calls[calls.length - 1]).toBe(false);
    });
  });
});
