/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The /workflow/start payload of a CODE program (§3.9): the poison block an old
// image trips over loudly, the language the new server routes on, the files,
// and the same siblings a Blockly run sends. Plus the fail-CLOSED capability
// gate: a robot that has not said `code_languages` gets nothing sent.

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunControls from '../RunControls';
import { CODE_LIMITS } from '../code/codeProject';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockRos = vi.hoisted(() => ({
  callService: vi.fn(() =>
    Promise.resolve({
      success: true,
      message: 'gestartet',
      unreachable_block_ids: [],
      unreachable_messages: [],
    }),
  ),
  pauseWorkflow: vi.fn(),
  stepWorkflow: vi.fn(),
  continueWorkflow: vi.fn(),
  setWorkflowBreakpoints: vi.fn(),
}));
vi.mock('../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

vi.mock('../../../services/workflowApi', () => ({
  __esModule: true,
  getTrajectoryByName: vi.fn(),
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

const FULL_CAPS = {
  recordable: true, editable: true, trainable: true, inferable: true,
  roboter_studio: true, has_leader: true,
  code_languages: ['python', 'java'],
};

function baseState(caps = FULL_CAPS) {
  return {
    workshop: {
      runState: 'idle',
      phase: '',
      currentBlockId: null,
      paused: false,
      log: [],
      workflowError: null,
      debuggerVisible: false,
      debuggerWarnings: [],
      breakpoints: [],
    },
    auth: { session: { access_token: 'jwt-1' } },
    tasks: { taskStatus: { robotType: 'omx_f', capabilities: caps } },
  };
}

const FILES = {
  'main.py': 'import robot\nimport hilfe\nrobot.home()\n',
  'hilfe.py': 'def x():\n    return 1\n',
};

async function clickStart(props) {
  render(
    <RunControls
      workflowId="wf-1"
      blocklyJson={null}
      simMode={false}
      simScene={null}
      codeLanguage="python"
      codeFiles={FILES}
      {...props}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: /Start/ }));
}

beforeEach(() => {
  mockState = baseState();
  mockDispatch.mockClear();
  mockRos.callService.mockClear();
  mockToast.mockClear();
  mockToast.success.mockClear();
  mockToast.error.mockClear();
  global.fetch = vi.fn(() => Promise.reject(new Error('no bridge')));
});

describe('RunControls — a code run’s payload', () => {
  test('carries the poison block, the language, the files and the four siblings', async () => {
    await clickStart();
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    const [name, , payload] = mockRos.callService.mock.calls[0];
    expect(name).toBe('/workflow/start');
    const parsed = JSON.parse(payload.workflow_json);
    expect(Object.keys(parsed).sort()).toEqual(
      ['blocks', 'destinations', 'files', 'language', 'tempo', 'trajectories', 'variables', 'zones'],
    );
    // An old server (`Interpreter.from_json` reads only `blocks`) raises
    // „Unbekannter Block-Typ: edubotics_code_program_v1" — loud, never a
    // silent green run; the new server routes on `language` first.
    expect(parsed.blocks).toEqual({ blocks: [{ type: 'edubotics_code_program_v1', id: 'code' }] });
    expect(parsed.variables).toEqual([]);
    expect(parsed.language).toBe('python');
    expect(parsed.files).toEqual(FILES);
    expect(parsed.trajectories).toEqual({});
    expect(parsed.destinations).toEqual([]);
    expect(typeof parsed.tempo).toBe('number');
    expect(parsed.zones).toEqual([]);
    expect(payload.workflow_id).toBe('wf-1');
  });

  test('in simMode the sim sibling joins the same base', async () => {
    await clickStart({ simMode: true, simScene: { objects: [{ type: 'wuerfel', x: 0.2, y: 0 }], zones: [] } });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    const parsed = JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json);
    expect(parsed.sim).toEqual({ enabled: true, objects: [{ type: 'wuerfel', x: 0.2, y: 0 }] });
    expect(parsed.language).toBe('python');
    expect(parsed.blocks.blocks[0].type).toBe('edubotics_code_program_v1');
  });
});

describe('RunControls — the fail-closed capability gate', () => {
  const REFUSAL = /Bitte zuerst die Umgebung aktualisieren/;

  test.each([
    ['caps null', null, 'python'],
    ['caps {}', {}, 'python'],
    ['caps without code_languages', { ...FULL_CAPS, code_languages: undefined }, 'python'],
    ['java on a python-only manifest', { ...FULL_CAPS, code_languages: ['python'] }, 'java'],
  ])('refuses in German and sends nothing — %s', async (_label, caps, language) => {
    mockState = baseState(caps);
    await clickStart({ codeLanguage: language, codeFiles: language === 'java'
      ? { 'Main.java': 'public class Main {}' } : FILES });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(mockToast.error.mock.calls[0][0]).toMatch(REFUSAL);
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('a matching manifest lets the run through', async () => {
    mockState = baseState({ ...FULL_CAPS, code_languages: ['java'] });
    await clickStart({ codeLanguage: 'java', codeFiles: { 'Main.java': 'public class Main {}' } });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    expect(mockToast.error).not.toHaveBeenCalled();
  });
});

describe('RunControls — the project caps are judged before anything is sent', () => {
  test('a file over MAX_CODE_FILE_BYTES is refused with its name', async () => {
    await clickStart({
      codeFiles: { 'main.py': 'a'.repeat(CODE_LIMITS.MAX_CODE_FILE_BYTES + 1) },
    });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(mockToast.error.mock.calls[0][0]).toMatch(/main\.py.*zu groß/);
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('a project over MAX_CODE_PROJECT_BYTES is refused', async () => {
    const files = { 'main.py': 'a'.repeat(60 * 1024), 'b.py': 'a'.repeat(60 * 1024), 'c.py': 'a'.repeat(9 * 1024) };
    await clickStart({ codeFiles: files });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(mockToast.error.mock.calls[0][0]).toMatch(/insgesamt zu groß/);
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('an empty project is refused with the server’s own sentence', async () => {
    await clickStart({ codeFiles: {} });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(mockToast.error.mock.calls[0][0]).toMatch(/keine Dateien/);
    expect(mockRos.callService).not.toHaveBeenCalled();
  });
});
