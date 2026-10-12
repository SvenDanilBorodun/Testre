// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Cloud-Inferenz (spec §6.8): the teacher drawer's run list says where each run
// ran — „Cloud" or „Klassen-Jetson" — under the heading „Inferenz-Läufe". A run
// from an older API carries no `backend` and reads as the Jetson (every run was
// the Jetson's then). Teachers read cloud runs; they cannot start one.

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import StudentTrainingHistoryDrawer from '../StudentTrainingHistoryDrawer';

vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel({ auth: { session: { access_token: 'jwt-teacher' } } }),
}));
vi.mock('react-hot-toast', () => ({ __esModule: true, default: { error: vi.fn(), success: vi.fn() } }));

const api = vi.hoisted(() => ({
  listStudentTrainings: vi.fn(() => Promise.resolve([])),
  listStudentInferenceRuns: vi.fn(),
  listTrainingCheckpoints: vi.fn(() => Promise.resolve({ steps: [] })),
}));
vi.mock('../../../services/teacherApi', () => ({ __esModule: true, ...api }));

const RUNS = [
  { id: 3, policy_repo: 'EduBotics-Solutions/cloud-run', started_at: null, duration_s: 61, exit_reason: 'error',
    error_message_de: 'Die Verbindung zur Cloud war zu lange unterbrochen.', created_at: '2026-10-11T10:00:00Z', backend: 'cloud' },
  { id: 2, policy_repo: 'EduBotics-Solutions/jetson-run', started_at: null, duration_s: 30, exit_reason: 'finished',
    error_message_de: '', created_at: '2026-10-10T10:00:00Z', backend: 'jetson' },
  { id: 1, policy_repo: 'EduBotics-Solutions/old-api-run', started_at: null, duration_s: 12, exit_reason: 'stopped',
    error_message_de: '', created_at: '2026-10-09T10:00:00Z' },
];

describe('StudentTrainingHistoryDrawer — the backend of each inference run', () => {
  it('heads the list „Inferenz-Läufe" and tags every run Cloud or Klassen-Jetson', async () => {
    api.listStudentInferenceRuns.mockResolvedValue(RUNS);
    render(<StudentTrainingHistoryDrawer student={{ id: 's1', full_name: 'Lena', username: 'lena' }} onClose={() => {}} />);
    expect(await screen.findByText('Inferenz-Läufe')).toBeInTheDocument();
    expect(screen.queryByText('Inferenz-Läufe (Klassen-Jetson)')).toBeNull();
    const row = (id) => within(screen.getByTestId(`inference-run-${id}`));
    expect(row(3).getByText('EduBotics-Solutions/cloud-run')).toBeInTheDocument();
    expect(row(3).getByText('Cloud')).toBeInTheDocument();
    expect(row(2).getByText('Klassen-Jetson')).toBeInTheDocument();
    expect(row(1).getByText('Klassen-Jetson')).toBeInTheDocument();
    expect(row(3).getByText('Die Verbindung zur Cloud war zu lange unterbrochen.')).toBeInTheDocument();
  });
});
