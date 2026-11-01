// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The camera station session, app-wide (station spec v6.13, §9.1; owner
// decisions D10, D13). A module-level controller in the cloudInference pattern:
// pages read its snapshot through useSyncExternalStore (useStation.js) and call
// its actions; StudentApp starts it once and feeds it the rig context, so it
// survives every tab switch. No Redux; the only browser storage is the per-tab
// client id (clientId.js).
//
// IT IS THE ONLY WRITER OF THE GUI'S STUDIO STREAM (§9.1). `{"on": true}` is
// POSTed only right after an ACCEPTED server keepalive, from the one app-wide
// 2 s timer, so a page that has lost the server stops renewing and the GUI's
// 10 s lease ends the stream by itself; a stray `off` from anywhere else is
// undone by the next tick (§3.3). A refused tab never touches the stream.
//
// THE TIMER GENERATION. Every stop of the timer bumps `timerGen`, and every
// await of a tick checks it afterwards, so a keepalive still in flight when the
// student leaves can never POST `on` after the leave's `off`. The GUI POSTs run
// one at a time, in order (`guiChain`), and a queued `on` re-checks the
// generation right before it is sent.
//
// Steps of §9.1, in the code below:
//   0. the client id and the duplicated-tab check   → start()
//   1. resume by `status` at start, then enter      → resumeLoop(), maybeAutoEnter()
//   2. `enter` and its refusals                     → enter()
//   3. the 2 s keepalive + stream lease             → tick()
//   4. leaving (button, leader switch, sign-out)    → leave(), endStationSessionOnLogout()
//   5. `pagehide`                                   → onPagehide()
//   6. timing and edge cases                        → configure(), tick()

import {
  COMMAND_TIMEOUT_MS,
  CHECK_RETRY_RESULTS,
  ENTER_RETRY_MS,
  JOB_END_STATES,
  JOB_POLL_MS,
  KEEPALIVE_MS,
  KEEPALIVE_TIMEOUT_MS,
  RESUME_POLL_MS,
  STATION_PROTOCOL,
} from './stationContract';
import {
  STATION_DE,
  SESSION_REASON_DE,
  checkMessageDe,
  endMessageDe,
  sessionMessageDe,
} from './stationCopy';
import * as transport from './stationTransport';
import { checkDuplicateTab, getClientId, remintClientId } from './clientId';

const SESSION_FLAGS_OFF = Object.freeze({
  sourceOk: false,
  cameraOk: false,
  robotOk: false,
  verified: false,
  workflowActive: false,
});

const INITIAL = Object.freeze({
  // The rig (configure()).
  stationRig: false,
  active: false,
  // This tab's session: 'idle' | 'entering' | 'held' | 'leaving'.
  phase: 'idle',
  held: false,
  sessionId: '',
  generation: 0,
  epoch: 0,
  // What the server observes (every StationSession answer).
  serverState: '',
  stationConfigured: null,
  layoutId: '',
  calibrationDigest: '',
  ...SESSION_FLAGS_OFF,
  // Why not, and what the student can do.
  startOffered: false,
  leaderBlocked: false,
  notice: '',
  noticeKind: '',
  streamError: '',
  // The checks (§7).
  checking: '',
  cameraCheck: null,
  robotCheck: null,
  resolveJob: null,
});

let state = INITIAL;
const listeners = new Set();

// The context StudentApp and WorkshopPage feed in.
const ctx = {
  stationProtocolKnown: false,
  stationRig: false,
  piMode: false,
  piModeResolved: false,
  jetsonConnected: false,
  workshopOpen: false,
  leaderOn: false,
  leaderProbed: false,
};
const leaderSwitching = new Map();

let started = false;
let clientId = null;
let clientIdReady = false;
let resumeDone = false;
let resumeTimer = null;
let resumeInFlight = false;
let autoArmed = false;
let armOnLeaderOff = false;
let enterSeq = 0;
let enterRetryTimer = null;
let timerGen = 0;
let tickTimer = null;
let tickPending = false;
let guiChain = Promise.resolve();
let jobTimer = null;
let pagehideAttached = false;
const usedPairs = new Set();

function set(patch) {
  state = { ...state, ...patch };
  listeners.forEach((fn) => {
    try { fn(); } catch { /* a listener never breaks the controller */ }
  });
}

/** useSyncExternalStore's subscribe. */
export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** useSyncExternalStore's getSnapshot: a new object on every change. */
export function getSnapshot() {
  return state;
}

function cid() {
  if (!clientId) clientId = getClientId();
  return clientId;
}

// ── answers ─────────────────────────────────────────────────────────────────

const str = (v) => (typeof v === 'string' ? v : '');
const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);

/** What every StationSession answer tells anyone (§5.2 `status`). */
function rigFacts(res) {
  return {
    serverState: str(res.state),
    stationConfigured: Boolean(res.station_configured),
    layoutId: str(res.layout_id),
    calibrationDigest: str(res.calibration_digest),
  };
}

/** What an answer to the OWNER tells about its session. */
function sessionFacts(res) {
  return {
    generation: num(res.generation),
    epoch: num(res.camera_check_epoch),
    sourceOk: Boolean(res.source_ok),
    cameraOk: Boolean(res.camera_ok),
    robotOk: Boolean(res.robot_ok),
    verified: Boolean(res.verified),
    workflowActive: Boolean(res.workflow_active),
  };
}

/** This tab no longer holds a session. */
function dropHeld(patch = {}) {
  stopJobPoll();
  set({
    phase: 'idle',
    held: false,
    sessionId: '',
    ...SESSION_FLAGS_OFF,
    checking: '',
    cameraCheck: null,
    robotCheck: null,
    resolveJob: null,
    streamError: '',
    ...patch,
  });
}

// ── the GUI stream (serialized) ─────────────────────────────────────────────

function chain(step) {
  guiChain = guiChain.then(step).catch(() => {});
  return guiChain;
}

/** Wait for every GUI POST in flight (§9.1 step 4). Never rejects. */
function guiIdle() {
  return guiChain;
}

function enqueueStreamOn(gen) {
  return chain(async () => {
    // A leave (or any stop) bumped the generation meanwhile: no late `on`.
    if (gen !== timerGen) return;
    const r = await transport.postStream(true);
    if (gen !== timerGen) return;
    if (r.unreachable) set({ streamError: STATION_DE.guiUnreachable });
    else if (!r.ok) {
      const body = r.body || {};
      set({ streamError: str(body.message) || str(body.error_de) || STATION_DE.guiRefused });
    } else if (state.streamError) set({ streamError: '' });
  });
}

function enqueueStreamOff() {
  return chain(() => transport.postStream(false));
}

// ── the timer (§9.1 step 3) ─────────────────────────────────────────────────

function stopTimer() {
  if (tickTimer !== null) {
    clearInterval(tickTimer);
    tickTimer = null;
  }
  timerGen += 1;
}

function startTimer() {
  stopTimer();
  const gen = timerGen;
  tickTimer = setInterval(() => { tick(gen); }, KEEPALIVE_MS);
  // The first tick turns the stream on (§9.1 step 2).
  tick(gen);
}

async function tick(gen) {
  if (gen !== timerGen || !state.held) return;
  // Calls never overlap: a tick whose previous call is still pending is skipped.
  if (tickPending) return;
  tickPending = true;
  let r;
  try {
    r = await transport.callSession(
      { command: 'keepalive', session_id: state.sessionId, client_id: cid() },
      KEEPALIVE_TIMEOUT_MS,
    );
  } finally {
    tickPending = false;
  }
  if (gen !== timerGen) return;
  if (!r || !r.ok) {
    // No answer (rosbridge down, a timeout): POST nothing. The GUI lease ends
    // the stream within 10 s and the server the session after 10 s.
    set({ notice: STATION_DE.linkLost, noticeKind: 'bad' });
    return;
  }
  const res = r.res;
  if (res.accepted) {
    set({
      ...rigFacts(res),
      ...sessionFacts(res),
      ...(state.notice === STATION_DE.linkLost ? { notice: '', noticeKind: '' } : {}),
    });
    enqueueStreamOn(gen);
    maybeAutoCameraCheck(res);
    return;
  }
  const code = str(res.reason_code);
  if (code === 'SESSION_ENDED') {
    stopTimer();
    dropHeld({ ...rigFacts(res), notice: endMessageDe(res), noticeKind: 'warn', startOffered: true });
    autoArmed = false;
    enqueueStreamOff();
    return;
  }
  if (code === 'OWNER_MISMATCH') {
    // Another tab took over and holds the stream now: POST nothing.
    stopTimer();
    dropHeld({ ...rigFacts(res), notice: SESSION_REASON_DE.OWNER_MISMATCH, noticeKind: 'warn', startOffered: true });
    autoArmed = false;
    return;
  }
  set({ notice: sessionMessageDe(res), noticeKind: 'warn' });
}

// ── enter (§9.1 steps 1–2) ──────────────────────────────────────────────────

function clearEnterRetry() {
  if (enterRetryTimer !== null) {
    clearTimeout(enterRetryTimer);
    enterRetryTimer = null;
  }
}

function scheduleEnterRetry() {
  clearEnterRetry();
  enterRetryTimer = setTimeout(() => {
    enterRetryTimer = null;
    // Only while the Roboter-Studio tab is open (§9.1 step 2).
    if (!ctx.workshopOpen || !state.active || state.held || state.phase !== 'idle') return;
    enter();
  }, ENTER_RETRY_MS);
}

async function enter() {
  if (state.held || state.phase === 'entering' || state.phase === 'leaving') return;
  clearEnterRetry();
  enterSeq += 1;
  const my = enterSeq;
  set({ phase: 'entering', startOffered: false, notice: '', noticeKind: '' });
  const r = await transport.callSession({ command: 'enter', session_id: '', client_id: cid() }, COMMAND_TIMEOUT_MS);
  if (my !== enterSeq) return; // a sign-out or a reset came meanwhile
  if (!r.ok) {
    if (r.oldImage) {
      set({ phase: 'idle', notice: STATION_DE.oldImage, noticeKind: 'bad', startOffered: true });
      return;
    }
    set({ phase: 'idle', notice: STATION_DE.enterNoAnswer, noticeKind: 'warn' });
    scheduleEnterRetry();
    return;
  }
  const res = r.res;
  if (res.accepted) {
    usedPairs.clear();
    set({
      ...rigFacts(res),
      ...sessionFacts(res),
      phase: 'held',
      held: true,
      sessionId: str(res.session_id),
      notice: '',
      noticeKind: '',
      streamError: '',
      cameraCheck: null,
      robotCheck: null,
      resolveJob: null,
    });
    startTimer();
    return;
  }
  const code = str(res.reason_code);
  const message = sessionMessageDe(res);
  if (code === 'RETORQUE_PENDING' || code === 'BUSY_OTHER_TAB') {
    // Retried every 2 s while the tab is open; BUSY_OTHER_TAB takes over once
    // the other window has been silent > 4 s (the server decides). POST nothing.
    set({
      ...rigFacts(res),
      phase: 'idle',
      notice: code === 'BUSY_OTHER_TAB' ? SESSION_REASON_DE.BUSY_OTHER_TAB : message,
      noticeKind: 'warn',
    });
    scheduleEnterRetry();
    return;
  }
  if (code === 'LEADER_ACTIVE') {
    // D7: the page shows the leader switch; switching the leader off re-arms.
    armOnLeaderOff = true;
    set({ ...rigFacts(res), phase: 'idle', notice: message, noticeKind: 'warn' });
    return;
  }
  set({ ...rigFacts(res), phase: 'idle', notice: message, noticeKind: 'warn', startOffered: true });
}

function leaderBlockedNow() {
  if (ctx.leaderOn) return true;
  for (const v of leaderSwitching.values()) if (v) return true;
  return false;
}

function maybeAutoEnter() {
  if (!autoArmed || !state.active || !ctx.workshopOpen) return;
  if (state.held || state.phase !== 'idle' || state.startOffered) return;
  if (!clientIdReady || !resumeDone) return;
  if (!ctx.leaderProbed || leaderBlockedNow()) return;
  autoArmed = false;
  enter();
}

/** „Station starten" (§9.1 step 1): an explicit enter. */
export function startSession() {
  if (!state.active) return;
  clearEnterRetry();
  set({ startOffered: false });
  enter();
}

// ── resume at start (§9.1 step 1) ───────────────────────────────────────────

function stopResume() {
  if (resumeTimer !== null) {
    clearTimeout(resumeTimer);
    resumeTimer = null;
  }
}

function scheduleResume(delay = 0) {
  stopResume();
  resumeTimer = setTimeout(() => {
    resumeTimer = null;
    resumeStep();
  }, delay);
}

function finishResume() {
  resumeDone = true;
  stopResume();
  maybeAutoEnter();
}

async function resumeStep() {
  if (resumeDone || resumeInFlight || !clientIdReady) return;
  // Not in Pi mode, not while a Jetson owns rosbridge, not on a rig that says
  // it is no station; before the capabilities arrive the question is asked.
  if (!ctx.piModeResolved) {
    scheduleResume(RESUME_POLL_MS);
    return;
  }
  if (ctx.piMode || ctx.jetsonConnected || (ctx.stationProtocolKnown && !ctx.stationRig)) {
    finishResume();
    return;
  }
  if (!transport.isRosReady()) {
    scheduleResume(RESUME_POLL_MS);
    return;
  }
  resumeInFlight = true;
  let r;
  try {
    r = await transport.callSession({ command: 'status', session_id: '', client_id: cid() }, COMMAND_TIMEOUT_MS);
  } finally {
    resumeInFlight = false;
  }
  if (resumeDone) return;
  if (!r.ok) {
    // An older server has no such service: the answer is ignored (§9.1 step 1).
    if (r.oldImage) {
      finishResume();
      return;
    }
    scheduleResume(RESUME_POLL_MS);
    return;
  }
  const res = r.res;
  set(rigFacts(res));
  resumeDone = true;
  stopResume();
  if (str(res.session_id)) {
    // This tab still owns the session: resume at once, whatever page is open.
    enter();
    return;
  }
  maybeAutoEnter();
}

// ── the camera check, the robot check, the re-solve (§7) ────────────────────

function checkSummary(res) {
  return {
    result: str(res.result),
    success: Boolean(res.success),
    message: checkMessageDe(res),
    actionDe: str(res.action_de),
    rmsMm: res.rms_mm,
    maxMm: res.max_mm,
    centroidMm: res.centroid_mm,
    rotationDeg: res.rotation_deg,
    refIds: Array.isArray(res.ref_ids) ? res.ref_ids.slice() : [],
    refResidualMm: Array.isArray(res.ref_residual_mm) ? res.ref_residual_mm.slice() : [],
    dotId: num(res.dot_id),
    dotDxyMm: res.dot_dxy_mm,
    dotDzMm: res.dot_dz_mm,
  };
}

function checkFlags(res) {
  if (!res.accepted) return {};
  return {
    cameraOk: Boolean(res.camera_ok),
    robotOk: Boolean(res.robot_ok),
    verified: Boolean(res.verified),
    ...(Number.isFinite(Number(res.generation)) ? { generation: num(res.generation) } : {}),
  };
}

function maybeAutoCameraCheck(res) {
  if (!state.held || state.checking) return;
  if (str(res.state) !== 'ACTIVE' || !res.source_ok || res.camera_ok || !res.station_configured) return;
  const pair = `${state.sessionId}:${num(res.camera_check_epoch)}`;
  if (usedPairs.has(pair)) return;
  runCameraCheck();
}

/**
 * „Kamera prüfen" (§7.1): by itself once per (session_id, camera_check_epoch),
 * and on demand. NO_FRAME / WRONG_SIZE / BUSY (or no answer) do not use up the
 * occasion: the next tick tries again.
 */
export async function runCameraCheck() {
  if (!state.held || state.checking) return null;
  const sid = state.sessionId;
  const pair = `${sid}:${state.epoch}`;
  set({ checking: 'camera' });
  const r = await transport.callCheck({ command: 'camera', session_id: sid, client_id: cid() }, COMMAND_TIMEOUT_MS);
  if (state.sessionId !== sid || !state.held) {
    if (state.checking === 'camera') set({ checking: '' });
    return null;
  }
  if (!r.ok) {
    set({ checking: '' });
    return null;
  }
  const res = r.res;
  if (!CHECK_RETRY_RESULTS.includes(str(res.result))) usedPairs.add(pair);
  set({ checking: '', cameraCheck: checkSummary(res), ...checkFlags(res) });
  return res;
}

/** „Prüfen" of the robot check (§7.2); the hand-guide is the caller's. */
export async function runRobotCheck() {
  if (!state.held || state.checking) return null;
  const sid = state.sessionId;
  set({ checking: 'robot' });
  const r = await transport.callCheck({ command: 'robot', session_id: sid, client_id: cid() }, COMMAND_TIMEOUT_MS);
  if (state.sessionId !== sid || !state.held) {
    if (state.checking === 'robot') set({ checking: '' });
    return null;
  }
  if (!r.ok) {
    set({ checking: '', robotCheck: { result: '', success: false, message: STATION_DE.linkLost } });
    return null;
  }
  const res = r.res;
  set({ checking: '', robotCheck: checkSummary(res), ...checkFlags(res) });
  return res;
}

function stopJobPoll() {
  if (jobTimer !== null) {
    clearTimeout(jobTimer);
    jobTimer = null;
  }
}

function pollJob(jobId, sid) {
  stopJobPoll();
  jobTimer = setTimeout(async () => {
    jobTimer = null;
    if (!state.held || state.sessionId !== sid) return;
    const r = await transport.callCheck({ command: 'status', session_id: sid, client_id: cid() }, COMMAND_TIMEOUT_MS);
    if (!state.held || state.sessionId !== sid) return;
    if (!r.ok) {
      pollJob(jobId, sid);
      return;
    }
    const res = r.res;
    const jobState = str(res.job_state);
    if (num(res.job_id) !== jobId) {
      // Another job replaced ours as the owner's last job: ours is over.
      set({ resolveJob: { jobId, jobState: 'stale', result: '', message: '' } });
      return;
    }
    if (JOB_END_STATES.includes(jobState)) {
      set({
        checking: state.checking === 'resolve' ? '' : state.checking,
        resolveJob: { jobId, jobState, result: str(res.result), message: checkMessageDe(res), actionDe: str(res.action_de) },
        cameraCheck: str(res.result) ? checkSummary(res) : state.cameraCheck,
        ...checkFlags(res),
      });
      return;
    }
    set({ resolveJob: { jobId, jobState, result: '', message: '' } });
    pollJob(jobId, sid);
  }, JOB_POLL_MS);
}

/** „Kamera neu ausrichten (Lehrkraft)" (§7.3): a station worker job, polled. */
export async function resolveCamera() {
  if (!state.held || state.checking) return null;
  const sid = state.sessionId;
  set({ checking: 'resolve', resolveJob: { jobId: 0, jobState: 'queued', result: '', message: '' } });
  const r = await transport.callCheck({ command: 'resolve_camera', session_id: sid, client_id: cid() }, COMMAND_TIMEOUT_MS);
  if (!state.held || state.sessionId !== sid) return null;
  if (!r.ok) {
    set({ checking: '', resolveJob: { jobId: 0, jobState: 'failed', result: '', message: STATION_DE.linkLost } });
    return null;
  }
  const res = r.res;
  if (str(res.result) !== 'QUEUED') {
    set({ checking: '', resolveJob: { jobId: 0, jobState: 'failed', result: str(res.result), message: checkMessageDe(res) } });
    return res;
  }
  const jobId = num(res.job_id);
  set({ resolveJob: { jobId, jobState: 'queued', result: '', message: '' } });
  pollJob(jobId, sid);
  return res;
}

/**
 * One StationCommission command with this tab's ids (§8.3). Resolves the
 * transport's `{ok, res, timedOut, oldImage, unreachable}`; never rejects.
 */
export function commission(command, fields = {}) {
  return transport.callCommission(
    { ...fields, command, session_id: state.sessionId, client_id: cid() },
    COMMAND_TIMEOUT_MS,
  );
}

// ── leaving (§9.1 step 4) ───────────────────────────────────────────────────

/**
 * „Station verlassen" and the leader switch's leave. Always: stop the timer
 * (generation bump), wait for every GUI POST in flight, send `leave`; then by
 * its answer. Resolves `{left, message}`.
 */
export async function leave() {
  clearEnterRetry();
  autoArmed = false;
  armOnLeaderOff = false;
  if (state.phase === 'leaving') return { left: false, message: STATION_DE.leaving };
  if (!state.held) {
    if (state.phase === 'idle') set({ startOffered: state.active, notice: '', noticeKind: '' });
    return { left: true, message: '' };
  }
  const sid = state.sessionId;
  stopTimer();
  set({ phase: 'leaving' });
  await guiIdle();
  const r = await transport.callSession({ command: 'leave', session_id: sid, client_id: cid() }, COMMAND_TIMEOUT_MS);
  const res = r.ok ? r.res : null;
  const code = res ? str(res.reason_code) : '';
  if (res && (res.accepted || code === 'SESSION_ENDED')) {
    dropHeld({ ...rigFacts(res), startOffered: true, notice: '', noticeKind: '' });
    await enqueueStreamOff();
    return { left: true, message: '' };
  }
  if (res && code === 'WORKFLOW_ACTIVE') {
    const message = sessionMessageDe(res) || STATION_DE.stopProgramFirst;
    set({ phase: 'held', workflowActive: true, notice: message, noticeKind: 'warn' });
    startTimer();
    return { left: false, message };
  }
  // OWNER_MISMATCH (another tab owns the stream) or no answer (the GUI lease
  // ends it within 10 s): POST nothing, count as left.
  dropHeld({ ...(res ? rigFacts(res) : {}), startOffered: true, notice: '', noticeKind: '' });
  return { left: true, message: '' };
}

/**
 * Sign-out (utils/signOut::signOutStudent, right after the cloud-inference
 * step): only if this tab holds the session, `leave` started and `off` chained
 * on its answer, neither awaited; in every case a fresh client id.
 */
export function endStationSessionOnLogout() {
  clearEnterRetry();
  stopJobPoll();
  autoArmed = false;
  armOnLeaderOff = false;
  enterSeq += 1;
  if (state.held) {
    const sid = state.sessionId;
    const id = cid();
    stopTimer();
    transport.callSession({ command: 'leave', session_id: sid, client_id: id }, COMMAND_TIMEOUT_MS)
      .then((r) => {
        const res = r && r.ok ? r.res : null;
        if (res && (res.accepted || str(res.reason_code) === 'SESSION_ENDED')) enqueueStreamOff();
      })
      .catch(() => {});
  } else {
    stopTimer();
  }
  usedPairs.clear();
  dropHeld({ startOffered: false, notice: '', noticeKind: '' });
  clientId = remintClientId();
}

// ── pagehide (§9.1 step 5) ──────────────────────────────────────────────────

function onPagehide() {
  // Only the tab that holds the session; no `leave` (a reload resumes, D10).
  if (state.held) transport.beaconStreamOff();
}

// ── the context (StudentApp, WorkshopPage, LeaderToggle) ────────────────────

function recompute() {
  const stationRig = ctx.stationRig;
  const active = stationRig && ctx.piModeResolved && !ctx.piMode && !ctx.jetsonConnected;
  const leaderBlocked = leaderBlockedNow();
  const patch = {};
  if (state.stationRig !== stationRig) patch.stationRig = stationRig;
  if (state.active !== active) patch.active = active;
  if (state.leaderBlocked !== leaderBlocked) patch.leaderBlocked = leaderBlocked;
  if (!active && state.held) {
    // A claimed Jetson (or a rig that stopped being a station): stop the timer,
    // switch the stream off; the server ends the session after 10 s.
    stopTimer();
    enqueueStreamOff();
    clearEnterRetry();
    dropHeld({ ...patch, startOffered: false });
    return;
  }
  if (!active) clearEnterRetry();
  if (Object.keys(patch).length) set(patch);
}

/**
 * The rig context, from StudentApp on every change.
 * @param {object} o
 * @param {?number} o.stationProtocol  capabilities.station_protocol (undefined until known)
 * @param {boolean} o.capabilitiesKnown the capability manifest has arrived
 * @param {boolean} o.piMode
 * @param {boolean} o.piModeResolved
 * @param {boolean} o.jetsonConnected
 */
export function configure({
  stationProtocol, capabilitiesKnown = false, piMode = false, piModeResolved = true, jetsonConnected = false,
} = {}) {
  ctx.stationProtocolKnown = Boolean(capabilitiesKnown);
  ctx.stationRig = stationProtocol === STATION_PROTOCOL;
  ctx.piMode = Boolean(piMode);
  ctx.piModeResolved = Boolean(piModeResolved);
  ctx.jetsonConnected = Boolean(jetsonConnected);
  recompute();
  if (!resumeDone && clientIdReady) scheduleResume();
  maybeAutoEnter();
}

/**
 * The Roboter-Studio page, while it is shown (WorkshopPage). Opening it is
 * „navigating into the tab": a new session is entered by itself unless one is
 * held. Closing it never leaves (D13).
 * @param {object} o
 * @param {boolean} o.open
 * @param {boolean} [o.leaderOn]      the control bridge reports the leader on
 * @param {boolean} [o.leaderProbed]  the bridge has answered once
 */
export function setWorkshop({ open, leaderOn = false, leaderProbed = true } = {}) {
  const wasOpen = ctx.workshopOpen;
  const wasBlocked = leaderBlockedNow();
  ctx.workshopOpen = Boolean(open);
  ctx.leaderOn = Boolean(open && leaderOn);
  ctx.leaderProbed = Boolean(leaderProbed);
  if (ctx.workshopOpen && !wasOpen) {
    // A new visit of the tab: an explicit leave of an earlier visit is forgotten.
    autoArmed = true;
    armOnLeaderOff = false;
    if (state.startOffered) set({ startOffered: false, notice: '', noticeKind: '' });
  }
  if (!ctx.workshopOpen && wasOpen) clearEnterRetry();
  if (wasBlocked && !leaderBlockedNow() && armOnLeaderOff) {
    armOnLeaderOff = false;
    autoArmed = true;
  }
  recompute();
  maybeAutoEnter();
}

/** A LeaderToggle reports its switch or its readiness overlay (§9.1 step 1). */
export function reportLeaderSwitching(key, switching) {
  const wasBlocked = leaderBlockedNow();
  if (switching) leaderSwitching.set(key, true);
  else leaderSwitching.delete(key);
  if (wasBlocked && !leaderBlockedNow() && armOnLeaderOff) {
    armOnLeaderOff = false;
    autoArmed = true;
  }
  recompute();
  maybeAutoEnter();
}

/** StudentApp, once: the client id, the duplicated-tab check, the resume. */
export function start() {
  if (started) return;
  started = true;
  if (!pagehideAttached && typeof window !== 'undefined') {
    window.addEventListener('pagehide', onPagehide);
    pagehideAttached = true;
  }
  checkDuplicateTab().then((id) => {
    clientId = id;
    clientIdReady = true;
    scheduleResume();
    maybeAutoEnter();
  });
}

/** Tests only: back to the initial state with no timer or listener left. */
export function _resetForTests() {
  stopTimer();
  stopResume();
  stopJobPoll();
  clearEnterRetry();
  if (pagehideAttached && typeof window !== 'undefined') window.removeEventListener('pagehide', onPagehide);
  pagehideAttached = false;
  started = false;
  clientId = null;
  clientIdReady = false;
  resumeDone = false;
  resumeInFlight = false;
  autoArmed = false;
  armOnLeaderOff = false;
  enterSeq += 1;
  tickPending = false;
  guiChain = Promise.resolve();
  usedPairs.clear();
  leaderSwitching.clear();
  Object.assign(ctx, {
    stationProtocolKnown: false,
    stationRig: false,
    piMode: false,
    piModeResolved: false,
    jetsonConnected: false,
    workshopOpen: false,
    leaderOn: false,
    leaderProbed: false,
  });
  state = INITIAL;
  listeners.clear();
}
