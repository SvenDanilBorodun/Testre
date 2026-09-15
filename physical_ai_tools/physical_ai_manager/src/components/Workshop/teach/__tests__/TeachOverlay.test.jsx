/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// TeachOverlay — the Vormachen dialog. Most tests drive the overlay's side of
// its contract with the hook (the props it hands useTeachSession, and what it
// renders for a given snapshot) through a controllable hook; the „real hook"
// block wires both together over the actual keyboard path.

import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import toast from 'react-hot-toast';
import TeachOverlay, { teachListMeta, teachRenameEnabled, teachSlotsLine } from '../TeachOverlay';
import { DE, formatDe } from '../../blocks/messages_de';
import { compactTrajectoryPoints } from '../../../../utils/trajectoryCompact';
import { applyCleanup } from '../../../../utils/recordingCleanup';
import * as workflowApi from '../../../../services/workflowApi';
import { renamePlace, renameRecording } from '../../sammlung/assetCommands';

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

const mockGlide = vi.hoisted(() => ({ offer: vi.fn(), active: false }));
vi.mock('../../HomeGlidePrompt', () => ({
  __esModule: true,
  useHomeGlide: () => ({ offerHomeGlide: mockGlide.offer, homeGlideDialog: null, homeGlideActive: mockGlide.active }),
}));

vi.mock('../teachSounds', () => ({
  __esModule: true,
  createTeachSounds: () => ({ tick() {}, start() {}, stop() {}, capture() {}, dispose() {} }),
}));
vi.mock('../../../../utils/rosConnectionManager', () => ({ __esModule: true, default: { ros: null } }));
vi.mock('roslib', () => ({
  __esModule: true,
  default: { Topic: function Topic() { this.subscribe = () => {}; this.unsubscribe = () => {}; } },
}));

// Leader mode renders LeaderActivationGate, whose hook reads s.ros — mocked
// here so no test needs that slice; `status` null = unknown (not blocked).
const mockActivation = vi.hoisted(() => ({ status: null }));
vi.mock('../../../../hooks/useRobotActivation', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    default: () => ({ status: mockActivation.status, activate: vi.fn(), calling: false, error: null }),
  };
});

const mockStore = vi.hoisted(() => ({ add: vi.fn(), getById: vi.fn(), taken: [] }));
vi.mock('../../sammlung/destinationStore', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    getDestinationStore: () => mockStore,
    takenDestinationNames: () => mockStore.taken,
  };
});
const mockInsert = vi.hoisted(() => ({ fn: vi.fn() }));
vi.mock('../insertProgram', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, insertProgram: (...args) => mockInsert.fn(...args) };
});
vi.mock('../../sammlung/assetCommands', () => ({
  __esModule: true,
  renamePlace: vi.fn(),
  renameRecording: vi.fn(),
}));

// A controllable hook: with `snapshot` set, the overlay gets that snapshot and
// `actions`; otherwise the REAL useTeachSession runs. Never toggled mid-test.
const mockHook = vi.hoisted(() => ({ snapshot: null, props: null, namer: null, actions: null }));
vi.mock('../useTeachSession', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    default: (props) => {
      mockHook.props = props;
      if (!mockHook.snapshot) return actual.default(props);
      return {
        state: 'fest', countdownLeft: 0, elapsedS: 0, busy: false, take: null, relock: 'none',
        releasedOnce: false, previewNoMotionHint: false,
        ...mockHook.snapshot,
        actions: mockHook.actions,
        onKeyDown: () => {},
        setCaptureNamer: (fn) => { mockHook.namer = fn; },
      };
    },
  };
});

// The time column spans durationS: the kept duration and the review meta are
// derived from the cleaned rows (lead gap ≤ 300 ms, so nothing is trimmed here).
const TAKE = {
  points: [[0.1, 0.2, 0.3, 0.4, 0.5, 0.8, 0], [0.11, 0.21, 0.31, 0.41, 0.51, 0.8, 0.3],
    [0.123456789, 0.2, 0.3, 0.4, 0.5, 0.8, 1.2004]],
  fps: 25, sampleCount: 3, durationS: 1.2, relockOk: true,
};

function makeActions() {
  return {
    space: vi.fn(), toggleFree: vi.fn(), lock: vi.fn(), capturePose: vi.fn(), captureZiel: vi.fn(),
    keep: vi.fn(), again: vi.fn(), discard: vi.fn(), previewOnRobot: vi.fn(), stopPreview: vi.fn(),
    finish: vi.fn(), continueTeaching: vi.fn(), discardStaleLeaderTake: vi.fn(),
  };
}

function baseProps(over = {}) {
  return {
    mode: 'hand', focus: null, onClose: vi.fn(), workspace: { id: 'ws' }, accessToken: 'jwt',
    workflowId: 'wf-1', robotType: 'omx_f', caps: null, heartbeatOk: true,
    rsBridge: { available: true, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false },
    saveWorkflowNow: vi.fn(), refetchTrajectories: vi.fn(), ...over,
  };
}

function setState({ collision = false, items = [] } = {}) {
  mockState = {
    tasks: { collision: { active: collision } },
    studioAssets: { trajectories: { workflowId: 'wf-1', status: 'ready', items, error: null, fetchedAt: 1 } },
  };
}

function withSnapshot(snapshot) {
  mockHook.snapshot = snapshot;
  mockHook.actions = makeActions();
  return mockHook.actions;
}

const flush = async () => {
  for (let i = 0; i < 20; i += 1) await Promise.resolve(); // eslint-disable-line no-await-in-loop
};

beforeEach(() => {
  setState();
  mockHook.snapshot = null;
  mockHook.props = null;
  mockHook.namer = null;
  mockHook.actions = null;
  mockGlide.offer.mockReset();
  mockActivation.status = null;
  mockGlide.active = false;
  mockStore.add.mockReset();
  mockStore.getById.mockReset();
  mockStore.taken = [];
  mockInsert.fn.mockReset();
  Object.values(mockRos).forEach((fn) => fn.mockReset());
  workflowApi.createTrajectory.mockReset();
  renamePlace.mockReset();
  renameRecording.mockReset();
  toast.success.mockClear();
  toast.error.mockClear();
});

describe('TeachOverlay — dialog, focus and the CollisionModal', () => {
  test('is a labelled modal dialog that takes focus and gives it back on close', () => {
    withSnapshot({ state: 'fest' });
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(<TeachOverlay {...baseProps()} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    // aria-labelledby resolves to the header title.
    expect(screen.getByRole('dialog', { name: `✋ ${DE.TEACH_TITLE}` })).toBe(dialog);
    expect(screen.getByText(DE.TEACH_MODE_HAND)).toBeInTheDocument();
    expect(dialog).toHaveFocus();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  test('a pointerup on an action button hands focus back to the container', () => {
    vi.useFakeTimers();
    try {
      withSnapshot({ state: 'fest' });
      render(<TeachOverlay {...baseProps()} />);
      const btn = screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_POSE) });
      btn.focus();
      expect(btn).toHaveFocus();
      fireEvent.pointerUp(btn);
      act(() => { vi.advanceTimersByTime(1); });
      expect(screen.getByRole('dialog')).toHaveFocus();
    } finally {
      vi.useRealTimers();
    }
  });

  test('while a collision is active the overlay is inert and the modal button has focus; then focus returns', () => {
    withSnapshot({ state: 'frei' });
    const modal = document.createElement('div');
    modal.setAttribute('role', 'alertdialog');
    modal.setAttribute('aria-label', 'Kollision erkannt');
    const modalButton = document.createElement('button');
    modalButton.textContent = 'X';
    modal.appendChild(modalButton);
    document.body.appendChild(modal);
    try {
      setState({ collision: true });
      const { rerender } = render(<TeachOverlay {...baseProps()} />);
      const root = screen.getByTestId('teach-overlay');
      expect(root.hasAttribute('inert')).toBe(true);
      expect(root.hasAttribute('aria-modal')).toBe(false);
      expect(modalButton).toHaveFocus();
      setState({ collision: false });
      rerender(<TeachOverlay {...baseProps()} />);
      expect(root.hasAttribute('inert')).toBe(false);
      expect(root.getAttribute('aria-modal')).toBe('true');
      expect(root).toHaveFocus();
    } finally {
      modal.remove();
    }
  });

  test('the collision flag reaches the hook (it ignores every key while the modal owns the keyboard)', () => {
    withSnapshot({ state: 'fest' });
    setState({ collision: true });
    render(<TeachOverlay {...baseProps()} />);
    expect(mockHook.props.collisionActive).toBe(true);
    expect(mockHook.props.leaderLive).toBe(false);
    expect(mockHook.props.heartbeatOk).toBe(true);
    expect(mockHook.props.mode).toBe('hand');
  });

  test('„Fertig (Esc)" in the header finishes', () => {
    const actions = withSnapshot({ state: 'fest' });
    render(<TeachOverlay {...baseProps()} />);
    fireEvent.click(screen.getByRole('button', { name: `${DE.TEACH_DONE} (Esc)` }));
    expect(actions.finish).toHaveBeenCalledTimes(1);
  });
});

describe('TeachOverlay — keeping a take uploads it (and D5 saves the workflow first)', () => {
  test('a keep uploads the compacted take under the next free „Bewegung n" with the robot profile', async () => {
    withSnapshot({ state: 'pruefen', take: TAKE });
    setState({ items: [{ id: 't1', name: 'Bewegung 1' }] });
    workflowApi.createTrajectory.mockResolvedValue({ id: 't2' });
    const props = baseProps();
    render(<TeachOverlay {...props} />);
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    expect(workflowApi.createTrajectory).toHaveBeenCalledTimes(1);
    expect(workflowApi.createTrajectory).toHaveBeenCalledWith('jwt', 'wf-1', {
      name: 'Bewegung 2',
      fps: 25,
      points: compactTrajectoryPoints(TAKE.points),
      duration_s: 1.2,
      robot_profile: 'omx_f',
    });
    expect(props.saveWorkflowNow).not.toHaveBeenCalled();
    expect(props.refetchTrajectories).toHaveBeenCalledTimes(1);
    expect(screen.getByText(formatDe(DE.TEACH_LIST_RECORDING_META, '1,2', DE.TEACH_LIST_SAVED))).toBeInTheDocument();
  });

  test('an empty robot type is omitted from the upload', async () => {
    withSnapshot({ state: 'pruefen', take: TAKE });
    workflowApi.createTrajectory.mockResolvedValue({ id: 't2' });
    render(<TeachOverlay {...baseProps({ robotType: '  ' })} />);
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    expect(workflowApi.createTrajectory.mock.calls[0][2]).not.toHaveProperty('robot_profile');
  });

  test('D5: two quick keeps with no workflow create it ONCE, upload twice and toast once', async () => {
    withSnapshot({ state: 'fest' });
    const saveWorkflowNow = vi.fn()
      .mockResolvedValueOnce({ ok: true, workflowId: 'wf-new', created: true })
      .mockResolvedValueOnce({ ok: true, workflowId: 'wf-new', created: false });
    workflowApi.createTrajectory.mockResolvedValue({ id: 'x' });
    render(<TeachOverlay {...baseProps({ workflowId: null, saveWorkflowNow })} />);
    await act(async () => {
      mockHook.props.onKeep(TAKE);
      mockHook.props.onKeep({ ...TAKE, durationS: 2.5 });
      await flush();
    });
    expect(saveWorkflowNow).toHaveBeenCalledTimes(2);
    expect(saveWorkflowNow).toHaveBeenCalledWith({ toastOnSuccess: false });
    expect(workflowApi.createTrajectory).toHaveBeenCalledTimes(2);
    expect(workflowApi.createTrajectory.mock.calls.map((c) => c[1])).toEqual(['wf-new', 'wf-new']);
    expect(workflowApi.createTrajectory.mock.calls.map((c) => c[2].name)).toEqual(['Bewegung 1', 'Bewegung 2']);
    expect(toast.success.mock.calls.filter((c) => c[0] === DE.TEACH_AUTOSAVED_WORKFLOW)).toHaveLength(1);
  });

  test('a failed workflow save leaves the take „nicht gespeichert" and uploads nothing', async () => {
    withSnapshot({ state: 'fest' });
    const saveWorkflowNow = vi.fn().mockResolvedValue({ ok: false });
    render(<TeachOverlay {...baseProps({ workflowId: null, saveWorkflowNow })} />);
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    expect(workflowApi.createTrajectory).not.toHaveBeenCalled();
    expect(screen.getByText(formatDe(DE.TEACH_LIST_RECORDING_META, '1,2', DE.TEACH_LIST_FAILED))).toBeInTheDocument();
    expect(toast.success).not.toHaveBeenCalled();
  });

  test('an upload failure shows „nicht gespeichert" with the reason, and „Erneut speichern" retries', async () => {
    withSnapshot({ state: 'fest' });
    workflowApi.createTrajectory
      .mockRejectedValueOnce(new Error('Netzwerkfehler'))
      .mockResolvedValueOnce({ id: 't9' });
    render(<TeachOverlay {...baseProps()} />);
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    expect(screen.getByText(formatDe(DE.TEACH_LIST_RECORDING_META, '1,2', DE.TEACH_LIST_FAILED))).toBeInTheDocument();
    expect(screen.getByText('Netzwerkfehler')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: DE.TEACH_LIST_RETRY }));
    await act(async () => { await flush(); });
    expect(workflowApi.createTrajectory).toHaveBeenCalledTimes(2);
    expect(workflowApi.createTrajectory.mock.calls[1][2].name).toBe('Bewegung 1');
    expect(screen.getByText(formatDe(DE.TEACH_LIST_RECORDING_META, '1,2', DE.TEACH_LIST_SAVED))).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: DE.TEACH_LIST_RETRY })).toBeNull();
  });

  test('not signed in: the take fails locally with the German reason', async () => {
    withSnapshot({ state: 'fest' });
    render(<TeachOverlay {...baseProps({ accessToken: null })} />);
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    expect(workflowApi.createTrajectory).not.toHaveBeenCalled();
    expect(screen.getByText('Nicht angemeldet — Speichern nicht möglich.')).toBeInTheDocument();
  });
});

describe('TeachOverlay — captures go into the document store, named on the key press', () => {
  test('P stores a Position with source „capture" and the robot type; the list shows its height', () => {
    withSnapshot({ state: 'fest' });
    mockStore.add.mockImplementation((input) => ({
      ok: true, entry: { id: 'd_1', name: input.name, kind: input.kind, x: input.x, y: input.y, z: input.z },
    }));
    render(<TeachOverlay {...baseProps()} />);
    act(() => {
      mockHook.props.onCapture({
        kind: 'pose', name: 'Position 1', response: { success: true, world_x: 0.2, world_y: -0.05, world_z: 0.118 },
      });
    });
    expect(mockStore.add).toHaveBeenCalledWith({
      name: 'Position 1', kind: 'pose', source: 'capture', x: 0.2, y: -0.05, z: 0.118, robot_type: 'omx_f',
    });
    const row = screen.getByTestId('teach-item-pose');
    expect(within(row).getByText('Position 1')).toBeInTheDocument();
    expect(within(row).getByText('z 118 mm')).toBeInTheDocument();
    // The list is the feedback: a stored capture raises no toast.
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  test('Z stores a Ziel (kind pin) and the list names where it was touched', () => {
    withSnapshot({ state: 'frei' });
    mockStore.add.mockImplementation((input) => ({
      ok: true, entry: { id: 'd_2', name: input.name, kind: input.kind, x: input.x, y: input.y, z: input.z },
    }));
    render(<TeachOverlay {...baseProps()} />);
    act(() => {
      mockHook.props.onCapture({
        kind: 'ziel', name: 'Ziel 1', response: { success: true, world_x: 0.182, world_y: -0.064, world_z: 0.01 },
      });
    });
    expect(mockStore.add.mock.calls[0][0]).toMatchObject({ kind: 'pin', source: 'capture' });
    const row = screen.getByTestId('teach-item-ziel');
    expect(within(row).getByText(`x 182 · y −64 mm · ${DE.CARD_SOURCE_TOUCH}`)).toBeInTheDocument();
  });

  test('S3: the capture\'s joint snapshot is stored when present and the list names the gripper', () => {
    withSnapshot({ state: 'fest' });
    mockStore.add.mockImplementation((input) => ({
      ok: true,
      entry: {
        id: 'd_1', name: input.name, kind: input.kind, x: input.x, y: input.y, z: input.z,
        joints: input.joints, joint_names: input.joint_names,
      },
    }));
    render(<TeachOverlay {...baseProps()} />);
    const names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'];
    act(() => {
      mockHook.props.onCapture({
        kind: 'pose',
        name: 'Position 1',
        response: {
          success: true, world_x: 0.2, world_y: -0.05, world_z: 0.118,
          joint_positions: [0, -0.9, 1.1, 0.3, 0, -0.3], joint_names: names,
        },
      });
    });
    expect(mockStore.add).toHaveBeenCalledWith({
      name: 'Position 1', kind: 'pose', source: 'capture', x: 0.2, y: -0.05, z: 0.118, robot_type: 'omx_f',
      joints: [0, -0.9, 1.1, 0.3, 0, -0.3], joint_names: names,
    });
    const row = screen.getByTestId('teach-item-pose');
    expect(within(row).getByText(`z 118 mm · ${DE.TEACH_GRIPPER_CLOSED}`)).toBeInTheDocument();
  });

  test.each([
    ['absent (older server)', {}],
    ['empty arrays (a refusal default)', { joint_positions: [], joint_names: [] }],
    ['a length mismatch', { joint_positions: [0, 1], joint_names: ['joint1'] }],
  ])('S3: joints are omitted when %s', (_label, extra) => {
    withSnapshot({ state: 'fest' });
    mockStore.add.mockImplementation((input) => ({
      ok: true, entry: { id: 'd_1', name: input.name, kind: input.kind, x: input.x, y: input.y, z: input.z },
    }));
    render(<TeachOverlay {...baseProps()} />);
    act(() => {
      mockHook.props.onCapture({
        kind: 'pose', name: 'Position 1', response: { success: true, world_x: 0.2, world_y: -0.05, world_z: 0.118, ...extra },
      });
    });
    const input = mockStore.add.mock.calls[0][0];
    expect(input).not.toHaveProperty('joints');
    expect(input).not.toHaveProperty('joint_names');
    expect(within(screen.getByTestId('teach-item-pose')).getByText('z 118 mm')).toBeInTheDocument();
  });

  test('a store refusal is toasted and lists nothing', () => {
    withSnapshot({ state: 'fest' });
    mockStore.add.mockReturnValue({ ok: false, error: 'Der Name „Position 1" ist schon vergeben.' });
    render(<TeachOverlay {...baseProps()} />);
    act(() => {
      mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: { success: true, world_x: 0, world_y: 0, world_z: 0 } });
    });
    expect(toast.error).toHaveBeenCalledWith('Der Name „Position 1" ist schon vergeben.');
    expect(screen.queryByTestId('teach-item-pose')).toBeNull();
  });

  test('the capture namer skips taken names and names still on their way to the server', () => {
    withSnapshot({ state: 'fest' });
    mockStore.taken = ['Position 1', 'Ziel 1'];
    render(<TeachOverlay {...baseProps()} />);
    expect(mockHook.namer('pose')).toBe('Position 2');
    expect(mockHook.namer('pose')).toBe('Position 3');
    expect(mockHook.namer('ziel')).toBe('Ziel 2');
  });
});

describe('TeachOverlay — D7: the home glide is offered once, at „Fertig"', () => {
  const cases = [
    ['a confirmed re-lock after a release', { releasedOnce: true, relockOk: true, offline: false }, 1],
    ['a failed re-lock', { releasedOnce: true, relockOk: false, offline: false }, 0],
    ['an offline close', { releasedOnce: true, relockOk: false, offline: true }, 0],
    ['offline even with relockOk', { releasedOnce: true, relockOk: true, offline: true }, 0],
    ['never releasing the arm', { releasedOnce: false, relockOk: true, offline: false }, 0],
  ];
  test.each(cases)('%s', (_label, payload, offers) => {
    withSnapshot({ state: 'fest' });
    const props = baseProps();
    render(<TeachOverlay {...props} />);
    act(() => { mockHook.props.onFinished(payload); });
    act(() => { mockHook.props.onFinished(payload); });
    expect(mockGlide.offer).toHaveBeenCalledTimes(offers);
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });
});

describe('TeachOverlay — banners and the review', () => {
  test('the relock banner shows in frei and pruefen, and „Arm festsetzen" calls actions.lock', () => {
    const actions = withSnapshot({ state: 'frei', relock: 'failed' });
    const { unmount } = render(<TeachOverlay {...baseProps()} />);
    const banner = screen.getByRole('alert');
    expect(within(banner).getByText(DE.TEACH_RELOCK_FAILED)).toBeInTheDocument();
    fireEvent.click(within(banner).getByRole('button', { name: DE.TEACH_KEY_LOCK }));
    expect(actions.lock).toHaveBeenCalledTimes(1);
    unmount();
    withSnapshot({ state: 'pruefen', relock: 'failed', take: TAKE });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.getByText(DE.TEACH_RELOCK_FAILED)).toBeInTheDocument();
  });

  test.each(['aufnahme', 'countdown', 'vorschau', 'fest'])('no relock banner in %s', (state) => {
    withSnapshot({ state, relock: 'failed', take: state === 'vorschau' ? TAKE : null, countdownLeft: 2 });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.queryByText(DE.TEACH_RELOCK_FAILED)).toBeNull();
  });

  test('„Auf dem Roboter ansehen" asks first and only then starts the preview', () => {
    const actions = withSnapshot({ state: 'pruefen', relock: 'ok', take: TAKE });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    try {
      render(<TeachOverlay {...baseProps()} />);
      const btn = screen.getByRole('button', { name: DE.TEACH_REVIEW_ON_ROBOT });
      expect(btn).toBeEnabled();
      fireEvent.click(btn);
      expect(confirmSpy).toHaveBeenCalledWith(DE.TEACH_REVIEW_ON_ROBOT_CONFIRM);
      expect(actions.previewOnRobot).not.toHaveBeenCalled();
      fireEvent.click(btn);
      expect(actions.previewOnRobot).toHaveBeenCalledTimes(1);
    } finally {
      confirmSpy.mockRestore();
    }
  });

  test.each([
    ['the re-lock failed', { relock: 'failed' }, {}],
    ['a service call is in flight', { busy: true }, {}],
    ['offline', {}, { heartbeatOk: false }],
  ])('„Auf dem Roboter ansehen" is disabled while %s', (_l, snap, props) => {
    withSnapshot({ state: 'pruefen', relock: 'ok', take: TAKE, ...snap });
    render(<TeachOverlay {...baseProps(props)} />);
    expect(screen.getByRole('button', { name: DE.TEACH_REVIEW_ON_ROBOT })).toBeDisabled();
  });

  test('„Auf dem Roboter ansehen" is disabled during a home glide', () => {
    mockGlide.active = true;
    withSnapshot({ state: 'pruefen', relock: 'ok', take: TAKE });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.getByRole('button', { name: DE.TEACH_REVIEW_ON_ROBOT })).toBeDisabled();
  });

  test('the review names the take and its size, and Enter/R/Entf map to keep/again/discard', () => {
    const actions = withSnapshot({ state: 'pruefen', relock: 'ok', take: TAKE });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.getByText(formatDe(DE.TEACH_REVIEW_META, 'Bewegung 1', '1,2', 3))).toBeInTheDocument();
    expect(within(screen.getByTestId('teach-list-reviewing')).getByText(DE.TEACH_LIST_REVIEWING)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: new RegExp(DE.TEACH_REVIEW_KEEP) }));
    fireEvent.click(screen.getByRole('button', { name: new RegExp(DE.TEACH_REVIEW_AGAIN) }));
    fireEvent.click(screen.getByRole('button', { name: new RegExp(DE.TEACH_REVIEW_DISCARD) }));
    expect(actions.keep).toHaveBeenCalledTimes(1);
    expect(actions.again).toHaveBeenCalledTimes(1);
    expect(actions.discard).toHaveBeenCalledTimes(1);
  });

  test('vorschau shows the running banner, the no-motion hint when reported, and „Stopp" stops', () => {
    const actions = withSnapshot({ state: 'vorschau', relock: 'ok', take: TAKE, previewNoMotionHint: true });
    render(<TeachOverlay {...baseProps()} />);
    const status = screen.getByRole('status');
    expect(within(status).getByText(DE.TEACH_ROBOT_PREVIEW_RUNNING)).toBeInTheDocument();
    expect(within(status).getByText(DE.TEACH_ROBOT_PREVIEW_NO_MOTION)).toBeInTheDocument();
    fireEvent.click(within(status).getByRole('button', { name: DE.TEACH_ROBOT_PREVIEW_STOP }));
    expect(actions.stopPreview).toHaveBeenCalledTimes(1);
  });

  test('vorschau without the hint shows only the running line', () => {
    withSnapshot({ state: 'vorschau', relock: 'ok', take: TAKE, previewNoMotionHint: false });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.queryByText(DE.TEACH_ROBOT_PREVIEW_NO_MOTION)).toBeNull();
    expect(screen.getByText(DE.TEACH_ROBOT_PREVIEW_RUNNING)).toBeInTheDocument();
  });

  test('offline and leader-on banners', () => {
    withSnapshot({ state: 'fest' });
    const { rerender } = render(<TeachOverlay {...baseProps({ heartbeatOk: false })} />);
    expect(screen.getByText(DE.TEACH_OFFLINE)).toBeInTheDocument();
    const leaderOn = { available: true, followerOnly: false, busy: false, leaderOn: true };
    rerender(<TeachOverlay {...baseProps({ rsBridge: leaderOn })} />);
    expect(screen.getByText(DE.TEACH_LEADER_TURNED_ON)).toBeInTheDocument();
    expect(mockHook.props.leaderLive).toBe(true);
  });
});

function addPose(name = 'Position 1') {
  mockStore.add.mockImplementation((input) => ({
    ok: true, entry: { id: `id-${input.name.replace(' ', '')}`, name: input.name, kind: input.kind, x: 0.1, y: 0, z: 0.118 },
  }));
  act(() => {
    mockHook.props.onCapture({ kind: 'pose', name, response: { success: true, world_x: 0.1, world_y: 0, world_z: 0.118 } });
  });
}

describe('TeachOverlay — the summary and renaming (never while the arm is limp)', () => {
  test('abschluss shows the list with ✎ enabled and „Weiter vormachen" / „Schließen" — no action buttons', () => {
    const actions = withSnapshot({ state: 'abschluss' });
    render(<TeachOverlay {...baseProps()} />);
    addPose();
    expect(screen.getByText(DE.TEACH_STATE_DONE)).toBeInTheDocument();
    expect(screen.getByText(DE.TEACH_HINT_DONE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Position 1` })).toBeEnabled();
    expect(screen.queryByRole('button', { name: new RegExp(DE.TEACH_KEY_POSE) })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: DE.TEACH_CONTINUE }));
    fireEvent.click(screen.getByRole('button', { name: new RegExp(DE.TEACH_CLOSE) }));
    expect(actions.continueTeaching).toHaveBeenCalledTimes(1);
    expect(actions.finish).toHaveBeenCalledTimes(1);
  });

  test.each(['frei', 'aufnahme', 'countdown', 'vorschau'])('✎ is disabled in %s with the „locked" title', (state) => {
    withSnapshot({ state, take: state === 'vorschau' ? TAKE : null, countdownLeft: 2 });
    render(<TeachOverlay {...baseProps()} />);
    addPose();
    const pencil = screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Position 1` });
    expect(pencil).toBeDisabled();
    expect(pencil.getAttribute('title')).toBe(DE.TEACH_RENAME_LOCKED);
  });

  test('✎ is disabled in pruefen while the re-lock failed, enabled once it is ok', () => {
    withSnapshot({ state: 'pruefen', relock: 'failed', take: TAKE });
    const { unmount } = render(<TeachOverlay {...baseProps()} />);
    addPose();
    expect(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Position 1` })).toBeDisabled();
    unmount();
    withSnapshot({ state: 'pruefen', relock: 'ok', take: TAKE });
    render(<TeachOverlay {...baseProps()} />);
    addPose();
    expect(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Position 1` })).toBeEnabled();
  });

  test('renaming a Position goes through renamePlace and relabels the row', async () => {
    withSnapshot({ state: 'fest' });
    renamePlace.mockReturnValue({ ok: true, oldName: 'Position 1', entry: { id: 'id-Position1', name: 'Kiste' } });
    const props = baseProps();
    render(<TeachOverlay {...props} />);
    addPose();
    fireEvent.click(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Position 1` }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Kiste!' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    await act(async () => { await flush(); });
    // The Ziel/Position alphabet is enforced while typing.
    expect(renamePlace).toHaveBeenCalledWith({ workspace: props.workspace, entryId: 'id-Position1', toName: 'Kiste' });
    expect(within(screen.getByTestId('teach-item-pose')).getByText('Kiste')).toBeInTheDocument();
  });

  test('a recording is renamable only once saved, through renameRecording', async () => {
    withSnapshot({ state: 'fest' });
    let resolveUpload;
    workflowApi.createTrajectory.mockImplementation(() => new Promise((r) => { resolveUpload = r; }));
    renameRecording.mockResolvedValue({ ok: true });
    const props = baseProps();
    render(<TeachOverlay {...props} />);
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    expect(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Bewegung 1` })).toBeDisabled();
    await act(async () => { resolveUpload({ id: 't1' }); await flush(); });
    fireEvent.click(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Bewegung 1` }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Winken' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    await act(async () => { await flush(); });
    expect(renameRecording).toHaveBeenCalledTimes(1);
    expect(renameRecording.mock.calls[0][0]).toMatchObject({
      workspace: props.workspace, api: workflowApi, accessToken: 'jwt', workflowId: 'wf-1',
      fromName: 'Bewegung 1', toName: 'Winken', saveWorkflowNow: props.saveWorkflowNow,
    });
    expect(screen.getByText('Winken')).toBeInTheDocument();
  });

  test('a refused rename is toasted and keeps the field open', async () => {
    withSnapshot({ state: 'fest' });
    renamePlace.mockReturnValue({ ok: false, error: 'Der Name „Kiste" ist schon vergeben.' });
    render(<TeachOverlay {...baseProps()} />);
    addPose();
    fireEvent.click(screen.getByRole('button', { name: `${DE.DRAWER_RENAME}: Position 1` }));
    fireEvent.change(screen.getByRole('textbox', { name: DE.DRAWER_NAME }), { target: { value: 'Kiste' } });
    fireEvent.click(screen.getByRole('button', { name: DE.DRAWER_SAVE_NAME }));
    await act(async () => { await flush(); });
    expect(toast.error).toHaveBeenCalledWith('Der Name „Kiste" ist schon vergeben.');
    expect(screen.getByRole('textbox', { name: DE.DRAWER_NAME })).toBeInTheDocument();
  });
});

describe('TeachOverlay — list meta and slot lines', () => {
  test('list meta strings per kind, verbatim', () => {
    expect(teachListMeta({ kind: 'recording', durationS: 3.1, status: 'saving' })).toBe('3,1 s · wird gespeichert …');
    expect(teachListMeta({ kind: 'recording', durationS: 3.1, status: 'saved' })).toBe('3,1 s · gespeichert');
    expect(teachListMeta({ kind: 'recording', durationS: 3.1, status: 'failed' })).toBe('3,1 s · nicht gespeichert');
    expect(teachListMeta({ kind: 'pose', z: 0.118 })).toBe('z 118 mm');
    expect(teachListMeta({ kind: 'pose', z: 0.118, gripper: 'open' })).toBe('z 118 mm · Greifer offen');
    expect(teachListMeta({ kind: 'pose', z: 0.118, gripper: 'closed' })).toBe('z 118 mm · Greifer zu');
    expect(teachListMeta({ kind: 'pose', z: 0.118, gripper: null })).toBe('z 118 mm');
    expect(teachListMeta({ kind: 'ziel', x: 0.182, y: -0.064, z: 0 })).toBe('x 182 · y −64 mm · am Tisch');
  });

  test('slot line: nothing below 14, „noch n" at 14 and 15, full at 16', () => {
    expect(teachSlotsLine(13)).toBeNull();
    expect(teachSlotsLine(14)).toBe('Noch 2 Plätze für Aufnahmen frei.');
    expect(teachSlotsLine(15)).toBe('Noch 1 Plätze für Aufnahmen frei.');
    expect(teachSlotsLine(16)).toBe(DE.TEACH_SLOTS_FULL);
  });

  test.each([[14, 'Noch 2 Plätze für Aufnahmen frei.'], [16, DE.TEACH_SLOTS_FULL]])(
    'the review shows the slot line with %i rows in the cloud',
    (n, text) => {
      withSnapshot({ state: 'pruefen', relock: 'ok', take: TAKE });
      setState({ items: Array.from({ length: n }, (_, i) => ({ id: `t${i}`, name: `Alt ${i}` })) });
      render(<TeachOverlay {...baseProps()} />);
      expect(within(screen.getByTestId('teach-review')).getByText(text)).toBeInTheDocument();
    },
  );

  test('a pending upload counts toward the slots', async () => {
    workflowApi.createTrajectory.mockImplementation(() => new Promise(() => {}));
    withSnapshot({ state: 'pruefen', relock: 'ok', take: TAKE });
    setState({ items: Array.from({ length: 13 }, (_, i) => ({ id: `t${i}`, name: `Alt ${i}` })) });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.queryByText(/Plätze für Aufnahmen/)).toBeNull();
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    expect(screen.getByText('Noch 2 Plätze für Aufnahmen frei.')).toBeInTheDocument();
  });
});

describe('TeachOverlay — with the real session hook', () => {
  function deferred() {
    let resolve;
    const promise = new Promise((res) => { resolve = res; });
    return { promise, resolve };
  }
  function queueResponses(fn) {
    const pending = [];
    fn.mockImplementation(() => {
      const d = deferred();
      pending.push(d);
      return d.promise;
    });
    return pending;
  }
  const key = (k, over = {}) => {
    const dialog = screen.getByRole('dialog');
    const ev = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...over });
    act(() => { dialog.dispatchEvent(ev); });
    return ev;
  };

  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  test('F frees the arm after the countdown, Esc locks it, and „Fertig" offers the glide once and closes', async () => {
    const hand = queueResponses(mockRos.handGuide);
    const props = baseProps();
    render(<TeachOverlay {...props} />);
    expect(screen.getByText(DE.TEACH_STATE_LOCKED)).toBeInTheDocument();
    expect(key('f').defaultPrevented).toBe(true);
    expect(screen.getByText(formatDe(DE.TEACH_COUNTDOWN, 3))).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(3000); await flush(); });
    expect(mockRos.handGuide).toHaveBeenCalledWith(true);
    await act(async () => { hand[0].resolve({ success: true }); await flush(); });
    expect(screen.getByText(DE.TEACH_STATE_FREE)).toBeInTheDocument();
    key('Escape');
    await act(async () => { await flush(); });
    expect(mockRos.handGuide).toHaveBeenLastCalledWith(false);
    expect(props.onClose).not.toHaveBeenCalled();
    await act(async () => { hand[1].resolve({ success: true }); await flush(); });
    expect(mockGlide.offer).toHaveBeenCalledTimes(1);
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  test('P captures under the overlay\'s automatic name and the list shows it', async () => {
    const caps = queueResponses(mockRos.capturePose);
    mockStore.taken = ['Position 1'];
    mockStore.add.mockImplementation((input) => ({
      ok: true, entry: { id: 'd_9', name: input.name, kind: input.kind, x: input.x, y: input.y, z: input.z },
    }));
    render(<TeachOverlay {...baseProps()} />);
    key('p');
    await act(async () => { await flush(); });
    expect(mockRos.capturePose).toHaveBeenCalledWith('Position 2');
    await act(async () => {
      caps[0].resolve({ success: true, world_x: 0.1, world_y: 0.02, world_z: 0.05, message: 'ok' });
      await flush();
    });
    expect(mockStore.add.mock.calls[0][0]).toMatchObject({ name: 'Position 2', kind: 'pose', source: 'capture' });
    expect(within(screen.getByTestId('teach-item-pose')).getByText('z 50 mm')).toBeInTheDocument();
  });

  test('during a collision the overlay passes keys through untouched', () => {
    setState({ collision: true });
    render(<TeachOverlay {...baseProps()} />);
    const ev = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
    act(() => { document.body.dispatchEvent(ev); });
    expect(ev.defaultPrevented).toBe(false);
    expect(mockRos.recordControl).not.toHaveBeenCalled();
    expect(mockRos.handGuide).not.toHaveBeenCalled();
  });

  test('closing the overlay removes its keyboard listener', () => {
    const { unmount } = render(<TeachOverlay {...baseProps()} />);
    unmount();
    const ev = new KeyboardEvent('keydown', { key: 'f', bubbles: true, cancelable: true });
    document.body.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(false);
  });
});

describe('TeachOverlay — review clean-up (trims with undo, cleaned rows kept and previewed)', () => {
  // A 2.346 s wait before the first move, then a release: the last 280 ms
  // joint2 falls at −5 rad/s.
  function cleanupTake() {
    const points = [[0, 0.5, -0.5, 0, 0, 0.8, 0]];
    for (let ms = 2346; ms <= 3346; ms += 40) points.push([0.001 * (ms - 2346), 0.5, -0.5, 0, 0, 0.8, ms / 1000]);
    const onset = points.length - 1;
    let q2 = 0.5;
    for (let i = 1; i <= 7; i += 1) {
      q2 -= 0.2;
      points.push([1, q2, -0.5, 0, 0, 0.8, (3346 + 40 * i) / 1000]);
    }
    return { take: { points, fps: 25, sampleCount: points.length, durationS: 3.626, relockOk: true }, onset };
  }

  test('the notes name both trims; „Kürzung zurücknehmen" restores the end and a keep then stores it all', async () => {
    const { take, onset } = cleanupTake();
    withSnapshot({ state: 'pruefen', relock: 'ok', take });
    workflowApi.createTrajectory.mockResolvedValue({ id: 't' });
    render(<TeachOverlay {...baseProps()} />);
    const review = screen.getByTestId('teach-review');
    expect(within(review).getByText(DE.TEACH_TRIM_START)).toBeInTheDocument();
    expect(within(review).getByText(DE.TEACH_TRIM_END)).toBeInTheDocument();
    expect(within(review).getByRole('slider', { name: DE.TEACH_HANDLE_END })).toHaveAttribute('aria-valuenow', String(onset));
    expect(within(review).getByText(formatDe(DE.TEACH_REVIEW_META, 'Bewegung 1', '1,3', onset + 1))).toBeInTheDocument();
    fireEvent.click(within(review).getByRole('button', { name: DE.TEACH_TRIM_UNDO }));
    expect(within(review).queryByText(DE.TEACH_TRIM_END)).toBeNull();
    expect(within(review).getByText(DE.TEACH_TRIM_START)).toBeInTheDocument();
    expect(within(review).getByRole('slider', { name: DE.TEACH_HANDLE_END }))
      .toHaveAttribute('aria-valuenow', String(take.points.length - 1));
    await act(async () => { mockHook.props.onKeep(take); await flush(); });
    const sent = workflowApi.createTrajectory.mock.calls[0][2];
    expect(sent.points).toHaveLength(take.points.length);
    expect(sent.duration_s).toBe(1.58);
  });

  test('a take with a 2.346 s leading gap keeps with duration_s = the cleaned rows\' last time', async () => {
    const { take, onset } = cleanupTake();
    withSnapshot({ state: 'pruefen', relock: 'ok', take });
    workflowApi.createTrajectory.mockResolvedValue({ id: 't' });
    render(<TeachOverlay {...baseProps()} />);
    await act(async () => { mockHook.props.onKeep(take); await flush(); });
    const expected = compactTrajectoryPoints(applyCleanup(take.points, { endIndex: onset }));
    const sent = workflowApi.createTrajectory.mock.calls[0][2];
    expect(sent.points).toEqual(expected);
    expect(sent.points[1][6]).toBe(0.3);
    expect(sent.duration_s).toBe(expected[expected.length - 1][6]);
    expect(sent.duration_s).toBe(1.3);
    expect(sent.duration_s).not.toBe(take.durationS);
    expect(screen.getByText(formatDe(DE.TEACH_LIST_RECORDING_META, '1,3', DE.TEACH_LIST_SAVED))).toBeInTheDocument();
  });

  test('„Auf dem Roboter ansehen" previews exactly the cleaned rows', () => {
    const { take, onset } = cleanupTake();
    const actions = withSnapshot({ state: 'pruefen', relock: 'ok', take });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      render(<TeachOverlay {...baseProps()} />);
      fireEvent.click(screen.getByRole('button', { name: DE.TEACH_REVIEW_ON_ROBOT }));
      expect(actions.previewOnRobot).toHaveBeenCalledWith(
        compactTrajectoryPoints(applyCleanup(take.points, { endIndex: onset })),
      );
    } finally {
      confirm.mockRestore();
    }
  });

  test('„Pausen kürzen" shows only when the take has a pause, and changes what a keep stores', async () => {
    const points = [[0, 0, 0, 0, 0, 0.8, 0], [0.1, 0, 0, 0, 0, 0.8, 0.2], [0.2, 0, 0, 0, 0, 0.8, 2.2],
      [0.3, 0, 0, 0, 0, 0.8, 2.4]];
    const take = { points, fps: 25, sampleCount: 4, durationS: 2.4, relockOk: true };
    withSnapshot({ state: 'pruefen', relock: 'ok', take });
    workflowApi.createTrajectory.mockResolvedValue({ id: 't' });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.queryByText(DE.TEACH_TRIM_START)).toBeNull();
    const box = screen.getByRole('checkbox', { name: DE.TEACH_PAUSES });
    expect(box).not.toBeChecked();
    fireEvent.click(box);
    await act(async () => { mockHook.props.onKeep(take); await flush(); });
    expect(workflowApi.createTrajectory.mock.calls[0][2].duration_s).toBe(0.9);
  });

  test('no pause → no checkbox; the clean-up controls are locked while the robot preview runs', () => {
    const { take } = cleanupTake();
    withSnapshot({ state: 'vorschau', relock: 'ok', take });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.queryByRole('checkbox', { name: DE.TEACH_PAUSES })).toBeNull();
    expect(screen.getByRole('button', { name: DE.TEACH_TRIM_UNDO })).toBeDisabled();
    expect(screen.getByRole('slider', { name: DE.TEACH_HANDLE_END })).toHaveAttribute('tabindex', '-1');
  });
});

describe('TeachOverlay — Ziel by touch: too high asks „Als Position speichern?"', () => {
  const HIGH = { success: true, world_x: 0.18, world_y: -0.06, world_z: 0.16 }; // OMX: 120 mm above the table

  function storeEchoes() {
    mockStore.add.mockImplementation((input) => ({
      ok: true, entry: { id: `d_${input.name}`, name: input.name, kind: input.kind, x: input.x, y: input.y, z: input.z },
    }));
  }

  function captureHigh() {
    act(() => { mockHook.props.onCapture({ kind: 'ziel', name: 'Ziel 1', response: HIGH }); });
  }

  test('the question names the height in cm, stores nothing yet, and focuses „Als Position speichern"', () => {
    withSnapshot({ state: 'frei' });
    storeEchoes();
    render(<TeachOverlay {...baseProps()} />);
    captureHigh();
    const dialog = screen.getByRole('alertdialog');
    expect(within(dialog).getByText(formatDe(DE.TEACH_ZIEL_TOO_HIGH, '12,0'))).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: new RegExp(DE.TEACH_ZIEL_AS_POSE) })).toHaveFocus();
    expect(mockStore.add).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_ZIEL) })).toBeDisabled();
  });

  test.each([['Enter'], ['Escape']])('%s stores a Position (measured z kept, named as a Position)', (key) => {
    withSnapshot({ state: 'frei' });
    storeEchoes();
    render(<TeachOverlay {...baseProps()} />);
    captureHigh();
    fireEvent.keyDown(screen.getByRole('dialog'), { key });
    expect(mockStore.add).toHaveBeenCalledTimes(1);
    expect(mockStore.add).toHaveBeenCalledWith({
      name: 'Position 1', kind: 'pose', source: 'capture', x: 0.18, y: -0.06, z: 0.16, robot_type: 'omx_f',
    });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(within(screen.getByTestId('teach-item-pose')).getByText('Position 1')).toBeInTheDocument();
  });

  test('„Trotzdem als Ziel" stores a Ziel under its own name', () => {
    withSnapshot({ state: 'frei' });
    storeEchoes();
    render(<TeachOverlay {...baseProps()} />);
    captureHigh();
    fireEvent.click(screen.getByRole('button', { name: DE.TEACH_ZIEL_AS_PIN }));
    expect(mockStore.add).toHaveBeenCalledWith(expect.objectContaining({ name: 'Ziel 1', kind: 'pin', source: 'capture', z: 0.16 }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.getByTestId('teach-item-ziel')).toBeInTheDocument();
  });

  test('a touch within 30 mm of the table is a Ziel without a question; the threshold is per arm', () => {
    withSnapshot({ state: 'frei' });
    storeEchoes();
    const { unmount } = render(<TeachOverlay {...baseProps()} />);
    act(() => { mockHook.props.onCapture({ kind: 'ziel', name: 'Ziel 1', response: { ...HIGH, world_z: 0.07 } }); });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(mockStore.add.mock.calls[0][0]).toMatchObject({ kind: 'pin', z: 0.07 });
    unmount();
    // Edu:6: the TCP is the fingertip, so the same 0.07 m is 70 mm up.
    render(<TeachOverlay {...baseProps({ caps: { urdf_asset_id: 'edu6', arm_joints: 6 } })} />);
    act(() => { mockHook.props.onCapture({ kind: 'ziel', name: 'Ziel 2', response: { ...HIGH, world_z: 0.07 } }); });
    expect(within(screen.getByRole('alertdialog')).getByText(formatDe(DE.TEACH_ZIEL_TOO_HIGH, '7,0'))).toBeInTheDocument();
  });

  test('P is never questioned; closing with a question open keeps the capture as a Position', () => {
    withSnapshot({ state: 'frei' });
    storeEchoes();
    const { unmount } = render(<TeachOverlay {...baseProps()} />);
    act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: HIGH }); });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    mockStore.taken = ['Position 1'];
    captureHigh();
    unmount();
    expect(mockStore.add.mock.calls.map((c) => [c[0].name, c[0].kind])).toEqual([
      ['Position 1', 'pose'], ['Position 2', 'pose'],
    ]);
  });
});

describe('TeachOverlay — „Als Programm einfügen"', () => {
  async function fillRound() {
    workflowApi.createTrajectory.mockResolvedValue({ id: 't' });
    mockStore.add.mockImplementation((input) => ({
      ok: true, entry: { id: 'd_p1', name: input.name, kind: input.kind, x: input.x, y: input.y, z: input.z },
    }));
    mockStore.getById.mockImplementation((id) => (id === 'd_p1' ? { id, name: 'Position 1' } : null));
    await act(async () => { mockHook.props.onKeep(TAKE); await flush(); });
    act(() => {
      mockHook.props.onCapture({ kind: 'pose', name: 'Position 1', response: { success: true, world_x: 0.1, world_y: 0, world_z: 0.1 } });
    });
  }

  test.each(['fest', 'abschluss'])('in %s the button counts the insertable items and inserts them with a toast', async (state) => {
    withSnapshot({ state });
    const props = baseProps();
    render(<TeachOverlay {...props} />);
    expect(screen.getByRole('button', { name: formatDe(DE.TEACH_INSERT, 0) })).toBeDisabled();
    await fillRound();
    const button = screen.getByRole('button', { name: formatDe(DE.TEACH_INSERT, 2) });
    expect(button).toBeEnabled();
    mockInsert.fn.mockReturnValue({ blockId: 'b1', count: 2 });
    fireEvent.click(button);
    expect(mockInsert.fn).toHaveBeenCalledTimes(1);
    const [ws, items, opts] = mockInsert.fn.mock.calls[0];
    expect(ws).toBe(props.workspace);
    expect(items.map((it) => it.name)).toEqual(['Bewegung 1', 'Position 1']);
    expect(opts.placeNameOf(items[1])).toBe('Position 1');
    expect(toast.success).toHaveBeenCalledWith(formatDe(DE.TEACH_INSERT_DONE, 2));
  });

  test('a place no longer in the store does not count', async () => {
    withSnapshot({ state: 'fest' });
    render(<TeachOverlay {...baseProps()} />);
    await fillRound();
    mockStore.getById.mockReturnValue(null);
    act(() => { mockHook.props.onCapture({ kind: 'pose', name: 'Position 2', response: { success: true, world_x: 0.1, world_y: 0, world_z: 0.1 } }); });
    expect(screen.getByRole('button', { name: formatDe(DE.TEACH_INSERT, 1) })).toBeInTheDocument();
  });

  test('„Greifer merken": a changed captured gripper state adds a gripper block to the count and the insert', () => {
    withSnapshot({ state: 'fest' });
    const props = baseProps();
    render(<TeachOverlay {...props} />);
    const names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'];
    const entries = {};
    let seq = 0;
    mockStore.add.mockImplementation((input) => {
      seq += 1;
      const entry = { id: `d_${seq}`, ...input };
      entries[entry.id] = entry;
      return { ok: true, entry };
    });
    mockStore.getById.mockImplementation((id) => entries[id] || null);
    const capture = (name, grip) => act(() => {
      mockHook.props.onCapture({
        kind: 'pose',
        name,
        response: {
          success: true, world_x: 0.1, world_y: 0, world_z: 0.1,
          joint_positions: [0, 0, 0, 0, 0, grip], joint_names: names,
        },
      });
    });
    capture('Position 1', 0.8);
    capture('Position 2', -0.3);
    // two moves + „schließe Greifer" after the second.
    const button = screen.getByRole('button', { name: formatDe(DE.TEACH_INSERT, 3) });
    mockInsert.fn.mockReturnValue({ blockId: 'b1', count: 3 });
    fireEvent.click(button);
    const [, items, opts] = mockInsert.fn.mock.calls[0];
    expect(opts.gripperStateOf(items[0])).toEqual({ state: 'open' });
    expect(opts.gripperStateOf(items[1])).toEqual({ state: 'closed' });
  });
});

describe('TeachOverlay — leader mode (D8)', () => {
  const LEADER_BRIDGE = { available: true, followerOnly: false, hasLeader: true, busy: false, leaderOn: true };
  const leaderProps = (over = {}) => baseProps({ mode: 'leader', caps: { has_leader: true }, rsBridge: LEADER_BRIDGE, ...over });

  test('bereit: leader header, state and hint lines, no F button, the light-touch Z hint', () => {
    withSnapshot({ state: 'bereit' });
    render(<TeachOverlay {...leaderProps()} />);
    expect(screen.getByText(DE.TEACH_MODE_LEADER)).toBeInTheDocument();
    expect(screen.queryByText(DE.TEACH_MODE_HAND)).toBeNull();
    expect(screen.getByText(DE.TEACH_STATE_LEADER_READY)).toBeInTheDocument();
    expect(screen.getByText(DE.TEACH_HINT_LEADER)).toBeInTheDocument();
    expect(screen.queryByText(DE.TEACH_KEY_FREE)).toBeNull();
    expect(screen.queryByText(DE.TEACH_KEY_LOCK)).toBeNull();
    const z = screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_ZIEL) });
    expect(within(z).getByText(DE.TEACH_LEADER_ZIEL_HINT)).toBeInTheDocument();
    expect(z).toBeEnabled();
    expect(screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_REC) })).toBeEnabled();
    expect(mockHook.props).toMatchObject({ mode: 'leader', leaderGone: false, activationBlocked: false });
  });

  test('aufnahme: the leader recording hint, Stop, P and Z enabled (no „Erst Aufnahme beenden")', () => {
    withSnapshot({ state: 'aufnahme', elapsedS: 3 });
    render(<TeachOverlay {...leaderProps()} />);
    expect(screen.getByText(DE.TEACH_STATE_REC)).toBeInTheDocument();
    expect(screen.getByText(DE.TEACH_HINT_LEADER_REC)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_STOP) })).toBeEnabled();
    const z = screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_ZIEL) });
    expect(z).toBeEnabled();
    expect(z).not.toHaveAttribute('title', DE.TEACH_ZIEL_BLOCKED_REC);
  });

  test('the review never offers a real-arm replay, and keep waits out the grace window', () => {
    const actions = withSnapshot({ state: 'pruefen', take: TAKE, keepHeld: true });
    const { rerender } = render(<TeachOverlay {...leaderProps()} />);
    const review = screen.getByTestId('teach-review');
    expect(within(review).queryByRole('button', { name: DE.TEACH_REVIEW_ON_ROBOT })).toBeNull();
    expect(within(review).getByTestId('teach-review-on-robot-leader'))
      .toHaveTextContent(DE.TEACH_REVIEW_ON_ROBOT_LEADER);
    const held = within(review).getByRole('button', { name: new RegExp(DE.TEACH_LIST_REVIEWING) });
    expect(held).toBeDisabled();
    expect(within(review).queryByRole('button', { name: new RegExp(DE.TEACH_REVIEW_KEEP) })).toBeNull();
    withSnapshot({ state: 'pruefen', take: TAKE, keepHeld: false });
    mockHook.actions = actions;
    rerender(<TeachOverlay {...leaderProps()} />);
    const keepBtn = screen.getByRole('button', { name: new RegExp(DE.TEACH_REVIEW_KEEP) });
    expect(keepBtn).toBeEnabled();
    fireEvent.click(keepBtn);
    expect(actions.keep).toHaveBeenCalledTimes(1);
    expect(actions.previewOnRobot).not.toHaveBeenCalled();
  });

  test.each(['idle', 'activating', 'failed'])('activation %s: the blocked panel replaces the teaching content', (state) => {
    mockActivation.status = { state, step: '', message: '', robotType: 'omx_full', hasLeader: true, required: true };
    withSnapshot({ state: 'bereit' });
    render(<TeachOverlay {...leaderProps()} />);
    expect(screen.getByText(DE.TEACH_BLOCK_NOT_ACTIVE)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: new RegExp(DE.TEACH_KEY_REC) })).toBeNull();
    expect(mockHook.props.activationBlocked).toBe(true);
    // „Fertig (Esc)" stays.
    expect(screen.getByRole('button', { name: `${DE.TEACH_DONE} (Esc)` })).toBeInTheDocument();
  });

  test('activation active or unknown: teaching content, not blocked', () => {
    mockActivation.status = { state: 'active', step: '', message: '', robotType: 'omx_full', hasLeader: true, required: true };
    withSnapshot({ state: 'bereit' });
    render(<TeachOverlay {...leaderProps()} />);
    expect(screen.queryByText(DE.TEACH_BLOCK_NOT_ACTIVE)).toBeNull();
    expect(mockHook.props.activationBlocked).toBe(false);
  });

  test('hand mode never renders the activation gate', () => {
    mockActivation.status = { state: 'idle', step: '', message: '', robotType: 'omx_full', hasLeader: true, required: true };
    withSnapshot({ state: 'fest' });
    render(<TeachOverlay {...baseProps()} />);
    expect(screen.queryByText(DE.TEACH_BLOCK_NOT_ACTIVE)).toBeNull();
    expect(mockHook.props.activationBlocked).toBe(false);
    expect(mockHook.props.leaderGone).toBe(false);
  });

  test('leaderGone needs a POSITIVE follower-only answer; a failed probe is not leader-gone', () => {
    withSnapshot({ state: 'aufnahme' });
    const { rerender } = render(<TeachOverlay {...leaderProps({ rsBridge: { available: false, followerOnly: false, leaderOn: false } })} />);
    expect(mockHook.props.leaderGone).toBe(false);
    expect(screen.queryByText(DE.TEACH_LEADER_GONE)).toBeNull();
    expect(screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_STOP) })).toBeEnabled();
    rerender(<TeachOverlay {...leaderProps({ rsBridge: { available: true, followerOnly: true, leaderOn: false } })} />);
    expect(mockHook.props.leaderGone).toBe(true);
    expect(screen.getByText(DE.TEACH_LEADER_GONE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: new RegExp(DE.TEACH_KEY_STOP) })).toBeDisabled();
    // Never the hand-mode lock-out banner in leader mode.
    expect(screen.queryByText(DE.TEACH_LEADER_TURNED_ON)).toBeNull();
  });

  test('a take already running: „Alte Aufnahme verwerfen" calls discardStaleLeaderTake', () => {
    const actions = withSnapshot({ state: 'bereit', staleLeaderTake: true });
    render(<TeachOverlay {...leaderProps()} />);
    fireEvent.click(screen.getByRole('button', { name: DE.TEACH_LEADER_DISCARD_OLD }));
    expect(actions.discardStaleLeaderTake).toHaveBeenCalledTimes(1);
  });

  test('✎ rename: every state but aufnahme in leader mode (the follower stays torqued)', () => {
    ['bereit', 'pruefen', 'abschluss'].forEach((st) => expect(teachRenameEnabled(st, 'none', 'leader')).toBe(true));
    expect(teachRenameEnabled('aufnahme', 'none', 'leader')).toBe(false);
    expect(teachRenameEnabled('aufnahme', 'none')).toBe(false);
    expect(teachRenameEnabled('fest', 'none')).toBe(true);
  });
});

describe('TeachOverlay — leader mode with the real session hook (D8)', () => {
  const LEADER_BRIDGE = { available: true, followerOnly: false, hasLeader: true, busy: false, leaderOn: true };
  const leaderProps = (over = {}) => baseProps({ mode: 'leader', caps: { has_leader: true }, rsBridge: LEADER_BRIDGE, ...over });
  function pendingOf(fn) {
    const pending = [];
    fn.mockImplementation(() => new Promise((resolve) => { pending.push(resolve); }));
    return pending;
  }
  const key = (k) => {
    const ev = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true });
    act(() => { screen.getByRole('dialog').dispatchEvent(ev); });
    return ev;
  };

  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  test('a collision during a take toasts TEACH_COLLISION_DISCARDED and cancels it', async () => {
    const rec = pendingOf(mockRos.recordControl);
    const props = leaderProps();
    const { rerender } = render(<TeachOverlay {...props} />);
    key(' ');
    await act(async () => { await flush(); });
    expect(mockRos.recordControl).toHaveBeenCalledWith('start_leader');
    await act(async () => { rec[0]({ success: true }); await flush(); });
    expect(screen.getByText(DE.TEACH_HINT_LEADER_REC)).toBeInTheDocument();
    setState({ collision: true });
    rerender(<TeachOverlay {...props} />);
    await act(async () => { await flush(); });
    expect(toast.error).toHaveBeenCalledWith(DE.TEACH_COLLISION_DISCARDED);
    expect(mockRos.recordControl).toHaveBeenLastCalledWith('cancel_leader');
    expect(mockRos.handGuide).not.toHaveBeenCalled();
  });

  test('„Fertig" never offers the home glide in leader mode', async () => {
    const props = leaderProps();
    render(<TeachOverlay {...props} />);
    key('Escape');
    await act(async () => { await flush(); });
    expect(props.onClose).toHaveBeenCalledTimes(1);
    expect(mockGlide.offer).not.toHaveBeenCalled();
    expect(mockRos.handGuide).not.toHaveBeenCalled();
  });

  test('not activated: Space sends nothing behind the panel, Esc still closes', async () => {
    mockActivation.status = { state: 'idle', step: '', message: '', robotType: 'omx_full', hasLeader: true, required: true };
    const props = leaderProps();
    render(<TeachOverlay {...props} />);
    await act(async () => { await flush(); });
    key(' ');
    key('p');
    key('z');
    await act(async () => { await flush(); });
    expect(mockRos.recordControl).not.toHaveBeenCalled();
    expect(mockRos.capturePose).not.toHaveBeenCalled();
    key('Escape');
    await act(async () => { await flush(); });
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });
});
