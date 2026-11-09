// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The teacher sees the open GDPR deletion requests of their students on the
// dashboard and carries one out (launch plan step 5, owner decision „teacher
// confirms"): the queue's button names the count, a confirmation runs the
// cloud's confirm route (the full deletion), the answer is announced the way
// the roster's delete announces it, and the student leaves the roster.

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import authReducer from '../../../features/auth/authSlice';
import teacherReducer from '../../../features/teacher/teacherSlice';
import TeacherDashboard from '../TeacherDashboard';

const toastCalls = { success: [], error: [] };
vi.mock('react-hot-toast', () => {
  const t = {
    success: (m, o) => toastCalls.success.push([m, o]),
    error: (m, o) => toastCalls.error.push([m, o]),
  };
  return { __esModule: true, default: t, toast: t };
});

let requests;
let listError;
let confirmResult;
const calls = { list: 0, confirm: [], classrooms: 0 };
vi.mock('../../../services/teacherApi', () => ({
  __esModule: true,
  listClassrooms: () => {
    calls.classrooms += 1;
    return Promise.resolve([{ id: 'c1', name: '7a', student_count: 2, created_at: 'x' }]);
  },
  createClassroom: () => Promise.resolve({}),
  getTeacherCreditHistory: () => Promise.resolve({ entries: [] }),
  listDeletionRequests: () => {
    calls.list += 1;
    return listError ? Promise.reject(listError) : Promise.resolve(requests);
  },
  confirmDeletionRequest: (...a) => {
    calls.confirm.push(a);
    return confirmResult instanceof Error ? Promise.reject(confirmResult) : Promise.resolve(confirmResult);
  },
}));
vi.mock('../../../services/meApi', () => ({
  __esModule: true,
  getMe: () => Promise.resolve({ pool_total: 10, allocated_total: 3, pool_available: 7, student_count: 1 }),
}));
vi.mock('../../../components/teacher/ClassroomDetail', () => ({ __esModule: true, default: () => null }));

const ANNA = {
  student_id: 'stu-anna',
  username: 'anna',
  full_name: 'Anna Müller',
  classroom_id: 'c1',
  classroom_name: '7a',
  requested_at: '2026-11-03T10:00:00+00:00',
  due_by: '2026-12-03T10:00:00+00:00',
};

function mountDashboard() {
  const store = configureStore({
    reducer: { auth: authReducer, teacher: teacherReducer },
    preloadedState: {
      auth: { ...authReducer(undefined, { type: '@@init' }), session: { access_token: 'jwt' }, role: 'teacher' },
      teacher: {
        ...teacherReducer(undefined, { type: '@@init' }),
        selectedClassroom: { id: 'c1', students: [{ id: 'stu-anna' }, { id: 'stu-cara' }] },
      },
    },
  });
  render(
    <Provider store={store}>
      <TeacherDashboard onLogout={() => {}} />
    </Provider>
  );
  return store;
}

beforeEach(() => {
  toastCalls.success.length = 0;
  toastCalls.error.length = 0;
  calls.list = 0;
  calls.confirm.length = 0;
  calls.classrooms = 0;
  requests = [ANNA];
  listError = null;
  confirmResult = { ok: true, hf_erasure_complete: true };
});

describe('TeacherDashboard deletion requests', () => {
  it('names the open requests on the dashboard', async () => {
    mountDashboard();
    const button = await screen.findByTestId('deletion-requests-button');
    await waitFor(() => expect(button.textContent).toContain('1 offene Löschanfrage'));
  });

  it('carries one out after the confirmation and takes the student off the roster', async () => {
    const store = mountDashboard();
    const button = await screen.findByTestId('deletion-requests-button');
    await waitFor(() => expect(button.textContent).toContain('1 offene Löschanfrage'));
    await userEvent.click(button);
    // the queue's row (the classroom sidebar is a list too)
    const row = await screen.findByTestId('deletion-request-stu-anna');
    expect(within(row).getByText(/Anna Müller/)).toBeTruthy();
    expect(within(row).getByText(/7a/)).toBeTruthy();
    await userEvent.click(within(row).getByRole('button', { name: /Löschung ausführen/ }));
    await userEvent.click(screen.getByRole('button', { name: /Endgültig löschen/ }));
    await waitFor(() => expect(calls.confirm).toEqual([['jwt', 'stu-anna']]));
    await waitFor(() => expect(toastCalls.success).toEqual([['Schüler gelöscht', undefined]]));
    expect(store.getState().teacher.selectedClassroom.students.map((s) => s.id)).toEqual(['stu-cara']);
    expect(await screen.findByText('Keine offenen Löschanfragen.')).toBeTruthy();
    expect(calls.classrooms).toBeGreaterThanOrEqual(2); // the counts are read again
  });

  it('a request withdrawn meanwhile is refused, said and the list read again', async () => {
    const err = new Error('Diese Löschanfrage ist nicht mehr offen – sie wurde zurückgezogen oder bereits ausgeführt.');
    err.status = 409;
    confirmResult = err;
    const store = mountDashboard();
    const button = await screen.findByTestId('deletion-requests-button');
    await waitFor(() => expect(button.textContent).toContain('1 offene Löschanfrage'));
    await userEvent.click(button);
    const row = await screen.findByTestId('deletion-request-stu-anna');
    await userEvent.click(within(row).getByRole('button', { name: /Löschung ausführen/ }));
    requests = [];
    await userEvent.click(screen.getByRole('button', { name: /Endgültig löschen/ }));
    await waitFor(() => expect(toastCalls.error).toHaveLength(1));
    expect(toastCalls.error[0][0]).toBe(err.message);
    expect(store.getState().teacher.selectedClassroom.students.map((s) => s.id)).toEqual(['stu-anna', 'stu-cara']);
    await waitFor(() => expect(calls.list).toBeGreaterThanOrEqual(2));
  });

  it('an older Cloud API without the route shows no queue error', async () => {
    const err = new Error('Not Found');
    err.status = 404;
    listError = err;
    mountDashboard();
    const button = await screen.findByTestId('deletion-requests-button');
    expect(button.textContent).toContain('Löschanfragen');
    await userEvent.click(button);
    expect(await screen.findByText('Keine offenen Löschanfragen.')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
