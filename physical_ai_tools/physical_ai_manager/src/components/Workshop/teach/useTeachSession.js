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

import { useEffect, useRef, useState } from 'react';
import { DE } from '../blocks/messages_de';
import { compactTrajectoryPoints } from '../../../utils/trajectoryCompact';
import {
  TEACH_COUNTDOWN_S, TEACH_SPACE_DEBOUNCE_MS, TEACH_KEEPALIVE_MS, TEACH_RECORD_MAX_S,
  TEACH_MIN_POINTS, TEACH_ROBOT_PREVIEW_NO_MOTION_HINT_MS, TEACH_ROBOT_PREVIEW_SETTLE_DELTA_RAD,
  TEACH_ROBOT_PREVIEW_SETTLE_STEP_MS, TEACH_ROBOT_PREVIEW_SETTLE_SAMPLES,
  TEACH_ROBOT_PREVIEW_FEED_STALE_MS, TEACH_ROBOT_PREVIEW_STOP_TAIL_MAX_MS, replayDriveEstimateMs,
} from './teachGates';

const RECORD_START_FAILED_DE = 'Aufnahme konnte nicht gestartet werden.';
const PREVIEW_FAILED_DE = 'Vorschau nicht möglich.';
const CAPTURE_FAILED_DE = 'Position konnte nicht gespeichert werden.';
const ELAPSED_STEP_MS = 250;

const INTERACTIVE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON']);
const INTERACTIVE_ROLES = new Set(['slider', 'button', 'checkbox']);

export const TEACH_STATES = Object.freeze([
  'fest', 'frei', 'countdown', 'aufnahme', 'pruefen', 'vorschau', 'abschluss',
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
// answer success:false (a bare answer confirms, as RecordPanel always did).
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
  releasedOnce: false, previewNoMotionHint: false,
});

// The state machine, framework-free so every function can reference every
// other one and the handlers stay stable for document listeners.
export function createTeachEngine(getProps, publish) {
  const r = {
    state: 'fest', relock: 'none', take: null, releasedOnce: false, countdownLeft: 0,
    elapsedS: 0, previewNoMotionHint: false,
    sessionOpen: false, recordActive: false, pendingStart: 0, lastSpaceAt: -Infinity,
    keepaliveQueued: false, keepaliveStopped: false, closeAfterReview: false,
    leaderLockout: false, prevLeaderLive: false, capFired: false,
    preCountdown: 'fest', countdownKind: null, recordStartedAt: 0,
    queueTail: Promise.resolve(), queueCount: 0, tornDown: false, finished: false,
    namer: null, preview: null,
    timers: { countdown: null, elapsed: null, keepalive: null, previewTick: null, previewHint: null },
  };

  const now = () => {
    const fn = getProps().now;
    return typeof fn === 'function' ? fn() : Date.now();
  };

  function emit() {
    publish({
      state: r.state, countdownLeft: r.countdownLeft, elapsedS: r.elapsedS,
      busy: r.queueCount > 0, take: r.take, relock: r.relock, releasedOnce: r.releasedOnce,
      previewNoMotionHint: r.previewNoMotionHint,
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
          notify('onError', (res && res.message) || (kind === 'record' ? RECORD_START_FAILED_DE : DE.TEACH_OFFLINE));
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
    notify('onError', DE.TEACH_CLOSE_OFFLINE);
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
    setState('fest');
    emit();
  }

  // ---- review -----------------------------------------------------------

  function keep(thenFinish = false) {
    if (r.state !== 'pruefen' || !r.take) return;
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
    if (r.state === 'frei') runStart('record', 'frei', 'frei');
    else startCountdown('record');
  }

  // ---- captures ---------------------------------------------------------

  function captureAllowed(kind) {
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
        notify('onError', (res && res.message) || CAPTURE_FAILED_DE);
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
    if (!getProps().enabled) return;
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

  function previewOnRobot() {
    const p = getProps();
    if (r.state !== 'pruefen' || !r.take || r.relock === 'failed' || p.heartbeatOk === false
        || r.queueCount > 0) return;
    // The SAME rows go into points_json and into the drive estimate.
    const rows = compactTrajectoryPoints(r.take.points);
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
        notify('onError', (res && res.message) || PREVIEW_FAILED_DE);
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
    closePreviewFeed();
    const act = () => {
      let pending = null;
      if (r.recordActive) {
        pending = call('recordControl', 'cancel');
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
    if (key !== 'escape' && (p.heartbeatOk === false || r.leaderLockout)) return;
    const handler = (TABLE[r.state] || {})[key];
    if (handler) handler();
  }

  // Buttons follow the same offline / leader lock-out as the keys; only the
  // exits (finish, Stopp, continue) stay available.
  function canAct() {
    const p = getProps();
    return !!p.enabled && !r.tornDown && !r.finished && p.heartbeatOk !== false && !r.leaderLockout;
  }

  // A button does exactly what its key does in the current state.
  function press(key) {
    if (!canAct()) return;
    const handler = (TABLE[r.state] || {})[key];
    if (handler) handler();
  }

  return {
    onKeyDown,
    teardown,
    onLeaderLive,
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
      previewOnRobot: () => { if (canAct()) previewOnRobot(); },
      stopPreview: () => { if (!r.tornDown) stopPreview(); },
      // „Fertig": what Esc does in the current state.
      finish: () => { if (!r.tornDown) finish(); },
      continueTeaching: () => { if (!r.tornDown) continueTeaching(); },
    },
  };
}

/**
 * Vormachen session (hand mode). See createTeachEngine for the rules.
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
  const [snapshot, setSnapshot] = useState(INITIAL_SNAPSHOT);
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
