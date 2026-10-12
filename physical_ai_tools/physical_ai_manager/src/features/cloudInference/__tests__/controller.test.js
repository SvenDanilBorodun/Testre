// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The cloud session controller (spec §6.5). The cloud API and the robot are
// mocked at their module boundary; everything else is the real controller.
//
// Pinned: reserve only on an explicit click, debounced to ONE call; a stale
// answer is ignored (and its session ended, F-9); a failed reservation keeps
// Start enabled and only a model refusal disables it (R-16), except FastAPI's
// own route-404 (F-10); Start reserves first when needed, re-reserves ONCE on
// arm 410, drops to idle on arm 503; the robot's refusal keeps the
// reservation; a robot that never answers is stopped and the session ended; no
// click reserves while a Start or a run is in progress (G-3); a Start inside
// the debounce sends exactly one reserve and a Stop clears the timer (H-4); the
// heartbeat, the robot leaving the inference phases, pagehide and sign-out
// each end the session the right way; and BOTH tokens never reach the snapshot,
// Redux, storage, a console call or an Error.

import TaskPhase from '../../../constants/taskPhases';
import { cloudCopy } from '../../../components/CloudInference/cloudCopy';

const CONNECT = 'T'.repeat(43);
const EDGE = 'edge-' + 'E'.repeat(30);
const SID = '0a1b2c3d-0000-4000-8000-00000000abcd';
const SID2 = '0a1b2c3d-0000-4000-8000-00000000abce';
const SUMMARY = { type: 'act', expected_cameras: ['gripper', 'scene'], trained_fps: 29.97, model_repo: 'EduBotics-Solutions/m', model_revision: 'a'.repeat(40) };

const api = vi.hoisted(() => ({
  reserveInferenceSession: vi.fn(),
  armInferenceSession: vi.fn(),
  heartbeatInferenceSession: vi.fn(),
  endInferenceSession: vi.fn(),
  endInferenceSessionKeepalive: vi.fn(),
  getInferenceUsage: vi.fn(),
}));
vi.mock('../../../services/cloudInferenceApi', () => ({ __esModule: true, ...api }));

const robot = vi.hoisted(() => ({
  startCloudInference: vi.fn(),
  stopCloudInference: vi.fn(),
  subscribeCloudState: vi.fn(),
}));
vi.mock('../robotCalls', () => ({ __esModule: true, ...robot }));

// eslint-disable-next-line import/first
import * as controller from '../controller';

function apiError(status, detail) {
  const err = new Error(detail || `HTTP ${status}`);
  err.status = status;
  err.detail = detail;
  return err;
}

const token = () => 'jwt-student';
const FORM = { taskName: 'Würfel', taskInstruction: ['Greife'], fps: 15, tags: [], userId: 'u' };

async function flush(n = 6) {
  for (let i = 0; i < n; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await Promise.resolve();
  }
}

async function reserveVia(trainingId = 7) {
  controller.selectModel(token, trainingId, () => false);
  await vi.advanceTimersByTimeAsync(controller.SELECT_DEBOUNCE_MS);
  await flush();
}

beforeEach(() => {
  vi.useFakeTimers();
  controller._resetForTests();
  Object.values(api).forEach((f) => f.mockReset());
  Object.values(robot).forEach((f) => f.mockReset());
  api.reserveInferenceSession.mockResolvedValue({ session_id: SID, summary: SUMMARY, session_s: 1800, remaining_today_s: 7000, reserve_s: 300 });
  api.armInferenceSession.mockResolvedValue({ token: CONNECT, edge_token: EDGE, session_s: 1800 });
  api.heartbeatInferenceSession.mockResolvedValue({ status: 'reserved', end_reason: null, end_reason_de: '', max_end_in_s: null });
  api.endInferenceSession.mockResolvedValue({ status: 'ended', end_reason: 'user_stop', active_seconds: 0 });
  robot.startCloudInference.mockResolvedValue({ success: true, message: '', oldImage: false, timedOut: false });
  robot.stopCloudInference.mockResolvedValue(true);
});

afterEach(() => {
  controller._resetForTests();
  vi.useRealTimers();
});

describe('reserve on an explicit model click', () => {
  it('debounces the selection to ONE reserve 800 ms after the last change', async () => {
    controller.selectModel(token, 5, () => false);
    await vi.advanceTimersByTimeAsync(500);
    controller.selectModel(token, 6, () => false);
    await vi.advanceTimersByTimeAsync(500);
    controller.selectModel(token, 7, () => false);
    expect(api.reserveInferenceSession).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(800);
    await flush();
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
    expect(api.reserveInferenceSession).toHaveBeenCalledWith('jwt-student', 7);
    const s = controller.getSnapshot();
    expect(s).toMatchObject({ phase: 'reserved', sessionId: SID, trainingId: 7, summary: SUMMARY, sessionS: 1800 });
  });

  it('never reserves without a click', async () => {
    await vi.advanceTimersByTimeAsync(60000);
    expect(api.reserveInferenceSession).not.toHaveBeenCalled();
    expect(controller.getSnapshot().phase).toBe('idle');
  });

  it('shows the preparing phase while the reservation runs', async () => {
    let resolve;
    api.reserveInferenceSession.mockReturnValue(new Promise((r) => { resolve = r; }));
    controller.selectModel(token, 7, () => false);
    await vi.advanceTimersByTimeAsync(800);
    expect(controller.getSnapshot().phase).toBe('reserving');
    resolve({ session_id: SID, summary: SUMMARY, session_s: 1800 });
    await flush();
    expect(controller.getSnapshot().phase).toBe('reserved');
  });

  it('ignores a stale reserve answer and ends its session (F-9)', async () => {
    const answers = [];
    api.reserveInferenceSession.mockImplementation(() => new Promise((r) => answers.push(r)));
    await reserveVia(5);
    controller.selectModel(token, 6, () => false);
    await vi.advanceTimersByTimeAsync(800);
    answers[1]({ session_id: SID2, summary: SUMMARY, session_s: 1800 });
    await flush();
    answers[0]({ session_id: SID, summary: SUMMARY, session_s: 1800 });
    await flush();
    expect(controller.getSnapshot()).toMatchObject({ phase: 'reserved', sessionId: SID2, trainingId: 6 });
    expect(api.endInferenceSessionKeepalive).toHaveBeenCalledWith('jwt-student', SID);
  });

  it('release() during reserving: the late answer is ended by a keepalive and the controller stays idle (F-9)', async () => {
    let resolve;
    api.reserveInferenceSession.mockReturnValue(new Promise((r) => { resolve = r; }));
    controller.selectModel(token, 7, () => false);
    await vi.advanceTimersByTimeAsync(800);
    controller.release();
    expect(controller.getSnapshot().phase).toBe('idle');
    resolve({ session_id: SID, summary: SUMMARY, session_s: 1800 });
    await flush();
    expect(controller.getSnapshot().phase).toBe('idle');
    expect(api.endInferenceSessionKeepalive).toHaveBeenCalledWith('jwt-student', SID);
  });

  it('a failed reservation keeps Start enabled; only 404/409 refuse the model (R-16)', async () => {
    for (const [status, detail, refused] of [
      [429, 'Deine Cloud-Zeit für heute ist aufgebraucht. Morgen geht es weiter.', false],
      [503, 'Die Cloud ist gerade voll ausgelastet. Bitte in ein paar Minuten erneut versuchen.', false],
      [502, 'Hugging Face ist gerade nicht erreichbar. Bitte in einer Minute erneut versuchen.', false],
      [500, undefined, false],
      [409, 'Dieses Training ist noch nicht fertig.', true],
      [404, 'Modell nicht gefunden.', true],
    ]) {
      controller._resetForTests();
      api.reserveInferenceSession.mockRejectedValueOnce(apiError(status, detail));
      // eslint-disable-next-line no-await-in-loop
      await reserveVia(7);
      const s = controller.getSnapshot();
      expect(s.phase).toBe('idle');
      expect(s.modelRefused).toBe(refused);
      expect(s.message).toBe(detail || cloudCopy.errors.apiUnavailable);
    }
  });

  it('a network failure is apiUnavailable, never a refusal', async () => {
    api.reserveInferenceSession.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await reserveVia(7);
    expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', modelRefused: false, message: cloudCopy.errors.apiUnavailable });
  });

  it('FastAPI\'s own route-404 (an API without /inference/*) is apiUnavailable with Start enabled (F-10)', async () => {
    api.reserveInferenceSession.mockRejectedValueOnce(apiError(404, 'Not Found'));
    await reserveVia(7);
    expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', modelRefused: false, message: cloudCopy.errors.apiUnavailable });
  });
});

describe('no click reserves while a Start or a run is in progress (G-3)', () => {
  it('while running, arming, starting, stopping or with a task running', async () => {
    await reserveVia(7);
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(controller.getSnapshot().phase).toBe('running');
    api.reserveInferenceSession.mockClear();
    controller.selectModel(token, 8, () => false);
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.reserveInferenceSession).not.toHaveBeenCalled();

    controller._resetForTests();
    controller.selectModel(token, 8, () => true);
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.reserveInferenceSession).not.toHaveBeenCalled();
  });

  it('the timer re-checks the guard when it fires (H-4)', async () => {
    let running = false;
    controller.selectModel(token, 8, () => running);
    running = true;
    await vi.advanceTimersByTimeAsync(800);
    expect(api.reserveInferenceSession).not.toHaveBeenCalled();
  });
});

describe('start', () => {
  it('a Start within 800 ms of a row click sends exactly ONE reserve and reaches running (H-4)', async () => {
    controller.selectModel(token, 7, () => false);
    await vi.advanceTimersByTimeAsync(300);
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().phase).toBe('running');
  });

  it('without a live reservation reserves first, then arms, then calls the robot once', async () => {
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
    expect(api.armInferenceSession).toHaveBeenCalledWith('jwt-student', SID);
    expect(robot.startCloudInference).toHaveBeenCalledTimes(1);
    const req = robot.startCloudInference.mock.calls[0][0];
    expect(Object.keys(req)).toEqual(['task_info', 'session_token', 'edge_token']);
    expect(req.session_token).toBe(CONNECT);
    expect(req.edge_token).toBe(EDGE);
    // D28 + R-21: the form with four overrides; a fractional fps is rounded
    expect(req.task_info).toMatchObject({
      task_name: 'Würfel', task_type: 'inference', policy_path: 'EduBotics-Solutions/m', fps: 30,
      record_inference_mode: false, task_instruction: ['Greife'],
    });
    expect(controller.getSnapshot().phase).toBe('running');
  });

  it('a model without a trained fps runs at 30', async () => {
    api.reserveInferenceSession.mockResolvedValue({ session_id: SID, summary: { ...SUMMARY, trained_fps: null }, session_s: 1800 });
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(robot.startCloudInference.mock.calls[0][0].task_info.fps).toBe(30);
    controller._resetForTests();
    api.reserveInferenceSession.mockResolvedValue({ session_id: SID, summary: { ...SUMMARY, trained_fps: 14.6 }, session_s: 1800 });
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(robot.startCloudInference.mock.calls[1][0].task_info.fps).toBe(15);
  });

  it('a reservation for another model is replaced first', async () => {
    await reserveVia(5);
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(api.reserveInferenceSession).toHaveBeenLastCalledWith('jwt-student', 7);
    expect(controller.getSnapshot()).toMatchObject({ phase: 'running', trainingId: 7 });
  });

  it('arm 410 -> ONE re-reserve -> arm again', async () => {
    await reserveVia(7);
    api.armInferenceSession
      .mockRejectedValueOnce(apiError(410, 'Die Reservierung ist abgelaufen.'))
      .mockResolvedValueOnce({ token: CONNECT, edge_token: EDGE, session_s: 1800 });
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(2);
    expect(api.armInferenceSession).toHaveBeenCalledTimes(2);
    expect(controller.getSnapshot().phase).toBe('running');
  });

  it('arm 410 twice -> idle with the API\'s sentence', async () => {
    await reserveVia(7);
    api.armInferenceSession.mockRejectedValue(apiError(410, 'Die Reservierung ist abgelaufen.'));
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(2);
    expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', message: 'Die Reservierung ist abgelaufen.' });
    expect(robot.startCloudInference).not.toHaveBeenCalled();
  });

  it('arm 503 (the envelope did not open) -> idle (F-10)', async () => {
    await reserveVia(7);
    api.armInferenceSession.mockRejectedValue(apiError(503, 'Die Cloud-Sitzung ist nicht mehr nutzbar. Bitte erneut starten.'));
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', sessionId: null, message: 'Die Cloud-Sitzung ist nicht mehr nutzbar. Bitte erneut starten.' });
  });

  it('arm 409 keeps the reservation with the sentence', async () => {
    await reserveVia(7);
    api.armInferenceSession.mockRejectedValue(apiError(409, 'Die Cloud-GPU wird noch vorbereitet. Bitte einen Moment warten.'));
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(controller.getSnapshot()).toMatchObject({ phase: 'reserved', sessionId: SID, message: 'Die Cloud-GPU wird noch vorbereitet. Bitte einen Moment warten.' });
  });

  it('the robot\'s refusal keeps the reservation and shows its own sentence', async () => {
    await reserveVia(7);
    robot.startCloudInference.mockResolvedValue({ success: false, message: 'Der Leader-Arm ist aktiv.', oldImage: false, timedOut: false });
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(controller.getSnapshot()).toMatchObject({ phase: 'reserved', sessionId: SID, message: 'Der Leader-Arm ist aktiv.' });
    expect(api.endInferenceSession).not.toHaveBeenCalled();
  });

  it('an old image says „Roboter-Software aktualisieren"', async () => {
    await reserveVia(7);
    robot.startCloudInference.mockResolvedValue({ success: false, message: 'x', oldImage: true, timedOut: false });
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(controller.getSnapshot()).toMatchObject({ phase: 'reserved', message: cloudCopy.errors.robotOld });
  });

  it('a robot call that times out -> stop service + /end, ended', async () => {
    await reserveVia(7);
    robot.startCloudInference.mockResolvedValue({ success: false, message: '', oldImage: false, timedOut: true });
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(robot.stopCloudInference).toHaveBeenCalledTimes(1);
    expect(api.endInferenceSession).toHaveBeenCalledWith('jwt-student', SID);
    expect(controller.getSnapshot()).toMatchObject({ phase: 'ended', message: cloudCopy.errors.robotUnreachable });
  });
});

describe('stop, release, heartbeat, robot phase, pagehide, sign-out', () => {
  async function running() {
    await reserveVia(7);
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    expect(controller.getSnapshot().phase).toBe('running');
  }

  it('stop = stop service + /end, and clears a pending selection timer (H-4)', async () => {
    await running();
    controller.selectModel(token, 9, () => false); // a no-op while running
    await controller.stop();
    expect(robot.stopCloudInference).toHaveBeenCalledTimes(1);
    expect(api.endInferenceSession).toHaveBeenCalledWith('jwt-student', SID);
    expect(controller.getSnapshot()).toMatchObject({ phase: 'ended', sessionId: null, message: '' });
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.reserveInferenceSession).toHaveBeenCalledTimes(1);
  });

  it('release() on a reservation ends it by keepalive; a run is never released', async () => {
    await reserveVia(7);
    controller.release();
    expect(api.endInferenceSessionKeepalive).toHaveBeenCalledWith('jwt-student', SID);
    expect(controller.getSnapshot().phase).toBe('idle');
    await running();
    api.endInferenceSessionKeepalive.mockClear();
    controller.release();
    expect(controller.getSnapshot().phase).toBe('running');
    expect(api.endInferenceSessionKeepalive).not.toHaveBeenCalled();
  });

  it('heartbeats every 15 s; `ended` while reserved -> idle + panel.released', async () => {
    await reserveVia(7);
    await vi.advanceTimersByTimeAsync(controller.HEARTBEAT_MS);
    expect(api.heartbeatInferenceSession).toHaveBeenCalledWith('jwt-student', SID);
    api.heartbeatInferenceSession.mockResolvedValue({ status: 'ended', end_reason: 'never_connected', end_reason_de: 'x', max_end_in_s: null });
    await vi.advanceTimersByTimeAsync(controller.HEARTBEAT_MS);
    await flush();
    expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', sessionId: null, message: cloudCopy.panel.released });
  });

  it('`ended` while running -> the robot is stopped and the reason shown', async () => {
    await running();
    api.heartbeatInferenceSession.mockResolvedValue({ status: 'ended', end_reason: 'expired', end_reason_de: 'Das Zeitlimit für diesen Lauf war erreicht.', max_end_in_s: 0 });
    await vi.advanceTimersByTimeAsync(controller.HEARTBEAT_MS);
    await flush();
    expect(robot.stopCloudInference).toHaveBeenCalled();
    expect(controller.getSnapshot()).toMatchObject({ phase: 'ended', message: 'Das Zeitlimit für diesen Lauf war erreicht.' });
  });

  it('an `ended` beat landing while a Start is arming is left to start() (it re-reserves once)', async () => {
    await reserveVia(7);
    let armAnswer;
    api.armInferenceSession.mockImplementationOnce(() => new Promise((r) => { armAnswer = r; }));
    const pending = controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    await flush();
    expect(controller.getSnapshot().phase).toBe('arming');
    api.heartbeatInferenceSession.mockResolvedValue({ status: 'ended', end_reason: 'client_gone', end_reason_de: 'x', max_end_in_s: null });
    await vi.advanceTimersByTimeAsync(controller.HEARTBEAT_MS);
    await flush();
    expect(controller.getSnapshot().phase).toBe('arming');
    expect(robot.stopCloudInference).not.toHaveBeenCalled();
    api.heartbeatInferenceSession.mockResolvedValue({ status: 'reserved', end_reason: null, end_reason_de: '', max_end_in_s: null });
    armAnswer({ token: CONNECT, edge_token: EDGE, session_s: 1800 });
    await pending;
    expect(controller.getSnapshot().phase).toBe('running');
  });

  it('a network failure of the heartbeat changes nothing (retried next tick)', async () => {
    await running();
    api.heartbeatInferenceSession.mockRejectedValue(new TypeError('Failed to fetch'));
    await vi.advanceTimersByTimeAsync(controller.HEARTBEAT_MS * 2);
    expect(controller.getSnapshot().phase).toBe('running');
  });

  it('leaving the inference phases after being inside -> /end (the robot ended the run)', async () => {
    await running();
    controller.onRobotPhase(TaskPhase.READY); // a stale tick before the robot started: ignored
    expect(controller.getSnapshot().phase).toBe('running');
    controller.onRobotPhase(TaskPhase.INFERENCE_LOADING);
    controller.onRobotPhase(TaskPhase.INFERENCING);
    controller.onRobotPhase(TaskPhase.READY);
    await flush();
    expect(api.endInferenceSession).toHaveBeenCalledWith('jwt-student', SID);
    expect(controller.getSnapshot().phase).toBe('ended');
  });

  it('pagehide sends a keepalive POST …/end with the Authorization header and no body, and stops the robot', async () => {
    await running();
    window.dispatchEvent(new Event('pagehide'));
    expect(api.endInferenceSessionKeepalive).toHaveBeenCalledWith('jwt-student', SID);
    expect(robot.stopCloudInference).toHaveBeenCalled();
  });

  it('pagehide with a mere reservation ends it too (a reload ends it, A-Q3)', async () => {
    await reserveVia(7);
    window.dispatchEvent(new Event('pagehide'));
    expect(api.endInferenceSessionKeepalive).toHaveBeenCalledWith('jwt-student', SID);
    expect(robot.stopCloudInference).not.toHaveBeenCalled();
  });

  it('endCloudInferenceOnLogout ends a held session with the given JWT and resets synchronously', async () => {
    await running();
    controller.endCloudInferenceOnLogout('jwt-at-logout');
    expect(api.endInferenceSessionKeepalive).toHaveBeenCalledWith('jwt-at-logout', SID);
    expect(controller.getSnapshot()).toMatchObject({ phase: 'idle', sessionId: null, trainingId: null, summary: null });
    // no timer survives the sign-out
    api.heartbeatInferenceSession.mockClear();
    await vi.advanceTimersByTimeAsync(controller.HEARTBEAT_MS * 3);
    expect(api.heartbeatInferenceSession).not.toHaveBeenCalled();
    window.dispatchEvent(new Event('pagehide'));
    expect(api.endInferenceSessionKeepalive).toHaveBeenCalledTimes(1);
  });

  it('endCloudInferenceOnLogout with nothing held sends nothing', () => {
    controller.endCloudInferenceOnLogout('jwt-at-logout');
    expect(api.endInferenceSessionKeepalive).not.toHaveBeenCalled();
  });

  it('the after-reload Stopp: the stop service + the session the usage names (R-25)', async () => {
    await controller.stopOrphan({ getAccessToken: token, liveSessionId: SID2 });
    expect(robot.stopCloudInference).toHaveBeenCalledTimes(1);
    expect(api.endInferenceSession).toHaveBeenCalledWith('jwt-student', SID2);
  });
});

describe('both tokens stay inside start()', () => {
  it('never in the snapshot, storage, a console call or an Error', async () => {
    const consoleSpies = ['log', 'info', 'warn', 'error', 'debug'].map((m) => vi.spyOn(console, m));
    const snapshots = [];
    const unsubscribe = controller.subscribe(() => snapshots.push(controller.getSnapshot()));
    await reserveVia(7);
    await controller.start({ getAccessToken: token, getTaskInfo: () => FORM, trainingId: 7 });
    robot.startCloudInference.mockRejectedValueOnce(new Error('never'));
    await controller.stop();
    unsubscribe();
    const all = JSON.stringify(snapshots) + JSON.stringify(controller.getSnapshot());
    expect(all).not.toContain(CONNECT);
    expect(all).not.toContain(EDGE);
    for (const spy of consoleSpies) {
      for (const call of spy.mock.calls) {
        expect(JSON.stringify(call)).not.toContain(CONNECT);
        expect(JSON.stringify(call)).not.toContain(EDGE);
      }
    }
    for (const store of [localStorage, sessionStorage]) {
      for (let i = 0; i < store.length; i += 1) {
        const v = store.getItem(store.key(i)) || '';
        expect(v).not.toContain(CONNECT);
        expect(v).not.toContain(EDGE);
      }
    }
    expect(snapshots.every((s) => Object.keys(s).every((k) => !/token/i.test(k)))).toBe(true);
  });
});
