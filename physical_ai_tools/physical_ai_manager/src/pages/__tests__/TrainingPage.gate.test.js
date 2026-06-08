// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the Training tab's auth/profile GATING — the bug class this PR
// hardens. Order matters:
//   isLoading      → "Laden…"
//   !isAuthenticated → <LoginForm/>
//   profileError    → error card + "Erneut versuchen"
//   !profileLoaded  → "Profil wird geladen…" (the InferencePage:141 parity)
// The heavy children + cloud/ROS hooks are stubbed so only the gating render
// is exercised; the gates return BEFORE any child mounts in the cases tested.

import React from 'react';
import { render, screen } from '@testing-library/react';
import TrainingPage from '../TrainingPage';

// ── react-redux: selector-aware stub over a mutable module-level state ──
let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

// react-hot-toast: the page imports `toast` + `useToasterStore`.
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { dismiss: vi.fn(), success: vi.fn(), error: vi.fn() },
  useToasterStore: () => ({ toasts: [] }),
}));

// supabaseClient: pure stub so importing it doesn't touch the network/env.
vi.mock('../../lib/supabaseClient', () => ({
  __esModule: true,
  supabase: { auth: { signOut: vi.fn() } },
}));

// Cloud quota call — never resolves in these gating cases, but stub anyway.
vi.mock('../../services/cloudTrainingApi', () => ({
  __esModule: true,
  getQuota: vi.fn(() => Promise.resolve({ training_credits: 0, trainings_used: 0 })),
}));

// Hooks the page mounts unconditionally (before the gates).
vi.mock('../../hooks/useSupabaseTrainings', () => ({
  __esModule: true,
  default: () => ({ jobs: [], loading: false, refetch: vi.fn(), isRealtime: false }),
}));
vi.mock('../../hooks/useRefetchOnFocus', () => ({
  __esModule: true,
  default: () => {},
}));
vi.mock('../../hooks/useJetsonConnection', () => ({
  __esModule: true,
  resetJetsonOnLogout: vi.fn(),
}));

// Recognizable LoginForm stub.
vi.mock('../../components/LoginForm', () => ({
  __esModule: true,
  default: () => <div data-testid="login-form">LOGIN</div>,
}));

// Heavy children — only reached in the CONNECTED path (not the gates tested
// here), but stubbed so the module graph stays light and import-safe.
vi.mock('../../components/DatasetSelector', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/PolicySelector', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/TrainingOutputFolderInput', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/TrainingControlPanel', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/TrainingOptionInput', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/TrainingLiveChart', () => ({
  __esModule: true,
  default: () => <div />,
  pickSelectedJob: () => null,
}));
vi.mock('../../components/MyModels', () => ({ __esModule: true, default: () => <div /> }));
vi.mock('../../components/HeartbeatStatus', () => ({ __esModule: true, default: () => <div /> }));

function baseState(overrides = {}) {
  return {
    auth: {
      isAuthenticated: false,
      isLoading: false,
      profileLoaded: false,
      profileError: null,
      hfUsername: null,
      session: null,
      trainingCredits: 0,
      trainingsUsed: 0,
      ...overrides.auth,
    },
    training: {
      selectedTrainingId: null,
      cloudJobsRefreshCounter: 0,
      ...overrides.training,
    },
    jetson: { jetsonId: null, ...overrides.jetson },
  };
}

beforeEach(() => {
  mockDispatch.mockClear();
});

describe('TrainingPage auth/profile gating', () => {
  test('shows the LoginForm when not authenticated', () => {
    mockState = baseState({ auth: { isAuthenticated: false } });
    render(<TrainingPage />);
    expect(screen.getByTestId('login-form')).toBeInTheDocument();
  });

  test('shows the "Profil wird geladen…" spinner when authenticated but profile not loaded', () => {
    mockState = baseState({
      auth: { isAuthenticated: true, profileLoaded: false, profileError: null },
    });
    render(<TrainingPage />);
    expect(screen.getByText('Profil wird geladen…')).toBeInTheDocument();
    expect(screen.queryByTestId('login-form')).not.toBeInTheDocument();
  });

  test('shows the error card with a retry button when profileError is set', () => {
    mockState = baseState({
      auth: {
        isAuthenticated: true,
        profileLoaded: false,
        profileError: 'Server nicht erreichbar – bitte erneut versuchen.',
      },
    });
    render(<TrainingPage />);
    expect(
      screen.getByText(/Profil konnte nicht geladen werden/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Erneut versuchen/i })
    ).toBeInTheDocument();
  });

  test('shows "Laden…" while the auth bootstrap is still loading', () => {
    mockState = baseState({ auth: { isLoading: true } });
    render(<TrainingPage />);
    expect(screen.getByText('Laden…')).toBeInTheDocument();
  });
});
