// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Read access to the `hfToken` slice. Every selector TOLERATES A MISSING SLICE
// (every page test builds its own store without one, like `s.jetson?.…`
// elsewhere) and answers with a module-level frozen constant, so a selector
// never allocates and a `useSelector` on a slice-less store never re-renders.
//
// The values are derived in ONE place (decideSync / hfTokenStartBlock), so the
// card, the sync hook, the Aufnahme start block and the Benutzer-ID gate cannot
// disagree about what the robot holds.

import { ACCOUNT_UNKNOWN, ROBOT_UNKNOWN, SYNC_INITIAL } from './hfTokenSlice';
import { FAILED_VISIBLE_AFTER, decideSync, hfTokenStartBlock } from './syncDecision';

export const selectHfAccount = (s) => s.hfToken?.account ?? ACCOUNT_UNKNOWN;
export const selectHfRobot = (s) => s.hfToken?.robot ?? ROBOT_UNKNOWN;
export const selectHfSync = (s) => s.hfToken?.sync ?? SYNC_INITIAL;
export const selectHfEpoch = (s) => s.hfToken?.epoch ?? 0;
export const selectHfKick = (s) => s.hfToken?.kick ?? 0;

// The classroom Jetson keeps its own read-only token and is never sent a
// personal one, so every rule below steps aside while a student holds it.
const jetsonClaimed = (s) => s.jetson?.status === 'connected';

/** The robot holds exactly the token this account stores. */
export const selectHfInSync = (s) => {
  const a = selectHfAccount(s);
  const r = selectHfRobot(s);
  return a.status === 'stored' && r.present === true && r.fp !== null && r.fp === a.fp;
};

/**
 * May the Benutzer-ID list be loaded from the robot right now?
 *
 * The list is the account plus organisations the ROBOT'S token can push to
 * (a `whoami` with whatever token sits in its slot). Loading it while the slot
 * holds the PREVIOUS student's token would hand the next student that
 * student's namespace — and the upload namespace guard would agree, because
 * both sides name the same account. So the list is loaded only when the slot
 * is provably this student's, or provably not a personal slot at all.
 *
 * Fail-safe in the one direction that matters: an UNKNOWN robot state blocks,
 * except an old image that PROVED itself silent (`legacy`, set by
 * useHfTokenSync after 8 s with no state message). A store without the slice
 * (page tests, builds without the feature) and a claimed Jetson are allowed.
 * This is the single choke point: hooks/useHfUserList::reload asks it, which
 * covers all four callers (audit S1).
 */
export const selectHfListReloadAllowed = (s) => {
  if (!s.hfToken) return true;
  if (jetsonClaimed(s)) return true;
  const r = selectHfRobot(s);
  if (r.known === true) return r.accepts !== true || selectHfInSync(s);
  return r.legacy === true;
};

/** What the reconcile should do about the robot's slot: noop|push|clear|wait|taken_over. */
export const selectHfDecision = (s) => {
  const a = selectHfAccount(s);
  const y = selectHfSync(s);
  return decideSync({
    enabled: !jetsonClaimed(s),
    robot: selectHfRobot(s),
    accountStatus: a.status,
    accountFp: a.fp,
    lastOwnFp: y.lastOwnFp,
    clearedFp: y.clearedFp,
    awaitingOwnFp: y.awaitingOwnFp ?? null,
  });
};

/** Two failures in a row, or the circuit breaker open: say „fehlgeschlagen". */
export const selectHfSyncFailed = (s) => {
  const y = selectHfSync(s);
  return y.failures >= FAILED_VISIBLE_AFTER || y.breakerOpen === true;
};

/**
 * Why the Aufnahme page must refuse Start for the token's sake:
 * null | 'none' | 'unusable' | 'transfer' | 'busy' | 'failed' | 'taken_over'.
 * A primitive, so a `useSelector` on it only re-renders when it changes.
 * Unknown (no slice, robot state not seen, Jetson claimed) is null: it never blocks.
 */
export const selectHfStartBlock = (s) => {
  if (!s.hfToken || jetsonClaimed(s)) return null;
  const a = selectHfAccount(s);
  const y = selectHfSync(s);
  return hfTokenStartBlock({
    accountStatus: a.status,
    accountFp: a.fp,
    robot: selectHfRobot(s),
    syncPhase: selectHfSyncFailed(s) ? 'failed' : 'idle',
    lastOwnFp: y.lastOwnFp,
    clearedFp: y.clearedFp,
    awaitingOwnFp: y.awaitingOwnFp ?? null,
  });
};

/**
 * True while the browser waits to SEE its own successful push in the robot's
 * state (review c). The reconcile holds still meanwhile; a primitive.
 */
export const selectHfAwaitingOwnPush = (s) => Boolean(selectHfSync(s).awaitingOwnFp);

// The account states that answer nothing a decision could use (S2): the start
// block treats them as „unknown never blocks", so the page explains instead.
const UNDECIDABLE_ACCOUNT = new Set(['error', 'unavailable', 'unsupported']);

/**
 * The student signed in with „Ohne Anmeldung fortfahren": the auth service
 * resolved (not loading) with no session. Only reachable behind the student
 * login gate's offline escape (utils/authGate), where the account half of the
 * token feature is off (useHfTokenSync's accountEnabled needs a JWT).
 */
export const selectHfOfflineEscape = (s) => Boolean(s.auth)
  && s.auth.isLoading === false && s.auth.isAuthenticated !== true;

/**
 * A NON-blocking hint for the Aufnahme page (owner decision S2, 2026-10-04):
 * null | 'offline' | 'error' | 'unavailable' | 'unsupported'.
 *
 * Start is never refused for these („unknown never blocks"), but without a
 * usable account state the Benutzer-ID list stays empty (selectHfListReloadAllowed)
 * and the page used to say only „Keine Benutzer-ID gefunden". The hint names
 * the cause. 'offline' is answered without the robot's state: in that mode the
 * robot half is off, so the state never arrives, and the list is held back
 * either way. The other three need a robot that is known to take a personal
 * token. A primitive; null without the slice and under a claimed Jetson.
 *
 * Only while that list IS empty (review g, 2026-10-04): every sentence says
 * „deshalb gibt es keine Benutzer-ID", and a list loaded earlier stays on screen
 * (state.ui.hfUserList, cleared on sign-out) and is still a usable Benutzer-ID —
 * Start is allowed either way, so there is nothing to explain then.
 */
export const selectHfRecordHint = (s) => {
  if (!s.hfToken || jetsonClaimed(s)) return null;
  const shown = s.ui?.hfUserList;
  if (Array.isArray(shown) && shown.length > 0) return null;
  if (selectHfOfflineEscape(s)) return 'offline';
  const r = selectHfRobot(s);
  if (r.known !== true || r.accepts !== true) return null;
  const { status } = selectHfAccount(s);
  return UNDECIDABLE_ACCOUNT.has(status) ? status : null;
};
