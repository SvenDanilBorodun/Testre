/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// What a teacher sees of a student's Roboter-Studio work (decision A3): the
// programs as they stand right now, and the snapshots the student deliberately
// handed in („Abgeben"). Read-only in both cases.
//
// This ships in the TEACHER build, which has no rosbridge and no robot: the
// drawer talks to the Cloud API and to nothing else. Three of the five
// acceptance points are properties of the SOURCE rather than of a rendered
// pixel — no rosbridge hook, no localStorage key, the viewer genuinely
// read-only — so they are asserted against the files, the way
// `blocklyPayload.test.js` fences its call sites.
//
// `BlocklyWorkspace` is mocked (a real Blockly inject in jsdom belongs to its
// own test). `CodeViewer` deliberately is NOT: it is the thing under test.

import fs from 'fs';
import path from 'path';
import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import StudentProgramsDrawer from '../StudentProgramsDrawer';
import StudentRow from '../StudentRow';
import authReducer from '../../../features/auth/authSlice';
import teacherReducer from '../../../features/teacher/teacherSlice';

const api = vi.hoisted(() => ({
  listStudentWorkflows: vi.fn(),
  getStudentWorkflow: vi.fn(),
  listStudentSubmissions: vi.fn(),
  getStudentSubmission: vi.fn(),
}));
// The row's own calls are stubbed too, so the entry-point test below can
// render it without reaching the network.
vi.mock('../../../services/teacherApi', () => ({
  __esModule: true,
  ...api,
  deleteStudent: vi.fn(() => Promise.resolve({})),
  patchStudent: vi.fn(() => Promise.resolve({})),
  resetStudentPassword: vi.fn(() => Promise.resolve({})),
  adjustStudentCredits: vi.fn(() => Promise.resolve({ new_amount: 0 })),
}));
vi.mock('../../../services/meApi', () => ({
  __esModule: true,
  getMe: () => Promise.resolve({ credits_pool: 0 }),
}));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));

vi.mock('../../Workshop/BlocklyWorkspace', () => ({
  __esModule: true,
  default: function MockBlocklyWorkspace({ readOnly }) {
    return <div data-testid="blockly-preview" data-readonly={readOnly ? 'yes' : 'no'} />;
  },
}));

const STUDENT = { id: 'stu-1', username: 'lena', full_name: 'Lena M.' };

const PROGRAMS = [
  {
    id: 'wf-py', name: 'Würfel schieben', description: '', code_language: 'python',
    created_at: '2026-09-01T10:00:00Z', updated_at: '2026-09-18T09:30:00Z',
  },
  {
    id: 'wf-blocks', name: 'Erste Schritte', description: '', code_language: '',
    created_at: '2026-09-02T10:00:00Z', updated_at: '2026-09-17T09:30:00Z',
  },
];

const SUBMISSIONS = [
  {
    id: 'sub-1', workflow_id: 'wf-py', name: 'Würfel schieben', code_language: 'python',
    note: '', submitted_at: '2026-09-19T11:00:00Z',
  },
];

const PY_DOC = {
  ...PROGRAMS[0],
  blockly_json: {},
  code_files: { 'main.py': 'import robot\n\nrobot.home()\n', 'hilfe.py': '# Größe\n' },
  sim_scene: null,
};

function mount() {
  const store = configureStore({
    reducer: { auth: authReducer },
    preloadedState: { auth: { ...authReducer(undefined, { type: '@@i' }), session: { access_token: 'jwt' } } },
  });
  return render(
    <Provider store={store}>
      <StudentProgramsDrawer student={STUDENT} onClose={() => {}} />
    </Provider>,
  );
}

/** The two lists carry the same program NAME, so every query is scoped. */
const programs = () => within(screen.getByRole('region', { name: 'Aktuelle Programme' }));
const submissions = () => within(screen.getByRole('region', { name: 'Abgaben' }));

/** Open the student's Python program and wait for the lists to be there first. */
async function openPythonProgram() {
  await screen.findByRole('region', { name: 'Aktuelle Programme' });
  await userEvent.click(programs().getByRole('button', { name: /Würfel schieben/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listStudentWorkflows.mockResolvedValue(PROGRAMS);
  api.listStudentSubmissions.mockResolvedValue(SUBMISSIONS);
  api.getStudentWorkflow.mockResolvedValue(PY_DOC);
  api.getStudentSubmission.mockResolvedValue({ ...SUBMISSIONS[0], ...PY_DOC });
});

describe('StudentProgramsDrawer — what the teacher sees', () => {
  test('lists the current programs and the submissions, keyed to this student', async () => {
    mount();
    await screen.findByRole('region', { name: 'Aktuelle Programme' });
    expect(programs().getByText('Würfel schieben')).toBeInTheDocument();
    expect(programs().getByText('Erste Schritte')).toBeInTheDocument();
    expect(submissions().getByText(/Abgabe vom/)).toBeInTheDocument();
    expect(api.listStudentWorkflows).toHaveBeenCalledWith('jwt', 'stu-1');
    expect(api.listStudentSubmissions).toHaveBeenCalledWith('jwt', 'stu-1');
  });

  test('opening a Python program shows its files, read-only', async () => {
    mount();
    await openPythonProgram();
    await waitFor(() => expect(api.getStudentWorkflow).toHaveBeenCalledWith('jwt', 'stu-1', 'wf-py'));
    // Both files are offered; the entry file is shown first.
    expect(await screen.findByRole('button', { name: 'main.py' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'hilfe.py' })).toBeInTheDocument();
    const content = await screen.findByText(/robot\.home\(\)/);
    expect(content).toBeInTheDocument();
    // `EditorView.editable` false ⇒ CodeMirror never sets contenteditable.
    // eslint-disable-next-line testing-library/no-node-access
    expect(document.querySelector('[contenteditable="true"]')).toBeNull();
  });

  test('switching file shows the other one', async () => {
    mount();
    await openPythonProgram();
    await screen.findByRole('button', { name: 'hilfe.py' });
    await userEvent.click(screen.getByRole('button', { name: 'hilfe.py' }));
    expect(await screen.findByText(/Größe/)).toBeInTheDocument();
  });

  test('a Blockly program opens the read-only block preview, not the code view', async () => {
    api.getStudentWorkflow.mockResolvedValue({
      ...PROGRAMS[1], blockly_json: { blocks: { blocks: [] } }, code_files: {}, sim_scene: null,
    });
    mount();
    await screen.findByRole('region', { name: 'Aktuelle Programme' });
    await userEvent.click(programs().getByRole('button', { name: /Erste Schritte/ }));
    const preview = await screen.findByTestId('blockly-preview');
    expect(preview).toHaveAttribute('data-readonly', 'yes');
  });

  test('a submission opens its own snapshot, not the current program', async () => {
    mount();
    await screen.findByRole('region', { name: 'Abgaben' });
    await userEvent.click(submissions().getByRole('button', { name: /Abgabe vom/ }));
    await waitFor(() => expect(api.getStudentSubmission).toHaveBeenCalledWith('jwt', 'stu-1', 'sub-1'));
    expect(api.getStudentWorkflow).not.toHaveBeenCalled();
  });
});

describe('StudentProgramsDrawer — failures are German, never the raw message', () => {
  const RAW = 'TypeError: Failed to fetch';

  test('a failed list says so in German and shows no raw text', async () => {
    const err = new Error(RAW);
    api.listStudentWorkflows.mockRejectedValue(err);
    mount();
    expect(await screen.findByText('Programme konnten nicht geladen werden.')).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(RAW))).toBeNull();
  });

  test('a 404 on one program is its own German sentence', async () => {
    const err = new Error('Programm nicht gefunden');
    err.status = 404;
    err.detail = 'Programm nicht gefunden';
    api.getStudentWorkflow.mockRejectedValue(err);
    mount();
    await openPythonProgram();
    expect(await screen.findByText(
      'Dieses Programm gibt es nicht mehr — vielleicht wurde es gelöscht.',
    )).toBeInTheDocument();
  });

  test('a 401 tells the teacher to sign in again', async () => {
    const err = new Error('Unauthorized');
    err.status = 401;
    api.getStudentWorkflow.mockRejectedValue(err);
    mount();
    await openPythonProgram();
    expect(await screen.findByText(
      'Keine Berechtigung — bitte neu anmelden.',
    )).toBeInTheDocument();
  });

  test('an empty student says so rather than showing two empty lists', async () => {
    api.listStudentWorkflows.mockResolvedValue([]);
    api.listStudentSubmissions.mockResolvedValue([]);
    mount();
    expect(await screen.findByText(
      'Diese Schülerin/dieser Schüler hat noch kein Programm angelegt.',
    )).toBeInTheDocument();
  });
});

describe('StudentProgramsDrawer — the three source properties', () => {
  const read = (rel) => fs.readFileSync(path.resolve(__dirname, '..', rel), 'utf8');
  const DRAWER = read('StudentProgramsDrawer.js');
  const VIEWER = read('CodeViewer.jsx');

  test('neither file reaches for rosbridge — the teacher build has none', () => {
    for (const src of [DRAWER, VIEWER]) {
      expect(src).not.toMatch(/useRosServiceCaller|useRosTopicSubscription|rosConnectionManager|roslib/);
    }
  });

  test('neither file adds a browser-storage key', () => {
    // A shared staffroom PC is the same handover problem as a shared student
    // PC (utils/sessionScope.js). The simplest answer is to store nothing.
    for (const src of [DRAWER, VIEWER]) {
      expect(src).not.toMatch(/localStorage|sessionStorage|idb-keyval/);
    }
  });

  test('the viewer is read-only in BOTH senses CodeMirror has', () => {
    // `EditorState.readOnly` refuses changes; `EditorView.editable` is what
    // keeps `contenteditable` off the DOM. They are different facets and a
    // viewer needs both — the docs say so in as many words.
    expect(VIEWER).toContain('EditorState.readOnly.of(true)');
    expect(VIEWER).toContain('EditorView.editable.of(false)');
    expect(VIEWER).not.toContain('EditorState.readOnly.of(false)');
  });

  test('the CodeMirror import lives in the viewer alone, and the viewer is lazy', () => {
    // package.json's `no-restricted-imports` excludes exactly two files; this
    // is the second. The drawer must reach it through `lazy`, or CodeMirror
    // lands in whatever chunk the teacher dashboard is in.
    expect(DRAWER).not.toMatch(/from '@?codemirror/);
    expect(DRAWER).toMatch(/lazy\(\s*\(\)\s*=>\s*import\('\.\/CodeViewer'\)\s*\)/);
    expect(VIEWER).toMatch(/from '@codemirror\/(view|state)'/);
  });
});

describe('StudentRow — the way in', () => {
  test('the row opens the drawer itself, so no parent has to hold its state', async () => {
    const store = configureStore({
      reducer: { auth: authReducer, teacher: teacherReducer },
      preloadedState: {
        auth: { ...authReducer(undefined, { type: '@@i' }), session: { access_token: 'jwt' } },
      },
    });
    render(
      <Provider store={store}>
        <table><tbody>
          <StudentRow student={STUDENT} classrooms={[]} onShowHistory={() => {}} />
        </tbody></table>
      </Provider>,
    );
    expect(screen.queryByRole('region', { name: 'Aktuelle Programme' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Programme · Abgaben' }));
    expect(await screen.findByRole('region', { name: 'Aktuelle Programme' })).toBeInTheDocument();
    expect(api.listStudentWorkflows).toHaveBeenCalledWith('jwt', 'stu-1');
  });
});
