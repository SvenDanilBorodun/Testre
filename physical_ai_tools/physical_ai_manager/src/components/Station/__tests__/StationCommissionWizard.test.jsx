// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The commissioning wizard (station spec v6.13, §6, §9.2) against a mocked
// station controller whose `commission()` plays the server.
//
// Pinned: it polls `status` every 2 s while mounted; a command that creates a
// job answers QUEUED and the next step is offered only after THAT job's
// job_state ended in the poll, with its job_message shown; a reopened wizard
// resumes the running commission from `status`; the touch steps show the next
// dot and keep „Bitte den Arm loslassen …" on screen until a new touch begins.

import React from 'react';
import { act, render, screen } from '@testing-library/react';

import StationCommissionWizard, { stepFromStatus } from '../StationCommissionWizard';
import { COMMISSION_DE } from '../../../features/station/stationCopy';

const mockStation = vi.hoisted(() => {
  const listeners = new Set();
  return {
    snap: {
      active: true, held: true, phase: 'held', sessionId: 's1', sourceOk: true, layoutId: 'OMX-STATION-A',
    },
    status: {},
    answers: {},
    api: {
      subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
      getSnapshot: () => mockStation.snap,
      commission: vi.fn(),
    },
  };
});
vi.mock('../../../features/station/controller', () => ({ __esModule: true, ...mockStation.api }));
vi.mock('../../Workshop/CameraFeedOverlay', () => ({
  __esModule: true,
  default: ({ camera, sourceOk }) => <div data-testid="station-preview" data-camera={camera} data-source-ok={String(sourceOk)} />,
}));

function reply(res) {
  return Promise.resolve({ ok: true, res });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: false });
  mockStation.status = { commission_id: '', job_id: 0, job_state: 'none' };
  mockStation.answers = {};
  mockStation.api.commission.mockReset();
  mockStation.api.commission.mockImplementation((command, fields) => {
    if (command === 'status') return reply({ ...mockStation.status });
    const a = mockStation.answers[command];
    return reply(typeof a === 'function' ? a(fields) : (a || { accepted: true, success: true, result: 'OK' }));
  });
});

afterEach(() => {
  vi.useRealTimers();
});

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

async function tick(ms = 2000) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

const calls = (command) => mockStation.api.commission.mock.calls.filter((c) => c[0] === command);

describe('StationCommissionWizard', () => {
  it('polls status every 2 s while mounted, and stops when closed', async () => {
    const { unmount } = render(<StationCommissionWizard onClose={() => {}} />);
    await flush();
    expect(calls('status')).toHaveLength(1);
    await tick(4000);
    expect(calls('status')).toHaveLength(3);
    unmount();
    await tick(6000);
    expect(calls('status')).toHaveLength(3);
  });

  it('begin answers QUEUED; the lens step comes only once THAT job is done', async () => {
    mockStation.answers.begin = { accepted: true, success: true, result: 'QUEUED', job_id: 5 };
    render(<StationCommissionWizard onClose={() => {}} />);
    await flush();
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.begin }).click(); });
    expect(calls('begin')[0][1]).toEqual({ layout_id: 'OMX-STATION-A' });
    expect(screen.getByTestId('commission-message')).toHaveTextContent(COMMISSION_DE.waitJob);
    expect(screen.getByRole('button', { name: COMMISSION_DE.begin })).toBeDisabled();
    // Another job's end changes nothing.
    mockStation.status = { commission_id: '', job_id: 4, job_state: 'done', job_message: 'alt' };
    await tick();
    expect(screen.queryByText(COMMISSION_DE.lensIntro)).toBe(null);
    mockStation.status = { commission_id: 'c-1', job_id: 5, job_state: 'done', job_message: 'Einrichtung begonnen.' };
    await tick();
    expect(screen.getByText(COMMISSION_DE.lensIntro)).toBeInTheDocument();
    expect(screen.getByTestId('commission-message')).toHaveTextContent('Einrichtung begonnen.');
    expect(screen.getByTestId('station-preview')).toHaveAttribute('data-camera', 'station_scene');
  });

  it('a failed or stale claim stays on the start screen with the job sentence', async () => {
    mockStation.answers.begin = { accepted: true, success: true, result: 'QUEUED', job_id: 9 };
    render(<StationCommissionWizard onClose={() => {}} />);
    await flush();
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.begin }).click(); });
    mockStation.status = { commission_id: '', job_id: 9, job_state: 'failed', job_message: 'Diese Matte ist noch nicht freigegeben.' };
    await tick();
    expect(screen.getByText(COMMISSION_DE.intro)).toBeInTheDocument();
    expect(screen.getByTestId('commission-message')).toHaveTextContent('Diese Matte ist noch nicht freigegeben.');
  });

  it('lens capture sends the commission id and shows the counts', async () => {
    mockStation.status = { commission_id: 'c-1', step: 'lens', job_id: 5, job_state: 'done' };
    render(<StationCommissionWizard onClose={() => {}} />);
    await flush();
    expect(screen.getByText(COMMISSION_DE.lensIntro)).toBeInTheDocument();
    mockStation.status = { ...mockStation.status, lens_views_fit: 12, lens_views_heldout: 3, coverage_cells: 9 };
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.lensCapture }).click(); });
    expect(calls('lens_capture')[0][1]).toEqual({ commission_id: 'c-1' });
    expect(screen.getByTestId('lens-counts')).toHaveTextContent(COMMISSION_DE.lensCounts(12, 3, 9));
  });

  it('resumes a running commission at its step, with the next dot', async () => {
    mockStation.status = { commission_id: 'c-1', step: 'touch_capture', next_dot_id: 3, job_id: 7, job_state: 'done' };
    render(<StationCommissionWizard onClose={() => {}} />);
    await flush();
    expect(screen.getByTestId('next-dot')).toHaveTextContent(COMMISSION_DE.touchNext(3));
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.touchCapture }).click(); });
    expect(calls('touch_capture')[0][1]).toEqual({ commission_id: 'c-1', dot_id: 3 });
  });

  it('keeps „Bitte den Arm loslassen …" until a new touch has begun', async () => {
    const RELEASE = 'Bitte den Arm loslassen – er wird gerade wieder verriegelt.';
    mockStation.status = { commission_id: 'c-1', step: 'touch_capture', next_dot_id: 2, job_id: 7, job_state: 'done' };
    mockStation.answers.touch_capture = { accepted: true, success: false, result: 'REFUSED', message: RELEASE };
    mockStation.answers.touch_solve = { accepted: true, success: false, result: 'REFUSED', message: 'Bitte warten, der Roboter ist noch beschäftigt.' };
    mockStation.answers.touch_begin = { accepted: true, success: true, result: 'QUEUED', job_id: 8 };
    render(<StationCommissionWizard onClose={() => {}} />);
    await flush();
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.touchCapture }).click(); });
    expect(screen.getByTestId('commission-sticky')).toHaveTextContent(RELEASE);
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.touchSolve }).click(); });
    expect(screen.getByTestId('commission-sticky')).toHaveTextContent(RELEASE);
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.touchBegin }).click(); });
    mockStation.status = { commission_id: 'c-1', step: 'touch_capture', next_dot_id: 1, job_id: 8, job_state: 'done', job_message: '' };
    await tick();
    expect(screen.queryByTestId('commission-sticky')).toBe(null);
  });

  it('touch_solve waits for its job, then offers the commit; the commit job ends the wizard', async () => {
    mockStation.status = { commission_id: 'c-1', step: 'touch_capture', next_dot_id: 0, job_id: 7, job_state: 'done' };
    mockStation.answers.touch_solve = { accepted: true, success: true, result: 'QUEUED', job_id: 11 };
    mockStation.answers.commit = { accepted: true, success: true, result: 'QUEUED', job_id: 12 };
    render(<StationCommissionWizard onClose={() => {}} />);
    await flush();
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.touchSolve }).click(); });
    expect(screen.queryByRole('button', { name: COMMISSION_DE.commit })).toBe(null);
    mockStation.status = {
      commission_id: 'c-1', step: 'touch_solve', job_id: 11, job_state: 'done', job_message: 'Roboter vermessen.',
      registration_rms_mm: 1.2, registration_shift_mm: 3.0, registration_rot_deg: 0.4,
    };
    await tick();
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.commit }).click(); });
    mockStation.status = { commission_id: '', job_id: 12, job_state: 'done', job_message: 'Gespeichert.' };
    await tick();
    expect(screen.getAllByText(COMMISSION_DE.committed).length).toBeGreaterThan(0);
  });

  it('the world check captures the mark and cube turn the server names next', async () => {
    mockStation.status = { commission_id: 'c-1', step: 'mark_capture', next_mark_id: 'M3', next_cube_turn: 1, job_id: 7, job_state: 'done' };
    render(<StationCommissionWizard onClose={() => {}} />);
    await flush();
    expect(screen.getByTestId('next-mark')).toHaveTextContent(COMMISSION_DE.markNext('M3', true));
    await act(async () => { screen.getByRole('button', { name: COMMISSION_DE.markCapture }).click(); });
    expect(calls('mark_capture')[0][1]).toEqual({ commission_id: 'c-1', mark_id: 'M3', cube_turn: 1 });
  });
});

describe('stepFromStatus', () => {
  it('maps the server step, else the results', () => {
    expect(stepFromStatus({ commission_id: '' })).toBe('intro');
    expect(stepFromStatus({ commission_id: 'c', step: 'lens_capture' })).toBe('lens');
    expect(stepFromStatus({ commission_id: 'c', step: 'board_solve' })).toBe('board');
    expect(stepFromStatus({ commission_id: 'c', step: 'mark_capture' })).toBe('marks');
    expect(stepFromStatus({ commission_id: 'c', step: 'touch_begin' })).toBe('touch');
    expect(stepFromStatus({ commission_id: 'c', step: 'commit' })).toBe('commit');
    expect(stepFromStatus({ commission_id: 'c', step: '', board_reproj_px: 0.4 })).toBe('marks');
  });
});
