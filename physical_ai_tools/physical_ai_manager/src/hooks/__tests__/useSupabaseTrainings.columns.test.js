// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The direct live read of `trainings` names its columns (RunPod spec §4.11).
// Since migration 047 the request roles may SELECT only the columns granted to
// them: a `select=*` (or any read naming `worker_token`) answers 42501, and a
// column added later is readable only once its own migration grants it (049
// grants `status_note`). So the list is explicit, never `*`, never the token;
// `status_note` is what carries the German „GPU-Server ausgefallen …" line to
// the live chart. robotis_ai_setup/tests/test_managed_training_lockstep.py
// checks every name here against the GRANT lists of 047 and 049.

import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';

const mockSelect = vi.fn();
const mockGetTrainingJobs = vi.fn();

vi.mock('../../lib/supabaseClient', () => {
  const query = {
    or: () => query,
    eq: () => query,
    order: () => Promise.resolve({ data: [], error: null }),
  };
  const channel = { on: () => channel, subscribe: () => channel };
  return {
    __esModule: true,
    supabase: {
      from: () => ({ select: (cols) => { mockSelect(cols); return query; } }),
      channel: () => channel,
      removeChannel: () => {},
    },
  };
});
vi.mock('../../services/cloudTrainingApi', () => ({
  __esModule: true,
  getTrainingJobs: (...args) => mockGetTrainingJobs(...args),
}));

// eslint-disable-next-line import/first
import useSupabaseTrainings, { LIVE_COLUMNS } from '../useSupabaseTrainings';

const EXPECTED = [
  'id', 'status', 'model_name', 'model_type', 'dataset_name', 'current_step', 'total_steps',
  'current_loss', 'loss_history', 'error_message', 'requested_at', 'terminated_at',
  'last_progress_at', 'workgroup_id', 'status_note',
];

describe('useSupabaseTrainings LIVE_COLUMNS', () => {
  test('is the explicit list, with status_note, never * and never the token', () => {
    expect(LIVE_COLUMNS.split(',')).toEqual(EXPECTED);
    expect(LIVE_COLUMNS).not.toMatch(/\*/);
    expect(LIVE_COLUMNS).not.toMatch(/worker_token|cloud_job_id|user_id/);
    expect(LIVE_COLUMNS).not.toMatch(/\s/);
  });

  test('is the only column list the hook ever selects', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'useSupabaseTrainings.js'), 'utf8');
    const selects = src.match(/\.select\(([^)]*)\)/g) || [];
    expect(selects).toEqual(['.select(LIVE_COLUMNS)']);
  });

  test('the live poll of an active job reads exactly LIVE_COLUMNS', async () => {
    vi.useFakeTimers();
    try {
      mockSelect.mockClear();
      mockGetTrainingJobs.mockResolvedValue([
        { id: 1, status: 'running', requested_at: '2026-10-18T10:00:00Z' },
      ]);
      const store = configureStore({
        reducer: {
          auth: () => ({ session: { access_token: 't', user: { id: 'u1' } }, workgroupId: null }),
        },
      });
      const wrapper = ({ children }) => React.createElement(Provider, { store }, children);
      renderHook(() => useSupabaseTrainings(), { wrapper });
      await act(async () => { await Promise.resolve(); });
      await act(async () => { vi.advanceTimersByTime(6000); await Promise.resolve(); });
      expect(mockSelect).toHaveBeenCalled();
      for (const call of mockSelect.mock.calls) {
        expect(call[0]).toBe(LIVE_COLUMNS);
      }
    } finally {
      vi.useRealTimers();
    }
  });
});
