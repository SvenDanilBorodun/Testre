// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
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
vi.mock('../../features/editDataset/hooks/useDatasetLocalChanges', () => ({ __esModule: true, default: () => null }));

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
      hoursFree: null,
      hoursReserved: null,
      nextReservationHours: null,
      account: null,
      memberLimitHours: null,
      memberLeftHours: null,
      ...overrides.auth,
    },
    training: {
      selectedTrainingId: null,
      cloudJobsRefreshCounter: 0,
      trainingInfo: { datasetRepoId: null },
      ...overrides.training,
    },
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

// ── the loaded page: the training-hour readout (credit system v2, spec §10.1) ──

function loaded(auth) {
  return baseState({
    auth: { isAuthenticated: true, profileLoaded: true, session: { access_token: 't', user: { email: 'a@b.c' } }, ...auth },
  });
}

describe('TrainingPage training hours', () => {
  test('the readout: the free hours, „Std. frei" and the reserved ones', () => {
    mockState = loaded({ hoursFree: 4, hoursReserved: 2, nextReservationHours: 3, account: 'student' });
    render(<TrainingPage />);
    const readout = screen.getByTestId('hours-readout');
    expect(readout).toHaveTextContent('4');
    expect(readout).toHaveTextContent('Std. frei · 2 Std. reserviert');
    expect(readout).not.toHaveTextContent('Gruppenguthaben');
    expect(document.body.textContent).not.toMatch(/Trainingsguthaben|Credit/);
  });

  test('no reserved hours: the caption is „Std. frei" alone', () => {
    mockState = loaded({ hoursFree: 5, hoursReserved: 0, nextReservationHours: 3, account: 'student' });
    render(<TrainingPage />);
    expect(screen.getByText('Std. frei')).toBeInTheDocument();
  });

  test('in a group: „· Gruppenguthaben" and the member-limit line with „1 Stunde"', () => {
    mockState = loaded({
      hoursFree: 9, hoursReserved: 0, nextReservationHours: 1, account: 'group',
      memberLimitHours: 4, memberLeftHours: 1,
    });
    render(<TrainingPage />);
    expect(screen.getByText('Std. frei · Gruppenguthaben')).toBeInTheDocument();
    expect(screen.getByText('Dein Gruppenlimit: noch 1 Stunde')).toBeInTheDocument();
  });

  test('tones: 0 → rec, ≤ 2 → warn, else ok (on the next reservation)', () => {
    const tone = (n) => {
      mockState = loaded({ hoursFree: n, hoursReserved: 0, nextReservationHours: n });
      const { unmount } = render(<TrainingPage />);
      const cls = screen.getByTestId('hours-free').className;
      unmount();
      return cls;
    };
    expect(tone(0)).toBe('su-read--rec');
    expect(tone(2)).toBe('su-read--warn');
    expect(tone(3)).toBe('su-read--ok');
  });

  test('no free hours: the page notice, verbatim', () => {
    mockState = loaded({ hoursFree: 0, hoursReserved: 0, nextReservationHours: 0, account: 'student' });
    render(<TrainingPage />);
    expect(screen.getByText('Keine freien Trainingsstunden. Frag deine Lehrkraft nach weiteren Stunden.'))
      .toBeInTheDocument();
  });

  test('the member limit as the cause: its own sentence', () => {
    mockState = loaded({
      hoursFree: 6, hoursReserved: 0, nextReservationHours: 0, account: 'group',
      memberLimitHours: 2, memberLeftHours: 0,
    });
    render(<TrainingPage />);
    expect(screen.getByText('Du hast dein Stundenlimit in der Arbeitsgruppe erreicht. Frag deine Lehrkraft.'))
      .toBeInTheDocument();
    expect(screen.getByText('Dein Gruppenlimit: noch 0 Stunden')).toBeInTheDocument();
  });

  test('before the quota answered: „—" and no „no hours" claim', () => {
    mockState = loaded({});
    render(<TrainingPage />);
    expect(screen.getByTestId('hours-readout')).toHaveTextContent('—');
    expect(screen.queryByText(/Keine freien Trainingsstunden/)).toBeNull();
  });
});
