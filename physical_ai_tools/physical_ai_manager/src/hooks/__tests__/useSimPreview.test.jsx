/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// A simulator preview is a generated SIM run through /workflow/start. These
// tests pin the refusals (German, client-side, nothing sent), the exact
// payloads, the dispatch order and that the real-arm services are never used.

import { act, renderHook } from '@testing-library/react';
import { DE, formatDe } from '../../components/Workshop/blocks/messages_de';
import {
  buildDestinationPreviewProgram,
  buildRecordingPreviewProgram,
} from '../../utils/simPreview';
import { compactTrajectoryPoints } from '../../utils/trajectoryCompact';
import useSimPreview from '../useSimPreview';

let mockState;
const mockDispatch = vi.fn();
vi.mock('react-redux', () => ({
  __esModule: true,
  useSelector: (sel) => sel(mockState),
  useDispatch: () => mockDispatch,
}));

const mockRos = vi.hoisted(() => ({
  callService: vi.fn(),
  replayMotion: vi.fn(),
  jogArm: vi.fn(),
}));
vi.mock('../useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => mockRos,
}));

const mockApi = vi.hoisted(() => ({ getTrajectory: vi.fn() }));
vi.mock('../../services/workflowApi', () => ({ __esModule: true, ...mockApi }));

const mockStore = vi.hoisted(() => ({ getById: vi.fn() }));
vi.mock('../../components/Workshop/sammlung/destinationStore', () => ({
  __esModule: true,
  getDestinationStore: () => mockStore,
}));

const mockToast = vi.hoisted(() => {
  const t = vi.fn();
  t.success = vi.fn();
  t.error = vi.fn();
  return t;
});
vi.mock('react-hot-toast', () => ({ __esModule: true, default: mockToast }));

const TRAJ_ID = '3f9a1c0d-77aa-4bbb-8ccc-0123456789ab';
const RAW_POINTS = [
  [0.123456789, -1.5708123, 1.5708987, 0.00001, 0, 0.8, 0.0000004],
  [0.2, -1.4, 1.4, 0, 0, 0.8, 0.04012],
];
const ROW = { id: TRAJ_ID, name: 'Greifen links', robot_profile: 'omx_f', samples: { fps: 25, points: RAW_POINTS } };
const REC = { kind: 'recording', id: TRAJ_ID, name: 'Greifen links', robotProfile: 'omx_f' };
const ENTRY = { id: 'd_4f1c9a2e', name: 'Ablage', kind: 'pin', source: 'camera', robot_type: 'omx_f', x: 0.182, y: -0.064, z: 0.012 };
const SCENE = { version: 1, objects: [{ type: 'wuerfel', tag_id: 0, x: 0.15, y: 0, yaw: 0 }], zones: [] };
const GATES = {
  heartbeatStatus: 'connected', runState: 'idle', paused: false, teachOpen: false,
  jogHandGuideOn: false, simMode: true, activeTutorialId: null, rsLeaderOn: false,
};

let ensureSimMode;

function props(over = {}) {
  return {
    workspace: { id: 'ws' },
    simScene: SCENE,
    workflowId: 'wf-1',
    accessToken: 'jwt',
    robotType: 'omx_f',
    gates: GATES,
    ensureSimMode,
    ...over,
  };
}

function setup(over) {
  return renderHook((p) => useSimPreview(p), { initialProps: props(over) });
}

const types = () => mockDispatch.mock.calls.map((c) => c[0] && c[0].type);
const dispatched = (type) => mockDispatch.mock.calls.map((c) => c[0]).find((a) => a && a.type === type);

beforeEach(() => {
  vi.clearAllMocks();
  mockState = { studioAssets: { lastPreviewResult: {} } };
  ensureSimMode = vi.fn(async () => true);
  mockApi.getTrajectory.mockResolvedValue(ROW);
  mockStore.getById.mockImplementation((id) => (id === ENTRY.id ? ENTRY : null));
  mockRos.callService.mockResolvedValue({ success: true, message: 'ok', unreachable_block_ids: [], unreachable_messages: [] });
});

afterEach(() => {
  expect(mockRos.replayMotion).not.toHaveBeenCalled();
  expect(mockRos.jogArm).not.toHaveBeenCalled();
});

describe('useSimPreview — refusals (German, nothing sent)', () => {
  test.each([
    ['offline', { gates: { ...GATES, heartbeatStatus: 'disconnected' } }, DE.PREVIEW_BLOCK_OFFLINE],
    ['running', { gates: { ...GATES, runState: 'running' } }, DE.PREVIEW_BLOCK_RUNNING],
    ['paused', { gates: { ...GATES, paused: true } }, DE.PREVIEW_BLOCK_RUNNING],
    ['teach', { gates: { ...GATES, teachOpen: true } }, DE.PREVIEW_BLOCK_TEACH],
    ['handguide', { gates: { ...GATES, jogHandGuideOn: true } }, DE.PREVIEW_BLOCK_HANDGUIDE],
    ['tutorial', { gates: { ...GATES, simMode: false, activeTutorialId: 'tut' } }, DE.PREVIEW_BLOCK_TUTORIAL],
    ['leader', { gates: { ...GATES, rsLeaderOn: true } }, DE.PREVIEW_BLOCK_LEADER],
    ['otherRobot', { robotType: 'edu6_studio' }, DE.PREVIEW_BLOCK_OTHER_ROBOT],
    ['unsaved', { workflowId: null }, DE.PREVIEW_BLOCK_UNSAVED],
  ])('%s', async (_name, over, text) => {
    const { result } = setup(over);
    await act(async () => { await result.current.startPreview(REC); });
    expect(mockToast.error).toHaveBeenCalledWith(text);
    expect(ensureSimMode).not.toHaveBeenCalled();
    expect(mockApi.getTrajectory).not.toHaveBeenCalled();
    expect(mockRos.callService).not.toHaveBeenCalled();
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  test('an asset of kind „variable" returns silently', async () => {
    const { result } = setup();
    await act(async () => { await result.current.startPreview({ kind: 'variable', id: 'v1', name: 'x' }); });
    expect(mockToast.error).not.toHaveBeenCalled();
    expect(mockToast.success).not.toHaveBeenCalled();
    expect(ensureSimMode).not.toHaveBeenCalled();
    expect(mockRos.callService).not.toHaveBeenCalled();
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  test('a blocked sim entry stops before any fetch or service call', async () => {
    ensureSimMode = vi.fn(async () => false);
    const { result } = setup({ gates: { ...GATES, simMode: false } });
    await act(async () => { await result.current.startPreview(REC); });
    expect(ensureSimMode).toHaveBeenCalledWith({ showPath: true });
    expect(mockApi.getTrajectory).not.toHaveBeenCalled();
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('a second call while one is in flight is silent', async () => {
    let release;
    ensureSimMode = vi.fn(() => new Promise((r) => { release = r; }));
    const { result } = setup();
    let first;
    await act(async () => { first = result.current.startPreview(REC); });
    await act(async () => { await result.current.startPreview(REC); });
    expect(ensureSimMode).toHaveBeenCalledTimes(1);
    expect(mockToast.error).not.toHaveBeenCalled();
    await act(async () => { release(true); await first; });
    expect(mockRos.callService).toHaveBeenCalledTimes(1);
  });

  test('cross-profile is refused AFTER the fetch (the row tag decides)', async () => {
    mockApi.getTrajectory.mockResolvedValue({ ...ROW, robot_profile: 'edu1_studio' });
    const { result } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    expect(mockApi.getTrajectory).toHaveBeenCalledWith('jwt', 'wf-1', TRAJ_ID);
    expect(mockToast.error).toHaveBeenCalledWith(DE.PREVIEW_BLOCK_OTHER_ROBOT);
    expect(mockRos.callService).not.toHaveBeenCalled();
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  test('an oversize payload is refused with PREVIEW_TOO_BIG', async () => {
    const huge = Array.from({ length: 9000 }, (_, i) => [0.1234, -1.2345, 1.2345, 0.5, 0.25, 0.8, i * 0.04]);
    mockApi.getTrajectory.mockResolvedValue({ ...ROW, samples: { fps: 25, points: huge } });
    const { result } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    expect(mockToast.error).toHaveBeenCalledWith(DE.PREVIEW_TOO_BIG);
    expect(mockRos.callService).not.toHaveBeenCalled();
  });

  test('a missing recording fails in German and records the refusal', async () => {
    mockApi.getTrajectory.mockResolvedValue({ id: TRAJ_ID, name: 'Greifen links', samples: { fps: 25, points: [] } });
    const { result } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    const msg = 'Bewegung „Greifen links" wurde nicht gefunden.';
    expect(mockToast.error).toHaveBeenCalledWith(formatDe(DE.PREVIEW_FAILED, msg));
    expect(dispatched('studioAssets/previewFailed').payload).toEqual({ key: `rec:${TRAJ_ID}`, message: msg });
    expect(mockRos.callService).not.toHaveBeenCalled();
  });
});

describe('useSimPreview — starting a preview', () => {
  test('recording: the payload deep-equals the builder output with compacted points', async () => {
    const { result } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    expect(ensureSimMode).toHaveBeenCalledWith({ showPath: true });
    expect(mockRos.callService).toHaveBeenCalledTimes(1);
    const [name, type, req] = mockRos.callService.mock.calls[0];
    expect(name).toBe('/workflow/start');
    expect(type).toBe('physical_ai_interfaces/srv/StartWorkflow');
    expect(req.workflow_id).toBe('vorschau-aufnahme-3f9a1c0d');
    expect(JSON.parse(req.workflow_json)).toEqual(buildRecordingPreviewProgram({
      name: 'Greifen links', fps: 25, points: compactTrajectoryPoints(RAW_POINTS), simScene: SCENE, tempo: 1.0,
    }));
    expect(JSON.parse(req.workflow_json).trajectories['Greifen links'].points[0][0]).toBe(0.1235);
    expect(types()).toEqual([
      'workshop/clearWorkflowLog',
      'workshop/clearWorkflowError',
      'workshop/setWorkflowStatus',
      'studioAssets/previewStarted',
      'workshop/setRunState',
      'workshop/setPaused',
    ]);
    expect(dispatched('workshop/setWorkflowStatus').payload).toEqual({
      log_message: formatDe(DE.PREVIEW_LOG_RECORDING, 'Greifen links'),
    });
    expect(dispatched('studioAssets/previewStarted').payload).toEqual({
      key: `rec:${TRAJ_ID}`, kind: 'recording', name: 'Greifen links', workflowId: 'vorschau-aufnahme-3f9a1c0d',
    });
    expect(dispatched('workshop/setRunState').payload).toBe('running');
    expect(dispatched('workshop/setPaused').payload).toBe(false);
  });

  test('previewStarted is dispatched BEFORE the service call', async () => {
    const { result } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    const idx = mockDispatch.mock.calls.findIndex((c) => c[0].type === 'studioAssets/previewStarted');
    const startedOrder = mockDispatch.mock.invocationCallOrder[idx];
    expect(startedOrder).toBeLessThan(mockRos.callService.mock.invocationCallOrder[0]);
  });

  test('the drawer tempo reaches the payload', async () => {
    const { result } = setup();
    await act(async () => { await result.current.startPreview(REC, { tempo: 0.5 }); });
    expect(JSON.parse(mockRos.callService.mock.calls[0][2].workflow_json).tempo).toBe(0.5);
  });

  test('destination: sends only that entry as destinations', async () => {
    const { result } = setup();
    await act(async () => {
      await result.current.startPreview({ kind: 'pin', id: ENTRY.id, name: 'Ablage' }, { tempo: 2.0 });
    });
    const req = mockRos.callService.mock.calls[0][2];
    expect(req.workflow_id).toBe('vorschau-ziel-d_4f1c9a2e');
    const parsed = JSON.parse(req.workflow_json);
    expect(parsed).toEqual(buildDestinationPreviewProgram({ entry: ENTRY, simScene: SCENE, tempo: 2.0 }));
    expect(parsed.destinations).toEqual([{ name: 'Ablage', kind: 'pin', x: 0.182, y: -0.064, z: 0.012 }]);
    expect(dispatched('workshop/setWorkflowStatus').payload.log_message).toBe(formatDe(DE.PREVIEW_LOG_PLACE, 'Ablage'));
    expect(mockApi.getTrajectory).not.toHaveBeenCalled();
  });

  test('a Position logs as a Position', async () => {
    mockStore.getById.mockReturnValue({ ...ENTRY, id: 'p1', name: 'Über Kiste', kind: 'pose' });
    const { result } = setup();
    await act(async () => { await result.current.startPreview({ kind: 'pose', id: 'p1', name: 'Über Kiste' }); });
    expect(dispatched('workshop/setWorkflowStatus').payload.log_message)
      .toBe(formatDe(DE.PREVIEW_LOG_POSE, 'Über Kiste'));
  });

  test('a deleted destination returns silently', async () => {
    const { result } = setup();
    await act(async () => { await result.current.startPreview({ kind: 'pin', id: 'gone', name: 'X' }); });
    expect(mockRos.callService).not.toHaveBeenCalled();
    expect(mockToast.error).not.toHaveBeenCalled();
  });

  test('a server refusal → previewFailed + the mapped toast, no running state', async () => {
    mockRos.callService.mockResolvedValue({ success: false, message: 'Zielpunkt liegt unter der Tischebene.' });
    const { result } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    expect(dispatched('studioAssets/previewFailed').payload).toEqual({
      key: `rec:${TRAJ_ID}`, message: 'Zielpunkt liegt unter der Tischebene.',
    });
    expect(mockToast.error).toHaveBeenCalledWith('Zielpunkt liegt unter der Tischebene.');
    expect(types()).not.toContain('workshop/setRunState');
  });

  test('a thrown service call → previewFailed + PREVIEW_FAILED', async () => {
    mockRos.callService.mockRejectedValue(new Error('Service call timeout for /workflow/start'));
    const { result } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    expect(dispatched('studioAssets/previewFailed').payload.message).toBe('Service call timeout for /workflow/start');
    expect(mockToast.error).toHaveBeenCalledWith(
      formatDe(DE.PREVIEW_FAILED, 'Service call timeout for /workflow/start'));
  });

  test('unreachable ids → previewUnreachable, never setDebuggerWarnings', async () => {
    mockRos.callService.mockResolvedValue({
      success: true, message: 'ok', unreachable_block_ids: ['vorschau-2'], unreachable_messages: ['zu weit weg'],
    });
    const { result } = setup();
    await act(async () => { await result.current.startPreview({ kind: 'pin', id: ENTRY.id, name: 'Ablage' }); });
    expect(dispatched('studioAssets/previewUnreachable').payload).toEqual({ key: 'dest:d_4f1c9a2e', message: 'zu weit weg' });
    expect(types()).not.toContain('workshop/setDebuggerWarnings');
    expect(dispatched('workshop/setRunState').payload).toBe('running');
  });
});

describe('useSimPreview — the result of the started preview', () => {
  test('ok → „Vorschau beendet." once; refused → no extra toast', async () => {
    const { result, rerender } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    expect(mockToast.success).not.toHaveBeenCalled();
    mockState = { studioAssets: { lastPreviewResult: { [`rec:${TRAJ_ID}`]: { status: 'ok', message: '', ts: 1 } } } };
    rerender(props());
    expect(mockToast.success).toHaveBeenCalledWith(DE.PREVIEW_DONE);
    mockState = { studioAssets: { lastPreviewResult: { [`rec:${TRAJ_ID}`]: { status: 'ok', message: '', ts: 2 } } } };
    rerender(props());
    expect(mockToast.success).toHaveBeenCalledTimes(1);

    await act(async () => { await result.current.startPreview(REC); });
    mockState = { studioAssets: { lastPreviewResult: { [`rec:${TRAJ_ID}`]: { status: 'refused', message: 'x', ts: 3 } } } };
    rerender(props());
    expect(mockToast.success).toHaveBeenCalledTimes(1);
  });

  test('a stopped preview adds nothing', async () => {
    const { result, rerender } = setup();
    await act(async () => { await result.current.startPreview(REC); });
    mockState = { studioAssets: { lastPreviewResult: { [`rec:${TRAJ_ID}`]: { status: 'stopped', message: '', ts: 1 } } } };
    rerender(props());
    expect(mockToast.success).not.toHaveBeenCalled();
  });
});

describe('useSimPreview — late changes while the preview starts', () => {
  function deferred() {
    let resolve;
    const promise = new Promise((res) => { resolve = res; });
    return { promise, resolve };
  }

  test('the leader switched on during the sim-entry settle refuses before anything is sent', async () => {
    const entry = deferred();
    ensureSimMode = vi.fn(() => entry.promise);
    const { result, rerender } = setup();
    let pending;
    act(() => { pending = result.current.startPreview(REC); });
    rerender(props({ gates: { ...GATES, rsLeaderOn: true } }));
    await act(async () => { entry.resolve(true); await pending; });
    expect(mockToast.error).toHaveBeenCalledWith(DE.PREVIEW_BLOCK_LEADER);
    expect(mockRos.callService).not.toHaveBeenCalled();
    expect(types()).not.toContain('studioAssets/previewStarted');
  });

  test('a terminal result that lands before the start reply is not overwritten with running', async () => {
    const reply = deferred();
    mockRos.callService.mockImplementation(() => reply.promise);
    const { result, rerender } = setup();
    let pending;
    await act(async () => {
      pending = result.current.startPreview(REC);
      for (let i = 0; i < 10 && !mockRos.callService.mock.calls.length; i += 1) await Promise.resolve();
    });
    expect(mockRos.callService).toHaveBeenCalledTimes(1);
    mockState = { studioAssets: { lastPreviewResult: { [`rec:${TRAJ_ID}`]: { status: 'refused', message: 'x', ts: 1 } } } };
    rerender(props());
    await act(async () => {
      reply.resolve({ success: true, message: 'ok', unreachable_block_ids: [], unreachable_messages: [] });
      await pending;
    });
    expect(types()).toContain('studioAssets/previewStarted');
    expect(types()).not.toContain('workshop/setRunState');
  });
});
