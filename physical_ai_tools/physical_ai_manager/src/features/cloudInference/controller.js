// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Inferenz page's cloud session (spec §6.5; D2/D4/D28/D31). A module-level
// controller, read by hooks/useCloudInferenceSession through useSyncExternalStore.
//
// RESERVE, THEN ARM. An explicit click on a model row (debounced 800 ms) reserves
// a cloud GPU for it (POST /inference/sessions — no secret in the answer); the
// GPU and the model come up while the student reads the summary. Start arms the
// reservation (POST …/arm) and hands the two credentials it answers to the robot
// in ONE service call. The page never pre-selects or restores a model choice: a
// reservation costs a GPU slot (F-9).
//
// THE CONNECT TOKEN AND THE EDGE TOKEN are local variables of `start()` only —
// never module state, never the snapshot, never Redux, storage, a log, a toast or
// an error (D4). That is hygiene, not a boundary against a script running with
// the student's JWT, which could call /arm itself.
//
// No browser storage (D3): a reload ends the run and its reservation through the
// pagehide keepalive (owner A-Q3); a hidden or minimized window is throttled by
// the browser and its 15 s heartbeat stops, which ends the run after ~5-6 min
// (owner A-Q2).

import TaskPhase from '../../constants/taskPhases';
import {
  armInferenceSession,
  endInferenceSession,
  endInferenceSessionKeepalive,
  heartbeatInferenceSession,
  reserveInferenceSession,
} from '../../services/cloudInferenceApi';
import { cloudCopy } from '../../components/CloudInference/cloudCopy';
import { startCloudInference, stopCloudInference } from './robotCalls';
import { taskInfoWire } from './taskInfoWire';

export const SELECT_DEBOUNCE_MS = 800;
export const HEARTBEAT_MS = 15000;

// A Start or a run is in progress: model rows are disabled and a click is a
// no-op (a reserve would supersede the running session, G-3).
const BUSY_PHASES = new Set(['arming', 'starting', 'running', 'stopping']);
// The controller holds a reservation or a run the page must keep alive.
const HOLDING_PHASES = new Set(['reserved', 'arming', 'starting', 'running']);

const INITIAL = Object.freeze({
  phase: 'idle',
  sessionId: null,
  trainingId: null,
  summary: null,
  sessionS: null,
  message: '',
  modelRefused: false,
  seq: 0,
});

let state = INITIAL;
const listeners = new Set();

let tokenGetter = () => null;
let taskRunningGetter = () => false;
let selectionTimer = null;
let heartbeatTimer = null;
let pagehideAttached = false;
let sawRobotInside = false;

function set(patch) {
  state = { ...state, ...patch };
  listeners.forEach((fn) => {
    try { fn(); } catch { /* a listener never breaks the controller */ }
  });
}

function accessToken() {
  try {
    return tokenGetter() || null;
  } catch {
    return null;
  }
}

function taskRunning() {
  try {
    return Boolean(taskRunningGetter());
  } catch {
    return false;
  }
}

/** useSyncExternalStore's subscribe. */
export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** useSyncExternalStore's getSnapshot: a new object on every change, never a token. */
export function getSnapshot() {
  return state;
}

/** A run (not a reservation) is in progress: the TargetSwitch is locked. */
export function isLiveRun(snapshot = state) {
  return BUSY_PHASES.has(snapshot.phase);
}

function clearSelectionTimer() {
  if (selectionTimer !== null) {
    clearTimeout(selectionTimer);
    selectionTimer = null;
  }
}

function onPagehide() {
  const id = state.sessionId;
  const token = accessToken();
  if (id && token && HOLDING_PHASES.has(state.phase)) {
    endInferenceSessionKeepalive(token, id);
  }
  if (BUSY_PHASES.has(state.phase)) {
    stopCloudInference().catch(() => {});
  }
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(beat, HEARTBEAT_MS);
  if (!pagehideAttached && typeof window !== 'undefined') {
    window.addEventListener('pagehide', onPagehide);
    pagehideAttached = true;
  }
}

function stopHeartbeat() {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function clearTimers() {
  stopHeartbeat();
  clearSelectionTimer();
  if (pagehideAttached && typeof window !== 'undefined') {
    window.removeEventListener('pagehide', onPagehide);
  }
  pagehideAttached = false;
}

async function endQuietly(sessionId) {
  const token = accessToken();
  if (!sessionId || !token) return;
  try {
    await endInferenceSession(token, sessionId);
  } catch {
    /* idempotent server-side; the 45 s rule ends what a failed call leaves */
  }
}

async function beat() {
  const id = state.sessionId;
  const token = accessToken();
  if (!id || !token) return;
  let res;
  try {
    res = await heartbeatInferenceSession(token, id);
  } catch (err) {
    if (err?.status !== 404) return; // network/5xx: retried next tick (the 45 s floor)
    res = { status: 'ended', end_reason_de: '' };
  }
  if (state.sessionId !== id || !res || res.status !== 'ended') return;
  if (state.phase === 'reserved') {
    clearTimers();
    set({ phase: 'idle', sessionId: null, summary: state.summary, message: cloudCopy.panel.released });
  } else if (state.phase === 'running') {
    // (While arming or starting, start() itself meets the end: the arm answers
    // 410 and re-reserves once, a robot call answers and the next beat ends it.)
    stopCloudInference().catch(() => {});
    clearTimers();
    set({ phase: 'ended', sessionId: null, message: res.end_reason_de || '' });
  }
}

function apiMessage(err) {
  // FastAPI's own English 404 („Not Found") means the API has no /inference/*
  // at all (an older API): never shown, never a model refusal (F-10).
  const routeMissing = err?.status === 404 && err?.detail === 'Not Found';
  const detail = typeof err?.detail === 'string' && err.detail && !routeMissing ? err.detail : '';
  return { routeMissing, message: detail || cloudCopy.errors.apiUnavailable };
}

async function reserve(trainingId) {
  const my = state.seq + 1;
  stopHeartbeat();
  set({ seq: my, phase: 'reserving', trainingId, sessionId: null, summary: null, message: '', modelRefused: false });
  let res;
  try {
    res = await reserveInferenceSession(accessToken(), trainingId);
  } catch (err) {
    if (my !== state.seq) return;
    const { routeMissing, message } = apiMessage(err);
    // Only the model itself refused (404/409) disables Start for this row (R-16).
    const refused = !routeMissing && (err?.status === 404 || err?.status === 409);
    set({ phase: 'idle', sessionId: null, message, modelRefused: refused });
    return;
  }
  if (my !== state.seq) {
    // A newer choice or a release() came meanwhile: nothing else would end this one (F-9).
    if (res && res.session_id) endInferenceSessionKeepalive(accessToken(), res.session_id);
    return;
  }
  set({
    phase: 'reserved',
    sessionId: res?.session_id || null,
    summary: res?.summary || null,
    sessionS: res?.session_s ?? null,
    message: '',
    modelRefused: false,
  });
  // A reservation is kept alive exactly like a run: 45 s without a heartbeat ends it.
  startHeartbeat();
}

/**
 * A click on a model row. Debounced: ONE reserve 800 ms after the last change.
 * A no-op while a Start or a run is in progress or a task runs on the robot
 * (G-3); the timer re-checks the same guard when it fires (H-4).
 *
 * @param {() => ?string} getAccessToken
 * @param {number} trainingId
 * @param {() => boolean} [isTaskRunning]  taskStatus.running, read when needed
 */
export function selectModel(getAccessToken, trainingId, isTaskRunning) {
  if (typeof getAccessToken === 'function') tokenGetter = getAccessToken;
  if (typeof isTaskRunning === 'function') taskRunningGetter = isTaskRunning;
  if (BUSY_PHASES.has(state.phase) || taskRunning()) return;
  clearSelectionTimer();
  selectionTimer = setTimeout(() => {
    selectionTimer = null;
    if (BUSY_PHASES.has(state.phase) || taskRunning()) return;
    reserve(trainingId);
  }, SELECT_DEBOUNCE_MS);
}

async function arm() {
  try {
    const body = await armInferenceSession(accessToken(), state.sessionId);
    return { ok: true, body };
  } catch (err) {
    const { message } = apiMessage(err);
    return { ok: false, status: err?.status, message };
  }
}

async function dropToIdle(message, { end = true } = {}) {
  const id = state.sessionId;
  clearTimers();
  set({ phase: 'idle', sessionId: null, message });
  if (end && id) await endQuietly(id);
}

/**
 * Start pressed (spec §6.5). Reserves first when needed, arms, then ONE robot call.
 *
 * @param {object} o
 * @param {() => ?string} o.getAccessToken
 * @param {() => object}  o.getTaskInfo  the current Redux `tasks.taskInfo`
 * @param {number}        o.trainingId
 */
export async function start({ getAccessToken, getTaskInfo, trainingId }) {
  if (typeof getAccessToken === 'function') tokenGetter = getAccessToken;
  // H-4: a Start within 800 ms of a row click must not trigger a second reserve.
  clearSelectionTimer();
  if (BUSY_PHASES.has(state.phase)) return;
  if (state.phase !== 'reserved' || state.trainingId !== trainingId) {
    await reserve(trainingId);
    if (state.phase !== 'reserved' || state.trainingId !== trainingId) return;
  }
  set({ phase: 'arming', message: '' });
  let armed = await arm();
  if (!armed.ok && armed.status === 410) {
    // The reservation ended (expired, the page was gone): reserve again ONCE.
    await reserve(trainingId);
    if (state.phase !== 'reserved' || state.trainingId !== trainingId) return;
    set({ phase: 'arming', message: '' });
    armed = await arm();
    if (!armed.ok) {
      await dropToIdle(armed.message);
      return;
    }
  }
  if (!armed.ok) {
    if (armed.status === 503) {
      // The envelope did not open; the row is ended `edge_failed` (F-10).
      await dropToIdle(armed.message, { end: false });
      return;
    }
    set({ phase: 'reserved', message: armed.message });
    return;
  }

  let token = armed.body?.token;
  let edge = armed.body?.edge_token;
  const sessionS = armed.body?.session_s ?? state.sessionS;
  armed = null;
  set({ phase: 'starting', sessionS });
  let result;
  try {
    const summary = state.summary || {};
    const taskInfo = taskInfoWire(getTaskInfo ? getTaskInfo() : {}, {
      task_type: 'inference',
      policy_path: String(summary.model_repo || ''),
      fps: Math.round(summary.trained_fps) || 30,
      record_inference_mode: false,
    });
    result = await startCloudInference({ task_info: taskInfo, session_token: token, edge_token: edge });
  } finally {
    token = null;
    edge = null;
  }
  if (state.phase !== 'starting') return; // a stop, a release or a sign-out came meanwhile
  if (result.timedOut) {
    // No answer within 10 s: the robot may have started. Stop it and end the session.
    const id = state.sessionId;
    await stopCloudInference();
    clearTimers();
    set({ phase: 'ended', sessionId: null, message: cloudCopy.errors.robotUnreachable });
    await endQuietly(id);
    return;
  }
  if (!result.success) {
    // An explicit refusal: the reservation stays (the armed token was never used
    // and dies at the next arm). The robot's own German sentence is shown as-is.
    set({ phase: 'reserved', message: result.oldImage ? cloudCopy.errors.robotOld : (result.message || cloudCopy.errors.robotFailed) });
    return;
  }
  sawRobotInside = false;
  set({ phase: 'running', message: '' });
}

/** Stopp: the robot's stop service + the session's end (D2). */
export async function stop() {
  clearSelectionTimer();
  const id = state.sessionId;
  set({ phase: 'stopping' });
  await stopCloudInference();
  await endQuietly(id);
  clearTimers();
  set({ phase: 'ended', sessionId: null, message: '' });
}

/**
 * The target leaves „In der Cloud", the panel unmounts, a model row is
 * deselected: a pending or held RESERVATION is let go (F-9). A run is never
 * released this way — only stop() ends it.
 */
export function release() {
  clearSelectionTimer();
  if (state.phase !== 'reserving' && state.phase !== 'reserved') return;
  const id = state.sessionId;
  clearTimers();
  // seq += 1: a reserve answer still in flight is then ignored and ended.
  set({ seq: state.seq + 1, phase: 'idle', sessionId: null, summary: null, message: '', modelRefused: false });
  if (id) endInferenceSessionKeepalive(accessToken(), id);
}

/**
 * Every /task/status phase while a run is live: once the robot has been seen
 * inside the inference phases, leaving them means the robot ended the run (its
 * German sentence came as the /task/status error toast) — end the session.
 */
export function onRobotPhase(phase) {
  if (state.phase !== 'running') return;
  if (phase === TaskPhase.INFERENCE_LOADING || phase === TaskPhase.INFERENCING) {
    sawRobotInside = true;
    return;
  }
  if (!sawRobotInside) return;
  const id = state.sessionId;
  clearTimers();
  set({ phase: 'ended', sessionId: null, message: '' });
  endQuietly(id);
}

/**
 * The after-reload Stopp (R-25): the page has no session, the robot still runs.
 * The robot's stop service, and the cloud session the usage names, if any.
 */
export async function stopOrphan({ getAccessToken, liveSessionId }) {
  if (typeof getAccessToken === 'function') tokenGetter = getAccessToken;
  set({ phase: 'stopping' });
  await stopCloudInference();
  if (liveSessionId) await endQuietly(liveSessionId);
  set({ phase: 'ended', sessionId: null, message: '' });
}

/**
 * Sign-out (utils/signOut.js): end a reserved or live session with the same
 * keepalive request and reset the controller synchronously.
 *
 * @param {?string} accessTokenAtLogout the JWT before the revoke
 */
export function endCloudInferenceOnLogout(accessTokenAtLogout) {
  const id = state.sessionId;
  const holding = HOLDING_PHASES.has(state.phase) || state.phase === 'stopping' || state.phase === 'reserving';
  if (id && holding && accessTokenAtLogout) {
    endInferenceSessionKeepalive(accessTokenAtLogout, id);
  }
  clearTimers();
  tokenGetter = () => null;
  taskRunningGetter = () => false;
  sawRobotInside = false;
  set({ ...INITIAL, seq: state.seq + 1 });
}

/** Tests only: back to the initial state with no timer or listener left. */
export function _resetForTests() {
  clearTimers();
  tokenGetter = () => null;
  taskRunningGetter = () => false;
  sawRobotInside = false;
  state = INITIAL;
  listeners.clear();
}
