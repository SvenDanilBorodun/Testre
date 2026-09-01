// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The GDPR honesty fix has to reach a HUMAN.
//
// `DELETE /teacher/students/{id}` was changed on 2026-08-06 to stop claiming an
// erasure it had not performed: it answers 200 with `hf_erasure_complete`,
// `hf_failures` and a German `detail` whenever the ACCOUNT was deleted but the
// student's HuggingFace repos were not. `StudentRow.handleDelete` discarded
// that response entirely (`await apiDeleteStudent(...)`) and showed an
// unconditional „Schüler gelöscht", so the whole change stopped at the wire.
//
// This is not an edge case. The platform token does not own a repo in a
// student's own namespace, so `delete_repo` 403s on essentially EVERY deletion
// — the partial outcome is the ORDINARY one until the org-namespace change
// lands.
//
// Three properties, and they fail for different reasons:
//   1. the partial outcome is SURFACED, in German, naming what to do;
//   2. it is still a SUCCESS for the account — 200, so the row must leave the
//      roster and nothing may be rolled back;
//   3. an OLDER Cloud API (a bare `{ok: true}`) keeps the previous message
//      byte-for-byte, so the gate gets no chance to invent a warning about a
//      field that was never sent.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import StudentRow from '../StudentRow';
import teacherReducer from '../../../features/teacher/teacherSlice';
import authReducer from '../../../features/auth/authSlice';

const toastCalls = { success: [], error: [] };
vi.mock('react-hot-toast', () => {
  const t = {
    success: (m, o) => toastCalls.success.push([m, o]),
    error: (m, o) => toastCalls.error.push([m, o]),
  };
  return { __esModule: true, default: t, toast: t };
});

let deleteResponse;
let deleteCalls;
vi.mock('../../../services/teacherApi', () => ({
  __esModule: true,
  deleteStudent: (...a) => {
    deleteCalls.push(a);
    if (deleteResponse instanceof Error) return Promise.reject(deleteResponse);
    return Promise.resolve(deleteResponse);
  },
  patchStudent: () => Promise.resolve({}),
  resetStudentPassword: () => Promise.resolve({}),
  adjustStudentCredits: () => Promise.resolve({}),
}));

vi.mock('../../../services/meApi', () => ({
  __esModule: true,
  getMe: () => Promise.resolve({ credits_pool: 0 }),
}));

const student = {
  id: 'stu1',
  full_name: 'Anna Müller',
  username: 'anna',
  credits_used: 0,
  credits_limit: 10,
};

function renderRow() {
  const store = configureStore({
    reducer: { teacher: teacherReducer, auth: authReducer },
    preloadedState: {
      auth: { ...authReducer(undefined, { type: '@@init' }),
              session: { access_token: 'jwt' } },
      // Seeded so `removeStudentFromSelected` has something OBSERVABLE to do —
      // its reducer is a no-op when selectedClassroom is null, which would make
      // the "still removes the student" assertion vacuous.
      teacher: { ...teacherReducer(undefined, { type: '@@init' }),
                 selectedClassroom: { id: 'c1', students: [student] } },
    },
  });
  return {
    store,
    ...render(
      <Provider store={store}>
        <table><tbody>
          {/* `classrooms` is a required prop (the move-to dropdown filters it)
              and `token` comes from Redux, not from a prop. */}
          <StudentRow student={student} classrooms={[]} />
        </tbody></table>
      </Provider>
    ),
  };
}

async function clickDelete() {
  // The row's delete control is icon-only; find it by its title/aria text.
  const btn = await screen.findByTitle(/löschen/i);
  await userEvent.click(btn);
}

beforeEach(() => {
  toastCalls.success.length = 0;
  toastCalls.error.length = 0;
  deleteCalls = [];
  deleteResponse = { ok: true, hf_erasure_complete: true };
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('deleting a student reports what actually happened', () => {
  it('says „Schüler gelöscht" when the erasure really completed', async () => {
    renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.success.length).toBe(1));
    expect(toastCalls.success[0][0]).toBe('Schüler gelöscht');
    expect(toastCalls.error).toEqual([]);
  });

  it('does NOT claim success when the HuggingFace repos survived', async () => {
    deleteResponse = {
      ok: true,
      hf_erasure_complete: false,
      hf_failures: [
        { repo_id: 'anna/omx_f_pick', repo_type: 'dataset', reason: '403' },
      ],
      detail:
        'Das Konto wurde gelöscht, aber 1 HuggingFace-Repository/-s konnten '
        + 'NICHT gelöscht werden — die Daten liegen noch dort. Bitte im '
        + 'HuggingFace-Konto der Schülerin/des Schülers manuell löschen.',
    };
    renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.error.length).toBe(1));
    expect(toastCalls.success).toEqual([]);
    expect(toastCalls.error[0][0]).toContain('NICHT gelöscht');
  });

  it('prefers the server’s German detail verbatim', async () => {
    const detail = 'Serverseitige deutsche Meldung mit Umlauten: ä ö ü ß.';
    deleteResponse = { ok: true, hf_erasure_complete: false, hf_failures: [], detail };
    renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.error.length).toBe(1));
    expect(toastCalls.error[0][0]).toBe(detail);
  });

  it('falls back to its own German sentence when the server sends no detail', async () => {
    deleteResponse = {
      ok: true,
      hf_erasure_complete: false,
      hf_failures: [{ repo_id: 'a/b' }, { repo_id: 'c/d' }],
    };
    renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.error.length).toBe(1));
    const msg = toastCalls.error[0][0];
    expect(msg).toContain('2 HuggingFace-');
    expect(msg).toContain('manuell löschen');
    // Rule §1: literal umlauts, never ae/oe/ue transliterations.
    expect(msg).not.toMatch(/geloescht|Schueler/);
  });

  it('renders the zero-attempt body: account gone, HuggingFace never asked', async () => {
    // The 2026-08-31 server branch, verbatim. `_delete_student_hf_artifacts`
    // returned `attempted == 0`, so the API deliberately does NOT claim an
    // erasure it never performed — `hf_erasure_complete: false` with an EMPTY
    // `hf_failures`. That combination did not exist before, and the SPA must
    // not read the empty list as „nothing went wrong, say success".
    deleteResponse = {
      ok: true,
      hf_erasure_complete: false,
      hf_failures: [],
      detail:
        'Das Konto wurde gelöscht. Für diese Schülerin/diesen Schüler waren '
        + 'keine HuggingFace-Datensätze registriert, die hier gelöscht werden '
        + 'können — es wurde deshalb nicht bei HuggingFace nachgefragt und '
        + 'nicht geprüft, ob dort noch Daten liegen. Bitte im '
        + 'HuggingFace-Konto der Schülerin/des Schülers nachsehen.',
    };
    const { store } = renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.error.length).toBe(1));
    // The server's sentence, not the count fallback — with `hf_failures: []`
    // that fallback would read „0 HuggingFace-Repository/-s".
    expect(toastCalls.error[0][0]).toBe(deleteResponse.detail);
    expect(toastCalls.error[0][0]).not.toContain('0 HuggingFace-');
    // Never both, and never the unconditional success line.
    expect(toastCalls.success).toEqual([]);
    // The ACCOUNT deletion still succeeded — the row must leave the roster.
    expect(store.getState().teacher.selectedClassroom.students).toEqual([]);
  });

  it('gives the warning long enough to act on', async () => {
    deleteResponse = { ok: true, hf_erasure_complete: false, hf_failures: [] };
    renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.error.length).toBe(1));
    // It is an instruction to go and do something on another site, not a
    // status line — the 6 s error default is not enough to read and act.
    expect(toastCalls.error[0][1]?.duration).toBeGreaterThanOrEqual(10000);
  });

  it('still removes the student from the roster — the ACCOUNT was deleted', async () => {
    // A 200 with hf_erasure_complete:false is a partial ERASURE, never a
    // failed request. Rolling the row back would leave a ghost student the
    // teacher can click on and the API no longer knows.
    deleteResponse = { ok: true, hf_erasure_complete: false, hf_failures: [] };
    const { store } = renderRow();
    expect(store.getState().teacher.selectedClassroom.students).toHaveLength(1);
    await clickDelete();
    await waitFor(() => expect(toastCalls.error.length).toBe(1));
    expect(deleteCalls).toHaveLength(1);
    expect(store.getState().teacher.selectedClassroom.students).toEqual([]);
  });

  it('is unchanged against an OLDER Cloud API that sends a bare {ok:true}', async () => {
    // `undefined === false` is false, so the gate cannot fire on a field the
    // server never sent. Same explicit-false doctrine as navGating and
    // LeaderToggle.
    deleteResponse = { ok: true };
    renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.success.length).toBe(1));
    expect(toastCalls.success[0][0]).toBe('Schüler gelöscht');
    expect(toastCalls.error).toEqual([]);
  });

  it('is unchanged when the response is not an object at all', async () => {
    deleteResponse = null;
    renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.success.length).toBe(1));
  });

  it('still reports a genuinely FAILED request as an error', async () => {
    deleteResponse = new Error('Konto konnte nicht gelöscht werden');
    renderRow();
    await clickDelete();
    await waitFor(() => expect(toastCalls.error.length).toBe(1));
    expect(toastCalls.error[0][0]).toBe('Konto konnte nicht gelöscht werden');
    expect(toastCalls.success).toEqual([]);
  });

  it('does nothing at all when the confirm is declined', async () => {
    window.confirm.mockReturnValue(false);
    renderRow();
    await clickDelete();
    expect(deleteCalls).toEqual([]);
    expect(toastCalls.success).toEqual([]);
    expect(toastCalls.error).toEqual([]);
  });
});
