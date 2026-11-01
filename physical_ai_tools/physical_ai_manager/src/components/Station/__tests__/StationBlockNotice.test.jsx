// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Inferenz page's station block (station spec v6.13, §9.2, D13): the
// sentence and „Station verlassen" while this tab holds the station claim,
// nothing otherwise; the button is the controller's leave, disabled while a
// real program runs, and its refusal is toasted.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import toast from 'react-hot-toast';

import StationBlockNotice from '../StationBlockNotice';
import { STATION_DE } from '../../../features/station/stationCopy';

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
    },
  };
});
vi.mock('../../../features/station/controller', () => ({ __esModule: true, ...mockStation.api }));
vi.mock('react-hot-toast', () => {
  const fn = vi.fn();
  fn.error = vi.fn();
  return { __esModule: true, default: fn };
});

beforeEach(() => {
  mockStation.box.snap = { held: false, phase: 'idle', workflowActive: false };
  mockStation.api.leave.mockReset();
  toast.error.mockClear();
});

describe('StationBlockNotice', () => {
  it('renders nothing without a station claim', () => {
    render(<StationBlockNotice />);
    expect(screen.queryByTestId('station-block-notice')).toBe(null);
  });

  it('shows the sentence and leaves on „Station verlassen"', async () => {
    mockStation.box.snap = { held: true, phase: 'held', workflowActive: false };
    mockStation.api.leave.mockResolvedValue({ left: true, message: '' });
    render(<StationBlockNotice />);
    expect(screen.getByText(STATION_DE.busyElsewhere)).toBeInTheDocument();
    await act(async () => { screen.getByRole('button', { name: STATION_DE.leave }).click(); });
    expect(mockStation.api.leave).toHaveBeenCalledTimes(1);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('is disabled with its reason while a program runs; a server refusal is toasted', async () => {
    mockStation.box.snap = { held: true, phase: 'held', workflowActive: true };
    const { rerender } = render(<StationBlockNotice />);
    const btn = screen.getByRole('button', { name: STATION_DE.leave });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('title', STATION_DE.stopProgramFirst);
    act(() => mockStation.set({ workflowActive: false }));
    rerender(<StationBlockNotice />);
    mockStation.api.leave.mockResolvedValue({ left: false, message: STATION_DE.stopProgramFirst });
    await act(async () => { screen.getByRole('button', { name: STATION_DE.leave }).click(); });
    expect(toast.error).toHaveBeenCalledWith(STATION_DE.stopProgramFirst);
  });
});
