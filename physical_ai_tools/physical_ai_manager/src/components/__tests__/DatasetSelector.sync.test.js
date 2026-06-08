// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the "Datensätze synchronisieren" escape hatch: clicking it calls
// POST /datasets/sync with the bearer token, refreshes the cloud list, and
// toasts the German result — and on a 400 (HF not linked) shows the German
// "Benutzer-ID verknüpfen" prompt instead. This is the student's recovery
// path when an auto-register on upload was missed.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DatasetSelector from '../DatasetSelector';

// react-redux: selector-aware stub + dispatch spy.
let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

// Cloud registry hook — controllable datasets/error + a refetch spy.
const mockRefetchCloud = vi.fn(() => Promise.resolve());
let mockGroupDatasets;
vi.mock('../../hooks/useGroupDatasets', () => ({
  __esModule: true,
  default: () => mockGroupDatasets,
}));

// ROS service caller — the component calls getUserList on mount. The
// returned object + its functions MUST be stable identities, or fetchUsers'
// useCallback churns each render and its effect re-fires forever (the
// component's real hook returns stable useCallbacks). Hoisted so the same
// instances back every render.
const mockRosCaller = vi.hoisted(() => {
  const getUserList = vi.fn(() =>
    Promise.resolve({ success: true, user_list: [] })
  );
  const getDatasetList = vi.fn(() =>
    Promise.resolve({ success: true, dataset_list: [] })
  );
  return { getUserList, getDatasetList };
});
vi.mock('../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRosCaller,
}));

// datasetsApi.syncDatasets — the unit under test.
const mockSync = vi.fn();
vi.mock('../../services/datasetsApi', () => ({
  __esModule: true,
  syncDatasets: (...a) => mockSync(...a),
}));

// vi.hoisted: the factory below is hoisted above all other code, so the
// toast spies it references must be created in the same hoisted scope (a
// plain `const mockToast` would be in the temporal dead zone when the
// hoisted factory runs). vi.hoisted is the idiomatic fix.
const mockToast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: mockToast,
}));

function baseState() {
  return {
    training: {
      userList: [],
      selectedUser: null,
      selectedDataset: null,
      isTraining: false,
    },
    auth: {
      session: { access_token: 'jwt-1' },
      hfUsername: 'student42',
    },
    tasks: { heartbeatStatus: 'connected' },
  };
}

beforeEach(() => {
  mockState = baseState();
  mockGroupDatasets = {
    datasets: [],
    error: null,
    refetch: mockRefetchCloud,
  };
  mockDispatch.mockClear();
  mockSync.mockReset();
  mockRefetchCloud.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
});

describe('DatasetSelector — Datensätze synchronisieren', () => {
  test('clicking sync POSTs with the token, refreshes, and toasts the count', async () => {
    mockSync.mockResolvedValue({
      registered: 2,
      already_known: 1,
      total_found: 3,
      hf_username: 'student42',
    });
    render(<DatasetSelector />);

    const btn = await screen.findByRole('button', {
      name: /Datensätze synchronisieren/i,
    });
    await userEvent.click(btn);

    await waitFor(() => expect(mockSync).toHaveBeenCalledWith('jwt-1'));
    expect(mockRefetchCloud).toHaveBeenCalled();
    await waitFor(() =>
      expect(mockToast.success).toHaveBeenCalledWith(
        expect.stringContaining('2 neue Datensätze synchronisiert')
      )
    );
  });

  test('zero new datasets toasts the German "keine neuen" message', async () => {
    mockSync.mockResolvedValue({
      registered: 0,
      already_known: 5,
      total_found: 5,
      hf_username: 'student42',
    });
    render(<DatasetSelector />);

    const btn = await screen.findByRole('button', {
      name: /Datensätze synchronisieren/i,
    });
    await userEvent.click(btn);

    await waitFor(() =>
      expect(mockToast.success).toHaveBeenCalledWith(
        'Keine neuen Datensätze gefunden'
      )
    );
  });

  test('a 400 (HF not linked) shows the German "Benutzer-ID verknüpfen" prompt', async () => {
    const err = new Error('not linked');
    err.status = 400;
    mockSync.mockRejectedValue(err);
    render(<DatasetSelector />);

    const btn = await screen.findByRole('button', {
      name: /Datensätze synchronisieren/i,
    });
    await userEvent.click(btn);

    await waitFor(() =>
      expect(mockToast.error).toHaveBeenCalledWith(
        expect.stringContaining('keine HuggingFace-ID')
      )
    );
  });

  test('surfaces the cloud list error in the selector', async () => {
    mockGroupDatasets = {
      datasets: [],
      error: 'Datensätze konnten nicht geladen werden. Bitte später erneut versuchen.',
      refetch: mockRefetchCloud,
    };
    render(<DatasetSelector />);
    expect(
      await screen.findByText(/Datensätze konnten nicht geladen werden/i)
    ).toBeInTheDocument();
  });
});
