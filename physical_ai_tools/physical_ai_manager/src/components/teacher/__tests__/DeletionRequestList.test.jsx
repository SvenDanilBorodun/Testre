// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The teacher's (and the admin's) queue of open GDPR deletion requests (launch
// plan step 5): every request with its deadline, a deletion only after the
// German confirmation dialog, and no way to decline.

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DeletionRequestList from '../DeletionRequestList';
import { DELETION_DE } from '../../../utils/deletionRequest';

const ANNA = {
  id: 'stu-anna',
  name: 'Anna Müller',
  username: 'anna',
  role: 'student',
  classroomName: '7a',
  requestedAt: '2026-11-03T10:00:00+00:00',
  dueBy: '2026-12-03T10:00:00+00:00',
};
const LATE = { ...ANNA, id: 'stu-ben', name: 'Ben Berg', username: 'ben', dueBy: '2026-11-01T10:00:00+00:00' };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-11-20T12:00:00Z'));
});
afterEach(() => vi.useRealTimers());

describe('DeletionRequestList', () => {
  it('lists each request with its date and deadline under the Art. 12(3) note', () => {
    render(<DeletionRequestList requests={[ANNA, LATE]} onConfirm={vi.fn()} />);
    expect(screen.getByTestId('deletion-deadline-note').textContent).toContain('Art. 12 Abs. 3 DSGVO');
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText(/Anna Müller/)).toBeTruthy();
    expect(within(items[0]).getByText(/Beantragt am 03\.11\.2026/)).toBeTruthy();
    expect(within(items[0]).getByText('fällig bis 03.12.2026')).toBeTruthy();
    expect(within(items[1]).getByText('überfällig seit 01.11.2026')).toBeTruthy();
    expect(screen.queryByText(/ablehn/i)).toBeNull();
  });

  it('deletes only after the confirmation dialog, and a cancel deletes nothing', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(<DeletionRequestList requests={[ANNA]} onConfirm={onConfirm} />);
    await userEvent.click(screen.getByRole('button', { name: /Löschung ausführen/ }));
    const dialogText = screen.getByText(DELETION_DE.confirmBody('Anna Müller'));
    expect(dialogText).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Abbrechen' }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.queryByText(DELETION_DE.confirmBody('Anna Müller'))).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: /Löschung ausführen/ }));
    await userEvent.click(screen.getByRole('button', { name: /Endgültig löschen/ }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
    expect(onConfirm.mock.calls[0][0].id).toBe('stu-anna');
    await waitFor(() => expect(screen.queryByText(DELETION_DE.confirmBody('Anna Müller'))).toBeNull());
  });

  it('says so when nothing is open, and shows a load failure', () => {
    const { rerender } = render(<DeletionRequestList requests={[]} onConfirm={vi.fn()} />);
    expect(screen.getByText(DELETION_DE.none)).toBeTruthy();
    rerender(<DeletionRequestList requests={[]} onConfirm={vi.fn()} error={DELETION_DE.loadFailed} />);
    expect(screen.getByRole('alert').textContent).toBe(DELETION_DE.loadFailed);
  });

  it('the admin view names the usual teacher, a missing classroom and a teacher account', () => {
    render(
      <DeletionRequestList
        showTeacher
        onConfirm={vi.fn()}
        requests={[
          { ...ANNA, teacherName: 'Frau Lehrer' },
          { ...ANNA, id: 'emil', name: 'Emil', classroomName: null },
          { ...ANNA, id: 't3', name: 'Herr Gegangen', role: 'teacher', classroomName: null },
        ]}
      />
    );
    const items = screen.getAllByRole('listitem');
    expect(within(items[0]).getByText(/Frau Lehrer/)).toBeTruthy();
    expect(within(items[1]).getByText(DELETION_DE.noClassroom)).toBeTruthy();
    expect(within(items[2]).getByText(DELETION_DE.teacherAccount)).toBeTruthy();
  });
});
