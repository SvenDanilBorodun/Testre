// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Covers the robust /me loader + HF auto-link — the heart of this PR.
// Cases: success, 401 → sign-out, 404 → profileError (NO sign-out), and the
// HF auto-link (PATCH /me once with the Benutzer-ID, including the case where
// the Benutzer-ID arrives AFTER /me).
//
// Backed by a REAL Redux store (auth + ui reducers) + <Provider>, so
// dispatched actions properly re-render the subscribed selectors and re-run
// the auto-link effect — exactly like the live app, minus the rest of the
// store. Only the network/auth side effects are mocked.

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
vi.mock('../../hooks/useJetsonConnection', () => ({
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

// This jsdom config runs WITHOUT a backing localStorage file, so the real
// `localStorage.setItem` throws (the hook's resolveBenutzerId() try/catches
// this — safe in prod). For the persisted-Benutzer-ID test we install a tiny
// in-memory Storage so the read path is exercised; helper installs/clears it.
function withMemoryLocalStorage(entries) {
  const map = new Map(Object.entries(entries));
  const fake = {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
  };
  const original = Object.getOwnPropertyDescriptor(window, 'localStorage');
  Object.defineProperty(window, 'localStorage', {
    value: fake,
    configurable: true,
  });
  return () => {
    if (original) Object.defineProperty(window, 'localStorage', original);
    else delete window.localStorage;
  };
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

  test('401 signs out (resetJetson + signOut + clearSession), no profileError', async () => {
    const store = makeStore();
    const err = new Error('expired');
    err.status = 401;
    mockGetMe.mockRejectedValue(err);

    renderHook(() => useMeProfile({}), { wrapper: wrapperFor(store) });

    await waitFor(() => expect(mockSignOut).toHaveBeenCalledTimes(1));
    expect(mockResetJetson).toHaveBeenCalled();
    expect(store.getState().auth.session).toBeNull(); // clearSession ran
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

describe('useMeProfile — HF identity auto-link', () => {
  test('links once when profile loads with null hf_username and a sole whoami entry', async () => {
    const store = makeStore({ hfUserList: ['student42'] });
    mockGetMe.mockResolvedValue({ role: 'student', hf_username: null });
    mockPatchHf.mockResolvedValue({ role: 'student', hf_username: 'student42' });

    renderHook(() => useMeProfile({ enableHfLink: true }), {
      wrapper: wrapperFor(store),
    });

    await waitFor(() =>
      expect(mockPatchHf).toHaveBeenCalledWith('jwt-1', 'student42')
    );
    await waitFor(() =>
      expect(store.getState().auth.hfUsername).toBe('student42')
    );
    // Exactly once — the one-shot guard prevents a loop.
    expect(mockPatchHf).toHaveBeenCalledTimes(1);
  });

  test('does NOT link when hf_username is already set', async () => {
    const store = makeStore({ hfUserList: ['student42'] });
    mockGetMe.mockResolvedValue({ role: 'student', hf_username: 'already' });

    renderHook(() => useMeProfile({ enableHfLink: true }), {
      wrapper: wrapperFor(store),
    });

    await waitFor(() => expect(store.getState().auth.profileLoaded).toBe(true));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockPatchHf).not.toHaveBeenCalled();
  });

  test('links the persisted Benutzer-ID even before the whoami list resolves', async () => {
    const restore = withMemoryLocalStorage({
      edubotics_userId: 'persisted-id',
    });
    try {
      const store = makeStore({ hfUserList: [] }); // whoami not loaded yet
      mockGetMe.mockResolvedValue({ role: 'student', hf_username: null });
      mockPatchHf.mockResolvedValue({
        role: 'student',
        hf_username: 'persisted-id',
      });

      renderHook(() => useMeProfile({ enableHfLink: true }), {
        wrapper: wrapperFor(store),
      });

      await waitFor(() =>
        expect(mockPatchHf).toHaveBeenCalledWith('jwt-1', 'persisted-id')
      );
    } finally {
      restore();
    }
  });

  test('auto-links when the whoami list arrives AFTER /me (sole entry)', async () => {
    const store = makeStore({ hfUserList: [] }); // empty at load
    mockGetMe.mockResolvedValue({ role: 'student', hf_username: null });
    mockPatchHf.mockResolvedValue({ role: 'student', hf_username: 'late-id' });

    renderHook(() => useMeProfile({ enableHfLink: true }), {
      wrapper: wrapperFor(store),
    });

    await waitFor(() => expect(store.getState().auth.profileLoaded).toBe(true));
    expect(mockPatchHf).not.toHaveBeenCalled(); // nothing to link yet

    // Benutzer-ID resolves later (ROS whoami) → the effect must react.
    act(() => {
      store.dispatch(setHfUserList(['late-id']));
    });

    await waitFor(() =>
      expect(mockPatchHf).toHaveBeenCalledWith('jwt-1', 'late-id')
    );
  });

  test('does not auto-link when enableHfLink is false (teacher web)', async () => {
    const store = makeStore({ hfUserList: ['student42'] });
    mockGetMe.mockResolvedValue({ role: 'teacher', hf_username: null });

    renderHook(() => useMeProfile({ enableHfLink: false }), {
      wrapper: wrapperFor(store),
    });

    await waitFor(() => expect(store.getState().auth.profileLoaded).toBe(true));
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockPatchHf).not.toHaveBeenCalled();
  });
});
