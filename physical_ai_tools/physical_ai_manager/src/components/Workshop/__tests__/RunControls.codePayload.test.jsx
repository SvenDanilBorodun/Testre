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
//
// 2026-09-27 (migration 041): a code program's stored Ziele/Positionen ride the
// `destinations` sibling exactly like a Blockly program's — the store the page
// hands over (`destinationStore`); still `[]` when it hands none. And an
// UNSAVED program that calls `replay("…")` is refused with the same „erst
// speichern" sentence a block program gets, instead of starting and failing on
// the robot (O9).

import React from 'react';
import {
  fireEvent, render, screen, waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunControls from '../RunControls';
import * as workflowApi from '../../../services/workflowApi';
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
  workflowApi.getTrajectoryByName.mockReset();
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

describe('RunControls — a code run carries the recordings its replay calls name', () => {
  // E2E fix round 1: `trajectories` was hardcoded `{}` for a code run, so
  // `robot.replay(...)` — a full row in `ROBOT_API`, rendered into both stubs
  // and offered by the editor's autocomplete with a German promise — could
  // only ever reach the server's „Unbekannte Aufnahme: …" and abort the run.
  const REPLAY_FILES = {
    'main.py': 'import robot\nrobot.replay("Winken", 1.5)\n',
  };
  const ROW = {
    fps: 25,
    points: [[0, 0, 0, 0, 0, 0.8, 0], [0.1, 0, 0, 0, 0, 0.8, 0.04]],
    robot_profile: 'omx_f',
  };

  test('a literal name is fetched and rides the run payload', async () => {
    workflowApi.getTrajectoryByName.mockResolvedValue(ROW);
    await clickStart({ codeFiles: REPLAY_FILES });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    expect(workflowApi.getTrajectoryByName).toHaveBeenCalledWith('jwt-1', 'wf-1', 'Winken');
    const parsed = JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json);
    expect(Object.keys(parsed.trajectories)).toEqual(['Winken']);
    expect(parsed.trajectories.Winken.fps).toBe(25);
    expect(parsed.trajectories.Winken.points).toHaveLength(2);
  });

  test('a recording of another rig is refused in German, and nothing is sent', async () => {
    workflowApi.getTrajectoryByName.mockResolvedValue({ ...ROW, robot_profile: 'edu6_studio' });
    await clickStart({ codeFiles: REPLAY_FILES });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(mockToast.error.mock.calls[0][0]).toMatch(/anderen Robotertyp/);
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('a name the cloud says it does not have is SKIPPED, never a refused start', async () => {
    // The scan reads free-form text, so a hit may be a comment or a string.
    // Aborting the start on it would refuse a program that never replays;
    // the run reports the server's own sentence if the call is really made.
    // A 404 is the ONE answer that means „there is no such Bewegung".
    workflowApi.getTrajectoryByName.mockRejectedValue(
      Object.assign(new Error('Bewegung nicht gefunden'), { status: 404 }));
    await clickStart({ codeFiles: REPLAY_FILES });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    expect(mockToast.error).not.toHaveBeenCalled();
    const parsed = JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json);
    expect(parsed.trajectories).toEqual({});
  });

  test.each([
    ['a dead connection or a timeout', 0],
    ['a server error', 500],
    ['an expired session', 401],
  ])('%s is refused LOUDLY, never silently dropped', async (_why, status) => {
    // „Wir konnten nicht fragen" is NOT „diese Bewegung gibt es nicht":
    // `WorkflowApiError` carries status 0 for a timeout or a dead connection
    // and the HTTP code otherwise, the same split `isCloudUnreachableAuthError`
    // already makes. Dropping the name on one of those starts a run that then
    // aborts on the server's „Unbekannte Aufnahme: …", blaming a recording the
    // student has and which is perfectly fine.
    workflowApi.getTrajectoryByName.mockRejectedValue(
      Object.assign(new Error('Verbindung zum Server fehlgeschlagen.'), { status }));
    await clickStart({ codeFiles: REPLAY_FILES });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(mockToast.error.mock.calls[0][0]).toMatch(/konnte nicht geladen werden/);
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('a row that arrives but will not parse is refused loudly too', async () => {
    // A 200 whose body is not a usable recording is corrupt data, not a
    // phantom name — the student HAS this Bewegung. Loud on both paths.
    workflowApi.getTrajectoryByName.mockResolvedValue({ fps: 25, points: 'nope' });
    await clickStart({ codeFiles: REPLAY_FILES });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(mockToast.error.mock.calls[0][0]).toMatch(/Winken/);
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('a keyword name on a line of its own is fetched too (review round 4, mc7)', async () => {
    // The loose run-time scan reads one line; the editor's exact scanner
    // knows this call, and the run fetches every name that scanner reports.
    workflowApi.getTrajectoryByName.mockResolvedValue(ROW);
    await clickStart({ codeFiles: { 'main.py': 'import robot\nrobot.replay(speed=2,\n             name="Winken")\n' } });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    expect(workflowApi.getTrajectoryByName).toHaveBeenCalledWith('jwt-1', 'wf-1', 'Winken');
    const parsed = JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json);
    expect(Object.keys(parsed.trajectories)).toEqual(['Winken']);
  });

  test('… and so is a Java call split over lines', async () => {
    workflowApi.getTrajectoryByName.mockResolvedValue(ROW);
    await clickStart({
      codeLanguage: 'java',
      codeFiles: { 'Main.java': 'import edubotics.Robot;\npublic class Main {\n  public static void main(String[] a) {\n    Robot.replay(\n        "Winken");\n  }\n}\n' },
    });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    expect(workflowApi.getTrajectoryByName).toHaveBeenCalledWith('jwt-1', 'wf-1', 'Winken');
  });

  test('a project with no replay call fetches nothing', async () => {
    workflowApi.getTrajectoryByName.mockResolvedValue(ROW);
    await clickStart();
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    expect(workflowApi.getTrajectoryByName).not.toHaveBeenCalled();
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

describe('RunControls — a code run sends the document’s Ziele (041)', () => {
  const ENTRIES = [
    { id: 'd_1', name: 'Ablage', kind: 'pin', x: 0.2, y: -0.05, z: 0, source: 'camera' },
    { id: 'd_2', name: 'Hoch', kind: 'pose', x: 0.1, y: 0.1, z: 0.15, source: 'capture', joints: [0, 1], joint_names: ['a', 'b'] },
  ];
  const store = { getEntries: () => ENTRIES };

  test('the store’s entries ride the destinations sibling as {name, kind, x, y, z}', async () => {
    await clickStart({ destinationStore: store });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    const parsed = JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json);
    expect(parsed.destinations).toEqual([
      { name: 'Ablage', kind: 'pin', x: 0.2, y: -0.05, z: 0 },
      { name: 'Hoch', kind: 'pose', x: 0.1, y: 0.1, z: 0.15 },
    ]);
  });

  test('in the simulator too', async () => {
    await clickStart({ destinationStore: store, simMode: true, simScene: { objects: [], zones: [] } });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    const parsed = JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json);
    expect(parsed.destinations.map((d) => d.name)).toEqual(['Ablage', 'Hoch']);
    expect(parsed.sim).toBeTruthy();
  });
});

describe('RunControls — an unsaved code program that replays is told to save first (O9)', () => {
  const SAVE_FIRST = 'Bitte zuerst den Workflow speichern — aufgenommene Bewegungen '
    + 'gehören zu einem gespeicherten Workflow.';

  test('replay() without a workflow id refuses in German and sends nothing', async () => {
    await clickStart({ workflowId: null, codeFiles: { 'main.py': 'import robot\nrobot.replay("Winken")\n' } });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(SAVE_FIRST));
    expect(mockRos.callService).not.toHaveBeenCalled();
    expect(workflowApi.getTrajectoryByName).not.toHaveBeenCalled();
  });

  test('the Java spelling is caught the same way', async () => {
    await clickStart({
      workflowId: null,
      codeLanguage: 'java',
      codeFiles: {
        'Main.java': 'import edubotics.Robot;\npublic class Main {\n  public static void main(String[] a) {\n    Robot.replay("Winken");\n  }\n}\n',
      },
    });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(SAVE_FIRST));
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('the keyword form `replay(name="…")` is caught too (review round 2, ni1)', async () => {
    await clickStart({ workflowId: null, codeFiles: { 'main.py': 'import robot\nrobot.replay(speed=2, name="Winken")\n' } });
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(SAVE_FIRST));
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('a replay only inside a comment or a string does not block an unsaved run', async () => {
    await clickStart({
      workflowId: null,
      codeFiles: { 'main.py': 'import robot\n# robot.replay("Alt")\nprint(\'robot.replay("x")\')\nrobot.home()\n' },
    });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    expect(mockToast.error).not.toHaveBeenCalled();
  });

  test('an unsaved program without replay runs as before', async () => {
    await clickStart({ workflowId: null });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
    expect(mockRos.callService.mock.calls[0][2].workflow_id).toMatch(/^local-/);
  });
});

describe('RunControls — Start says why it cannot start (review round 4, MC2)', () => {
  test('with a reason set, Start is disabled, names it in German, and a click sends nothing', async () => {
    const reason = 'Eine frühere Version wird gerade wiederhergestellt – bitte kurz warten.';
    render(
      <RunControls
        workflowId="wf-1"
        blocklyJson={null}
        simMode={false}
        simScene={null}
        codeLanguage="python"
        codeFiles={FILES}
        startBlockedReason={reason}
      />,
    );
    const start = screen.getByRole('button', { name: /Start/ });
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute('title', reason);
    await userEvent.click(start);
    fireEvent.click(start);
    await new Promise((r) => { setTimeout(r, 20); });
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('without one, the same Start runs', async () => {
    await clickStart({ startBlockedReason: null });
    await waitFor(() => expect(mockRos.callService).toHaveBeenCalledTimes(1));
  });
});
