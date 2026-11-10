// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The student SPA shows an open GDPR deletion request (launch plan step 5): when
// it was filed, by when the teacher carries it out, and the withdrawal — which
// goes through the cloud API (migration 055 refuses a direct table write).

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import authReducer, { setProfile } from '../../features/auth/authSlice';
import { signedOut } from '../../features/session/sessionActions';
import DeletionRequestNotice from '../DeletionRequestNotice';

const toastCalls = { success: [], error: [] };
vi.mock('react-hot-toast', () => {
  const t = {
    success: (m, o) => toastCalls.success.push([m, o]),
    error: (m, o) => toastCalls.error.push([m, o]),
  };
  return { __esModule: true, default: t, toast: t };
});

let withdrawResult;
const withdrawCalls = [];
vi.mock('../../services/meApi', () => ({
  __esModule: true,
  withdrawDeletionRequest: (...a) => {
    withdrawCalls.push(a);
    return withdrawResult instanceof Error ? Promise.reject(withdrawResult) : Promise.resolve(withdrawResult);
  },
}));

const ME = {
  id: 'u-1',
  role: 'student',
  username: 'anna',
  full_name: 'Anna',
  deletion_requested_at: '2026-11-03T10:00:00+00:00',
  deletion_due_by: '2026-12-03T10:00:00+00:00',
};

function mountNotice(profile = ME) {
  const store = configureStore({
    reducer: { auth: authReducer },
    preloadedState: { auth: { ...authReducer(undefined, { type: '@@init' }), session: { access_token: 'jwt' } } },
  });
  store.dispatch(setProfile(profile));
  render(
    <Provider store={store}>
      <DeletionRequestNotice />
    </Provider>
  );
  return store;
}

beforeEach(() => {
  toastCalls.success.length = 0;
  toastCalls.error.length = 0;
  withdrawCalls.length = 0;
  withdrawResult = { withdrawn: true, message: 'Deine Löschanfrage wurde zurückgezogen. …' };
});

describe('DeletionRequestNotice', () => {
  it('renders nothing without an open request (and for an older API)', () => {
    mountNotice({ ...ME, deletion_requested_at: null, deletion_due_by: null });
    expect(screen.queryByTestId('deletion-request-notice')).toBeNull();
    mountNotice({ id: 'u-1', role: 'student' });
    expect(screen.queryByTestId('deletion-request-notice')).toBeNull();
  });

  it('names the request day and the deadline', () => {
    mountNotice();
    const text = screen.getByTestId('deletion-request-notice').textContent;
    expect(text).toContain('am 03.11.2026 beantragt');
    expect(text).toContain('spätestens bis zum 03.12.2026');
    expect(text).toContain('Lehrkraft');
  });

  it('withdraws through the API after a second click, then disappears', async () => {
    const store = mountNotice();
    await userEvent.click(screen.getByRole('button', { name: 'Antrag zurückziehen' }));
    expect(withdrawCalls).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: /Ja, zurückziehen/ }));
    await waitFor(() => expect(withdrawCalls).toEqual([['jwt']]));
    await waitFor(() => expect(screen.queryByTestId('deletion-request-notice')).toBeNull());
    expect(store.getState().auth.deletionRequestedAt).toBeNull();
    expect(toastCalls.success[0][0]).toContain('zurückgezogen');
  });

  it('keeps the notice and says why when the withdrawal fails', async () => {
    const err = new Error('Die Löschanfrage konnte nicht zurückgezogen werden — bitte erneut versuchen.');
    err.status = 500;
    withdrawResult = err;
    const store = mountNotice();
    await userEvent.click(screen.getByRole('button', { name: 'Antrag zurückziehen' }));
    await userEvent.click(screen.getByRole('button', { name: /Ja, zurückziehen/ }));
    await waitFor(() => expect(toastCalls.error).toHaveLength(1));
    expect(toastCalls.error[0][0]).toBe(err.message);
    expect(store.getState().auth.deletionRequestedAt).toBe(ME.deletion_requested_at);
    expect(screen.getByTestId('deletion-request-notice')).toBeTruthy();
  });

  it('a cancel withdraws nothing', async () => {
    mountNotice();
    await userEvent.click(screen.getByRole('button', { name: 'Antrag zurückziehen' }));
    await userEvent.click(screen.getByRole('button', { name: 'Abbrechen' }));
    expect(withdrawCalls).toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Antrag zurückziehen' })).toBeTruthy();
  });
});

describe('the auth slice', () => {
  it('takes the request from /me and forgets it on sign-out', () => {
    let state = authReducer(undefined, setProfile(ME));
    expect(state.deletionRequestedAt).toBe(ME.deletion_requested_at);
    expect(state.deletionDueBy).toBe(ME.deletion_due_by);
    state = authReducer(state, signedOut());
    expect(state.deletionRequestedAt).toBeNull();
    expect(state.deletionDueBy).toBeNull();
  });
});
