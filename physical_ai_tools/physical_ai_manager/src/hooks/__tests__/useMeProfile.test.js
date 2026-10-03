// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Covers the robust /me loader. Cases: success, 401 → sign-out, 404 →
// profileError (NO sign-out), and that the old HF auto-link is GONE: the hook
// never PATCHes /me with a Benutzer-ID any more (the cloud proves the name from
// the stored token instead, PUT /me/hf-token).
//
// Backed by a REAL Redux store (auth + ui reducers) + <Provider>, so
// dispatched actions properly re-render the subscribed selectors — exactly like
// the live app, minus the rest of the store. Only the network/auth side
// effects are mocked.

import React from 'react';
import { renderHook, act, waitFor } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import authReducer, { setSession } from '../../features/auth/authSlice';
import uiReducer, { setHfUserList } from '../../features/ui/uiSlice';
import { useMeProfile } from '../useMeProfile';

// ── service + side-effect mocks ────────────────────────────────────────
const mockGetMe = vi.fn();
const mockPatchHf = vi.fn();
vi.mock('../../services/meApi', () => ({
  __esModule: true,
  getMe: (...a) => mockGetMe(...a),
  patchMyHfUsername: (...a) => mockPatchHf(...a),
}));

const mockSignOut = vi.fn();
vi.mock('../../lib/supabaseClient', () => ({
  __esModule: true,
  supabase: { auth: { signOut: () => mockSignOut() } },
}));

const mockResetJetson = vi.fn();
vi.mock('../../features/jetson/sessionReset', () => ({
  __esModule: true,
  resetJetsonOnLogout: (...a) => mockResetJetson(...a),
}));

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { success: vi.fn(), error: vi.fn() },
}));

function makeStore({ accessToken = 'jwt-1', hfUserList = [] } = {}) {
  const store = configureStore({
    reducer: { auth: authReducer, ui: uiReducer },
  });
  if (accessToken) {
    store.dispatch(setSession({ access_token: accessToken }));
  }
  if (hfUserList.length) {
    store.dispatch(setHfUserList(hfUserList));
  }
  return store;
}

function wrapperFor(store) {
  return ({ children }) => <Provider store={store}>{children}</Provider>;
}

beforeEach(() => {
  mockGetMe.mockReset();
  mockPatchHf.mockReset();
  mockSignOut.mockReset();
  mockResetJetson.mockReset();
});

describe('useMeProfile — load + error branches', () => {
  test('success dispatches setProfile and calls onProfile', async () => {
    const store = makeStore();
    mockGetMe.mockResolvedValue({ role: 'student', hf_username: 'student42' });
    const onProfile = vi.fn();

    renderHook(() => useMeProfile({ onProfile }), { wrapper: wrapperFor(store) });

    await waitFor(() => expect(mockGetMe).toHaveBeenCalledWith('jwt-1'));
    await waitFor(() => expect(store.getState().auth.profileLoaded).toBe(true));
    expect(store.getState().auth.role).toBe('student');
    expect(store.getState().auth.hfUsername).toBe('student42');
    expect(onProfile).toHaveBeenCalledWith(
      expect.objectContaining({ role: 'student' })
    );
    expect(mockSignOut).not.toHaveBeenCalled();
  });

  test('401 signs out (resetJetson + signOut + session/signedOut), no profileError', async () => {
    const store = makeStore();
    const err = new Error('expired');
    err.status = 401;
    mockGetMe.mockRejectedValue(err);

    renderHook(() => useMeProfile({}), { wrapper: wrapperFor(store) });

    await waitFor(() => expect(mockSignOut).toHaveBeenCalledTimes(1));
    expect(mockResetJetson).toHaveBeenCalled();
    expect(store.getState().auth.session).toBeNull(); // the broadcast reached authSlice
    expect(store.getState().auth.profileError).toBeNull();
  });

  test('404 sets a German profileError and does NOT sign out (valid JWT, missing row)', async () => {
    const store = makeStore();
    const err = new Error('no row');
    err.status = 404;
    mockGetMe.mockRejectedValue(err);

    renderHook(() => useMeProfile({}), { wrapper: wrapperFor(store) });

    await waitFor(() =>
      expect(store.getState().auth.profileError).toMatch(/Profil nicht gefunden/i)
    );
    expect(mockSignOut).not.toHaveBeenCalled();
    expect(store.getState().auth.profileLoaded).toBe(false);
  });
});

describe('useMeProfile — no HF identity auto-link any more', () => {
  // It used to PATCH /me with whatever Benutzer-ID the ROBOT's token reported:
  // an unverified name, on a shared PC the previous student's. The cloud now
  // sets users.hf_username from the token's own whoami (PUT /me/hf-token) and
  // answers PATCH /me with 409 once a token is stored.
  test('never PATCHes /me, whatever the robot reports and whatever the profile says', async () => {
    const store = makeStore({ hfUserList: ['student42'] });
    mockGetMe.mockResolvedValue({ role: 'student', hf_username: null });

    renderHook(() => useMeProfile({}), { wrapper: wrapperFor(store) });

    await waitFor(() => expect(store.getState().auth.profileLoaded).toBe(true));
    await act(async () => {
      store.dispatch(setHfUserList(['late-id']));
      await Promise.resolve();
    });
    expect(mockPatchHf).not.toHaveBeenCalled();
    expect(store.getState().auth.hfUsername).toBeNull();
  });

  test('ignores the retired enableHfLink option', async () => {
    const store = makeStore({ hfUserList: ['student42'] });
    mockGetMe.mockResolvedValue({ role: 'student', hf_username: null });

    renderHook(() => useMeProfile({ enableHfLink: true }), { wrapper: wrapperFor(store) });

    await waitFor(() => expect(store.getState().auth.profileLoaded).toBe(true));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockPatchHf).not.toHaveBeenCalled();
  });

  test('takes the linked name from /me and from nowhere else', async () => {
    const store = makeStore();
    mockGetMe.mockResolvedValue({ role: 'student', hf_username: 'anna-hf' });

    renderHook(() => useMeProfile({}), { wrapper: wrapperFor(store) });

    await waitFor(() => expect(store.getState().auth.hfUsername).toBe('anna-hf'));
    expect(mockPatchHf).not.toHaveBeenCalled();
  });
});
