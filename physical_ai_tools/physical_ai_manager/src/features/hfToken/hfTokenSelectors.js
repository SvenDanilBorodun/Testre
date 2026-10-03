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
  });
};

/** Two failures in a row, or the circuit breaker open: say „fehlgeschlagen". */
export const selectHfSyncFailed = (s) => {
  const y = selectHfSync(s);
  return y.failures >= FAILED_VISIBLE_AFTER || y.breakerOpen === true;
};

/**
 * Why the Aufnahme page must refuse Start for the token's sake:
 * null | 'none' | 'unusable' | 'transfer' | 'failed' | 'taken_over'.
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
  });
};
