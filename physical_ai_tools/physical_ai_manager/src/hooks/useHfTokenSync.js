// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Keeps the robot's ONE Hugging-Face token slot equal to the signed-in
// student's account. Mounted once, in StudentApp, beside useMeProfile.
//
//   account half — reads GET /me/hf-token whenever the student, the JWT, a
//                  focus event or a card action says it may have changed, and
//                  retries a failed load with a backoff (a cloud blip must not
//                  strand a student on „unknown": the Benutzer-ID list is held
//                  back while the account state is unanswered, see
//                  features/hfToken/hfTokenSelectors::selectHfListReloadAllowed).
//   robot half   — subscribes to /edubotics/hf_token_state, decides with the
//                  pure features/hfToken/syncDecision::decideSync, and pushes
//                  or clears through the plain thunks in hfTokenThunks.js.
//
// The robot half is OFF in cloud-only mode, while a classroom Jetson is
// claimed (its rosbridge is the Jetson proxy, which keeps its own token), and
// whenever the rosbridge is not the local same-origin one.
//
// This hook holds NO token. It sees fingerprints, flags and counts; the token
// itself exists only inside the push thunk, between the cloud's `reveal` and the
// robot's service call.
//
// SAFETY NETS around the loop (audit M10): a push or clear is single-flight; a
// 15 s watchdog retires one that never came back; after a successful write the
// reconcile pauses until the robot's own state has caught up; and three
// automatic writes within a minute open a circuit breaker that waits for the
// student to press „Erneut übertragen".

import { useCallback, useEffect, useRef, useState } from 'react';
import { useDispatch, useSelector, useStore } from 'react-redux';

import useRefetchOnFocus from './useRefetchOnFocus';
import {
  identityChanged,
  kicked,
  robotLegacyConfirmed,
  robotStateLost,
  robotStateReceived,
  syncSettleElapsed,
  syncWatchdogFired,
} from '../features/hfToken/hfTokenSlice';
import {
  selectHfAccount,
  selectHfAwaitingOwnPush,
  selectHfDecision,
  selectHfEpoch,
  selectHfInSync,
  selectHfKick,
  selectHfRobot,
  selectHfSync,
} from '../features/hfToken/hfTokenSelectors';
import {
  clearHfTokenOnRobot,
  pushHfTokenToRobot,
  refreshHfTokenAccount,
} from '../features/hfToken/hfTokenThunks';
import { STATE_STALE_MS, subscribeTokenState } from '../features/hfToken/robotChannel';
import {
  SYNC_WATCHDOG_MS,
  WAIT_RECHECK_MS,
  backoffDelayMs,
} from '../features/hfToken/syncDecision';
import { setHfUserList } from '../features/ui/uiSlice';
import { isCloudOnlyMode } from '../utils/cloudMode';
import { isLocalRosbridgeUrl } from '../utils/rosConnectionManager';

/**
 * How long a freshly subscribed state topic may stay silent before the robot
 * is declared an OLD image that has no such topic. The robot republishes every
 * second, so 8 s is generous; only after it does an image that predates the
 * feature get its Benutzer-ID list back (audit S1).
 */
export const LEGACY_GRACE_MS = 8000;
const STALE_CHECK_MS = 1000;
// A few ms past the exact instant, so a timer that fires a hair early never
// finds the pause still running and goes back to sleep for a whole new period.
const TIMER_SLACK_MS = 25;

/**
 * @param {object}   [opts]
 * @param {() => void} [opts.onRobotTokenReady] called when the robot first holds
 *        exactly the student's token (the Benutzer-ID list can be loaded now).
 * @returns {{accountEnabled: boolean, robotEnabled: boolean}}
 */
export default function useHfTokenSync({ onRobotTokenReady } = {}) {
  const dispatch = useDispatch();
  const store = useStore();

  const accessToken = useSelector((s) => s.auth?.session?.access_token ?? null);
  const userId = useSelector((s) => s.auth?.session?.user?.id ?? null);
  const profileLoaded = useSelector((s) => Boolean(s.auth?.profileLoaded));
  const role = useSelector((s) => s.auth?.role ?? null);
  const jetsonConnected = useSelector((s) => s.jetson?.status === 'connected');
  const heartbeatStatus = useSelector((s) => s.tasks?.heartbeatStatus);
  const rosbridgeUrl = useSelector((s) => s.ros?.rosbridgeUrl ?? '');

  const accountStatus = useSelector((s) => selectHfAccount(s).status);
  const accountFailures = useSelector((s) => selectHfAccount(s).failures);
  const robotPresent = useSelector((s) => selectHfRobot(s).present);
  const robotKnown = useSelector((s) => selectHfRobot(s).known);
  const inSync = useSelector(selectHfInSync);
  const decision = useSelector(selectHfDecision);
  const syncPhase = useSelector((s) => selectHfSync(s).phase);
  const syncAttempt = useSelector((s) => selectHfSync(s).attempt);
  const syncFailures = useSelector((s) => selectHfSync(s).failures);
  const nextAttemptAt = useSelector((s) => selectHfSync(s).nextAttemptAt);
  const breakerOpen = useSelector((s) => selectHfSync(s).breakerOpen);
  const awaitingOwnPush = useSelector(selectHfAwaitingOwnPush);
  const awaitingUntil = useSelector((s) => selectHfSync(s).awaitingUntil ?? null);
  const kick = useSelector(selectHfKick);
  const epoch = useSelector(selectHfEpoch);

  const cloudOnly = isCloudOnlyMode();
  const accountEnabled = Boolean(accessToken) && profileLoaded && role === 'student' && !cloudOnly;
  // isLocalRosbridgeUrl is evaluated ONLY behind the accountEnabled short-circuit:
  // suites that render the real StudentApp mock the connection manager with a
  // handful of methods, and a named import missing from such a mock throws on
  // access. Until the profile has loaded this hook must be inert.
  const robotEnabled = accountEnabled
    && !jetsonConnected
    && heartbeatStatus === 'connected'
    && isLocalRosbridgeUrl(rosbridgeUrl);

  // The reconcile's OWN wake-ups (the robot was busy, a pause ended). Kept out
  // of Redux's `kick` on purpose: that one is an explicit request — a card
  // action, the retry button, a failed account load — and re-reads the account
  // from the cloud, which a 5 s poll of a busy robot should not.
  const [nudge, setNudge] = useState(0);
  const wakeReconcile = useCallback(() => setNudge((n) => n + 1), []);

  const readyRef = useRef(onRobotTokenReady);
  useEffect(() => {
    readyRef.current = onRobotTokenReady;
  }, [onRobotTokenReady]);

  // ── 0. a different student in an already open tab (audit M11) ─────────────
  // Supabase can swap the session in place (a login in a second tab) without a
  // sign-out. The slice then still carries the previous user's account
  // fingerprint, dampener memory and breaker for as long as it takes /me/hf-token
  // to answer. Reset it, and bump the epoch so a push awaiting its `reveal` for
  // the OLD user drops the token. Declared before the account load below so the
  // reset lands first within the same commit.
  const prevUserRef = useRef(userId);
  useEffect(() => {
    const previous = prevUserRef.current;
    prevUserRef.current = userId;
    if (previous && previous !== userId) {
      dispatch(identityChanged());
      // The Benutzer-ID list describes the PREVIOUS student's token; only a
      // sign-out clears it otherwise, and an in-place session swap is none.
      dispatch(setHfUserList([]));
    }
  }, [userId, dispatch]);

  // ── 1. the account half ───────────────────────────────────────────────────
  const refetchAccount = useCallback(() => { dispatch(refreshHfTokenAccount()); }, [dispatch]);
  useEffect(() => {
    if (!accountEnabled) return;
    refetchAccount();
  }, [accountEnabled, accessToken, kick, refetchAccount]);
  useRefetchOnFocus(accountEnabled ? refetchAccount : null);

  // A failed load with nothing known yet retries on the same 2 / 5 / 15 / 30 s
  // ladder as a failed push. Without it the state would stay „error" until the
  // next focus event, and with the Benutzer-ID list gated on a known account
  // that would strand recording after one cloud blip.
  useEffect(() => {
    if (!accountEnabled || accountStatus !== 'error') return undefined;
    const timer = setTimeout(() => dispatch(kicked()), backoffDelayMs(accountFailures));
    return () => clearTimeout(timer);
  }, [accountEnabled, accountStatus, accountFailures, dispatch]);

  // ── 2. the robot subscription ─────────────────────────────────────────────
  useEffect(() => {
    if (!robotEnabled) return undefined;
    let cancelled = false;
    let unsubscribe = null;
    let legacyTimer = null;

    const stopLegacyTimer = () => {
      if (legacyTimer !== null) {
        clearTimeout(legacyTimer);
        legacyTimer = null;
      }
    };

    subscribeTokenState(rosbridgeUrl, (parsed) => {
      if (cancelled) return;
      stopLegacyTimer();
      dispatch(robotStateReceived(parsed));
    })
      .then((stop) => {
        if (cancelled) {
          stop();
          return;
        }
        unsubscribe = stop;
        // 8 s of silence on a live subscription: an image that predates the
        // topic. Only an image that PROVED silent gets the legacy allowance.
        legacyTimer = setTimeout(() => {
          legacyTimer = null;
          if (!cancelled && !store.getState().hfToken?.robot?.known) dispatch(robotLegacyConfirmed());
        }, LEGACY_GRACE_MS);
      })
      .catch(() => {
        /* the heartbeat's story, not this hook's */
      });

    // A state older than the robot's 1 Hz period times a few is no state at all
    // (the node died, the link is half open): back to UNKNOWN, never to „none".
    const staleCheck = setInterval(() => {
      const robot = store.getState().hfToken?.robot;
      if (robot && robot.known && Number.isFinite(robot.receivedAt)
          && Date.now() - robot.receivedAt > STATE_STALE_MS) {
        dispatch(robotStateLost());
      }
    }, STALE_CHECK_MS);

    return () => {
      cancelled = true;
      stopLegacyTimer();
      clearInterval(staleCheck);
      if (unsubscribe) {
        try { unsubscribe(); } catch { /* swallow */ }
      }
      dispatch(robotStateLost());
    };
  }, [robotEnabled, rosbridgeUrl, dispatch, store]);

  // ── 3. the reconcile ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!robotEnabled) return undefined;
    if (decision === 'wait') {
      // The robot is recording or talking to Hugging Face and refuses a change
      // for now. Look again later; the state topic also re-runs this when it flips.
      const timer = setTimeout(wakeReconcile, WAIT_RECHECK_MS);
      return () => clearTimeout(timer);
    }
    if (decision !== 'push' && decision !== 'clear') return undefined;
    // single-flight, and a breaker that waits for the student
    if (syncPhase === 'pushing' || syncPhase === 'clearing' || breakerOpen) return undefined;
    // A push that no state has shown yet (review c): the foreign slot this
    // decision rests on may be older than the push. Effect 3b ends the wait.
    if (awaitingOwnPush) return undefined;
    const pause = nextAttemptAt === null ? 0 : nextAttemptAt - Date.now();
    if (pause > 0) {
      const timer = setTimeout(wakeReconcile, pause + TIMER_SLACK_MS);
      return () => clearTimeout(timer);
    }
    dispatch(decision === 'push' ? pushHfTokenToRobot() : clearHfTokenOnRobot());
    return undefined;
    // `syncFailures`, `kick`, `nudge` and `epoch` are not read here, but each is
    // a reason to look again: a failure moves `nextAttemptAt`, a kick is an
    // explicit request, a nudge is this effect's own timer, and a new epoch is a
    // new student.
  }, [robotEnabled, decision, syncPhase, breakerOpen, awaitingOwnPush, nextAttemptAt, syncFailures, kick,
    nudge, epoch, wakeReconcile, dispatch]);

  // ── 3b. the end of the wait to SEE a push (review c) ──────────────────────
  // An in-sync state ends it at once (the slice); this ends it when the robot
  // never showed the push within WRITE_SETTLE_MS. Then a foreign slot is a
  // takeover again (the dampener) and an empty one is pushed once more.
  useEffect(() => {
    if (awaitingUntil === null) return undefined;
    const delay = Math.max(0, awaitingUntil - Date.now()) + TIMER_SLACK_MS;
    const timer = setTimeout(() => dispatch(syncSettleElapsed()), delay);
    return () => clearTimeout(timer);
  }, [awaitingUntil, dispatch]);

  // ── 4. the watchdog (audit M10) ───────────────────────────────────────────
  // A push or clear stays „in flight" until the thunk's `finally` says
  // otherwise; this is the belt for a call that never returns at all.
  useEffect(() => {
    if (syncPhase !== 'pushing' && syncPhase !== 'clearing') return undefined;
    const timer = setTimeout(() => dispatch(syncWatchdogFired(syncAttempt)), SYNC_WATCHDOG_MS);
    return () => clearTimeout(timer);
  }, [syncPhase, syncAttempt, dispatch]);

  // ── 5. edges the rest of the app reacts to ────────────────────────────────
  const prevInSyncRef = useRef(false);
  useEffect(() => {
    const now = robotEnabled && inSync;
    if (now && !prevInSyncRef.current && typeof readyRef.current === 'function') readyRef.current();
    prevInSyncRef.current = now;
  }, [robotEnabled, inSync]);

  // The slot went from holding a token to AFFIRMATIVELY holding none (a clear
  // took effect, the container restarted): the Benutzer-ID list described that
  // token's account. An unknown state (a dropped link) is not this edge.
  const prevHeldRef = useRef(false);
  useEffect(() => {
    const held = robotEnabled && robotKnown && robotPresent;
    if (prevHeldRef.current && robotEnabled && robotKnown && !robotPresent) dispatch(setHfUserList([]));
    prevHeldRef.current = held;
  }, [robotEnabled, robotKnown, robotPresent, dispatch]);

  return { accountEnabled, robotEnabled };
}
