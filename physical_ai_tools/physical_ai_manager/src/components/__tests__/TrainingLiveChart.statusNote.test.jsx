// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The live chart of a managed training (RunPod spec §4.11).
//
// * `status_note` is the API's German line for a training that moves between
//   GPU servers („GPU-Server ausgefallen – das Training wird ab Schritt 2.500
//   auf einem anderen Server fortgesetzt.") or is being published. It is shown
//   under the hero while the job is active, and only then.
// * A managed row knows its `total_steps` from the start, so „starting" is no
//   longer only `total_steps <= 0`: a `queued` row (no attempt has written
//   `running` yet, or the training was requeued after a loss) shows the
//   indeterminate „GPU-Worker startet…" state, never a 0 % progress figure.
// * The monitor's subtitle names no provider: a training runs on RunPod or on
//   Modal, so it says „Cloud".

// WHERE the note stands (inside the hero, after its stats) and which hero
// figure is drawn are the point of these tests, so they walk the DOM.
/* eslint-disable testing-library/no-node-access, testing-library/no-container */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import trainingReducer from '../../features/training/trainingSlice';
import TrainingLiveChart from '../TrainingLiveChart';
import { statusSubtitle } from '../../pages/TrainingPage';

// The page module is imported for its subtitle helper only; keep its cloud
// client from touching the environment (same stub as TrainingPage.gate.test).
vi.mock('../../lib/supabaseClient', () => ({
  __esModule: true,
  supabase: { auth: { signOut: vi.fn() } },
}));

const S1 = 'GPU-Server ausgefallen – das Training wird ab Schritt 2.500 auf einem anderen Server fortgesetzt.';

function job(over = {}) {
  return {
    id: 7,
    status: 'running',
    model_name: 'EduBotics-Solutions/act-omx-pick',
    model_type: 'act',
    dataset_name: 'alice/omx_f_pick',
    current_step: 2500,
    total_steps: 50000,
    current_loss: 0.42,
    loss_history: [],
    requested_at: '2026-10-18T10:00:00Z',
    last_progress_at: '2026-10-18T10:20:00Z',
    terminated_at: null,
    error_message: null,
    status_note: null,
    ...over,
  };
}

function renderChart(j) {
  const store = configureStore({ reducer: { training: trainingReducer } });
  return render(
    <Provider store={store}>
      <TrainingLiveChart jobs={[j]} isRealtime />
    </Provider>,
  );
}

describe('TrainingLiveChart status note', () => {
  test.each(['running', 'queued'])('a %s job shows its note as a status line under the hero', (status) => {
    const { container } = renderChart(job({ status, status_note: S1 }));
    const note = screen.getByRole('status');
    expect(note).toHaveTextContent(S1);
    expect(note.tagName).toBe('P');
    expect(note).toHaveClass('su-trn-note');
    // Inside the hero block, after its stats.
    const hero = container.querySelector('.su-trn-hero');
    expect(hero).toContainElement(note);
    expect(hero.lastElementChild).toBe(note);
  });

  test.each([null, undefined, '', '   '])('no note line for an active job with note %p', (note) => {
    renderChart(job({ status_note: note }));
    expect(screen.queryByRole('status')).toBeNull();
    expect(document.querySelector('.su-trn-note')).toBeNull();
  });

  test.each(['succeeded', 'failed', 'canceled'])('a %s job never shows its last note', (status) => {
    renderChart(job({ status, status_note: S1, terminated_at: '2026-10-18T11:00:00Z' }));
    expect(screen.queryByText(S1)).toBeNull();
    expect(document.querySelector('.su-trn-note')).toBeNull();
  });
});

describe('TrainingLiveChart starting state', () => {
  test('a queued job with known steps is starting, not at 5 %', () => {
    renderChart(job({ status: 'queued', current_step: 2500, total_steps: 50000 }));
    expect(screen.getByText('GPU-Worker startet…')).toBeInTheDocument();
    expect(document.querySelector('.su-trn-pct')).toBeNull();
  });

  test('a queued job without steps is starting too (the legacy path)', () => {
    renderChart(job({ status: 'queued', current_step: 0, total_steps: 0 }));
    expect(screen.getByText('GPU-Worker startet…')).toBeInTheDocument();
  });

  test('a running job with steps shows its progress', () => {
    renderChart(job({ status: 'running', current_step: 2500, total_steps: 50000 }));
    expect(screen.queryByText('GPU-Worker startet…')).toBeNull();
    expect(document.querySelector('.su-trn-pct')).toHaveTextContent('5%');
  });

  test('a running job that has not reported its steps yet is starting', () => {
    renderChart(job({ status: 'running', current_step: 0, total_steps: 0 }));
    expect(screen.getByText('GPU-Worker startet…')).toBeInTheDocument();
  });
});

describe('Training monitor subtitle', () => {
  test.each([
    ['queued', 'Wird eingereiht · Cloud'],
    ['running', 'Live · aktualisiert laufend · Cloud'],
    ['succeeded', 'Erfolgreich · Cloud'],
    ['failed', 'Fehlgeschlagen · Cloud'],
    ['canceled', 'Abgebrochen · Cloud'],
    [undefined, 'Bereit · Cloud'],
  ])('%s → %s', (status, text) => {
    expect(statusSubtitle(status)).toBe(text);
    expect(statusSubtitle(status)).not.toMatch(/Modal|RunPod/);
  });
});
