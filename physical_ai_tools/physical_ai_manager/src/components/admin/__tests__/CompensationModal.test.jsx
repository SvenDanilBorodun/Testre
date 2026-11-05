// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// „Ausgleich gutschreiben" (owner decision D4, spec §10.5) and „Grund löschen"
// (review m11), down to the request the API receives: the admin API's wire
// client (`apiRequest`) is the one thing stubbed, so the paths, methods and
// bodies below are the ones services/adminApi.js really sends.
//
// * The compensation is preset from a charge row (its paying account, its
//   training, its charged hours) and can NEVER be sent with a blank reason.
// * Only a charge with hours > 0 offers it; a row with a reason offers
//   „Grund löschen", whose erasure leaves the booking and shows „(gelöscht)".

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import toast from 'react-hot-toast';
import CompensationModal from '../CompensationModal';
import TeacherRow from '../TeacherRow';
import authReducer, { setSession } from '../../../features/auth/authSlice';
import adminReducer from '../../../features/admin/adminSlice';
import { changeTeacherCredits, getCreditHistory } from '../../../services/adminApi';

vi.mock('react-hot-toast', () => ({ __esModule: true, default: { success: vi.fn(), error: vi.fn() } }));
const mockRequest = vi.fn();
vi.mock('../../../services/apiClient', () => ({
  __esModule: true,
  apiRequest: (...a) => mockRequest(...a),
}));

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const PRESET = { target_kind: 'group', target_id: '6f1c0000-0000-4000-8000-000000000001', training_id: 41, hours: 2 };

beforeEach(() => {
  vi.clearAllMocks();
  mockRequest.mockResolvedValue({});
});

describe('CompensationModal', () => {
  function renderModal(preset = PRESET) {
    const onClose = vi.fn();
    const onDone = vi.fn();
    render(<CompensationModal token="jwt" preset={preset} onClose={onClose} onDone={onDone} />);
    return { onClose, onDone };
  }

  test('every sentence verbatim; the hours default to the charged hours', () => {
    renderModal();
    expect(screen.getByText('Ausgleich gutschreiben')).toBeInTheDocument();
    expect(screen.getByText(
      'Die Stunden werden direkt dem Guthaben gutgeschrieben, das das Training bezahlt hat – nicht aus dem Lehrer-Pool.',
    )).toBeInTheDocument();
    expect(screen.getByText('Stunden')).toBeInTheDocument();
    expect(screen.getByText('Grund (Pflicht)')).toBeInTheDocument();
    expect(screen.getByText(
      'Die Lehrkraft sieht diesen Grund im Stundenverlauf. Ohne Namen von Schülerinnen und Schülern.',
    )).toBeInTheDocument();
    expect(screen.getByRole('spinbutton')).toHaveValue(2);
    expect(screen.getByRole('button', { name: 'Zurück' })).toBeEnabled();
  });

  test('„Gutschreiben" stays off while the reason is blank — whitespace included — and Enter sends nothing', () => {
    renderModal();
    const send = screen.getByRole('button', { name: 'Gutschreiben' });
    expect(send).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '    ' } });
    expect(send).toBeDisabled();
    fireEvent.submit(screen.getByRole('textbox'));
    expect(mockRequest).not.toHaveBeenCalled();
  });

  test('hours outside 1…100 cannot be sent', () => {
    renderModal();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'GPU-Ausfall' } });
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '101' } });
    expect(screen.getByRole('button', { name: 'Gutschreiben' })).toBeDisabled();
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '0' } });
    expect(screen.getByRole('button', { name: 'Gutschreiben' })).toBeDisabled();
  });

  test('sends POST /admin/credit-grants with the preset, the trimmed reason and a request_id', async () => {
    mockRequest.mockResolvedValue({ target_kind: 'group', target_id: PRESET.target_id, hours_free: 7 });
    const { onClose, onDone } = renderModal();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '  GPU-Ausfall am Montag  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Gutschreiben' }));
    await waitFor(() => expect(mockRequest).toHaveBeenCalledTimes(1));
    const [path, method, token, body] = mockRequest.mock.calls[0];
    expect(path).toBe('/admin/credit-grants');
    expect(method).toBe('POST');
    expect(token).toBe('jwt');
    expect(body).toMatchObject({
      target_kind: 'group', target_id: PRESET.target_id, training_id: 41, hours: 2, reason: 'GPU-Ausfall am Montag',
    });
    expect(body.request_id).toMatch(UUID);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Ausgleich gutgeschrieben: 2 Stunden'));
    expect(onDone).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  test('one hour reads „1 Stunde"', async () => {
    renderModal({ ...PRESET, hours: 1 });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Gutschreiben' }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Ausgleich gutgeschrieben: 1 Stunde'));
  });

  test('an API refusal shows its German detail; a retry of the same change reuses the request_id', async () => {
    const err = new Error('x');
    err.detail = 'Dieses Training wurde nicht aus diesem Guthaben bezahlt.';
    mockRequest.mockRejectedValueOnce(err).mockResolvedValueOnce({});
    renderModal();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Grund' } });
    fireEvent.click(screen.getByRole('button', { name: 'Gutschreiben' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(err.detail);
    fireEvent.click(screen.getByRole('button', { name: 'Gutschreiben' }));
    await waitFor(() => expect(mockRequest).toHaveBeenCalledTimes(2));
    expect(mockRequest.mock.calls[1][3].request_id).toBe(mockRequest.mock.calls[0][3].request_id);
  });
});

describe('the admin API sends deltas and ids, never an absolute amount', () => {
  test('changeTeacherCredits: POST with delta, request_id, and a reason only when there is one', async () => {
    await changeTeacherCredits('jwt', 't1', { delta: -3, reason: '  ', request_id: 'r1' });
    expect(mockRequest).toHaveBeenLastCalledWith('/admin/teachers/t1/credits', 'POST', 'jwt', { delta: -3, request_id: 'r1' });
    await changeTeacherCredits('jwt', 't1', { delta: 5, reason: ' Neues Halbjahr ', request_id: 'r2' });
    expect(mockRequest).toHaveBeenLastCalledWith('/admin/teachers/t1/credits', 'POST', 'jwt',
      { delta: 5, request_id: 'r2', reason: 'Neues Halbjahr' });
  });

  test('getCreditHistory builds the scope query', async () => {
    await getCreditHistory('jwt', { scope: 'teacher_all', teacher_id: 't1', before: 99, limit: 50 });
    expect(mockRequest).toHaveBeenLastCalledWith(
      '/admin/credit-history?scope=teacher_all&teacher_id=t1&before=99&limit=50', 'GET', 'jwt',
    );
  });
});

describe('the admin history of a teacher', () => {
  const TEACHER = {
    id: 't1', username: 'frau.x', full_name: 'Frau X', classroom_count: 1, student_count: 3,
    pool_available: 4, allocated_total: 6, hours_spent: 2,
  };
  const ENTRIES = [
    { id: 30, at: '2026-10-28T10:00:00Z', kind: 'charge', hours: 2, account_kind: 'student',
      account_id: 's-1', training_id: 41, member_id: 's-1', actor_role: 'system', model_name: 'EduBotics-Solutions/act-1' },
    { id: 29, at: '2026-10-28T09:00:00Z', kind: 'charge', hours: 0, account_kind: 'student',
      account_id: 's-1', training_id: 40, actor_role: 'system' },
    { id: 28, at: '2026-10-28T08:00:00Z', kind: 'grant', hours: 10, account_kind: 'teacher',
      account_id: 't1', actor_role: 'admin', reason: 'Schuljahr 26/27', reason_id: 7 },
  ];

  function renderRow() {
    mockRequest.mockImplementation((path) => (
      path.startsWith('/admin/credit-history') ? Promise.resolve({ entries: ENTRIES }) : Promise.resolve({ erased: true })
    ));
    const store = configureStore({ reducer: { auth: authReducer, admin: adminReducer } });
    store.dispatch(setSession({ access_token: 'jwt', user: { id: 'a1' } }));
    render(
      <Provider store={store}>
        <table><tbody><TeacherRow teacher={TEACHER} onChanged={vi.fn()} /></tbody></table>
      </Provider>,
    );
  }

  test('„Verlauf" asks for the teacher_all scope', async () => {
    renderRow();
    fireEvent.click(screen.getByRole('button', { name: /Verlauf/ }));
    await screen.findByTestId('credit-row-30');
    expect(mockRequest.mock.calls[0][0]).toBe('/admin/credit-history?scope=teacher_all&teacher_id=t1&limit=50');
  });

  test('only a charge with hours > 0 offers „Ausgleich gutschreiben", preset from its row', async () => {
    renderRow();
    fireEvent.click(screen.getByRole('button', { name: /Verlauf/ }));
    const charged = await screen.findByTestId('credit-row-30');
    expect(within(screen.getByTestId('credit-row-29')).queryByRole('button', { name: 'Ausgleich gutschreiben' })).toBeNull();
    expect(within(screen.getByTestId('credit-row-28')).queryByRole('button', { name: 'Ausgleich gutschreiben' })).toBeNull();
    fireEvent.click(within(charged).getByRole('button', { name: 'Ausgleich gutschreiben' }));
    expect(screen.getByRole('spinbutton')).toHaveValue(2);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Fehlerhafte Abrechnung' } });
    fireEvent.click(screen.getByRole('button', { name: 'Gutschreiben' }));
    await waitFor(() => expect(mockRequest.mock.calls.some(([p]) => p === '/admin/credit-grants')).toBe(true));
    const body = mockRequest.mock.calls.find(([p]) => p === '/admin/credit-grants')[3];
    expect(body).toMatchObject({ target_kind: 'student', target_id: 's-1', training_id: 41, hours: 2 });
  });

  test('„Grund löschen": confirm, DELETE the reason by id, the row then reads „(gelöscht)"', async () => {
    renderRow();
    fireEvent.click(screen.getByRole('button', { name: /Verlauf/ }));
    const grant = await screen.findByTestId('credit-row-28');
    expect(grant).toHaveTextContent('Grund: Schuljahr 26/27');
    expect(within(screen.getByTestId('credit-row-30')).queryByRole('button', { name: 'Grund löschen' })).toBeNull();
    fireEvent.click(within(grant).getByRole('button', { name: 'Grund löschen' }));
    expect(screen.getByText('Den Grund endgültig löschen? Die Buchung selbst bleibt.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Zurück' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Löschen' }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Grund gelöscht.'));
    expect(mockRequest).toHaveBeenCalledWith('/admin/credit-reasons/7', 'DELETE', 'jwt');
    await waitFor(() => expect(screen.getByTestId('credit-row-28')).toHaveTextContent('Grund: (gelöscht)'));
    expect(screen.getByTestId('credit-row-28')).not.toHaveTextContent('Schuljahr');
  });
});
