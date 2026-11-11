// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The admin is the backstop for GDPR deletion requests (launch plan step 5): the
// dashboard lists every open request — an orphaned student's and a teacher's own
// included — and carries one out through the admin's confirm route.

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import authReducer from '../../../features/auth/authSlice';
import adminReducer from '../../../features/admin/adminSlice';
import AdminDashboard from '../AdminDashboard';

const toastCalls = { success: [], error: [] };
vi.mock('react-hot-toast', () => {
  const t = {
    success: (m, o) => toastCalls.success.push([m, o]),
    error: (m, o) => toastCalls.error.push([m, o]),
  };
  return { __esModule: true, default: t, toast: t };
});

const confirmCalls = [];
let confirmResult;
vi.mock('../../../services/adminApi', () => ({
  __esModule: true,
  listTeachers: () => Promise.resolve([]),
  createTeacher: () => Promise.resolve({}),
  listAllDeletionRequests: () =>
    Promise.resolve([
      {
        user_id: 'emil', role: 'student', username: 'emil', full_name: 'Emil Ohneklasse',
        classroom_id: null, classroom_name: null, teacher_name: null,
        requested_at: '2026-10-20T10:00:00+00:00', due_by: '2026-11-20T10:00:00+00:00',
      },
      {
        user_id: 't3', role: 'teacher', username: 't3', full_name: 'Herr Gegangen',
        classroom_id: null, classroom_name: null, teacher_name: null,
        requested_at: '2026-11-05T12:00:00+00:00', due_by: '2026-12-05T12:00:00+00:00',
      },
    ]),
  confirmAnyDeletionRequest: (...a) => {
    confirmCalls.push(a);
    return Promise.resolve(confirmResult);
  },
}));

function renderDashboard() {
  const store = configureStore({
    reducer: { auth: authReducer, admin: adminReducer },
    preloadedState: {
      auth: { ...authReducer(undefined, { type: '@@init' }), session: { access_token: 'jwt' }, role: 'admin' },
    },
  });
  render(
    <Provider store={store}>
      <AdminDashboard onLogout={() => {}} />
    </Provider>
  );
}

beforeEach(() => {
  toastCalls.success.length = 0;
  toastCalls.error.length = 0;
  confirmCalls.length = 0;
  confirmResult = { ok: true };
});

describe('AdminDashboard deletion requests', () => {
  it('lists every open request and carries a teacher’s own out', async () => {
    renderDashboard();
    expect(await screen.findByText('2 offene Löschanfragen')).toBeTruthy();
    const emil = await screen.findByTestId('deletion-request-emil');
    expect(within(emil).getByText(/Emil Ohneklasse/)).toBeTruthy();
    expect(within(emil).getByText('keine Klasse')).toBeTruthy();
    const teacher = screen.getByTestId('deletion-request-t3');
    expect(within(teacher).getByText(/Herr Gegangen/)).toBeTruthy();
    expect(within(teacher).getByText('Lehrkraft-Konto')).toBeTruthy();
    await userEvent.click(within(teacher).getByRole('button', { name: /Löschung ausführen/ }));
    await userEvent.click(screen.getByRole('button', { name: /Endgültig löschen/ }));
    await waitFor(() => expect(confirmCalls).toEqual([['jwt', 't3']]));
    await waitFor(() => expect(toastCalls.success).toEqual([['Lehrkraft gelöscht', undefined]]));
    expect(await screen.findByText('1 offene Löschanfrage')).toBeTruthy();
  });

  it('a student’s deletion is announced like the teacher’s', async () => {
    confirmResult = { ok: true, hf_erasure_complete: false, hf_failures: [], detail: 'Das Konto wurde gelöscht. …' };
    renderDashboard();
    const emil = await screen.findByTestId('deletion-request-emil');
    await userEvent.click(within(emil).getByRole('button', { name: /Löschung ausführen/ }));
    await userEvent.click(screen.getByRole('button', { name: /Endgültig löschen/ }));
    await waitFor(() => expect(confirmCalls).toEqual([['jwt', 'emil']]));
    await waitFor(() => expect(toastCalls.error).toEqual([['Das Konto wurde gelöscht. …', { duration: 15000 }]]));
  });
});
