// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The live chart and the training hours (credit system v2, spec §10, review M2).
//
// * The failed run's error tail once said „Das Trainingsguthaben wurde
//   erstattet." under EVERY failure — false since the ledger charges started
//   hours. It now says what the run cost: „Berechnet: N Stunden." or „Es wurde
//   nichts berechnet."; a pre-052 row (charged_hours null) says nothing.
// * The lost time of the selected training stands under its chart [Q-B].

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import trainingReducer from '../../features/training/trainingSlice';
import TrainingLiveChart from '../TrainingLiveChart';

const S7 = 'Das Modell hat die Sicherheitsprüfung nicht bestanden und wurde nicht veröffentlicht. '
  + 'Die bisherige Trainingszeit wird berechnet.';

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

function failed(over = {}) {
  return job({ status: 'failed', error_message: S7, terminated_at: '2026-10-18T11:00:00Z', ...over });
}

function renderChart(j) {
  const store = configureStore({ reducer: { training: trainingReducer } });
  return render(
    <Provider store={store}>
      <TrainingLiveChart jobs={[j]} isRealtime nextReservationHours={3} />
    </Provider>,
  );
}

describe('TrainingLiveChart charge line', () => {
  test('a charged failure: „Berechnet: 1 Stunde." in the error tail', () => {
    renderChart(failed({ reserved_hours: 3, charged_hours: 1 }));
    expect(screen.getByText('Berechnet: 1 Stunde.')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/1 Stunden/);
  });

  test('a charged failure of three hours', () => {
    renderChart(failed({ reserved_hours: 3, charged_hours: 3 }));
    expect(screen.getByText('Berechnet: 3 Stunden.')).toBeInTheDocument();
  });

  test('a failure that cost nothing: „Es wurde nichts berechnet."', () => {
    renderChart(failed({ reserved_hours: 2, charged_hours: 0 }));
    expect(screen.getByText('Es wurde nichts berechnet.')).toBeInTheDocument();
  });

  test('a pre-052 failure says nothing about hours and never claims a refund', () => {
    renderChart(failed({ charged_hours: null }));
    expect(screen.queryByText(/^Berechnet: /)).toBeNull();
    expect(screen.queryByText('Es wurde nichts berechnet.')).toBeNull();
    expect(document.body.textContent).not.toMatch(/erstattet|Trainingsguthaben/);
  });
});

describe('TrainingLiveChart lost time [Q-B]', () => {
  test('a running training: „Verlorene Zeit bisher: … zählt zum Zeitlimit"', () => {
    renderChart(job({ reserved_hours: 3, lost_seconds: 1200 }));
    expect(screen.getByTestId('lost-time')).toHaveTextContent(
      'Verlorene Zeit bisher: 20 Min. – zählt zum Zeitlimit, wird nicht berechnet.',
    );
  });

  test('a finished training: „Verlorene Zeit: … (nicht berechnet)"', () => {
    renderChart(job({ status: 'succeeded', current_step: 50000, charged_hours: 2, lost_seconds: 2100,
      terminated_at: '2026-10-18T13:00:00Z' }));
    expect(screen.getByTestId('lost-time')).toHaveTextContent('Verlorene Zeit: 35 Min. (nicht berechnet)');
  });

  test('no line under one minute, nor for a pre-052 row', () => {
    const { unmount } = renderChart(job({ lost_seconds: 30 }));
    expect(screen.queryByTestId('lost-time')).toBeNull();
    unmount();
    renderChart(job({}));
    expect(screen.queryByTestId('lost-time')).toBeNull();
  });
});
