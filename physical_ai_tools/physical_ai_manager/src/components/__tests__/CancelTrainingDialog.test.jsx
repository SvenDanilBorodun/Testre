// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Training abbrechen?" (credit system v2, spec §10.2): the dialog asks the API
// what a stop would do now and says it in one of four sentences, verbatim; the
// result toast follows the cancel answer's `outcome`.

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import toast from 'react-hot-toast';
import CancelTrainingDialog, { cancelResultToast, previewSentence } from '../CancelTrainingDialog';
import MyModels from '../MyModels';
import trainingReducer from '../../features/training/trainingSlice';
import * as api from '../../services/cloudTrainingApi';

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../../services/cloudTrainingApi', () => ({
  getStopPreview: vi.fn(),
  cancelCloudTraining: vi.fn(),
  continueCloudTraining: vi.fn(),
}));
vi.mock('../../hooks/useRosServiceCaller', () => ({
  useRosServiceCaller: () => ({ controlHfServer: vi.fn() }),
}));

const NOT_TRAINED = 'Das Training hat noch nicht begonnen. Es wird nichts berechnet – reserviert waren 3 Stunden, alles geht zurück an dein Guthaben.';
const NOT_TRAINED_1 = 'Das Training hat noch nicht begonnen. Es wird nichts berechnet – reserviert waren 1 Stunde, alles geht zurück an dein Guthaben.';
const PUBLISH = 'Der GPU-Server wird gestoppt und der letzte Zwischenstand (Schritt 7.500) als Modell veröffentlicht. Berechnet: 1 Stunde. Zurück an dein Guthaben: 2 Stunden.';
const NO_CKPT = 'Der GPU-Server wird gestoppt. Es gibt noch keinen Zwischenstand (der nächste wird bei Schritt 12.500 gespeichert), also wird kein Modell veröffentlicht. Berechnet: 2 Stunden. Zurück an dein Guthaben: 1 Stunde.';
const FAILED = 'Der Stand konnte nicht abgefragt werden. Wenn das Training schon auf einer GPU läuft, wird die angefangene Trainingszeit berechnet.';

const preview = (over = {}) => ({
  training_id: 5, trained: true, checkpoint_step: null, next_checkpoint_step: 2500, final_ready: false,
  reserved_hours: 3, charged_hours_now: 1, hours_back_now: 2, outcome: 'cancel', ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('previewSentence (the four previews)', () => {
  test('not trained', () => {
    expect(previewSentence(preview({ trained: false, charged_hours_now: 0, hours_back_now: 3 }))).toBe(NOT_TRAINED);
    expect(previewSentence(preview({ trained: false, reserved_hours: 1 }))).toBe(NOT_TRAINED_1);
  });

  test('publish: the checkpoint step with the thousands dot', () => {
    expect(previewSentence(preview({ outcome: 'publish', checkpoint_step: 7500 }))).toBe(PUBLISH);
  });

  test('trained without checkpoint: names next_checkpoint_step (also for a resumed training)', () => {
    expect(previewSentence(preview({ next_checkpoint_step: 12500, charged_hours_now: 2, hours_back_now: 1 })))
      .toBe(NO_CKPT);
  });

  test('a failed preview, or an answer that is not one', () => {
    expect(previewSentence(null)).toBe(FAILED);
    expect(previewSentence({ detail: 'x' })).toBe(FAILED);
  });
});

describe('CancelTrainingDialog', () => {
  function openDialog(over = {}) {
    const props = { token: 'jwt', trainingId: 5, onConfirm: vi.fn(), onClose: vi.fn(), ...over };
    render(<CancelTrainingDialog {...props} />);
    return props;
  }

  test.each([
    ['not trained', preview({ trained: false }), NOT_TRAINED],
    ['publish', preview({ outcome: 'publish', checkpoint_step: 7500 }), PUBLISH],
    ['no checkpoint', preview({ next_checkpoint_step: 12500, charged_hours_now: 2, hours_back_now: 1 }), NO_CKPT],
  ])('renders the %s preview from GET /trainings/{id}/stop-preview', async (_name, p, text) => {
    api.getStopPreview.mockResolvedValue(p);
    openDialog();
    expect(screen.getByRole('dialog', { name: 'Training abbrechen?' })).toBeInTheDocument();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(api.getStopPreview).toHaveBeenCalledWith('jwt', 5);
  });

  test('renders the failure sentence when the preview fails, and still lets the student cancel', async () => {
    api.getStopPreview.mockRejectedValue(new Error('500'));
    const props = openDialog();
    expect(await screen.findByText(FAILED)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Ja, abbrechen' }));
    expect(props.onConfirm).toHaveBeenCalledTimes(1);
  });

  test('„Ja, abbrechen" waits for the preview; „Weiter trainieren" closes and keeps the focus default', async () => {
    let resolve;
    api.getStopPreview.mockReturnValue(new Promise((r) => { resolve = r; }));
    const props = openDialog();
    expect(screen.getByRole('button', { name: 'Ja, abbrechen' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Weiter trainieren' })).toHaveFocus();
    resolve(preview());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Ja, abbrechen' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Weiter trainieren' }));
    expect(props.onClose).toHaveBeenCalledTimes(1);
    expect(props.onConfirm).not.toHaveBeenCalled();
  });
});

describe('cancelResultToast', () => {
  test('publishing, canceled, cancel_requested', () => {
    expect(cancelResultToast({ outcome: 'publishing', checkpoint_step: 7500 }))
      .toBe('Training gestoppt – der Zwischenstand bei Schritt 7.500 wird veröffentlicht.');
    expect(cancelResultToast({ outcome: 'canceled', charged_hours: 1 })).toBe('Training abgebrochen. Berechnet: 1 Stunde.');
    expect(cancelResultToast({ outcome: 'canceled', charged_hours: 0 })).toBe('Training abgebrochen. Berechnet: 0 Stunden.');
    expect(cancelResultToast({ outcome: 'canceled', charged_hours: null })).toBe('Training abgebrochen. Berechnet: 0 Stunden.');
    expect(cancelResultToast({ outcome: 'cancel_requested' })).toBe('Der Abbruch wird an den GPU-Server übermittelt …');
  });
});

describe('MyModels „Abbrechen"', () => {
  const running = {
    id: 5, status: 'running', model_name: 'EduBotics-Solutions/act-1', model_type: 'act',
    dataset_name: 'alice/pick', current_step: 9000, total_steps: 50000,
    requested_at: '2026-10-18T10:00:00Z', reserved_hours: 3,
  };

  function renderModels(onHoursChanged = vi.fn()) {
    const store = configureStore({
      reducer: {
        training: trainingReducer,
        auth: (s = { session: { access_token: 'jwt' } }) => s,
        tasks: (s = { heartbeatStatus: 'disconnected' }) => s,
      },
    });
    const refetch = vi.fn();
    render(
      <Provider store={store}>
        <MyModels jobs={[running]} refetch={refetch} onHoursChanged={onHoursChanged} />
      </Provider>,
    );
    return { refetch, onHoursChanged };
  }

  test('opens the dialog with the preview first; nothing is canceled before „Ja, abbrechen"', async () => {
    api.getStopPreview.mockResolvedValue(preview({ outcome: 'publish', checkpoint_step: 7500 }));
    renderModels();
    fireEvent.click(screen.getByRole('button', { name: /Abbrechen/ }));
    expect(await screen.findByText(PUBLISH)).toBeInTheDocument();
    expect(api.cancelCloudTraining).not.toHaveBeenCalled();
  });

  test('„Ja, abbrechen" cancels, toasts by outcome, refreshes the list and the hours', async () => {
    api.getStopPreview.mockResolvedValue(preview({ outcome: 'publish', checkpoint_step: 7500 }));
    api.cancelCloudTraining.mockResolvedValue({ status: 'running', training_id: 5, outcome: 'publishing', checkpoint_step: 7500, charged_hours: null });
    const { refetch, onHoursChanged } = renderModels();
    fireEvent.click(screen.getByRole('button', { name: /Abbrechen/ }));
    await screen.findByText(PUBLISH);
    fireEvent.click(screen.getByRole('button', { name: 'Ja, abbrechen' }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith(
      'Training gestoppt – der Zwischenstand bei Schritt 7.500 wird veröffentlicht.',
    ));
    expect(api.cancelCloudTraining).toHaveBeenCalledWith('jwt', 5);
    expect(refetch).toHaveBeenCalled();
    expect(onHoursChanged).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  test('a canceled answer names what was charged; an error keeps the old toast', async () => {
    api.getStopPreview.mockResolvedValue(preview());
    api.cancelCloudTraining.mockResolvedValueOnce({ outcome: 'canceled', charged_hours: 1 });
    renderModels();
    fireEvent.click(screen.getByRole('button', { name: /Abbrechen/ }));
    await screen.findByText(/Der GPU-Server wird gestoppt/);
    fireEvent.click(screen.getByRole('button', { name: 'Ja, abbrechen' }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Training abgebrochen. Berechnet: 1 Stunde.'));

    const err = new Error('Training ist nicht aktiv.');
    api.cancelCloudTraining.mockRejectedValueOnce(err);
    fireEvent.click(screen.getByRole('button', { name: /Abbrechen/ }));
    await screen.findByText(/Der GPU-Server wird gestoppt/);
    fireEvent.click(screen.getByRole('button', { name: 'Ja, abbrechen' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Abbrechen fehlgeschlagen: Training ist nicht aktiv.'));
  });
});
