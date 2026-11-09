// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// Only an ADMIN meets the second factor (owner decision 2026-11-10): the teacher
// web renders the TeacherDashboard straight away, the AdminDashboard only behind
// components/admin/AdminMfaGate.jsx. supabase-js and the two dashboards are
// stubbed; the WebApp routing and the gate are real.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import authReducer, { setProfile } from '../features/auth/authSlice';
import { MFA_DE } from '../components/admin/AdminMfaGate';
import WebApp from '../WebApp';

const mfa = vi.hoisted(() => ({
  getAuthenticatorAssuranceLevel: vi.fn(),
  listFactors: vi.fn(),
  enroll: vi.fn(),
  unenroll: vi.fn(),
  challengeAndVerify: vi.fn(),
}));
vi.mock('../lib/supabaseClient', () => ({
  supabase: {
    auth: {
      mfa,
      getSession: () => Promise.resolve({ data: { session: { access_token: 'jwt', user: { id: 'u' } } } }),
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: () => {} } } }),
    },
  },
}));
vi.mock('../hooks/useMeProfile', () => ({ useMeProfile: () => {} }));
vi.mock('../pages/teacher/TeacherDashboard', () => ({ default: () => <div>Lehrer-Dashboard</div> }));
vi.mock('../pages/admin/AdminDashboard', () => ({ default: () => <div>Admin-Dashboard</div> }));

function renderAs(role) {
  const store = configureStore({ reducer: { auth: authReducer } });
  store.dispatch(setProfile({ role, username: 'x', full_name: 'X' }));
  render(
    <Provider store={store}>
      <WebApp />
    </Provider>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe('WebApp and the second factor', () => {
  test('a teacher sees the dashboard and is never asked for a code', async () => {
    renderAs('teacher');
    expect(await screen.findByText('Lehrer-Dashboard')).toBeInTheDocument();
    expect(mfa.getAuthenticatorAssuranceLevel).not.toHaveBeenCalled();
  });

  test('an admin at aal1 meets the challenge, not the dashboard', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValue({ data: { currentLevel: 'aal1' }, error: null });
    mfa.listFactors.mockResolvedValue({
      data: { all: [{ id: 'f', factor_type: 'totp', status: 'verified' }], totp: [{ id: 'f' }] },
      error: null,
    });
    renderAs('admin');
    expect(await screen.findByRole('heading', { name: MFA_DE.challengeTitle })).toBeInTheDocument();
    expect(screen.queryByText('Admin-Dashboard')).not.toBeInTheDocument();
  });

  test('an admin at aal2 sees the dashboard', async () => {
    mfa.getAuthenticatorAssuranceLevel.mockResolvedValue({ data: { currentLevel: 'aal2' }, error: null });
    renderAs('admin');
    expect(await screen.findByText('Admin-Dashboard')).toBeInTheDocument();
  });
});
