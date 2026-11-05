// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// „Stundenverlauf" (credit system v2, spec §10.4): every kind label, the
// charge's extra lines (who stopped it, the waiver, the lost time and every
// loss label — checkpoint_overdue included), the erased reason, the refused
// starts, paging by `before`; and the teacher dashboard's pool readout that
// opens it.

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { describe, expect, test, vi } from 'vitest';
import CreditHistoryDrawer, {
  LOSS_LABELS, PAGE_SIZE, detailLines, hoursCell, kindLabel, lossLabel, refusalLabel,
} from '../CreditHistoryDrawer';
import TeacherDashboard from '../../../pages/teacher/TeacherDashboard';
import authReducer, { setProfile, setSession } from '../../../features/auth/authSlice';
import teacherReducer from '../../../features/teacher/teacherSlice';

const mockTeacherHistory = vi.fn();
vi.mock('../../../services/teacherApi', () => ({
  __esModule: true,
  listClassrooms: () => Promise.resolve([]),
  createClassroom: vi.fn(),
  getTeacherCreditHistory: (...a) => mockTeacherHistory(...a),
}));
vi.mock('react-hot-toast', () => ({ __esModule: true, default: { success: vi.fn(), error: vi.fn() } }));

const row = (over) => ({ id: 1, at: '2026-10-28T10:00:00Z', hours: 1, training_id: null, model_name: null, actor: 'teacher', ...over });

describe('kind labels', () => {
  test.each([
    [{ kind: 'grant', actor: 'admin' }, 'Vom Admin vergeben'],
    [{ kind: 'revoke', actor: 'admin' }, 'Vom Admin entzogen'],
    [{ kind: 'revoke', actor: 'system' }, 'Entzogen – das Konto der Lehrkraft wurde gelöscht'],
    [{ kind: 'assign' }, 'Zugewiesen'],
    [{ kind: 'reclaim' }, 'Zurückgeholt'],
    [{ kind: 'reserve' }, 'Reserviert (Trainingsstart)'],
    [{ kind: 'charge' }, 'Berechnet'],
    [{ kind: 'charge', stopped_by: 'teacher' }, 'Berechnet (gestoppt von Lehrkraft)'],
    [{ kind: 'charge', stopped_by: 'student' }, 'Berechnet (gestoppt von Schüler/in)'],
    [{ kind: 'release' }, 'Zurück ins Guthaben'],
    [{ kind: 'migrate', actor: 'system' }, 'Umstellung auf Stunden'],
    [{ kind: 'compensate', actor: 'admin' }, 'Ausgleich vom Admin'],
    [{ kind: 'limit_change', limit_old: null, limit_new: 3 }, 'Gruppenlimit geändert: kein Limit → 3 Stunden'],
    [{ kind: 'limit_change', limit_old: 1, limit_new: null }, 'Gruppenlimit geändert: 1 Stunde → kein Limit'],
  ])('%o → %s', (e, text) => {
    expect(kindLabel(row(e))).toBe(text);
  });

  test('a switch-over and a limit change carry no hours; a movement reads hoursDe', () => {
    expect(hoursCell(row({ kind: 'migrate', hours: 0 }))).toBe('—');
    expect(hoursCell(row({ kind: 'limit_change', hours: 0 }))).toBe('—');
    expect(hoursCell(row({ kind: 'assign', hours: 1 }))).toBe('1 Stunde');
    expect(hoursCell(row({ kind: 'charge', hours: 3 }))).toBe('3 Stunden');
  });
});

describe('detail lines', () => {
  test('a compensation names its reason, or that it was erased', () => {
    expect(detailLines(row({ kind: 'compensate', reason: 'GPU-Ausfall am Montag' })))
      .toEqual(['Grund: GPU-Ausfall am Montag']);
    expect(detailLines(row({ kind: 'compensate', reason: null, reason_erased: true })))
      .toEqual(['Grund: (gelöscht)']);
  });

  test('a waived charge says why nothing was charged [OQ-2]', () => {
    expect(detailLines(row({ kind: 'charge', hours: 0, waiver: 'lerobot_error' })))
      .toEqual(['Nicht berechnet: Fehler vor dem ersten Trainingsschritt']);
  });

  test('a charge with lost time: the minutes and every loss label', () => {
    const losses = Object.keys(LOSS_LABELS);
    const lines = detailLines(row({ kind: 'charge', hours: 1, lost_seconds: 1260, losses }));
    expect(lines[0]).toBe('Verlorene Zeit: 21 Min. (nicht berechnet)');
    expect(lines[1]).toBe(`Gründe: ${losses.map((l) => LOSS_LABELS[l]).join(', ')}`);
  });

  test.each([
    ['pod_gone', 'GPU-Server weg'],
    ['modal_ended', 'GPU-Server weg'],
    ['heartbeat_lost', 'Verbindung zum GPU-Server verloren'],
    ['stalled', 'Training hing'],
    ['start_timeout', 'GPU-Server startete nicht'],
    ['placement_guard', 'Ungeeigneter GPU-Server'],
    ['host_unfit:lerobot_crash', 'Training abgestürzt'],
    ['host_unfit:resume_crash', 'Fortsetzen abgestürzt'],
    ['checkpoint_overdue', 'Keine Zwischenstände gespeichert'],
    ['host_unfit:lerobot_error_unconfirmed', 'Fehler gemeldet, noch nicht bestätigt'],
    ['host_unfit:content_unconfirmed', 'Zwischenstand abgelehnt, noch nicht bestätigt'],
    ['host_unfit:time_cap_early', 'Unbestätigtes Zeitlimit'],
    ['host_unfit:wrong_image', 'Falsche Software auf dem GPU-Server'],
    ['host_unfit:download', 'GPU-Server unbrauchbar'],
    ['host_unfit:cuda_too_old', 'GPU-Server unbrauchbar'],
  ])('loss %s → %s', (reason, text) => {
    expect(lossLabel(reason)).toBe(text);
  });

  test('under one minute of lost time no line', () => {
    expect(detailLines(row({ kind: 'charge', hours: 1, lost_seconds: 59, losses: ['stalled'] }))).toEqual([]);
  });
});

describe('refusal labels', () => {
  test.each([
    [{ code: 'role' }, 'Keine Schülerin/kein Schüler'],
    [{ code: 'policy', model_type: 'pi0' }, 'Modelltyp nicht erlaubt (pi0)'],
    [{ code: 'policy', model_type: null }, 'Modelltyp nicht erlaubt'],
    [{ code: 'no_hours' }, 'Keine freien Stunden'],
    [{ code: 'member_limit' }, 'Gruppenlimit erreicht'],
  ])('%o → %s', (r, text) => {
    expect(refusalLabel(r)).toBe(text);
  });
});

describe('CreditHistoryDrawer', () => {
  test('the columns, a row per entry, the actor labels and the refused starts', async () => {
    const load = vi.fn(() => Promise.resolve({
      entries: [
        row({ id: 9, kind: 'charge', hours: 2, training_id: 41, model_name: 'EduBotics-Solutions/act-1',
          actor: 'system', stopped_by: 'teacher', lost_seconds: 600, losses: ['checkpoint_overdue'] }),
        row({ id: 8, kind: 'assign', hours: 3, actor: 'teacher' }),
        row({ id: 7, kind: 'grant', hours: 10, actor: 'admin' }),
        row({ id: 6, kind: 'reserve', hours: 3, actor: 'student', training_id: 41 }),
      ],
      refusals: [{ id: 1, at: '2026-10-28T09:00:00Z', code: 'policy', model_type: null }],
    }));
    render(<CreditHistoryDrawer title="Stundenverlauf · Anna" load={load} showRefusals onClose={vi.fn()} />);
    const dialog = await screen.findByRole('dialog', { name: 'Stundenverlauf · Anna' });
    for (const h of ['Datum', 'Art', 'Stunden', 'Training', 'Von']) {
      expect(within(dialog).getByRole('columnheader', { name: h })).toBeInTheDocument();
    }
    const charge = await screen.findByTestId('credit-row-9');
    expect(charge).toHaveTextContent('Berechnet (gestoppt von Lehrkraft)');
    expect(charge).toHaveTextContent('Verlorene Zeit: 10 Min. (nicht berechnet)');
    expect(charge).toHaveTextContent('Gründe: Keine Zwischenstände gespeichert');
    expect(charge).toHaveTextContent('EduBotics-Solutions/act-1');
    expect(charge).toHaveTextContent('2 Stunden');
    expect(charge).toHaveTextContent('System');
    expect(screen.getByTestId('credit-row-8')).toHaveTextContent('Lehrkraft');
    expect(screen.getByTestId('credit-row-7')).toHaveTextContent('Admin');
    expect(screen.getByTestId('credit-row-6')).toHaveTextContent('Schüler/in');
    expect(screen.getByTestId('credit-row-6')).toHaveTextContent('#41');
    const refused = screen.getByRole('region', { name: 'Abgelehnte Starts' });
    expect(refused).toHaveTextContent('Modelltyp nicht erlaubt');
    expect(refused).not.toHaveTextContent('(');
    expect(load).toHaveBeenCalledWith({ before: null, limit: PAGE_SIZE });
  });

  test('an empty history: „Noch keine Einträge."', async () => {
    render(<CreditHistoryDrawer title="Stundenverlauf · Lehrer-Pool" load={() => Promise.resolve([])} onClose={vi.fn()} />);
    expect(await screen.findByText('Noch keine Einträge.')).toBeInTheDocument();
  });

  test('„Ältere laden" pages by `before` = the smallest id shown', async () => {
    const first = Array.from({ length: PAGE_SIZE }, (_, i) => row({ id: 200 - i, kind: 'assign' }));
    const second = [row({ id: 100, kind: 'grant', actor: 'admin' })];
    const load = vi.fn()
      .mockResolvedValueOnce({ entries: first, refusals: [] })
      .mockResolvedValueOnce({ entries: second, refusals: [] });
    render(<CreditHistoryDrawer title="Stundenverlauf · Rot" load={load} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /Ältere laden/ }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    expect(load.mock.calls[1][0]).toEqual({ before: 200 - (PAGE_SIZE - 1), limit: PAGE_SIZE });
    expect(await screen.findByTestId('credit-row-100')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: /Ältere laden/ })).toBeNull());
  });

  test('a failed load shows the API\'s sentence', async () => {
    const err = new Error('Der Stundenverlauf konnte nicht geladen werden.');
    render(<CreditHistoryDrawer title="x" load={() => Promise.reject(err)} onClose={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Der Stundenverlauf konnte nicht geladen werden.');
  });
});

describe('TeacherDashboard pool readout', () => {
  function renderDashboard(profile) {
    const store = configureStore({ reducer: { auth: authReducer, teacher: teacherReducer } });
    store.dispatch(setSession({ access_token: 'jwt', user: { id: 't1' } }));
    store.dispatch(setProfile({
      role: 'teacher', username: 'frau.x', full_name: 'Frau X', classroom_id: null,
      pool_total: 20, allocated_total: 8, pool_available: 12, student_count: 4, group_count: 1,
      group_credits_total: 1, ...profile,
    }));
    render(<Provider store={store}><TeacherDashboard onLogout={vi.fn()} /></Provider>);
  }

  test('„Lehrer-Pool: N Std. frei", the admin\'s compensations, the new wording and „Verlauf"', async () => {
    mockTeacherHistory.mockResolvedValue({ entries: [], refusals: [] });
    renderDashboard({ compensated: 2 });
    expect(screen.getByTestId('pool-readout')).toHaveTextContent('Lehrer-Pool: 12 Std. frei');
    expect(screen.getByText('Ausgleich vom Admin: 2 Stunden')).toBeInTheDocument();
    expect(screen.getByText('Stunden insgesamt')).toBeInTheDocument();
    expect(screen.getByText('1 Stunde bei Gruppen')).toBeInTheDocument();
    expect(screen.getByText('Stunden vergibt der Admin. Du verteilst sie an Schüler oder an Arbeitsgruppen.'))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Verlauf/ }));
    expect(await screen.findByRole('dialog', { name: 'Stundenverlauf · Lehrer-Pool' })).toBeInTheDocument();
    expect(mockTeacherHistory).toHaveBeenCalledWith('jwt', { before: null, limit: PAGE_SIZE });
    expect(document.body.textContent).not.toMatch(/Credit/);
  });

  test('no compensation: no compensation line', () => {
    renderDashboard({ compensated: 0 });
    expect(screen.queryByText(/Ausgleich vom Admin/)).toBeNull();
  });
});
