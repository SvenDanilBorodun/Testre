/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// LeaderToggle on a camera station (station spec v6.13, §3.3, §9.1, D7):
//   * with a station session held, „Leader verbinden" becomes „Station verlassen
//     und Leader einschalten": the station's leave FIRST, `leader-enable` only
//     after it left; a refusal during a program sends nothing to the GUI;
//   * it reports a switch in flight to the station controller (the automatic
//     enter waits for it);
//   * the GUI's 409 („Roboter Studio nutzt gerade die Stationskamera.") is
//     shown as it comes.

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import toast from 'react-hot-toast';
import LeaderToggle from '../LeaderToggle';
import { STATION_DE } from '../../../features/station/stationCopy';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));
vi.mock('react-hot-toast', () => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return { __esModule: true, default: t };
});
vi.mock('../../../utils/rosConnectionManager', () => ({ __esModule: true, default: { ros: null } }));
vi.mock('roslib', () => ({
  __esModule: true,
  default: { Topic: function Topic() { this.subscribe = () => {}; this.unsubscribe = () => {}; } },
}));

const mockStation = vi.hoisted(() => {
  const listeners = new Set();
  const box = { snap: { held: false, phase: 'idle', workflowActive: false } };
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
      reportLeaderSwitching: vi.fn(),
    },
  };
});
vi.mock('../../../features/station/controller', () => ({ __esModule: true, ...mockStation.api }));

let order;
let statusBody;
let enableAnswer;

beforeEach(() => {
  mockState = { tasks: { taskStatus: { phase: 0, capabilities: { has_leader: true, station_protocol: 6 } } } };
  mockStation.box.snap = { held: true, phase: 'held', workflowActive: false };
  mockStation.api.leave.mockReset();
  mockStation.api.reportLeaderSwitching.mockReset();
  toast.error.mockClear();
  toast.success.mockClear();
  order = [];
  statusBody = { follower_only: true, busy: false, has_leader: true };
  enableAnswer = { ok: true, status: 200, body: { ok: true, message: 'Leader verbunden.' } };
  mockStation.api.leave.mockImplementation(async () => {
    order.push('leave');
    return { left: true, message: '' };
  });
  global.fetch = vi.fn((url, opts = {}) => {
    if (opts.method === 'POST') {
      order.push(url.replace(/^.*\/roboter-studio\//, ''));
      return Promise.resolve({ ok: enableAnswer.ok, status: enableAnswer.status, json: () => Promise.resolve(enableAnswer.body) });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(statusBody) });
  });
});

async function openAndConfirm() {
  const btn = await screen.findByRole('button', { name: STATION_DE.leaveAndLeaderOn });
  await act(async () => { btn.click(); });
  const confirm = screen.getAllByRole('button', { name: STATION_DE.leaveAndLeaderOn }).pop();
  await act(async () => { confirm.click(); });
}

describe('LeaderToggle with a station session held', () => {
  it('offers „Station verlassen und Leader einschalten" in place of „Leader verbinden"', async () => {
    render(<LeaderToggle isActive />);
    expect(await screen.findByRole('button', { name: STATION_DE.leaveAndLeaderOn })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Leader verbinden' })).toBe(null);
  });

  it('leaves the station first, then switches the leader on', async () => {
    render(<LeaderToggle isActive />);
    await openAndConfirm();
    await waitFor(() => expect(order).toEqual(['leave', 'leader-enable']));
  });

  it('a refusal during a program switches nothing and says why', async () => {
    mockStation.api.leave.mockImplementation(async () => {
      order.push('leave');
      return { left: false, message: STATION_DE.stopProgramFirst };
    });
    render(<LeaderToggle isActive />);
    await openAndConfirm();
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(STATION_DE.stopProgramFirst));
    expect(order).toEqual(['leave']);
  });

  it('is disabled while a real program runs (the server\'s workflow_active)', async () => {
    mockStation.box.snap = { held: true, phase: 'held', workflowActive: true };
    render(<LeaderToggle isActive />);
    const btn = await screen.findByRole('button', { name: STATION_DE.leaveAndLeaderOn });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('title', STATION_DE.stopProgramFirst);
  });

  it('without a station session it is the plain „Leader verbinden"', async () => {
    mockStation.box.snap = { held: false, phase: 'idle', workflowActive: false };
    render(<LeaderToggle isActive />);
    const btn = await screen.findByRole('button', { name: 'Leader verbinden' });
    await act(async () => { btn.click(); });
    const confirm = screen.getByRole('button', { name: 'Verbinden' });
    await act(async () => { confirm.click(); });
    await waitFor(() => expect(order).toEqual(['leader-enable']));
    expect(mockStation.api.leave).not.toHaveBeenCalled();
  });
});

describe('LeaderToggle and the GUI', () => {
  it('shows the GUI\'s 409 sentence while the Studio stream is on (§3.3)', async () => {
    mockStation.box.snap = { held: false, phase: 'idle', workflowActive: false };
    statusBody = { follower_only: false, busy: false, has_leader: true };
    enableAnswer = { ok: false, status: 409, body: { ok: false, message: 'Roboter Studio nutzt gerade die Stationskamera.' } };
    render(<LeaderToggle isActive />);
    const btn = await screen.findByRole('button', { name: /Leader abschalten/ });
    await act(async () => { btn.click(); });
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Roboter Studio nutzt gerade die Stationskamera.'));
  });

  it('reports a switch in flight (and the GUI reporting one) to the station controller', async () => {
    mockStation.box.snap = { held: false, phase: 'idle', workflowActive: false };
    statusBody = { follower_only: true, busy: true, has_leader: true };
    render(<LeaderToggle isActive />);
    await waitFor(() => expect(mockStation.api.reportLeaderSwitching).toHaveBeenLastCalledWith(expect.any(String), true));
  });
});
