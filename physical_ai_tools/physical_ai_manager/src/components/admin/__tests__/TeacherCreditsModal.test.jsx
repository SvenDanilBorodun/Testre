// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The admin's hours for a teacher (credit system v2, spec §10.5/§10.6):
// „Stunden vergeben" / „Stunden entziehen" send a DELTA with a request_id
// (the absolute set is gone, decision 8); the row reads the teacher's hours in
// one line; deleting a teacher names the free pool hours the deletion revokes
// (D8); the dashboard and the create dialog speak of hours.

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import toast from 'react-hot-toast';
import TeacherCreditsModal from '../TeacherCreditsModal';
import TeacherRow, { deleteTeacherConfirmText, teacherHoursLine } from '../TeacherRow';
import CreateTeacherModal from '../CreateTeacherModal';
import AdminDashboard from '../../../pages/admin/AdminDashboard';
import authReducer, { setSession } from '../../../features/auth/authSlice';
import adminReducer, { setTeachers } from '../../../features/admin/adminSlice';
import * as api from '../../../services/adminApi';

vi.mock('react-hot-toast', () => ({ __esModule: true, default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../../../services/adminApi', () => ({
  __esModule: true,
  listTeachers: vi.fn(() => Promise.resolve([])),
  createTeacher: vi.fn(),
  changeTeacherCredits: vi.fn(),
  compensate: vi.fn(),
  eraseReason: vi.fn(),
  getCreditHistory: vi.fn(() => Promise.resolve({ entries: [] })),
  resetTeacherPassword: vi.fn(),
  deleteTeacher: vi.fn(() => Promise.resolve({})),
}));

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TEACHER = {
  id: 't1', username: 'frau.x', full_name: 'Frau X', classroom_count: 0, student_count: 3,
  pool_total: 20, allocated_total: 8, pool_available: 12, hours_spent: 5, old_credits: 30,
};

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe('TeacherCreditsModal', () => {
  function renderModal(mode, onDone = vi.fn()) {
    const onClose = vi.fn();
    render(<TeacherCreditsModal token="jwt" teacher={TEACHER} mode={mode} onClose={onClose} onDone={onDone} />);
    return { onClose, onDone };
  }

  test('grant: title, fields, hint and keys, verbatim', () => {
    renderModal('grant');
    expect(screen.getByText('Stunden vergeben')).toBeInTheDocument();
    expect(screen.getByText('Stunden')).toBeInTheDocument();
    expect(screen.getByText('Grund (optional)')).toBeInTheDocument();
    expect(screen.getByText('Ohne Namen von Schülerinnen und Schülern.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Vergeben' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Zurück' })).toBeEnabled();
  });

  test('grant sends +N with a reason and a request_id, then toasts with hoursDe', async () => {
    api.changeTeacherCredits.mockResolvedValue({});
    const { onClose, onDone } = renderModal('grant');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '1' } });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '  Neues Halbjahr  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Vergeben' }));
    await waitFor(() => expect(api.changeTeacherCredits).toHaveBeenCalledTimes(1));
    const [token, tid, body] = api.changeTeacherCredits.mock.calls[0];
    expect(token).toBe('jwt');
    expect(tid).toBe('t1');
    expect(body.delta).toBe(1);
    expect(body.reason).toBe('Neues Halbjahr');
    expect(body.request_id).toMatch(UUID);
    expect(body).not.toHaveProperty('credits');
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Stunden vergeben: 1 Stunde'));
    expect(onDone).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  test('revoke sends -N; its error is the API\'s German detail, shown in the dialog', async () => {
    const err = new Error('x');
    err.detail = 'Entziehen geht höchstens bis zum freien Guthaben der Lehrkraft: 12 Stunden.';
    api.changeTeacherCredits.mockRejectedValueOnce(err).mockResolvedValueOnce({});
    renderModal('revoke');
    expect(screen.getByText('Stunden entziehen')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '15' } });
    fireEvent.click(screen.getByRole('button', { name: 'Entziehen' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(err.detail);
    expect(api.changeTeacherCredits.mock.calls[0][2].delta).toBe(-15);

    // The same change again is a retry: the same request id.
    fireEvent.click(screen.getByRole('button', { name: 'Entziehen' }));
    await waitFor(() => expect(api.changeTeacherCredits).toHaveBeenCalledTimes(2));
    expect(api.changeTeacherCredits.mock.calls[1][2].request_id)
      .toBe(api.changeTeacherCredits.mock.calls[0][2].request_id);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Stunden entzogen: 15 Stunden'));
  });

  test('another change after a failure gets another request id', async () => {
    api.changeTeacherCredits.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({});
    renderModal('grant');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Vergeben' }));
    await screen.findByRole('alert');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '6' } });
    fireEvent.click(screen.getByRole('button', { name: 'Vergeben' }));
    await waitFor(() => expect(api.changeTeacherCredits).toHaveBeenCalledTimes(2));
    expect(api.changeTeacherCredits.mock.calls[1][2].request_id)
      .not.toBe(api.changeTeacherCredits.mock.calls[0][2].request_id);
  });

  test('more than 10 000 hours cannot be sent', () => {
    renderModal('grant');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '10001' } });
    expect(screen.getByRole('button', { name: 'Vergeben' })).toBeDisabled();
  });

  test('a blank (optional) reason is left out of the request', async () => {
    api.changeTeacherCredits.mockResolvedValue({});
    renderModal('grant');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '2' } });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '   ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Vergeben' }));
    await waitFor(() => expect(api.changeTeacherCredits).toHaveBeenCalledTimes(1));
    expect(api.changeTeacherCredits.mock.calls[0][2].reason).toBe('');
  });
});

function adminStore(teachers) {
  const store = configureStore({ reducer: { auth: authReducer, admin: adminReducer } });
  store.dispatch(setSession({ access_token: 'jwt', user: { id: 'a1' } }));
  store.dispatch(setTeachers(teachers));
  return store;
}

function renderRow(teacher = TEACHER, onChanged = vi.fn()) {
  const store = adminStore([teacher]);
  render(
    <Provider store={store}>
      <table><tbody><TeacherRow teacher={teacher} onChanged={onChanged} /></tbody></table>
    </Provider>,
  );
  return { store, onChanged };
}

describe('TeacherRow', () => {
  test('the hours line, the pre-switch hint and the three hour keys; no absolute set', () => {
    renderRow();
    expect(screen.getByTestId('teacher-hours'))
      .toHaveTextContent('12 Std. frei · 8 Std. verteilt · 5 Std. verbraucht');
    expect(screen.getByText('Vor der Umstellung: 30 (alte Einheit)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stunden vergeben' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stunden entziehen' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Verlauf/ })).toBeInTheDocument();
    expect(screen.queryByTitle(/anpassen/)).toBeNull();
    expect(screen.queryByRole('spinbutton')).toBeNull();
    expect(teacherHoursLine({ pool_available: 1, allocated_total: 0, hours_spent: 0 }))
      .toBe('1 Std. frei · 0 Std. verteilt · 0 Std. verbraucht');
  });

  test('no old_credits → no hint', () => {
    renderRow({ ...TEACHER, old_credits: null });
    expect(screen.queryByText(/Vor der Umstellung/)).toBeNull();
  });

  test('„Stunden vergeben" opens the grant dialog; a grant refetches the list', async () => {
    api.changeTeacherCredits.mockResolvedValue({});
    const { onChanged } = renderRow();
    fireEvent.click(screen.getByRole('button', { name: 'Stunden vergeben' }));
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Vergeben' }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(api.changeTeacherCredits.mock.calls[0][2].delta).toBe(3);
  });

  test('the delete confirmation names the free pool hours it revokes (D8)', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderRow();
    fireEvent.click(screen.getByTitle('Lehrer löschen'));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toContain(
      'Frei im Lehrer-Pool: 12 Stunden. Beim Löschen der Lehrkraft wird dieses Guthaben entzogen.',
    );
    expect(api.deleteTeacher).not.toHaveBeenCalled();
  });

  test('with one free hour „1 Stunde"; with none the sentence is left out', () => {
    expect(deleteTeacherConfirmText({ ...TEACHER, pool_available: 1 }))
      .toContain('Frei im Lehrer-Pool: 1 Stunde. Beim Löschen der Lehrkraft wird dieses Guthaben entzogen.');
    expect(deleteTeacherConfirmText({ ...TEACHER, pool_available: 0 })).toBe('Lehrer Frau X wirklich löschen?');
  });
});

describe('AdminDashboard and CreateTeacherModal wording (spec §10.5/§10.6)', () => {
  test('the dashboard speaks of hours', async () => {
    api.listTeachers.mockResolvedValue([TEACHER]);
    const store = adminStore([TEACHER]);
    render(<Provider store={store}><AdminDashboard onLogout={vi.fn()} /></Provider>);
    expect(await screen.findByText('Stundenverteilung')).toBeInTheDocument();
    expect(screen.getByText('Stunden vergeben', { selector: '.eb-statbig__sub, span, div' })).toBeInTheDocument();
    expect(screen.getByText('Stunden verteilt')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Credit/);
  });

  test('an empty pool says „Keine Stunden vergeben"', async () => {
    const t = { ...TEACHER, pool_total: 0, allocated_total: 0, pool_available: 0 };
    api.listTeachers.mockResolvedValue([t]);
    render(<Provider store={adminStore([t])}><AdminDashboard onLogout={vi.fn()} /></Provider>);
    expect(await screen.findByText('Keine Stunden vergeben')).toBeInTheDocument();
  });

  test('CreateTeacherModal: „Start-Stunden (Pool)" and its help', () => {
    render(<CreateTeacherModal onClose={vi.fn()} onSubmit={vi.fn()} />);
    expect(screen.getByText('Start-Stunden (Pool)')).toBeInTheDocument();
    expect(screen.getByText(
      'Stunden, die die Lehrkraft an Schülerinnen und Schüler verteilt. 1 Stunde = 1 Stunde GPU-Trainingszeit.',
    )).toBeInTheDocument();
  });
});
