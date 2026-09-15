/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// Vormachen session core (hand mode): the keyboard state machine that frees,
// records, captures and re-locks the REAL follower through the four manual
// services. Rendering lives in the overlay; this file owns every service call.
//
// Three rules carry the safety weight, each with its measurement in the spec:
//   1. ONE FIFO service queue. Manual calls overlap at the server (a
//      hand_guide(false) landing between a hand_guide(true)'s claim and its
//      lock left the arm limp with on_manual False), so a call is SENT only
//      after the previous call's answer arrived. A key never drops an intent
//      because the queue is busy: it is queued and re-checks its state when
//      it runs.
//   2. The manual session is closed exactly ONCE (sessionOpen, set by a
//      successful record start / hand_guide(true), cleared only by a
//      CONFIRMED hand_guide(false) or a successful record cancel). The ONLY
//      other hand_guide(false) is the deliberate „Stopp" of a robot preview.
//   3. A real-arm preview (`vorschau`) ends on OBSERVED stillness, never on a
//      bare timer: /workshop/replay answers when the drive STARTS, and the
//      server stretches fast takes, so no timer can know when the arm stops.
//
// Leader mode (D8, `mode === 'leader'`): the follower is teleoperated and stays
// torqued, so there is no hand_guide call, no keepalive, no countdown, no
// real-arm preview and no glide — only start_leader / stop_leader /
// cancel_leader and the captures, through the SAME queue. The teleop e-stop
// stays armed, so a collision DISCARDS: a take in `aufnahme`, a stop answer
// that lands after the trip, and a returned take whose trip arrives within
// TEACH_LEADER_COLLISION_GRACE_MS of its stop (keep is held until then).
//
// R7 (fixed 2026-09-15): `leaderStatusUnknown` (the leader-status bridge cannot
// say whether the leader is on) and `mode === 'pending'` (a session TeachHost
// opened before it could pick hand or leader) block every NEW teaching action —
// free the arm, start a take or a countdown, R, a capture, a real-arm replay —
// and leave every EXIT alone: Space/F still stop a take in progress, cancel a
// countdown or re-lock, Enter/Entf still keep or discard a returned take, Esc and
// „Fertig" still close. An in-flight take is therefore never interrupted by the
// bridge going away: a hand take keeps sampling until the student stops it, a
// leader take until its stop or a server data stop, and `leaderGone` (which
// needs a POSITIVE follower-only answer) is not raised by an unavailable bridge.

import { useEffect, useRef, useState } from 'react';
import { DE } from '../blocks/messages_de';
import { compactTrajectoryPoints } from '../../../utils/trajectoryCompact';
import {
  TEACH_COUNTDOWN_S, TEACH_SPACE_DEBOUNCE_MS, TEACH_KEEPALIVE_MS, TEACH_RECORD_MAX_S,
  TEACH_MIN_POINTS, TEACH_ROBOT_PREVIEW_NO_MOTION_HINT_MS, TEACH_ROBOT_PREVIEW_SETTLE_DELTA_RAD,
  TEACH_ROBOT_PREVIEW_SETTLE_STEP_MS, TEACH_ROBOT_PREVIEW_SETTLE_SAMPLES,
  TEACH_ROBOT_PREVIEW_FEED_STALE_MS, TEACH_ROBOT_PREVIEW_STOP_TAIL_MAX_MS, replayDriveEstimateMs,
  TEACH_LEADER_COLLISION_GRACE_MS,
} from './teachGates';

// Server _assert_no_other_active('leader_teach') while a take is armed — from
// another tab, or one of ours whose cancel was lost. Matched verbatim.
export const LEADER_TAKE_BUSY_DE = 'Eine Leader-Aufnahme läuft gerade — bitte zuerst beenden.';
const ELAPSED_STEP_MS = 250;

const INTERACTIVE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON']);
// NOT 'slider': the review strip's handles use only the arrow keys, none of
// which is a teach key — so after trimming with the arrows, Enter still keeps,
// R and Entf still act (the handle keeps focus; a click was never needed).
const INTERACTIVE_ROLES = new Set(['button', 'checkbox']);

// `bereit` is leader mode's resting state (the follower is teleoperated, never
// „fest" or „frei").
export const TEACH_STATES = Object.freeze([
  'fest', 'frei', 'countdown', 'aufnahme', 'pruefen', 'vorschau', 'abschluss', 'bereit',
]);

// Map a keydown to a table column, or null. A key held with Ctrl/Meta/Alt is
// never ours (Ctrl+R reloads, Ctrl+F searches).
export function classifyTeachKey(e) {
  if (!e || e.ctrlKey || e.metaKey || e.altKey) return null;
  const { key } = e;
  if (key === ' ' || key === 'Spacebar' || e.code === 'Space') return 'space';
  if (key === 'Enter') return 'enter';
  if (key === 'Delete') return 'delete';
  if (key === 'Escape' || key === 'Esc') return 'escape';
  if (typeof key === 'string' && key.length === 1) {
    const k = key.toLowerCase();
    if (k === 'f' || k === 'p' || k === 'z' || k === 'r') return k;
  }
  return null;
}

function roleOf(target) {
  return target && typeof target.getAttribute === 'function' ? target.getAttribute('role') : null;
}

function isInteractiveTarget(target) {
  if (!target) return false;
  if (INTERACTIVE_TAGS.has(target.tagName)) return true;
  if (INTERACTIVE_ROLES.has(roleOf(target))) return true;
  return target.isContentEditable === true;
}

function isButtonTarget(target) {
  return !!target && (target.tagName === 'BUTTON' || roleOf(target) === 'button');
}

// A hand_guide(false) counts as CONFIRMED when it did not throw and did not
// answer success:false (a bare answer confirms, as the retired RecordPanel did).
function isConfirmed(res) {
  return !res || res.success !== false;
}

// Contract B → { executed, points, fps }. `executed` is false when the stop
// returned no trajectory at all (e.g. its lock-timeout refusal).
function parseStopPoints(pointsJson) {
  if (typeof pointsJson !== 'string' || pointsJson.trim() === '') {
    return { executed: false, points: [], fps: 0 };
  }
  try {
    const obj = JSON.parse(pointsJson);
    if (obj && Array.isArray(obj.points)) {
      return { executed: true, points: obj.points, fps: Number(obj.fps) || 0 };
    }
  } catch (_) { /* fall through */ }
  return { executed: false, points: [], fps: 0 };
}

const INITIAL_SNAPSHOT = Object.freeze({
  state: 'fest', countdownLeft: 0, elapsedS: 0, busy: false, take: null, relock: 'none',
  releasedOnce: false, previewNoMotionHint: false, staleLeaderTake: false, keepHeld: false,
});

function initialStateFor(mode) {
  return mode === 'leader' ? 'bereit' : 'fest';
}

// The state machine, framework-free so every function can reference every
// other one and the handlers stay stable for document listeners.
export function createTeachEngine(getProps, publish) {
  const r = {
    state: initialStateFor((getProps() || {}).mode), relock: 'none', take: null, releasedOnce: false,
    countdownLeft: 0, elapsedS: 0, previewNoMotionHint: false,
    sessionOpen: false, recordActive: false, pendingStart: 0, lastSpaceAt: -Infinity,
    keepaliveQueued: false, keepaliveStopped: false, closeAfterReview: false,
    leaderLockout: false, prevLeaderLive: false, capFired: false,
    preCountdown: 'fest', countdownKind: null, recordStartedAt: 0,
    queueTail: Promise.resolve(), queueCount: 0, tornDown: false, finished: false,
    namer: null, preview: null,
    // Leader mode: a take refused as already running; the stop answer's time
    // (the collision grace window runs from it); rising collision edges seen.
    staleLeaderTake: false, stoppedAt: 0, collisionSeq: 0, prevCollision: false,
    timers: {
      countdown: null, elapsed: null, keepalive: null, previewTick: null, previewHint: null, grace: null,
    },
  };

  const now = () => {
    const fn = getProps().now;
    return typeof fn === 'function' ? fn() : Date.now();
  };

  const isLeader = () => getProps().mode === 'leader';
  // R7: opened before the bridge could pick a mode; TeachHost remounts the
  // overlay (a fresh engine) with the resolved mode.
  const isPending = () => getProps().mode === 'pending';

  // Leader mode: Enter/Esc cannot keep a returned take while a trip of the
  // detector's debounce may still be on its way.
  function keepHeld() {
    return isLeader() && r.state === 'pruefen' && now() - r.stoppedAt <= TEACH_LEADER_COLLISION_GRACE_MS;
  }

  function emit() {
    publish({
      state: r.state, countdownLeft: r.countdownLeft, elapsedS: r.elapsedS,
      busy: r.queueCount > 0, take: r.take, relock: r.relock, releasedOnce: r.releasedOnce,
      previewNoMotionHint: r.previewNoMotionHint, staleLeaderTake: r.staleLeaderTake, keepHeld: keepHeld(),
    });
  }

  // A throwing overlay callback must never wedge the state machine.
  function notify(name, ...args) {
    const fn = getProps()[name];
    if (typeof fn !== 'function') return;
    try { fn(...args); } catch (e) { console.warn(`useTeachSession ${name} threw`, e); }
  }

  function sound(name) {
    const s = getProps().sounds;
    if (!s || typeof s[name] !== 'function') return;
    try { s[name](); } catch (_) { /* a sound never breaks teaching */ }
  }

  // Throws when the service is missing, so every caller treats it like a
  // failed transport.
  function call(name, ...args) {
    const fn = (getProps().services || {})[name];
    if (typeof fn !== 'function') return Promise.reject(new Error(`${name} unavailable`));
    try {
      return Promise.resolve(fn(...args));
    } catch (e) {
      return Promise.reject(e);
    }
  }

  function clearTimer(name) {
    const t = r.timers[name];
    if (t === null) return;
    clearTimeout(t);
    clearInterval(t);
    r.timers[name] = null;
  }

  function setState(next) {
    const prev = r.state;
    r.state = next;
    if (prev === 'frei' && next !== 'frei') clearTimer('keepalive');
    // Never arm a timer after teardown (an answer that lands after unmount).
    if (next === 'frei' && prev !== 'frei' && !r.tornDown) {
      clearTimer('keepalive');
      r.timers.keepalive = setInterval(keepaliveTick, TEACH_KEEPALIVE_MS);
    }
    if (prev === 'aufnahme' && next !== 'aufnahme') clearTimer('elapsed');
    if (prev === 'pruefen' && next !== 'pruefen') clearTimer('grace');
  }

  // The ONE FIFO queue. `fn` runs only after the previous entry settled;
  // after teardown only teardown work runs.
  function enqueue(fn, { onSkip } = {}) {
    r.queueCount += 1;
    emit();
    const run = async () => {
      try {
        if (r.tornDown) {
          if (onSkip) onSkip();
          return undefined;
        }
        return await fn();
      } catch (e) {
        console.warn('useTeachSession queued call failed', e);
        return undefined;
      } finally {
        r.queueCount -= 1;
        emit();
      }
    };
    const p = r.queueTail.then(run, run);
    r.queueTail = p.then(() => undefined, () => undefined);
    return p;
  }

  function afterState() {
    if (isLeader()) return 'bereit';
    return r.relock === 'failed' ? 'frei' : 'fest';
  }

  // ---- countdown + starts ------------------------------------------------

  function startCountdown(kind) {
    r.preCountdown = r.state;
    r.countdownKind = kind;
    r.countdownLeft = TEACH_COUNTDOWN_S;
    setState('countdown');
    sound('tick');
    clearTimer('countdown');
    r.timers.countdown = setInterval(() => {
      r.countdownLeft -= 1;
      if (r.countdownLeft > 0) {
        sound('tick');
        emit();
        return;
      }
      clearTimer('countdown');
      // R7: the leader status became unknown during the count — the start is a
      // NEW teaching action, so the count ends where it began instead.
      if (newActionsBlocked(getProps())) {
        setState(r.preCountdown);
        emit();
        return;
      }
      emit();
      runStart(kind, 'countdown', r.preCountdown);
    }, 1000);
    emit();
  }

  // Cancel is local and immediate — but only while the count is still
  // running; once it reached 0 the start is already queued.
  function cancelCountdown() {
    if (r.state !== 'countdown' || r.countdownLeft <= 0) return;
    clearTimer('countdown');
    r.countdownLeft = 0;
    setState(r.preCountdown);
    emit();
  }

  // `expected` is the state the start must still find when it runs: the
  // countdown (not cancelled), or `frei` for Space without a countdown.
  function runStart(kind, expected, fallback) {
    r.pendingStart += 1;
    const done = () => { r.pendingStart -= 1; };
    enqueue(async () => {
      try {
        if (r.state !== expected) return;
        let res;
        try {
          res = kind === 'record' ? await call('recordControl', 'start') : await call('handGuide', true);
        } catch (_) {
          setState(fallback);
          notify('onError', DE.TEACH_OFFLINE);
          emit();
          return;
        }
        if (!res || !res.success) {
          setState(fallback);
          notify('onError', (res && res.message)
            || (kind === 'record' ? DE.TEACH_RECORD_START_FAILED : DE.TEACH_FREE_FAILED));
          emit();
          return;
        }
        r.sessionOpen = true;
        r.releasedOnce = true;
        // A new take (or a new free phase) supersedes an earlier failed
        // re-lock; its own stop/close decides `relock` again.
        r.relock = 'none';
        if (kind === 'record') {
          r.recordActive = true;
          r.capFired = false;
          r.elapsedS = 0;
          r.recordStartedAt = now();
          setState('aufnahme');
          sound('start');
          clearTimer('elapsed');
          if (!r.tornDown) r.timers.elapsed = setInterval(elapsedTick, ELAPSED_STEP_MS);
        } else {
          setState('frei');
        }
        emit();
        // The leader came on while the start was in flight.
        if (r.leaderLockout) {
          if (kind === 'record') stop();
          else lock();
        }
      } finally {
        done();
      }
    }, { onSkip: done });
  }

  function elapsedTick() {
    if (r.state !== 'aufnahme') return;
    r.elapsedS = (now() - r.recordStartedAt) / 1000;
    if (r.elapsedS >= TEACH_RECORD_MAX_S && !r.capFired) {
      r.capFired = true;
      notify('onError', DE.TEACH_CAP_REACHED);
      stop();
    }
    emit();
  }

  // ---- stop: record('stop') then the ONE awaited close ------------------

  function stop() {
    if (r.state !== 'aufnahme') return;
    if (isLeader()) {
      stopLeader();
      return;
    }
    enqueue(async () => {
      if (r.state !== 'aufnahme') return;
      let res;
      try {
        res = await call('recordControl', 'stop');
      } catch (_) {
        // The server keeps recording until its cap; the student can retry.
        r.closeAfterReview = false;
        notify('onError', DE.TEACH_OFFLINE);
        emit();
        return;
      }
      r.recordActive = false;
      clearTimer('elapsed');
      let closeConfirmed = true;
      if (r.sessionOpen) {
        try {
          closeConfirmed = isConfirmed(await call('handGuide', false));
        } catch (_) {
          closeConfirmed = false;
        }
        if (closeConfirmed) r.sessionOpen = false;
      }
      // Decided by the LAST re-lock attempt (the close), never by record
      // stop's own success: a stop whose re-lock failed but whose close then
      // re-locked leaves the arm locked.
      r.relock = closeConfirmed ? 'ok' : 'failed';
      const parsed = parseStopPoints(res && res.points_json);
      if (parsed.points.length >= TEACH_MIN_POINTS) {
        const pts = parsed.points;
        const first = pts[0];
        const last = pts[pts.length - 1];
        const span = Number(last[last.length - 1]) - Number(first[first.length - 1]);
        r.take = {
          points: pts,
          fps: parsed.fps,
          sampleCount: Number(res && res.sample_count) || pts.length,
          durationS: Number(res && res.duration_s) || (Number.isFinite(span) ? span : 0),
          relockOk: closeConfirmed,
        };
        setState('pruefen');
        sound('stop');
        emit();
        notify('onTake', r.take);
        return;
      }
      // No replayable take: a still arm returns ONE point, and one point can
      // never be replayed. The server's own refusal is shown only when the
      // stop did not execute or the arm is still not locked — otherwise its
      // stale „nicht wieder verriegelt" would contradict a confirmed close.
      const serverSpoke = res && res.success === false && res.message;
      const msg = serverSpoke && (!parsed.executed || !closeConfirmed) ? res.message : DE.TEACH_NO_MOTION;
      r.closeAfterReview = false;
      setState(afterState());
      emit();
      notify('onError', msg);
    });
  }

  // ---- leader mode (D8): start_leader / stop_leader / cancel_leader -------

  // `bereit` + Space, and R after a discard. No countdown: nothing goes limp.
  function startLeader() {
    if (r.state !== 'bereit') return;
    r.pendingStart += 1;
    const done = () => { r.pendingStart -= 1; };
    enqueue(async () => {
      try {
        if (r.state !== 'bereit') return;
        const seq = r.collisionSeq;
        let res;
        try {
          res = await call('recordControl', 'start_leader');
        } catch (_) {
          notify('onError', DE.TEACH_OFFLINE);
          emit();
          return;
        }
        if (!res || !res.success) {
          const msg = (res && res.message) || DE.TEACH_RECORD_START_FAILED;
          if (msg === LEADER_TAKE_BUSY_DE) r.staleLeaderTake = true;
          emit();
          notify('onError', msg);
          return;
        }
        r.recordActive = true;
        r.staleLeaderTake = false;
        r.capFired = false;
        r.elapsedS = 0;
        r.recordStartedAt = now();
        // The e-stop tripped while the start was in flight: the server discards
        // the take on its next tick, and so do we.
        if (getProps().collisionActive || r.collisionSeq !== seq) {
          collisionDiscardRecording();
          return;
        }
        setState('aufnahme');
        sound('start');
        clearTimer('elapsed');
        if (!r.tornDown) r.timers.elapsed = setInterval(elapsedTick, ELAPSED_STEP_MS);
        emit();
      } finally {
        done();
      }
    }, { onSkip: done });
  }

  // The stop's answer is judged against collisions seen since it was SENT: a
  // trip that reached the client first already moved the state to `bereit`
  // and told the student, so a take returned afterwards is dropped silently.
  function stopLeader() {
    enqueue(async () => {
      if (r.state !== 'aufnahme') return;
      const seq = r.collisionSeq;
      let res;
      try {
        res = await call('recordControl', 'stop_leader');
      } catch (_) {
        if (r.state !== 'aufnahme' || r.collisionSeq !== seq) return;
        // The server keeps sampling until a data stop or its cap; retry.
        r.closeAfterReview = false;
        notify('onError', DE.TEACH_OFFLINE);
        emit();
        return;
      }
      r.recordActive = false;
      if (r.state !== 'aufnahme' || r.collisionSeq !== seq) {
        r.closeAfterReview = false;
        emit();
        return;
      }
      clearTimer('elapsed');
      const parsed = parseStopPoints(res && res.points_json);
      if (res && res.success !== false && parsed.points.length >= TEACH_MIN_POINTS) {
        const pts = parsed.points;
        const first = pts[0];
        const last = pts[pts.length - 1];
        const span = Number(last[last.length - 1]) - Number(first[first.length - 1]);
        r.take = {
          points: pts,
          fps: parsed.fps,
          sampleCount: Number(res.sample_count) || pts.length,
          durationS: Number(res.duration_s) || (Number.isFinite(span) ? span : 0),
          relockOk: true, // the follower never went limp
        };
        r.stoppedAt = now();
        setState('pruefen');
        clearTimer('grace');
        // Re-publish once the window has passed, so keepHeld clears on screen.
        if (!r.tornDown) {
          r.timers.grace = setTimeout(() => { if (r.state === 'pruefen') emit(); },
            TEACH_LEADER_COLLISION_GRACE_MS + 1);
        }
        sound('stop');
        emit();
        if (judgeReviewCollision()) return;
        notify('onTake', r.take);
        return;
      }
      // A refusal (a data stop's sentence, „keine Leader-Aufnahme") or a still
      // follower / an un-activated rig that returned fewer than 2 points.
      r.closeAfterReview = false;
      setState('bereit');
      emit();
      notify('onError', res && res.success === false && res.message ? res.message : DE.TEACH_NO_MOTION);
    });
  }

  // A trip during a take: the samples hold the press. Best-effort cancel (the
  // server's sampler discards on its own within one tick).
  function collisionDiscardRecording() {
    r.closeAfterReview = false;
    setState('bereit');
    emit();
    notify('onError', DE.TEACH_COLLISION_DISCARDED);
    enqueue(async () => {
      try {
        await call('recordControl', 'cancel_leader');
        r.recordActive = false;
      } catch (_) { /* teardown or the next start/stop answers for it */ }
    });
  }

  // LEVEL-triggered: a returned take is discarded while the e-stop is up and
  // its stop answered at most TEACH_LEADER_COLLISION_GRACE_MS ago.
  function judgeReviewCollision() {
    if (!isLeader() || !getProps().collisionActive || r.state !== 'pruefen' || !r.take) return false;
    if (now() - r.stoppedAt > TEACH_LEADER_COLLISION_GRACE_MS) return false;
    r.take = null;
    r.closeAfterReview = false;
    setState('bereit');
    emit();
    notify('onError', DE.TEACH_COLLISION_DISCARDED);
    return true;
  }

  function onCollisionChange(active) {
    const was = r.prevCollision;
    r.prevCollision = !!active;
    if (!getProps().enabled || !isLeader() || r.tornDown || r.finished) return;
    if (active && !was) {
      r.collisionSeq += 1;
      if (r.state === 'aufnahme') {
        collisionDiscardRecording();
        return;
      }
    }
    if (active) judgeReviewCollision();
  }

  // „Alte Aufnahme verwerfen": the take that refused our start.
  function discardStaleLeaderTake() {
    if (!isLeader() || !r.staleLeaderTake || r.state !== 'bereit') return;
    enqueue(async () => {
      if (!r.staleLeaderTake) return;
      let res;
      try {
        res = await call('recordControl', 'cancel_leader');
      } catch (_) {
        notify('onError', DE.TEACH_OFFLINE);
        return;
      }
      if (res && res.success === false) {
        notify('onError', res.message || DE.TEACH_OFFLINE);
        return;
      }
      r.staleLeaderTake = false;
      emit();
    });
  }

  // ---- lock: the confirmed hand_guide(false) ----------------------------

  function lockAllowed() {
    return r.state === 'frei' || (r.state === 'pruefen' && r.relock === 'failed');
  }

  // Resolves 'confirmed' | 'refused' | 'threw' | 'noop'. Never in `aufnahme`:
  // hand_guide(false) is the universal manual abort and would stop the
  // server's sampler under a still-„Aufnahme" overlay — only stop ends a take.
  function lock() {
    if (!lockAllowed()) return Promise.resolve('noop');
    return enqueue(async () => {
      if (!lockAllowed()) return 'noop';
      let res;
      try {
        res = await call('handGuide', false);
      } catch (_) {
        r.relock = 'failed';
        emit();
        notify('onError', DE.TEACH_OFFLINE);
        return 'threw';
      }
      if (isConfirmed(res)) {
        r.sessionOpen = false;
        r.relock = 'ok';
        if (r.state === 'frei') setState('fest');
        emit();
        return 'confirmed';
      }
      r.relock = 'failed';
      emit();
      notify('onError', (res && res.message) || DE.TEACH_RELOCK_FAILED);
      return 'refused';
    }).then((v) => v || 'noop');
  }

  // ---- finish / abschluss / offline close -------------------------------

  function finishedPayload(offline) {
    return { releasedOnce: r.releasedOnce, relockOk: offline ? false : r.relock !== 'failed', offline };
  }

  function emitFinished(offline) {
    if (r.finished) return;
    r.finished = true;
    notify('onFinished', finishedPayload(offline));
  }

  // `extraItems`: a take kept in this same call is not in the overlay's
  // roundItemCount prop yet.
  function completeFinish(extraItems = 0) {
    const count = Number(getProps().roundItemCount) || 0;
    if (count + extraItems === 0) {
      emitFinished(false);
      return;
    }
    setState('abschluss');
    emit();
  }

  function offlineClose() {
    teardown();
    // TEACH_CLOSE_OFFLINE tells the student to hold a LIMP arm — never true in
    // leader mode, where the follower stays torqued, nor in a pending session,
    // which never released anything.
    notify('onError', isLeader() || isPending() ? DE.TEACH_OFFLINE : DE.TEACH_CLOSE_OFFLINE);
    emitFinished(true);
  }

  // Esc and the „Fertig" button: what Esc does in the current state.
  function finish(extraItems = 0) {
    if (r.finished) return;
    if (getProps().heartbeatOk === false) {
      offlineClose();
      return;
    }
    switch (r.state) {
      case 'fest':
      case 'bereit':
        completeFinish(extraItems);
        break;
      case 'frei':
        lock().then((result) => {
          if (result === 'confirmed') completeFinish(extraItems);
          else if (result === 'threw') offlineClose();
        });
        break;
      case 'countdown':
        cancelCountdown();
        break;
      case 'aufnahme':
        r.closeAfterReview = true;
        stop();
        break;
      case 'pruefen':
        keep(true);
        break;
      case 'vorschau':
        stopPreview();
        break;
      case 'abschluss':
        emitFinished(false);
        break;
      default:
        break;
    }
  }

  function continueTeaching() {
    if (r.state !== 'abschluss') return;
    setState(initialStateFor(getProps().mode));
    emit();
  }

  // ---- review -----------------------------------------------------------

  function keep(thenFinish = false) {
    if (r.state !== 'pruefen' || !r.take) return;
    // Leader mode: a trip within the grace window must still be able to
    // discard this take (§5.7 row 12c).
    if (keepHeld()) return;
    const take = r.take;
    const close = thenFinish || r.closeAfterReview;
    r.take = null;
    r.closeAfterReview = false;
    setState(afterState());
    emit();
    notify('onKeep', take);
    if (close) finish(1);
  }

  function discard() {
    if (r.state !== 'pruefen') return;
    r.take = null;
    r.closeAfterReview = false;
    setState(afterState());
    emit();
  }

  // R: discard first (so a cancelled countdown or a failed start returns to
  // fest/frei, never to a take-less `pruefen`), then record again. While the
  // re-lock failed the arm is already free: record at once, no countdown.
  function again() {
    if (r.state !== 'pruefen') return;
    discard();
    if (isLeader()) startLeader();
    else if (r.state === 'frei') runStart('record', 'frei', 'frei');
    else startCountdown('record');
  }

  // ---- captures ---------------------------------------------------------

  function captureAllowed(kind) {
    // Leader mode: the follower is torqued throughout, so a Ziel during a take
    // is as safe as a Position (a light touch; a hard press trips the e-stop).
    if (isLeader()) return r.state === 'bereit' || r.state === 'aufnahme';
    if (kind === 'pose') return r.state === 'fest' || r.state === 'frei' || r.state === 'aufnahme';
    return r.state === 'fest' || r.state === 'frei';
  }

  function capture(kind) {
    if (!captureAllowed(kind)) return;
    // The name is chosen NOW, on the key press — never by a naming UI while
    // the arm is limp in the student's hand.
    let name = '';
    try {
      name = typeof r.namer === 'function' ? String(r.namer(kind) || '') : '';
    } catch (_) {
      name = '';
    }
    enqueue(async () => {
      if (!captureAllowed(kind)) return;
      let res;
      try {
        res = await call('capturePose', name);
      } catch (_) {
        notify('onError', DE.TEACH_OFFLINE);
        return;
      }
      if (!res || !res.success) {
        notify('onError', (res && res.message) || DE.TEACH_CAPTURE_FAILED);
        return;
      }
      sound('capture');
      notify('onCapture', { kind, name, response: res });
    });
  }

  // ---- keepalive: queued behind anything in flight, never skipped --------

  function keepaliveTick() {
    const p = getProps();
    if (!p.enabled || r.tornDown || r.state !== 'frei' || r.relock === 'failed'
        || p.heartbeatOk === false || r.keepaliveStopped || r.keepaliveQueued) return;
    r.keepaliveQueued = true;
    // A keepalive IS a hand_guide(true), so it counts as a pending start: the
    // server claims `_manual_persistent` BEFORE it waits for `_manual_lock`,
    // and a teardown hand_guide(false) completing inside that window is undone
    // by the keepalive (arm limp, on_manual False, idle watchdog inert).
    // Teardown therefore chains behind it exactly like behind a record start.
    r.pendingStart += 1;
    const done = () => { r.pendingStart -= 1; };
    enqueue(async () => {
      try {
        r.keepaliveQueued = false;
        if (r.state !== 'frei' || r.relock === 'failed' || r.keepaliveStopped) return;
        let res;
        try {
          res = await call('handGuide', true);
        } catch (_) {
          return; // the heartbeat owns „offline"; the next tick retries
        }
        if (res && res.success === false) {
          // e.g. another tab turned the leader on: the server stamps activity
          // BEFORE its leader check, so a refused keepalive still re-locks here.
          r.keepaliveStopped = true;
          if (res.message) notify('onError', res.message);
          lock();
        }
      } finally {
        done();
      }
    }, { onSkip: () => { r.keepaliveQueued = false; done(); } });
  }

  // ---- leader turned on while teaching by hand --------------------------

  function onLeaderLive(live) {
    // Tracked only while enabled, so a leader already on at enable still
    // counts as „turned on while open".
    // Leader mode expects a live leader; its loss is `leaderGone`. A pending
    // session is not hand mode yet: a leader answering „on" RESOLVES it to
    // leader mode (TeachHost), it is no lock-out.
    if (!getProps().enabled || isLeader() || isPending()) return;
    const was = r.prevLeaderLive;
    r.prevLeaderLive = live;
    if (!live || was || r.leaderLockout || r.tornDown || r.finished) return;
    r.leaderLockout = true;
    notify('onError', DE.TEACH_LEADER_TURNED_ON);
    if (r.state === 'countdown') cancelCountdown();
    if (r.state === 'aufnahme') stop();
    else lock();
  }

  // ---- robot preview (`vorschau`) ---------------------------------------

  // `cleanedRows`: the overlay's cleaned-up take (utils/recordingCleanup), the
  // rows a keep would store; absent or too short → the take as recorded.
  function previewOnRobot(cleanedRows) {
    const p = getProps();
    if (r.state !== 'pruefen' || !r.take || r.relock === 'failed' || p.heartbeatOk === false
        || r.queueCount > 0) return;
    // The SAME rows go into points_json and into the drive estimate.
    const source = Array.isArray(cleanedRows) && cleanedRows.length >= TEACH_MIN_POINTS
      ? cleanedRows : r.take.points;
    const rows = compactTrajectoryPoints(source);
    const pointsJson = JSON.stringify({ fps: r.take.fps, points: rows });
    enqueue(async () => {
      if (r.state !== 'pruefen' || r.relock === 'failed') return;
      let res;
      try {
        res = await call('replayMotion', { points_json: pointsJson, speed: 1.0 });
      } catch (_) {
        // No answer: the drive may or may not have started. Never re-arm the
        // teaching keys over an arm that might be moving — enter `vorschau`,
        // where stillness or a confirmed „Stopp" is the only way out.
        notify('onError', DE.TEACH_OFFLINE);
        enterPreview(rows);
        return;
      }
      if (!res || !res.success) {
        notify('onError', (res && res.message) || DE.TEACH_PREVIEW_FAILED);
        return;
      }
      enterPreview(rows);
    });
  }

  function enterPreview(rows) {
    if (r.tornDown) {
      // The drive started after the overlay went away: nobody can press
      // „Stopp" any more, so abort it here.
      call('handGuide', false).catch(() => {});
      return;
    }
    const t = now();
    r.preview = {
      answeredAt: t, estimateMs: replayDriveEstimateMs(rows, 1.0), motionAt: null,
      stopConfirmedAt: null, newest: null, judged: null, stillCount: 0, unsubscribe: null,
    };
    r.previewNoMotionHint = false;
    setState('vorschau');
    const subscribe = getProps().subscribeFollowerJoints;
    if (typeof subscribe === 'function') {
      const pv = r.preview;
      try {
        const unsub = subscribe((positions) => {
          if (r.preview !== pv || !Array.isArray(positions)) return;
          pv.newest = { positions: positions.map(Number), at: now() };
        });
        pv.unsubscribe = typeof unsub === 'function' ? unsub : null;
      } catch (e) {
        console.warn('useTeachSession subscribeFollowerJoints threw', e);
      }
    }
    clearTimer('previewTick');
    clearTimer('previewHint');
    r.timers.previewTick = setInterval(previewTick, TEACH_ROBOT_PREVIEW_SETTLE_STEP_MS);
    r.timers.previewHint = setTimeout(() => {
      if (r.state === 'vorschau' && r.preview && r.preview.motionAt === null) {
        r.previewNoMotionHint = true;
        emit();
      }
    }, TEACH_ROBOT_PREVIEW_NO_MOTION_HINT_MS);
    emit();
  }

  // Judges only a sample that ARRIVED since the last judgement: a feed that
  // stopped sending is unknown, never „still".
  function previewTick() {
    const pv = r.preview;
    if (r.state !== 'vorschau' || !pv) return;
    const t = now();
    const newest = pv.newest;
    if (!newest || t - newest.at >= TEACH_ROBOT_PREVIEW_FEED_STALE_MS) {
      pv.stillCount = 0;
    } else if (!pv.judged || newest !== pv.judged) {
      if (pv.judged) {
        const a = pv.judged.positions;
        const b = newest.positions;
        let moving = a.length !== b.length;
        for (let i = 0; !moving && i < b.length; i += 1) {
          const d = Math.abs(b[i] - a[i]);
          if (!Number.isFinite(d) || d > TEACH_ROBOT_PREVIEW_SETTLE_DELTA_RAD) moving = true;
        }
        if (moving) {
          if (pv.motionAt === null) pv.motionAt = t;
          pv.stillCount = 0;
          if (r.previewNoMotionHint) {
            r.previewNoMotionHint = false;
            emit();
          }
        } else {
          pv.stillCount += 1;
        }
      }
      pv.judged = newest;
    }
    const still = pv.stillCount >= TEACH_ROBOT_PREVIEW_SETTLE_SAMPLES;
    // (a) the drive had time to finish since it was first seen moving;
    // the estimate gate keeps a still PAUSE inside the take from ending it.
    const driveDone = pv.motionAt !== null && t - pv.motionAt >= pv.estimateMs && still;
    // (c) a confirmed „Stopp": the chunk already published plays out.
    const stopped = pv.stopConfirmedAt !== null
      && (still || t - pv.stopConfirmedAt >= TEACH_ROBOT_PREVIEW_STOP_TAIL_MAX_MS);
    // Deliberately NO „never moved → exit on time": a pending drive can still
    // start after any finite wait (see TEACH_ROBOT_PREVIEW_NO_MOTION_HINT_MS).
    if (driveDone || stopped) exitPreview();
  }

  function closePreviewFeed() {
    const pv = r.preview;
    clearTimer('previewTick');
    clearTimer('previewHint');
    r.preview = null;
    r.previewNoMotionHint = false;
    if (pv && pv.unsubscribe) {
      try { pv.unsubscribe(); } catch (_) { /* ignore */ }
    }
  }

  function exitPreview() {
    closePreviewFeed();
    setState('pruefen');
    emit();
  }

  // „Stopp": deliberately the universal manual abort. The server bumps its
  // exit generation before waiting for the replay's lock, so a drive that
  // has not started aborts without moving.
  function stopPreview() {
    if (r.state !== 'vorschau') return;
    enqueue(async () => {
      if (r.state !== 'vorschau' || !r.preview) return;
      const pv = r.preview;
      let res;
      try {
        res = await call('handGuide', false);
      } catch (_) {
        notify('onError', DE.TEACH_OFFLINE);
        return;
      }
      if (isConfirmed(res)) {
        if (r.preview === pv) {
          pv.stopConfirmedAt = now();
          pv.stillCount = 0;
        }
        return;
      }
      notify('onError', (res && res.message) || DE.TEACH_OFFLINE);
    });
  }

  // ---- teardown (unmount, pagehide, offline close) ----------------------

  // Fire-and-forget. A start still queued or in flight — a keepalive
  // hand_guide(true) included — is WAITED for (chained onto the queue tail):
  // an immediate cancel racing a start is exactly the
  // ordering that left the arm limp with on_manual False. A page killed before
  // the chained call is sent falls back to the server's own limits (the 30 s
  // idle watchdog, RECORD_MAX_S).
  function teardown() {
    if (r.tornDown) return;
    r.tornDown = true;
    const wasPreview = r.state === 'vorschau';
    clearTimer('countdown');
    clearTimer('elapsed');
    clearTimer('keepalive');
    clearTimer('grace');
    closePreviewFeed();
    const act = () => {
      let pending = null;
      if (r.recordActive) {
        pending = call('recordControl', isLeader() ? 'cancel_leader' : 'cancel');
      } else if (r.sessionOpen || wasPreview) {
        // In `vorschau` this is the „Stopp" of a drive nobody watches any more.
        pending = call('handGuide', false);
      }
      if (pending) pending.catch(() => {});
    };
    if (r.pendingStart > 0) r.queueTail.then(act, act);
    else act();
  }

  // ---- keyboard ---------------------------------------------------------

  const TABLE = {
    fest: {
      space: () => startCountdown('record'), f: () => startCountdown('free'),
      p: () => capture('pose'), z: () => capture('ziel'), escape: () => finish(),
    },
    countdown: { space: cancelCountdown, f: cancelCountdown, escape: () => finish() },
    frei: {
      space: () => runStart('record', 'frei', 'frei'), f: () => lock(),
      p: () => capture('pose'), z: () => capture('ziel'), escape: () => finish(),
    },
    aufnahme: {
      space: stop, f: stop, p: () => capture('pose'),
      z: () => notify('onError', DE.TEACH_ZIEL_BLOCKED_REC), escape: () => finish(),
    },
    pruefen: {
      f: () => { if (r.relock === 'failed') lock(); },
      enter: () => keep(false), r: again, delete: discard, escape: () => finish(),
    },
    vorschau: { escape: () => finish() },
    abschluss: { escape: () => finish() },
  };

  // Leader mode (D8). No F (nothing to free or lock), no preview.
  const LEADER_TABLE = {
    bereit: {
      space: startLeader, p: () => capture('pose'), z: () => capture('ziel'), escape: () => finish(),
    },
    aufnahme: {
      space: stop, p: () => capture('pose'), z: () => capture('ziel'), escape: () => finish(),
    },
    pruefen: {
      enter: () => keep(false), r: again, delete: discard, escape: () => finish(),
    },
    abschluss: { escape: () => finish() },
  };

  function handlerFor(key) {
    const table = isLeader() ? LEADER_TABLE : TABLE;
    return (table[r.state] || {})[key];
  }

  // Leader mode: every key but Esc waits while the bridge POSITIVELY reports
  // the leader gone, or while the rig is not activated (LeaderActivationGate).
  function leaderBlocked(p) {
    return isLeader() && (p.leaderGone === true || p.activationBlocked === true);
  }

  // R7: no NEW teaching action while the leader status is unknown (see the
  // module header for what counts as new and why the exits stay open).
  function newActionsBlocked(p) {
    return p.leaderStatusUnknown === true || isPending();
  }

  // Does `key` in the current state START something (rather than stop, cancel,
  // re-lock, keep, discard or close)?
  function startsNewAction(key) {
    if (key === 'p' || key === 'z') return true;
    if (key === 'r') return r.state === 'pruefen';
    if (isLeader()) return key === 'space' && r.state === 'bereit';
    if (key === 'space') return r.state === 'fest' || r.state === 'frei';
    return key === 'f' && r.state === 'fest';
  }

  function onKeyDown(e) {
    const p = getProps();
    if (!p.enabled || r.tornDown || r.finished) return;
    const key = classifyTeachKey(e);
    if (!key) return;
    // The CollisionModal outside the overlay owns the keyboard.
    if (p.collisionActive) return;
    const target = e.target;
    if (isInteractiveTarget(target) && !(key === 'escape' && isButtonTarget(target))) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.repeat) return;
    if (key === 'space') {
      const t = now();
      if (t - r.lastSpaceAt < TEACH_SPACE_DEBOUNCE_MS) return;
      r.lastSpaceAt = t;
    }
    if (key !== 'escape' && (p.heartbeatOk === false || r.leaderLockout || leaderBlocked(p))) return;
    if (newActionsBlocked(p) && startsNewAction(key)) return;
    const handler = handlerFor(key);
    if (handler) handler();
  }

  // Buttons follow the same offline / leader lock-out as the keys; only the
  // exits (finish, Stopp, continue) stay available.
  function canAct() {
    const p = getProps();
    return !!p.enabled && !r.tornDown && !r.finished && p.heartbeatOk !== false && !r.leaderLockout
      && !leaderBlocked(p);
  }

  // A button does exactly what its key does in the current state.
  function press(key) {
    if (!canAct() || (newActionsBlocked(getProps()) && startsNewAction(key))) return;
    const handler = handlerFor(key);
    if (handler) handler();
  }

  return {
    onKeyDown,
    teardown,
    onLeaderLive,
    onCollisionChange,
    setCaptureNamer: (fn) => { r.namer = typeof fn === 'function' ? fn : null; },
    actions: {
      space: () => press('space'),
      toggleFree: () => press('f'),
      // Only frei / pruefen-with-failed-relock; a no-op (no call) elsewhere.
      lock: () => { if (canAct()) lock(); },
      capturePose: () => press('p'),
      captureZiel: () => press('z'),
      keep: () => press('enter'),
      again: () => press('r'),
      discard: () => press('delete'),
      // Hand mode only: leader mode never replays on the real arm.
      previewOnRobot: (cleanedRows) => {
        if (canAct() && !isLeader() && !newActionsBlocked(getProps())) previewOnRobot(cleanedRows);
      },
      // Leader mode: cancel the take that refused our start.
      discardStaleLeaderTake: () => { if (canAct()) discardStaleLeaderTake(); },
      stopPreview: () => { if (!r.tornDown) stopPreview(); },
      // „Fertig": what Esc does in the current state.
      finish: () => { if (!r.tornDown) finish(); },
      continueTeaching: () => { if (!r.tornDown) continueTeaching(); },
    },
  };
}

/**
 * Vormachen session (hand and leader mode). See createTeachEngine for the rules.
 *
 * Leader mode (`mode: 'leader'`) inputs: collisionActive (tasks.collision),
 * leaderGone (the bridge POSITIVELY reports follower-only — a failed probe is
 * NOT leader-gone), activationBlocked (LeaderActivationGate's panel is up).
 * Both modes: leaderStatusUnknown (R7, teachGates.js::teachLeaderStatus is not
 * 'known') blocks new teaching actions only. `mode: 'pending'` is a session
 * whose mode is not resolved yet: it behaves as blocked throughout.
 *
 * subscribeFollowerJoints(cb) → unsubscribe: cb(positions: number[]) per
 * /joint_states message; opened ONLY while `vorschau`. Absent → the hook never
 * sees stillness (only a confirmed „Stopp" exits the preview).
 * services: { handGuide(enabled), recordControl(action), capturePose(name),
 * replayMotion({ points_json, speed }) } (useRosServiceCaller).
 */
export default function useTeachSession(props) {
  const propsRef = useRef(props);
  propsRef.current = props;
  const [snapshot, setSnapshot] = useState(
    () => ({ ...INITIAL_SNAPSHOT, state: initialStateFor(props && props.mode) }),
  );
  const engineRef = useRef(null);
  if (engineRef.current === null) {
    engineRef.current = createTeachEngine(() => propsRef.current, setSnapshot);
  }
  const engine = engineRef.current;

  const leaderLive = !!(props && props.leaderLive);
  const enabled = !!(props && props.enabled);
  useEffect(() => {
    engine.onLeaderLive(leaderLive);
  }, [engine, leaderLive, enabled]);

  const collisionActive = !!(props && props.collisionActive);
  useEffect(() => {
    engine.onCollisionChange(collisionActive);
  }, [engine, collisionActive, enabled]);

  useEffect(() => {
    const onPageHide = () => engine.teardown();
    window.addEventListener('pagehide', onPageHide);
    return () => {
      window.removeEventListener('pagehide', onPageHide);
      engine.teardown();
    };
  }, [engine]);

  return {
    ...snapshot,
    actions: engine.actions,
    onKeyDown: engine.onKeyDown,
    setCaptureNamer: engine.setCaptureNamer,
  };
}
