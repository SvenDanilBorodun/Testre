// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The claim screen of an unclaimed Orange Pi and the robot-login notice. The
// agent decides (role, code, attempts); the screen collects the code, shows
// what the agent said, and never offers a student the claim (owner decision 3).

import React from 'react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import authReducer, { setProfile, setSession } from '../../features/auth/authSlice';
import uiReducer from '../../features/ui/uiSlice';
import PageType from '../../constants/pageType';

const claimPi = vi.fn();
let mockPiAuth;
vi.mock('../../features/piAuth/piAuth', () => ({
  __esModule: true,
  claimPi: (...a) => claimPi(...a),
  usePiAuth: () => mockPiAuth,
  exchange: vi.fn(),
  PI_AUTH_PHASE: {
    OFF: 'off', PROBING: 'probing', UNREACHABLE: 'unreachable', LEGACY: 'legacy',
    UNCLAIMED: 'unclaimed', LOCKED: 'locked', EXCHANGING: 'exchanging', SESSION: 'session', ERROR: 'error',
  },
  PI_AUTH_TEXT_DE: { legacy: 'Die Pi-Software ist älter als diese Weboberfläche.' },
}));
vi.mock('../../lib/supabaseClient', () => ({
  __esModule: true,
  supabase: { auth: { signInWithPassword: vi.fn(), signOut: vi.fn(() => Promise.resolve({})) } },
}));
const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

// eslint-disable-next-line import/first
import PiClaimScreen from '../PiClaimScreen';
// eslint-disable-next-line import/first
import PiLoginNotice from '../PiLoginNotice';

function storeWith({ session = null, role = null } = {}) {
  const store = configureStore({ reducer: { auth: authReducer, ui: uiReducer } });
  if (session) store.dispatch(setSession(session));
  if (role) store.dispatch(setProfile({ role, username: 'x', full_name: 'X', classroom_id: null }));
  return store;
}

beforeEach(() => {
  claimPi.mockReset();
  mockToast.success.mockClear();
  mockPiAuth = { phase: 'unclaimed', hostname: 'edubotics-07', setupCode: 'active', resetCode: null };
});

describe('PiClaimScreen', () => {
  it('asks a signed-out visitor for a teacher or admin login first', () => {
    render(<Provider store={storeWith()}><PiClaimScreen /></Provider>);
    expect(screen.getByText(/Lehrkräfte- oder Admin-Konto anmelden/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Einrichtungscode/)).toBeNull();
  });

  it('a teacher enters the code; success leads to the System tab', async () => {
    claimPi.mockResolvedValue({ ok: true, message: 'Roboter eingerichtet.' });
    const store = storeWith({ session: { access_token: 'jwt-t', user: { email: 't@x' } }, role: 'teacher' });
    render(<Provider store={store}><PiClaimScreen /></Provider>);
    expect(screen.getByText('edubotics-07')).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Einrichtungscode'), 'abcd-efgh');
    await userEvent.click(screen.getByRole('button', { name: 'Roboter einrichten' }));
    await waitFor(() => expect(claimPi).toHaveBeenCalledWith('abcd-efgh'));
    expect(mockToast.success).toHaveBeenCalledWith('Roboter eingerichtet.');
    expect(store.getState().ui.currentPage).toBe(PageType.SYSTEM);
  });

  it('shows the agent sentence when the code is refused', async () => {
    claimPi.mockResolvedValue({ ok: false, code: 'code_wrong', message: 'Der Einrichtungscode stimmt nicht.' });
    const store = storeWith({ session: { access_token: 'jwt-a' }, role: 'admin' });
    render(<Provider store={store}><PiClaimScreen /></Provider>);
    await userEvent.type(screen.getByLabelText('Einrichtungscode'), 'ABCDEFGH');
    await userEvent.click(screen.getByRole('button', { name: 'Roboter einrichten' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Der Einrichtungscode stimmt nicht.');
  });

  it('never offers a student the claim', () => {
    const store = storeWith({ session: { access_token: 'jwt-s' }, role: 'student' });
    render(<Provider store={store}><PiClaimScreen /></Provider>);
    expect(screen.getByRole('alert')).toHaveTextContent(/Nur|nur eine Lehrkraft oder ein Admin/);
    expect(screen.queryByLabelText('Einrichtungscode')).toBeNull();
    expect(screen.getByRole('button', { name: /Abmelden/ })).toBeInTheDocument();
  });

  it('shows and prefills the code a factory reset just answered', () => {
    mockPiAuth = { ...mockPiAuth, resetCode: 'WXYZ-2345' };
    const store = storeWith({ session: { access_token: 'jwt-t' }, role: 'teacher' });
    render(<Provider store={store}><PiClaimScreen /></Provider>);
    expect(screen.getByTestId('pi-reset-code')).toHaveTextContent('WXYZ-2345');
    expect(screen.getByLabelText('Einrichtungscode')).toHaveValue('WXYZ-2345');
  });

  it('names the command for a code that is gone or voided', () => {
    mockPiAuth = { ...mockPiAuth, setupCode: 'void' };
    const store = storeWith({ session: { access_token: 'jwt-t' }, role: 'teacher' });
    render(<Provider store={store}><PiClaimScreen /></Provider>);
    expect(screen.getByText(/zu vielen Fehlversuchen gesperrt/)).toBeInTheDocument();
    expect(screen.getByText(/python3 -m pi_agent.setup_code/)).toBeInTheDocument();
  });
});

describe('PiLoginNotice', () => {
  it('says nothing off a Pi or with a live session', () => {
    mockPiAuth = { phase: 'legacy' };
    const { container, rerender } = render(<PiLoginNotice piMode={false} />);
    expect(container).toBeEmptyDOMElement();
    mockPiAuth = { phase: 'session', expiresAt: 1 };
    rerender(<PiLoginNotice piMode />);
    expect(container).toBeEmptyDOMElement();
  });

  it('names the way out of an agent older than the page', () => {
    mockPiAuth = { phase: 'legacy' };
    render(<PiLoginNotice piMode />);
    expect(screen.getByRole('alert')).toHaveTextContent('Pi-Software ist älter');
  });

  it('shows a refused exchange with a retry', () => {
    mockPiAuth = { phase: 'error', error: { code: 'cloud_unreachable', message: 'Cloud weg.' } };
    render(<PiLoginNotice piMode />);
    expect(screen.getByRole('alert')).toHaveTextContent('Cloud weg.');
    expect(screen.getByRole('button', { name: 'Erneut versuchen' })).toBeInTheDocument();
  });
});
