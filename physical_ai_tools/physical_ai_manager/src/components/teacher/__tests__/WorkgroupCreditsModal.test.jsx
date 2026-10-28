// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// A group's training hours (credit system v2, spec §10.4/§10.6): the modal's
// tiles, the delta changes with a request_id each (never an absolute amount),
// the per-member limit, and the members' used hours; and the panel's wording.

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import toast from 'react-hot-toast';
import WorkgroupCreditsModal, { limitStateText } from '../WorkgroupCreditsModal';
import WorkgroupsPanel from '../WorkgroupsPanel';
import CreateWorkgroupModal from '../CreateWorkgroupModal';
import CreateStudentModal from '../CreateStudentModal';
import ClassroomDetail from '../ClassroomDetail';
import authReducer, { setSession } from '../../../features/auth/authSlice';
import teacherReducer from '../../../features/teacher/teacherSlice';
import * as api from '../../../services/workgroupsApi';

vi.mock('react-hot-toast', () => ({ __esModule: true, default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('../../../services/workgroupsApi', () => ({
  __esModule: true,
  changeWorkgroupHours: vi.fn(),
  setMemberLimit: vi.fn(),
  listWorkgroups: vi.fn(),
  getWorkgroup: vi.fn(),
  createWorkgroup: vi.fn(),
  deleteWorkgroup: vi.fn(),
  renameWorkgroup: vi.fn(),
  listWorkgroupTrainings: vi.fn(() => Promise.resolve([])),
  getWorkgroupCreditHistory: vi.fn(() => Promise.resolve({ entries: [], refusals: [] })),
  addWorkgroupMember: vi.fn(),
  removeWorkgroupMember: vi.fn(),
}));
vi.mock('../../../services/meApi', () => ({ __esModule: true, getMe: () => Promise.resolve({ pool_available: 4 }) }));
vi.mock('../../../services/teacherApi', () => ({
  __esModule: true,
  getClassroom: vi.fn(() => Promise.resolve({
    id: 'c1', name: '7b',
    students: [{ id: 's1', username: 'anna', full_name: 'Anna', hours_free: 2, hours_reserved: 0, hours_spent: 1 }],
  })),
  createStudent: vi.fn(),
  deleteClassroom: vi.fn(),
  renameClassroom: vi.fn(),
  deleteStudent: vi.fn(),
  patchStudent: vi.fn(),
  resetStudentPassword: vi.fn(),
  changeStudentHours: vi.fn(),
  getStudentCreditHistory: vi.fn(),
}));
vi.mock('../JetsonSection', () => ({ __esModule: true, default: () => null }));
vi.mock('../../../pages/teacher/WorkflowTemplatesPage', () => ({ __esModule: true, default: () => null }));

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const GROUP = {
  id: 'g1', name: 'Team Rot', member_count: 2,
  hours_free: 6, hours_reserved: 2, hours_spent: 5, member_limit_hours: null,
  members: [
    { id: 's1', full_name: 'Anna', username: 'anna', hours_used: 1 },
    { id: 's2', full_name: 'Ben', username: 'ben', hours_used: 3 },
  ],
};

function renderModal(over = {}, props = {}) {
  const onChanged = vi.fn();
  const onClose = vi.fn();
  render(
    <WorkgroupCreditsModal token="jwt" workgroup={{ ...GROUP, ...over }} poolAvailable={9}
      onClose={onClose} onChanged={onChanged} {...props} />,
  );
  return { onChanged, onClose };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('WorkgroupCreditsModal', () => {
  test('title, the three tiles, the pool line and the section names, verbatim', () => {
    renderModal();
    expect(screen.getByText('Stunden · Team Rot')).toBeInTheDocument();
    expect(screen.getByTestId('tile-free')).toHaveTextContent('Frei6');
    expect(screen.getByTestId('tile-reserved')).toHaveTextContent('Reserviert2');
    expect(screen.getByTestId('tile-spent')).toHaveTextContent('Verbraucht5');
    expect(screen.getByText('Lehrer-Pool frei: 9 Std.')).toBeInTheDocument();
    expect(screen.getByText('Stunden der Gruppe anpassen')).toBeInTheDocument();
    expect(screen.getByText('Stundenlimit pro Mitglied', { selector: 'h4' })).toBeInTheDocument();
    const FOOT = 'Enter weist zu · Schnellbuttons weisen sofort zu · Zurückholen geht nur mit freien Stunden; reservierte und verbrauchte Stunden bleiben.';
    expect(screen.getByText((_, el) => el?.tagName === 'P' && el.textContent.trim() === FOOT)).toBeInTheDocument();
    expect(screen.getByTitle('+5 Std. sofort zuweisen')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Credit/);
  });

  test('the members\' used hours, with „1 Stunde" for one', () => {
    renderModal();
    expect(screen.getByText('Anna: 1 Stunde genutzt')).toBeInTheDocument();
    expect(screen.getByText('Ben: 3 Stunden genutzt')).toBeInTheDocument();
  });

  test('a quick key assigns a delta with a fresh request_id and toasts the group\'s free hours', async () => {
    api.changeWorkgroupHours.mockResolvedValue({ new_amount: 11, pool_available: 4, hours_free: 11, hours_reserved: 2, hours_spent: 5 });
    const { onChanged } = renderModal();
    fireEvent.click(screen.getByTitle('+5 Std. sofort zuweisen'));
    await waitFor(() => expect(api.changeWorkgroupHours).toHaveBeenCalledTimes(1));
    const [token, gid, body] = api.changeWorkgroupHours.mock.calls[0];
    expect(token).toBe('jwt');
    expect(gid).toBe('g1');
    expect(Object.keys(body).sort()).toEqual(['delta', 'request_id']);
    expect(body).toMatchObject({ delta: 5 });
    expect(body.request_id).toMatch(UUID);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Freie Stunden der Gruppe: 11'));
    expect(onChanged.mock.calls[0][0]).toMatchObject({ hours_free: 11, hours_reserved: 2, hours_spent: 5 });
  });

  test('reclaim: the minus key sends a negative delta, at most the free hours', async () => {
    api.changeWorkgroupHours.mockResolvedValue({ new_amount: 2, hours_free: 2 });
    renderModal();
    const input = screen.getByRole('spinbutton', { name: 'Stunden' });
    fireEvent.change(input, { target: { value: '7' } });
    expect(screen.getByRole('button', { name: 'Max. 6 zurückholbar' })).toBeDisabled();
    fireEvent.change(input, { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Zurückholen' }));
    await waitFor(() => expect(api.changeWorkgroupHours).toHaveBeenCalledTimes(1));
    expect(api.changeWorkgroupHours.mock.calls[0][2].delta).toBe(-4);
  });

  test('no free hours: „Keine freien Stunden zum Zurückholen"', () => {
    renderModal({ hours_free: 0 });
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Stunden' }), { target: { value: '1' } });
    expect(screen.getByRole('button', { name: 'Keine freien Stunden zum Zurückholen' })).toBeDisabled();
  });

  test('two changes carry two request ids', async () => {
    api.changeWorkgroupHours.mockResolvedValue({ new_amount: 7, hours_free: 7 });
    renderModal();
    fireEvent.click(screen.getByTitle('+1 Std. sofort zuweisen'));
    await waitFor(() => expect(api.changeWorkgroupHours).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByTitle('+1 Std. sofort zuweisen'));
    await waitFor(() => expect(api.changeWorkgroupHours).toHaveBeenCalledTimes(2));
    expect(api.changeWorkgroupHours.mock.calls[0][2].request_id)
      .not.toBe(api.changeWorkgroupHours.mock.calls[1][2].request_id);
  });

  test('the member limit: state lines, a number, „Kein Limit", „Limit speichern"', async () => {
    expect(limitStateText(null)).toBe('Kein Limit gesetzt');
    expect(limitStateText(1)).toBe('Höchstens 1 Stunde pro Mitglied');
    expect(limitStateText(4)).toBe('Höchstens 4 Stunden pro Mitglied');

    api.setMemberLimit.mockResolvedValue({ member_limit_hours: 4 });
    const { onChanged } = renderModal();
    expect(screen.getByTestId('limit-state')).toHaveTextContent('Kein Limit gesetzt');
    const save = screen.getByRole('button', { name: 'Limit speichern' });
    expect(save).toBeDisabled(); // nothing changed
    fireEvent.click(screen.getByRole('checkbox', { name: 'Kein Limit' }));
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Stundenlimit pro Mitglied' }), { target: { value: '4' } });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(api.setMemberLimit).toHaveBeenCalledWith('jwt', 'g1', 4));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Limit gespeichert'));
    expect(onChanged.mock.calls[0][0]).toMatchObject({ member_limit_hours: 4 });
  });

  test('clearing the limit sends null; a value out of 1…1000 cannot be saved', async () => {
    api.setMemberLimit.mockResolvedValue({ member_limit_hours: null });
    renderModal({ member_limit_hours: 3 });
    expect(screen.getByTestId('limit-state')).toHaveTextContent('Höchstens 3 Stunden pro Mitglied');
    const field = screen.getByRole('spinbutton', { name: 'Stundenlimit pro Mitglied' });
    fireEvent.change(field, { target: { value: '0' } });
    expect(screen.getByRole('button', { name: 'Limit speichern' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Kein Limit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Limit speichern' }));
    await waitFor(() => expect(api.setMemberLimit).toHaveBeenCalledWith('jwt', 'g1', null));
  });
});

describe('WorkgroupsPanel, CreateWorkgroupModal, CreateStudentModal wording (spec §10.6)', () => {
  function renderPanel(groups) {
    api.listWorkgroups.mockResolvedValue(groups);
    api.getWorkgroup.mockResolvedValue(groups[0] ? { ...GROUP, ...groups[0] } : null);
    const store = configureStore({ reducer: { auth: authReducer, teacher: teacherReducer } });
    store.dispatch(setSession({ access_token: 'jwt', user: { id: 't1' } }));
    render(<Provider store={store}><WorkgroupsPanel classroomId="c1" classroomStudents={[]} /></Provider>);
  }

  test('no group yet: the hours sentence', async () => {
    renderPanel([]);
    expect(await screen.findByText(
      'Erstelle eine Arbeitsgruppe, um Stunden, Trainings und Datensätze für mehrere Schüler zu bündeln.',
    )).toBeInTheDocument();
  });

  test('a group: the „Std. frei" pill, the „Stunden" key, the free-hours tile and its history', async () => {
    renderPanel([{ id: 'g1', name: 'Team Rot', member_count: 2, hours_free: 6, hours_reserved: 2, hours_spent: 5 }]);
    expect(await screen.findByText('6 Std. frei')).toBeInTheDocument();
    expect(await screen.findByTitle('Stunden der Gruppe anpassen')).toHaveTextContent('Stunden');
    expect(screen.getByText('Freie Stunden der Gruppe')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('Stundenverlauf'));
    expect(await screen.findByRole('dialog', { name: 'Stundenverlauf · Team Rot' })).toBeInTheDocument();
    expect(api.getWorkgroupCreditHistory).toHaveBeenCalledWith('jwt', 'g1', { before: null, limit: 50 });
    expect(document.body.textContent).not.toMatch(/Credit/);
  });

  test('CreateWorkgroupModal and CreateStudentModal speak of hours', () => {
    render(<CreateWorkgroupModal onClose={vi.fn()} onSubmit={vi.fn()} />);
    expect(screen.getByText('Bis zu 10 Schüler teilen sich Stunden, Trainings, Datensätze und Workflows.'))
      .toBeInTheDocument();
    render(<CreateStudentModal onClose={vi.fn()} onSubmit={vi.fn()} poolAvailable={12} />);
    expect(screen.getByText('Start-Stunden')).toBeInTheDocument();
    expect(screen.getByText('Frei im Lehrer-Pool: 12 Std.')).toBeInTheDocument();
  });
});

describe('ClassroomDetail', () => {
  test('the hours column is „Stunden frei" and its cell reads the free hours', async () => {
    const store = configureStore({ reducer: { auth: authReducer, teacher: teacherReducer } });
    store.dispatch(setSession({ access_token: 'jwt', user: { id: 't1' } }));
    render(<Provider store={store}><ClassroomDetail classroomId="c1" /></Provider>);
    expect(await screen.findByRole('columnheader', { name: 'Stunden frei' })).toBeInTheDocument();
    expect(screen.getByText('2 Std. frei')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Credit/);
  });
});
