// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Training starten?" (credit system v2, spec §10.1) and the panel that opens
// it: every sentence verbatim, and ONE idempotency_key per confirmed dialog —
// a retry of the same send reuses it, a new dialog mints a new one.

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import toast from 'react-hot-toast';
import StartTrainingDialog, { noHoursKind } from '../StartTrainingDialog';
import TrainingControlPanel from '../TrainingControlPanel';
import trainingReducer from '../../features/training/trainingSlice';
import authReducer, { setQuota, setSession } from '../../features/auth/authSlice';
import * as api from '../../services/cloudTrainingApi';

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../../services/cloudTrainingApi', async (importOriginal) => {
  const real = await importOriginal();
  return {
    newRequestId: real.newRequestId,
    startCloudTraining: vi.fn(),
    getQuota: vi.fn(() => Promise.resolve({})),
  };
});

const RESERVED_1 = 'Reserviert für dieses Training: 1 Stunde. Berechnet wird jede angefangene Stunde ab dem Start auf dem GPU-Server (mit dem Laden des Datensatzes) – die Wartezeit auf einen GPU-Server zählt nicht. Was übrig bleibt, geht nach dem Training automatisch zurück an dein Guthaben.';
const RESERVED_3 = 'Reserviert für dieses Training: 3 Stunden. Berechnet wird jede angefangene Stunde ab dem Start auf dem GPU-Server (mit dem Laden des Datensatzes) – die Wartezeit auf einen GPU-Server zählt nicht. Was übrig bleibt, geht nach dem Training automatisch zurück an dein Guthaben.';
const ONLY_LEFT_1 = 'Du hast nur noch 1 Stunde: Das Training stoppt nach 1 Stunde Trainingszeit, und der letzte Zwischenstand wird veröffentlicht.';
const ONLY_LEFT_2 = 'Du hast nur noch 2 Stunden: Das Training stoppt nach 2 Stunden Trainingszeit, und der letzte Zwischenstand wird veröffentlicht.';
const FROM_GROUP = 'Die Stunden kommen aus dem Guthaben deiner Arbeitsgruppe „Team Rot“.';

describe('StartTrainingDialog', () => {
  test('three hours, the maximum: title, the reservation sentence, the two keys', () => {
    render(<StartTrainingDialog hours={3} maxHours={3} workgroupName={null} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    const dialog = screen.getByRole('dialog', { name: 'Training starten?' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByText(RESERVED_3)).toBeInTheDocument();
    expect(screen.queryByText(/Du hast nur noch/)).toBeNull();
    expect(screen.queryByText(/Arbeitsgruppe/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Training starten' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Zurück' })).toBeInTheDocument();
  });

  test('fewer hours than the maximum add the „Du hast nur noch" paragraph, with „1 Stunde"', () => {
    render(<StartTrainingDialog hours={1} maxHours={3} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByText(RESERVED_1)).toBeInTheDocument();
    expect(screen.getByText(ONLY_LEFT_1)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/1 Stunden/);
  });

  test('two of three hours', () => {
    render(<StartTrainingDialog hours={2} maxHours={3} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByText(ONLY_LEFT_2)).toBeInTheDocument();
  });

  test('in a group: the third paragraph names the group', () => {
    render(<StartTrainingDialog hours={3} maxHours={3} workgroupName="Team Rot" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByText(FROM_GROUP)).toBeInTheDocument();
  });

  test('the primary key takes the focus; Esc and „Zurück" cancel; busy locks both', () => {
    const onCancel = vi.fn();
    const { rerender } = render(<StartTrainingDialog hours={3} maxHours={3} onConfirm={vi.fn()} onCancel={onCancel} />);
    expect(screen.getByRole('button', { name: 'Training starten' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Zurück' }));
    expect(onCancel).toHaveBeenCalledTimes(2);
    rerender(<StartTrainingDialog hours={3} maxHours={3} busy onConfirm={vi.fn()} onCancel={onCancel} />);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Training starten' })).toBeDisabled();
  });
});

describe('noHoursKind', () => {
  test('unknown is not „no hours"; 0 is; the member limit only while the group has hours', () => {
    expect(noHoursKind({ nextReservationHours: null })).toBeNull();
    expect(noHoursKind({ nextReservationHours: 2 })).toBeNull();
    expect(noHoursKind({ nextReservationHours: 0, hoursFree: 0 })).toBe('no_hours');
    expect(noHoursKind({ nextReservationHours: 0, hoursFree: 5, memberLimitHours: 3, memberLeftHours: 0 }))
      .toBe('member_limit');
    expect(noHoursKind({ nextReservationHours: 0, hoursFree: 0, memberLimitHours: 3, memberLeftHours: 0 }))
      .toBe('no_hours');
  });
});

// ── the panel ──────────────────────────────────────────────────────────────

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function renderPanel(quota, info = { datasetRepoId: 'alice/pick', policyType: 'act', outputFolderName: 'act-1' }) {
  const store = configureStore({
    reducer: { training: trainingReducer, auth: authReducer },
    preloadedState: {
      training: { ...trainingReducer(undefined, { type: '@@init' }),
        trainingInfo: { ...trainingReducer(undefined, { type: '@@init' }).trainingInfo, ...info } },
    },
  });
  store.dispatch(setSession({ access_token: 'jwt', user: { id: 'u1' } }));
  store.dispatch(setQuota(quota));
  render(<Provider store={store}><TrainingControlPanel /></Provider>);
  return store;
}

const QUOTA = {
  account: 'student', hours_free: 5, hours_reserved: 0, hours_spent: 0,
  next_reservation_hours: 3, max_hours_per_training: 3,
  member_limit_hours: null, member_left_hours: null,
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('TrainingControlPanel start', () => {
  test('„Training starten" opens the dialog first and sends nothing', () => {
    renderPanel(QUOTA);
    fireEvent.click(screen.getByRole('button', { name: 'Training starten' }));
    expect(screen.getByRole('dialog', { name: 'Training starten?' })).toBeInTheDocument();
    expect(api.startCloudTraining).not.toHaveBeenCalled();
  });

  test('confirming sends ONE fresh idempotency_key and toasts the reservation', async () => {
    api.startCloudTraining.mockResolvedValue({ training_id: 9, model_name: 'm', status: 'queued', reserved_hours: 3, replay: false });
    renderPanel(QUOTA);
    fireEvent.click(screen.getByRole('button', { name: 'Training starten' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Training starten' }));
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(api.startCloudTraining).toHaveBeenCalledTimes(1);
    const [, args] = api.startCloudTraining.mock.calls[0];
    expect(args.idempotencyKey).toMatch(UUID);
    expect(toast.success.mock.calls[0][0]).toBe('Training gestartet – 3 Stunden reserviert.');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  test('„1 Stunde reserviert" for a one-hour reservation', async () => {
    api.startCloudTraining.mockResolvedValue({ training_id: 9, reserved_hours: 1, replay: false });
    renderPanel({ ...QUOTA, next_reservation_hours: 1 });
    fireEvent.click(screen.getByRole('button', { name: 'Training starten' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Training starten' }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Training gestartet – 1 Stunde reserviert.', expect.anything()));
  });

  test('a retry of the same send reuses the key; a new dialog mints a new one', async () => {
    // The quota after the start still allows another one.
    api.getQuota.mockResolvedValue(QUOTA);
    const err = new Error('Netzwerkfehler');
    api.startCloudTraining.mockRejectedValueOnce(err).mockResolvedValueOnce({ training_id: 9, reserved_hours: 3 });
    renderPanel(QUOTA);
    fireEvent.click(screen.getByRole('button', { name: 'Training starten' }));
    const confirmKey = () => within(screen.getByRole('dialog')).getByRole('button', { name: 'Training starten' });
    const confirm = () => fireEvent.click(confirmKey());
    confirm();
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Cloud-Training konnte nicht gestartet werden: Netzwerkfehler'));
    // The dialog stays open after a failure; the retry reuses the key.
    await waitFor(() => expect(confirmKey()).toBeEnabled());
    confirm();
    await waitFor(() => expect(api.startCloudTraining).toHaveBeenCalledTimes(2));
    const k1 = api.startCloudTraining.mock.calls[0][1].idempotencyKey;
    const k2 = api.startCloudTraining.mock.calls[1][1].idempotencyKey;
    expect(k1).toMatch(UUID);
    expect(k2).toBe(k1);

    // A new dialog is a new start: a new key.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    api.startCloudTraining.mockResolvedValueOnce({ training_id: 10, reserved_hours: 3 });
    fireEvent.click(screen.getByRole('button', { name: 'Training starten' }));
    confirm();
    await waitFor(() => expect(api.startCloudTraining).toHaveBeenCalledTimes(3));
    expect(api.startCloudTraining.mock.calls[2][1].idempotencyKey).not.toBe(k1);
  });

  test('„Zurück" sends nothing', () => {
    renderPanel(QUOTA);
    fireEvent.click(screen.getByRole('button', { name: 'Training starten' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zurück' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(api.startCloudTraining).not.toHaveBeenCalled();
  });

  test('a group start names the group in the dialog', () => {
    renderPanel({ ...QUOTA, account: 'group', workgroup_name: 'Team Rot' });
    fireEvent.click(screen.getByRole('button', { name: 'Training starten' }));
    expect(screen.getByText(FROM_GROUP)).toBeInTheDocument();
  });

  test('no free hours: the key is off and its side label says so', () => {
    renderPanel({ ...QUOTA, hours_free: 0, next_reservation_hours: 0 });
    expect(screen.getByRole('button', { name: 'Training starten' })).toBeDisabled();
    expect(screen.getByText('Keine freien Stunden')).toBeInTheDocument();
  });

  test('before the quota answered the key is off but nothing claims „no hours"', () => {
    renderPanel({});
    expect(screen.getByRole('button', { name: 'Training starten' })).toBeDisabled();
    expect(screen.queryByText('Keine freien Stunden')).toBeNull();
  });

  test('a missing dataset is still refused before any dialog', () => {
    renderPanel(QUOTA, { datasetRepoId: '', policyType: 'act', outputFolderName: 'x' });
    fireEvent.click(screen.getByRole('button', { name: 'Training starten' }));
    expect(toast.error).toHaveBeenCalledWith('Bitte wähle einen Datensatz aus');
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
