// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Meine Modelle": a capped card offers „Fortsetzen"; a child card carries a
// „Fortsetzung ab Schritt …" chip; a fully finished card offers neither.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { describe, expect, test, vi } from 'vitest';
import trainingReducer from '../../features/training/trainingSlice';
import MyModels from '../MyModels';
import { CONTINUE_DE } from '../../features/training/continuation';

vi.mock('../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({ controlHfServer: vi.fn() }),
}));
vi.mock('../../services/cloudTrainingApi', () => ({ cancelCloudTraining: vi.fn(), continueCloudTraining: vi.fn() }));

function base(over = {}) {
  return {
    id: 1,
    status: 'succeeded',
    model_name: 'EduBotics-Solutions/act-omx-pick',
    model_type: 'act',
    dataset_name: 'alice/omx_f_pick',
    current_step: 50000,
    total_steps: 50000,
    requested_at: '2026-10-18T10:00:00Z',
    terminated_at: '2026-10-18T12:00:00Z',
    is_mine: true,
    ...over,
  };
}

function renderModels(jobs, remaining = 5) {
  const store = configureStore({
    reducer: {
      training: trainingReducer,
      auth: (s = { session: { access_token: 't' } }) => s,
      tasks: (s = { heartbeatStatus: 'disconnected' }) => s,
    },
  });
  return render(
    <Provider store={store}>
      <MyModels jobs={jobs} remaining={remaining} onContinued={vi.fn()} />
    </Provider>,
  );
}

describe('MyModels „Fortsetzen"', () => {
  test('a capped card shows the note and the key', () => {
    renderModels([base({
      capped_step: 42500,
      total_steps: 50000,
      continuable_until: new Date(Date.now() + 7 * 86400 * 1000).toISOString(),
      status_note: 'S23-note',
    })]);
    expect(screen.getByText('S23-note')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: new RegExp(CONTINUE_DE.button) })).toBeEnabled();
  });

  test('a fully finished card shows neither note nor key', () => {
    renderModels([base()]);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('button', { name: new RegExp(CONTINUE_DE.button) })).toBeNull();
  });

  test('a child card carries the „Fortsetzung ab Schritt 42.500" chip', () => {
    const parent = base({ id: 1, capped_step: 42500, total_steps: 50000 });
    const child = base({ id: 2, continued_from: 1, status: 'succeeded' });
    renderModels([parent, child]);
    expect(screen.getByText(CONTINUE_DE.continuedFrom(42500))).toBeInTheDocument();
  });

  test('a child whose parent is not in the list shows the generic chip', () => {
    renderModels([base({ id: 2, continued_from: 999, status: 'succeeded' })]);
    expect(screen.getByText(CONTINUE_DE.continuationChip)).toBeInTheDocument();
  });

  test('remaining 0 disables the capped card key', () => {
    renderModels([base({
      capped_step: 42500,
      total_steps: 50000,
      continuable_until: new Date(Date.now() + 7 * 86400 * 1000).toISOString(),
    })], 0);
    expect(screen.getByRole('button', { name: new RegExp(CONTINUE_DE.noCredit) })).toBeDisabled();
  });
});
