/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Vormachen over a PYTHON program (owner decisions O4–O6): the same overlay,
// handed the code asset document instead of a Blockly workspace. The document
// is REAL here (code/codeAssetDocument.js over a detached Ziele store), so what
// is asserted is what reaches the program: a capture lands in the document's
// store, named past the program's own pins; a rename rewrites the code; „Als
// Programm einfügen" writes lines directly below the student's cursor and says
// where — and without a cursor puts them on the clipboard (owner decision
// R3-O4).
//
// The session hook is the controllable one of TeachOverlay.test.jsx.

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import toast from 'react-hot-toast';
import TeachOverlay from '../TeachOverlay';
import { DE } from '../../blocks/messages_de';
import { CODE_DE, formatCode } from '../../code/codeMessagesDe';
import { createCodeAssetDocument } from '../../code/codeAssetDocument';
import { forgetVormachenCopy, vormachenPasteLines } from '../../code/vormachenClipboard';
import { createDetachedDestinationStore } from '../../sammlung/destinationStore';
import * as workflowApi from '../../../../services/workflowApi';

let mockState;
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
}));

vi.mock('react-hot-toast', () => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return { __esModule: true, default: t };
});

const mockRos = vi.hoisted(() => ({
  handGuide: vi.fn(), recordControl: vi.fn(), capturePose: vi.fn(), replayMotion: vi.fn(),
}));
vi.mock('../../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

vi.mock('../../../../services/workflowApi', () => ({
  __esModule: true,
  createTrajectory: vi.fn(),
  renameTrajectory: vi.fn(),
  deleteTrajectory: vi.fn(),
}));

vi.mock('../../HomeGlidePrompt', () => ({
  __esModule: true,
  useHomeGlide: () => ({ offerHomeGlide: vi.fn(), homeGlideDialog: null, homeGlideActive: false }),
}));
vi.mock('../teachSounds', () => ({
  __esModule: true,
  createTeachSounds: () => ({ tick() {}, start() {}, stop() {}, capture() {}, dispose() {} }),
}));
vi.mock('../../../../utils/rosConnectionManager', () => ({ __esModule: true, default: { ros: null } }));
vi.mock('../../../../utils/piMode', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, usePiMode: () => ({ piMode: false, piModeResolved: true }) };
});
vi.mock('roslib', () => ({
  __esModule: true,
  default: { Topic: function Topic() { this.subscribe = () => {}; this.unsubscribe = () => {}; } },
}));

const mockHook = vi.hoisted(() => ({ props: null, namer: null }));
vi.mock('../useTeachSession', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    default: (props) => {
      mockHook.props = props;
      return {
        state: 'fest', countdownLeft: 0, elapsedS: 0, busy: false, take: null, relock: 'none',
        releasedOnce: false, previewNoMotionHint: false,
        actions: {
          space: vi.fn(), toggleFree: vi.fn(), lock: vi.fn(), capturePose: vi.fn(), captureZiel: vi.fn(),
          keep: vi.fn(), again: vi.fn(), discard: vi.fn(), previewOnRobot: vi.fn(), stopPreview: vi.fn(),
          finish: vi.fn(), continueTeaching: vi.fn(), discardStaleLeaderTake: vi.fn(),
        },
        onKeyDown: () => {},
        setCaptureNamer: (fn) => { mockHook.namer = fn; },
      };
    },
  };
});

const TAKE = {
  points: [[0.1, 0.2, 0.3, 0.4, 0.5, 0.8, 0], [0.11, 0.21, 0.31, 0.41, 0.51, 0.8, 0.3],
    [0.12, 0.2, 0.3, 0.4, 0.5, 0.8, 1.2]],
  fps: 25, sampleCount: 3, durationS: 1.2, relockOk: true,
};
const POSE = { success: true, world_x: 0.1, world_y: 0.02, world_z: 0.12 };

function setState(items = []) {
  mockState = {
    tasks: { collision: { active: false } },
    studioAssets: { trajectories: { workflowId: 'wf-1', status: 'ready', items, error: null, fetchedAt: 1 } },
  };
}

function codeDoc(main, startCursor = null) {
  let files = { 'main.py': main };
  const store = createDetachedDestinationStore([]);
  const reveals = [];
  let cursor = startCursor;
  const doc = createCodeAssetDocument({
    language: 'python',
    store,
    getFiles: () => files,
    applyFiles: (next) => { files = next; },
    requestReveal: (at) => reveals.push(at),
    getCursor: () => cursor,
    setCursor: (at) => { cursor = at; },
  });
  return {
    doc, store, reveals, main: () => files['main.py'],
  };
}

function props(assetDoc, over = {}) {
  return {
    mode: 'hand', focus: null, onClose: vi.fn(), workspace: null, assetDoc, accessToken: 'jwt',
    workflowId: 'wf-1', robotType: 'omx_f', caps: null, heartbeatOk: true,
    rsBridge: { available: true, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false },
    saveWorkflowNow: vi.fn(async () => ({ ok: true })), refetchTrajectories: vi.fn(), ...over,
  };
}

const flush = async () => {
  for (let i = 0; i < 20; i += 1) await Promise.resolve(); // eslint-disable-line no-await-in-loop
};

beforeEach(() => {
  setState();
  mockHook.props = null;
  mockHook.namer = null;
  workflowApi.createTrajectory.mockReset();
  workflowApi.renameTrajectory.mockReset();
  toast.success.mockClear();
  toast.error.mockClear();
});

describe('TeachOverlay over a Python program', () => {
  test('a capture is named past the program’s own pins and lands in the document’s store', () => {
    const { doc, store } = codeDoc('import robot\nrobot.pin("Ziel 1", 0.1, 0.0, 0.0)\n');
    render(<TeachOverlay {...props(doc)} />);
    expect(mockHook.namer('ziel')).toBe('Ziel 2');
    act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
    expect(store.getByName('Position 1')).toMatchObject({ kind: 'pose', source: 'capture', z: 0.12 });
    expect(within(screen.getByTestId('teach-item-pose')).getByText('Position 1')).toBeInTheDocument();
    expect(toast.error).not.toHaveBeenCalled();
  });

  test('renaming a Position rewrites the program’s calls', async () => {
    const { doc, store, main } = codeDoc('import robot\nrobot.move_to("Position 1")\n');
    render(<TeachOverlay {...props(doc)} />);
    act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
    fireEvent.click(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Position 1` }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Kiste' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    await act(async () => { await flush(); });
    expect(store.getByName('Kiste')).toBeTruthy();
    expect(main()).toBe('import robot\nrobot.move_to("Kiste")\n');
  });

  test('renaming a kept recording renames the cloud row, rewrites the code, then saves', async () => {
    const { doc, main } = codeDoc('import robot\nrobot.replay("Bewegung 1")\n');
    workflowApi.createTrajectory.mockResolvedValue({ id: 't1' });
    workflowApi.renameTrajectory.mockResolvedValue({});
    const p = props(doc);
    const { rerender } = render(<TeachOverlay {...p} />);
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    setState([{ id: 't1', name: 'Bewegung 1', robot_profile: 'omx_f', created_at: '2026-09-27T10:00:00Z' }]);
    rerender(<TeachOverlay {...p} />);
    fireEvent.click(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Bewegung 1` }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Winken' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    await act(async () => { await flush(); });
    expect(workflowApi.renameTrajectory).toHaveBeenCalledWith('jwt', 'wf-1', 't1', 'Winken');
    expect(main()).toBe('import robot\nrobot.replay("Winken")\n');
    expect(p.saveWorkflowNow).toHaveBeenCalled();
  });

  test('„Als Programm einfügen" writes the round below the cursor’s line and says where', async () => {
    const { doc, main, reveals } = codeDoc('import robot\nrobot.home()\n', { file: 'main.py', line: 2 });
    workflowApi.createTrajectory.mockResolvedValue({ id: 't1' });
    render(<TeachOverlay {...props(doc)} />);
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
    const button = screen.getByRole('button', { name: formatCode(CODE_DE.TEACH_INSERT_LINES, 2) });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    // The insertion module loads on demand (review round 2, ni4).
    await waitFor(() => expect(main())
      .toBe('import robot\nrobot.home()\nrobot.replay("Bewegung 1")\nrobot.move_to("Position 1")\n'));
    expect(toast.success).toHaveBeenCalledWith(formatCode(CODE_DE.INSERTED_AT, 'main.py', 3));
    expect(reveals[reveals.length - 1]).toEqual({ file: 'main.py', line: 4 });
  });

  describe('without a cursor, nothing is written (R3-O4)', () => {
    const had = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    afterEach(() => {
      if (had) Object.defineProperty(navigator, 'clipboard', had);
      else delete navigator.clipboard;
    });

    test('the round goes to the clipboard, and the student is told to paste it', async () => {
      const writeText = vi.fn(async () => {});
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
      const { doc, main } = codeDoc('import robot\nrobot.home()\n');
      workflowApi.createTrajectory.mockResolvedValue({ id: 't1' });
      render(<TeachOverlay {...props(doc)} />);
      await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
      act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
      fireEvent.click(screen.getByRole('button', { name: formatCode(CODE_DE.TEACH_INSERT_LINES, 2) }));
      await waitFor(() => expect(toast.success).toHaveBeenCalledWith(CODE_DE.COPIED_PASTE_HINT));
      // One per line AND a final line break (review round 4, mc2): pasted at
      // the end of a line anywhere, the next statement starts on a line of
      // its own.
      const copied = 'robot.replay("Bewegung 1")\nrobot.move_to("Position 1")\n';
      expect(writeText).toHaveBeenCalledWith(copied);
      // Remembered for the editor's paste: exactly this text lands like
      // „Einfügen" (owner decision R4-O1); an edited copy does not.
      expect(vormachenPasteLines(copied, 'python'))
        .toEqual(['robot.replay("Bewegung 1")', 'robot.move_to("Position 1")']);
      expect(vormachenPasteLines(copied.replace('Bewegung 1', 'Bewegung 2'), 'python')).toBeNull();
      expect(vormachenPasteLines(copied, 'java')).toBeNull();
      expect(CODE_DE.COPIED_PASTE_HINT).toBe('Kopiert – klicke in deinen Code und drücke Strg+V.');
      expect(main()).toBe('import robot\nrobot.home()\n');
      expect(toast.error).not.toHaveBeenCalled();
    });

    test('no clipboard: the click-first hint instead', async () => {
      Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
      const { doc, main } = codeDoc('import robot\nrobot.home()\n');
      render(<TeachOverlay {...props(doc)} />);
      act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
      fireEvent.click(screen.getByRole('button', { name: CODE_DE.TEACH_INSERT_LINE_ONE }));
      await waitFor(() => expect(toast.error).toHaveBeenCalledWith(CODE_DE.CLICK_FIRST_HINT));
      expect(toast.success).not.toHaveBeenCalled();
      expect(main()).toBe('import robot\nrobot.home()\n');
    });

    test('a refused clipboard: the click-first hint as well, and nothing is remembered', async () => {
      forgetVormachenCopy();
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: vi.fn(async () => { throw new Error('nope'); }) }, configurable: true,
      });
      const { doc } = codeDoc('import robot\n');
      render(<TeachOverlay {...props(doc)} />);
      act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
      fireEvent.click(screen.getByRole('button', { name: CODE_DE.TEACH_INSERT_LINE_ONE }));
      await waitFor(() => expect(toast.error).toHaveBeenCalledWith(CODE_DE.CLICK_FIRST_HINT));
      expect(vormachenPasteLines('robot.move_to("Position 1")\n', 'python')).toBeNull();
    });
  });

  test('nb4: a second click while the round is being inserted writes it once', async () => {
    const { doc, main } = codeDoc('import robot\nrobot.home()\n', { file: 'main.py', line: 2 });
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const original = doc.insertProgram;
    let calls = 0;
    doc.insertProgram = async (...args) => {
      calls += 1;
      await gate;
      return original(...args);
    };
    render(<TeachOverlay {...props(doc)} />);
    act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
    const button = screen.getByRole('button', { name: CODE_DE.TEACH_INSERT_LINE_ONE });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(calls).toBe(1);
    await waitFor(() => expect(button).toBeDisabled());
    release();
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
    expect(main()).toBe('import robot\nrobot.home()\nrobot.move_to("Position 1")\n');
  });

  test('a Java class line gets the German reason, and nothing is written', async () => {
    let files = { 'Main.java': 'public class Main {\n}\n' };
    const doc = createCodeAssetDocument({
      language: 'java',
      store: createDetachedDestinationStore([]),
      getFiles: () => files,
      applyFiles: (next) => { files = next; },
      requestReveal: () => {},
      getCursor: () => ({ file: 'Main.java', line: 1 }),
    });
    render(<TeachOverlay {...props(doc)} />);
    act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
    fireEvent.click(screen.getByRole('button', { name: CODE_DE.TEACH_INSERT_LINE_ONE }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(CODE_DE.NOT_IN_METHOD_HINT));
    expect(toast.success).not.toHaveBeenCalled();
    expect(files['Main.java']).toBe('public class Main {\n}\n');
  });

  test('one line is „1 Zeile", never „1 Zeilen", and an empty round inserts nothing', () => {
    const { doc } = codeDoc('import robot\n');
    render(<TeachOverlay {...props(doc)} />);
    expect(screen.getByRole('button', { name: formatCode(CODE_DE.TEACH_INSERT_LINES, 0) })).toBeDisabled();
    act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: POSE }); });
    expect(screen.getByRole('button', { name: CODE_DE.TEACH_INSERT_LINE_ONE })).toBeEnabled();
    expect(CODE_DE.TEACH_INSERT_LINE_ONE).toBe('Als Programm einfügen (1 Zeile)');
  });
});
