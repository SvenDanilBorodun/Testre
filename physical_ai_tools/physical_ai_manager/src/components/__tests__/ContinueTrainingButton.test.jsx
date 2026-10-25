// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Fortsetzen" per kind (owner E32–E37, E46, review M2/m2).

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { describe, expect, test, vi, beforeEach } from 'vitest';
import ContinueTrainingButton from '../ContinueTrainingButton';
import { CONTINUE_DE } from '../../features/training/continuation';
import * as api from '../../services/cloudTrainingApi';
import toast from 'react-hot-toast';

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../../services/cloudTrainingApi', () => ({ continueCloudTraining: vi.fn() }));

// The action creators the button dispatches (the real slice is not imported here).
vi.mock('../../features/training/trainingSlice', () => ({
  triggerCloudJobsRefresh: () => ({ type: 'training/triggerCloudJobsRefresh' }),
  setSelectedTrainingId: (id) => ({ type: 'training/setSelectedTrainingId', payload: id }),
}));

function store() {
  const dispatched = [];
  const s = configureStore({
    reducer: {
      auth: () => ({ session: { access_token: 'tok' } }),
      training: (st = {}) => st,
    },
    middleware: (gdm) => gdm().concat(() => (next) => (action) => { dispatched.push(action); return next(action); }),
  });
  s.dispatched = dispatched;
  return s;
}

function renderBtn(job, { jobs = [job], remaining = 5, onContinued } = {}) {
  const s = store();
  const utils = render(
    <Provider store={s}>
      <ContinueTrainingButton job={job} jobs={jobs} remaining={remaining} onContinued={onContinued} />
    </Provider>,
  );
  return { ...utils, store: s };
}

const capped = (over = {}) => ({
  id: 1, status: 'succeeded', capped_step: 42500, total_steps: 50000,
  continuable_until: new Date(Date.now() + 7 * 86400 * 1000).toISOString(),
  is_mine: true, status_note: 'S23-note', ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ContinueTrainingButton', () => {
  test('nothing for a plain succeeded job', () => {
    const { container } = renderBtn({ id: 1, status: 'succeeded', total_steps: 50000, is_mine: true });
    expect(container).toBeEmptyDOMElement();
  });

  test('a continuable job: its note and an enabled key', () => {
    renderBtn(capped());
    expect(screen.getByRole('status')).toHaveTextContent('S23-note');
    expect(screen.getByRole('button', { name: new RegExp(CONTINUE_DE.button) })).toBeEnabled();
  });

  test('a groupmate capped row: the not_mine line, no key (review M2)', () => {
    renderBtn(capped({ is_mine: false }));
    expect(screen.getByRole('status')).toHaveTextContent(CONTINUE_DE.notMine(42500, 50000));
    expect(screen.queryByRole('button')).toBeNull();
  });

  test('no_credit: a disabled key and the label', () => {
    renderBtn(capped(), { remaining: 0 });
    const btn = screen.getByRole('button');
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('title', CONTINUE_DE.noCredit);
    expect(btn).toHaveTextContent(CONTINUE_DE.noCredit);
  });

  test.each(['expired', 'continuing', 'continued'])('%s: the state line, no key, not S23', (kind) => {
    const job = kind === 'expired' ? capped({ continuable_until: '2000-01-01T00:00:00Z' }) : capped();
    const jobs = kind === 'continuing' ? [job, { id: 2, continued_from: 1, status: 'running' }]
      : kind === 'continued' ? [job, { id: 2, continued_from: 1, status: 'succeeded' }] : [job];
    renderBtn(job, { jobs });
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByRole('status')).not.toHaveTextContent('S23-note');
  });

  test('a click continues: one call, started toast, refresh + select, onContinued, disabled while in flight', async () => {
    let resolve;
    api.continueCloudTraining.mockReturnValue(new Promise((r) => { resolve = r; }));
    const onContinued = vi.fn();
    const { store: s } = renderBtn(capped(), { onContinued });
    const btn = screen.getByRole('button', { name: new RegExp(CONTINUE_DE.button) });
    fireEvent.click(btn);
    expect(btn).toBeDisabled();
    resolve({ training_id: 99 });
    await waitFor(() => expect(onContinued).toHaveBeenCalled());
    expect(api.continueCloudTraining).toHaveBeenCalledTimes(1);
    expect(api.continueCloudTraining).toHaveBeenCalledWith('tok', 1);
    expect(toast.success).toHaveBeenCalledWith(CONTINUE_DE.started);
    const types = s.dispatched.map((a) => a.type);
    expect(types).toContain('training/triggerCloudJobsRefresh');
    expect(s.dispatched).toContainEqual({ type: 'training/setSelectedTrainingId', payload: 99 });
  });

  test('a 409 toasts its detail and refreshes', async () => {
    const err = new Error('x'); err.status = 409; err.detail = CONTINUE_DE.continued;
    api.continueCloudTraining.mockRejectedValue(err);
    const { store: s } = renderBtn(capped());
    fireEvent.click(screen.getByRole('button', { name: new RegExp(CONTINUE_DE.button) }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(CONTINUE_DE.continued));
    expect(s.dispatched.map((a) => a.type)).toContain('training/triggerCloudJobsRefresh');
  });
});
