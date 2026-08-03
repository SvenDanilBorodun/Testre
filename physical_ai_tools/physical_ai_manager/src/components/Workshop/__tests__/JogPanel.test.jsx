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
import { render as rtlRender, screen, waitFor, act, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import JogPanel from '../JogPanel';

// JogPanel reads the capability manifest (profile-driven jog rows, edu6 §4.5)
// via useSelector — wrap every render in a minimal store. null caps = the OMX
// default rows, keeping every pre-edu6 assertion byte-identical.
function makeStore(capabilities = null) {
  return configureStore({
    reducer: {
      tasks: () => ({ taskStatus: { capabilities } }),
    },
  });
}

function render(ui, { capabilities = null } = {}) {
  return rtlRender(<Provider store={makeStore(capabilities)}>{ui}</Provider>);
}

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
    // The mock records at CALL time: `toggleHandGuide` invokes handGuide(enabled)
    // and only AWAITS it before `setHandGuideOn(enabled)`, so the assertion above
    // is satisfied strictly BEFORE the state that flips the button label commits.
    // The synchronous `getByRole('Arm festsetzen')` this used to end on therefore
    // raced that commit and failed ~0.5 % of runs. `findByRole` retries, which is
    // all this call site needs — unlike the pagehide test below, nothing here
    // depends on a PASSIVE EFFECT having run, only on the rendered DOM.
    const festsetzen = await screen.findByRole('button', { name: 'Arm festsetzen' });

    // While freigeschaltet a jog nudge is disabled → no jogArm call.
    expect(screen.getByRole('button', { name: 'Gelenk 1 erhöhen' })).toBeDisabled();

    await userEvent.click(festsetzen);
    await waitFor(() => expect(mockRos.handGuide).toHaveBeenCalledWith(false));
  });

  test('disabled hides jogging: every nudge + freischalten is disabled', () => {
    render(<JogPanel disabled={true} />);
    expect(screen.getByRole('button', { name: 'Gelenk 1 erhöhen' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'X erhöhen' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Arm freischalten' })).toBeDisabled();
  });

  test('a pagehide while freigeschaltet re-fixes the arm (limp-arm safety)', async () => {
    // Waiting for the BUTTON to flip is not enough, and that mistake made this
    // test flake ~1.8 % of suite runs (measured over 220): `findByRole`'s
    // MutationObserver resolves on a DOM mutation produced at React's COMMIT,
    // while `handGuideOnRef.current` is written by a `useEffect` in the
    // PASSIVE-EFFECT phase scheduled AFTER that commit. When the observer's
    // microtask won, `refixArm` read a stale `false`, returned early, and the
    // `waitFor` below expired with zero calls.
    //
    // So gate on something the EFFECT PHASE itself produces. `onHandGuideChange`
    // is called from a second `useEffect` declared AFTER the ref-writing one in
    // JogPanel, and React runs a commit's effects in declaration order — so
    // observing `true` here proves the ref assignment already ran in the same
    // flush. (`await act(async () => {})` would also flush them, but it asserts
    // nothing about which one ran; this way the ordering is the assertion.)
    const onHandGuideChange = vi.fn();
    render(<JogPanel disabled={false} onHandGuideChange={onHandGuideChange} />);
    await userEvent.click(screen.getByRole('button', { name: 'Arm freischalten' }));
    await waitFor(() => expect(onHandGuideChange).toHaveBeenCalledWith(true));
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

// ── edu6 profile rows (§4.5/§9) ──────────────────────────────────────────────
const EDU6_CAPS = {
  recordable: false, editable: false, trainable: false, inferable: false,
  roboter_studio: true, has_leader: false,
  arm_joints: 6,
  joint_names: ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6',
    'end_gear_joint'],
  urdf_asset_id: 'edu6',
  gripper_open_rad: 1.75,
  gripper_closed_rad: 0.0,
  gripper_mm_per_rad: 25.2,
};

describe('JogPanel — edu6 profile rows', () => {
  it('renders Gelenk 1..6 + the millimetre gripper row', () => {
    render(<JogPanel disabled={false} />, { capabilities: EDU6_CAPS });
    for (let i = 1; i <= 6; i += 1) {
      expect(screen.getByText(`Gelenk ${i}`)).toBeInTheDocument();
    }
    expect(screen.getByText('Greifer öffnen/schließen')).toBeInTheDocument();
    expect(screen.queryByText('Greifer-Drehung')).not.toBeInTheDocument();
  });

  it('the gripper row jogs index 6 with an mm-derived radian delta', async () => {
    render(<JogPanel disabled={false} />, { capabilities: EDU6_CAPS });
    fireEvent.click(
      screen.getByRole('button', { name: 'Greifer öffnen/schließen erhöhen' }),
    );
    await waitFor(() => expect(mockRos.jogArm).toHaveBeenCalledTimes(1));
    const call = mockRos.jogArm.mock.calls[0][0];
    expect(call.mode).toBe('joint');
    expect(call.index).toBe(6);
    // medium preset: 0.01 m of jaw travel → (0.01·1000)/25.2 rad ≈ 0.3968.
    expect(call.delta).toBeCloseTo((0.01 * 1000) / 25.2, 9);
  });

  it('OMX (null caps) keeps the exact pre-edu6 rows', () => {
    render(<JogPanel disabled={false} />);
    for (let i = 1; i <= 5; i += 1) {
      expect(screen.getByText(`Gelenk ${i}`)).toBeInTheDocument();
    }
    expect(screen.queryByText('Gelenk 6')).not.toBeInTheDocument();
    expect(screen.getByText('Greifer-Drehung')).toBeInTheDocument();
  });
});
