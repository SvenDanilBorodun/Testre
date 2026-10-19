// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Meine Modelle" shows a failed training's error_message.
//
// * Every German sentence of the managed training's S-table is shown WHOLE
//   (S4 is the longest at 178 characters, S7 156): the card once cut every
//   message at 150 characters, mid-sentence. The Python lockstep test holds
//   ERROR_MESSAGE_MAX_CHARS above every S sentence.
// * A legacy worker's long blob (up to 2 KB of LeRobot output) is still cut.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import trainingReducer from '../../features/training/trainingSlice';
import MyModels, { ERROR_MESSAGE_MAX_CHARS, cardErrorText } from '../MyModels';

vi.mock('../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({ controlHfServer: vi.fn() }),
}));
vi.mock('../../services/cloudTrainingApi', () => ({ cancelCloudTraining: vi.fn() }));

// training_texts_de.py, verbatim.
const S4 = 'Das Training konnte auch nach 4 Versuchen auf GPU-Servern nicht abgeschlossen werden und wurde '
  + 'abgebrochen. Das Trainingsguthaben wurde erstattet – bitte starte das Training neu.';
const S7 = 'Das Modell hat die Sicherheitsprüfung nicht bestanden und wurde nicht veröffentlicht. '
  + 'Das Trainingsguthaben wurde erstattet – bitte starte das Training neu.';

function failedJob(errorMessage, id = 1) {
  return {
    id,
    status: 'failed',
    model_name: `EduBotics-Solutions/act-omx-${id}`,
    model_type: 'act',
    dataset_name: 'alice/omx_f_pick',
    current_step: 1000,
    total_steps: 50000,
    requested_at: '2026-10-18T10:00:00Z',
    terminated_at: '2026-10-18T12:00:00Z',
    error_message: errorMessage,
  };
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
  it('shows S4 and S7 whole', () => {
    expect(S4.length).toBe(178);
    expect(S7.length).toBe(156);
    renderModels([failedJob(S4, 1), failedJob(S7, 2)]);
    expect(screen.getByText(S4)).toBeInTheDocument();
    expect(screen.getByText(S7)).toBeInTheDocument();
  });

  it('still cuts a long legacy blob', () => {
    const blob = 'x'.repeat(2000);
    expect(cardErrorText(blob)).toBe(`${'x'.repeat(ERROR_MESSAGE_MAX_CHARS)}...`);
    expect(cardErrorText(S4)).toBe(S4);
    expect(ERROR_MESSAGE_MAX_CHARS).toBeGreaterThan(S4.length);
  });
});
