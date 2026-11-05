// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Meine Modelle" shows a failed training's error_message.
//
// * Every German sentence of the managed training's S-table is shown WHOLE
//   (S4 is the longest at 234 characters, S18 203): the card once cut every
//   message at 150 characters, mid-sentence. The Python lockstep test holds
//   ERROR_MESSAGE_MAX_CHARS above every S sentence at its widest.
// * A legacy worker's long blob (up to 2 KB of LeRobot output) is still cut.
// * Credit system v2 (review M2): the error box never claims a refund. A row
//   the hour ledger settled says what it cost („Berechnet: 1 Stunde." /
//   „Es wurde nichts berechnet."); a pre-052 row (charged_hours null) says
//   nothing about it.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import trainingReducer from '../../features/training/trainingSlice';
import MyModels, { ERROR_MESSAGE_MAX_CHARS, cardErrorText } from '../MyModels';

vi.mock('../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({ controlHfServer: vi.fn() }),
}));
vi.mock('../../services/cloudTrainingApi', () => ({ cancelCloudTraining: vi.fn(), getStopPreview: vi.fn() }));

// training_texts_de.py after 052 (spec §8.1), verbatim.
const S4 = 'Das Training ist auf 4 GPU-Servern immer wieder abgebrochen, bevor ein Zwischenstand gespeichert '
  + 'war. Wahrscheinlich liegt es am Datensatz – prüfe ihn im Tab Daten, bevor du neu startest. Berechnet '
  + 'wird nur gespeicherte Trainingszeit.';
const S7 = 'Das Modell hat die Sicherheitsprüfung nicht bestanden und wurde nicht veröffentlicht. '
  + 'Die bisherige Trainingszeit wird berechnet.';
const S18 = 'Zurzeit ist keine Cloud-GPU verfügbar und es gibt noch keinen Zwischenstand – das Training wurde '
  + 'beendet. Berechnet wird nur gespeicherte Trainingszeit, die übrigen reservierten Stunden sind wieder frei.';

function job(over = {}) {
  return {
    id: 1,
    status: 'failed',
    model_name: 'EduBotics-Solutions/act-omx-1',
    model_type: 'act',
    dataset_name: 'alice/omx_f_pick',
    current_step: 1000,
    total_steps: 50000,
    requested_at: '2026-10-18T10:00:00Z',
    terminated_at: '2026-10-18T12:00:00Z',
    ...over,
  };
}

function failedJob(errorMessage, id = 1, over = {}) {
  return job({ id, model_name: `EduBotics-Solutions/act-omx-${id}`, error_message: errorMessage, ...over });
}

function renderModels(jobs) {
  const store = configureStore({
    reducer: {
      training: trainingReducer,
      auth: (state = { session: { access_token: 't' } }) => state,
      tasks: (state = { heartbeatStatus: 'disconnected' }) => state,
    },
  });
  return render(
    <Provider store={store}>
      <MyModels jobs={jobs} />
    </Provider>,
  );
}

describe('MyModels error message', () => {
  it('shows S4, S7 and S18 whole', () => {
    expect(S4.length).toBe(234);
    expect(S7.length).toBe(129);
    expect(S18.length).toBe(203);
    renderModels([failedJob(S4, 1), failedJob(S7, 2), failedJob(S18, 3)]);
    expect(screen.getByText(S4)).toBeInTheDocument();
    expect(screen.getByText(S7)).toBeInTheDocument();
    expect(screen.getByText(S18)).toBeInTheDocument();
  });

  it('still cuts a long legacy blob', () => {
    const blob = 'x'.repeat(2000);
    expect(cardErrorText(blob)).toBe(`${'x'.repeat(ERROR_MESSAGE_MAX_CHARS)}...`);
    expect(cardErrorText(S4)).toBe(S4);
    expect(ERROR_MESSAGE_MAX_CHARS).toBeGreaterThan(S4.length);
  });

  it('a charged failure says what it cost, with „1 Stunde" for one', () => {
    renderModels([failedJob(S7, 1, { charged_hours: 1 }), failedJob(S8(), 2, { charged_hours: 2 })]);
    expect(screen.getByText('Berechnet: 1 Stunde.')).toBeInTheDocument();
    expect(screen.getByText('Berechnet: 2 Stunden.')).toBeInTheDocument();
    // The error box carries the charge; the card does not repeat it.
    expect(screen.queryByText('Berechnet: 1 Stunde')).toBeNull();
  });

  it('a failure that cost nothing says so', () => {
    renderModels([failedJob(S18, 1, { charged_hours: 0 })]);
    expect(screen.getByText('Es wurde nichts berechnet.')).toBeInTheDocument();
  });

  it('a pre-052 row (charged_hours null) says nothing about hours, and nothing claims a refund', () => {
    renderModels([failedJob(S4, 1, { charged_hours: null })]);
    expect(screen.queryByText(/^Berechnet: /)).toBeNull();
    expect(screen.queryByText('Es wurde nichts berechnet.')).toBeNull();
    expect(document.body.textContent).not.toMatch(/erstattet|Trainingsguthaben|Credit/);
  });
});

function S8() {
  return 'Das Modell konnte nicht auf Hugging Face veröffentlicht werden. Die Trainingszeit wird berechnet – '
    + 'bitte wende dich an deine Lehrkraft.';
}

describe('MyModels hour lines (spec §10.2)', () => {
  it('an active card shows its reservation', () => {
    renderModels([job({ status: 'running', reserved_hours: 3, current_step: 2000 })]);
    expect(screen.getByText('Reserviert: 3 Stunden')).toBeInTheDocument();
  });

  it('one reserved hour reads „Reserviert: 1 Stunde"', () => {
    renderModels([job({ status: 'queued', reserved_hours: 1, total_steps: 0 })]);
    expect(screen.getByText('Reserviert: 1 Stunde')).toBeInTheDocument();
  });

  it('a canceled card shows its charge, a teacher stop says who stopped it', () => {
    renderModels([job({ status: 'canceled', reserved_hours: 3, charged_hours: 1, stop_requested_by_role: 'teacher' })]);
    expect(screen.getByText('Berechnet: 1 Stunde')).toBeInTheDocument();
    expect(screen.getByText('Von deiner Lehrkraft gestoppt')).toBeInTheDocument();
  });

  it('a student stop is not labelled as the teacher\'s', () => {
    renderModels([job({ status: 'succeeded', charged_hours: 2, stop_requested_by_role: 'student' })]);
    expect(screen.getByText('Berechnet: 2 Stunden')).toBeInTheDocument();
    expect(screen.queryByText('Von deiner Lehrkraft gestoppt')).toBeNull();
  });

  it('lost time while active: „bisher … zählt zum Zeitlimit"', () => {
    renderModels([job({ status: 'running', reserved_hours: 2, lost_seconds: 1260 })]);
    expect(screen.getByText('Verlorene Zeit bisher: 21 Min. – zählt zum Zeitlimit, wird nicht berechnet.'))
      .toBeInTheDocument();
  });

  it('lost time once finished: „(nicht berechnet)"', () => {
    renderModels([job({ status: 'succeeded', charged_hours: 1, lost_seconds: 600 })]);
    expect(screen.getByText('Verlorene Zeit: 10 Min. (nicht berechnet)')).toBeInTheDocument();
  });

  it('less than a minute of lost time is not shown', () => {
    renderModels([job({ status: 'succeeded', charged_hours: 1, lost_seconds: 59 })]);
    expect(screen.queryByText(/Verlorene Zeit/)).toBeNull();
  });
});
