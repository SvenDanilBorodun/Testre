// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Reconcile, not push-once: what must the browser do about the robot's token
// slot, given what the ROBOT says it holds and what the ACCOUNT says it should
// hold? Pure — every input is a value, no clock, no store, no network — so the
// whole table is a unit test (syncDecision.test.js, 28 rows).
//
// The robot publishes a fingerprint (never the token) of the file in its slot;
// the cloud stores the same fingerprint beside the encrypted row. Comparing the
// two covers, with one rule, every way the two can part: first login, container
// restart, node respawn, a token replaced on another PC, a token deleted, and a
// hand-over to the next student at a shared PC.
//
// THE DAMPENER (`taken_over`). One Raspberry/Orange Pi serves several students
// at once but has ONE slot. Two clients that each "fix" the slot for their own
// account would overwrite each other for ever (80 writes in simulation). So a
// client that has SEEN its own token in the slot (`lastOwnFp`) and now finds
// another one does NOT push again: someone else took the slot, and re-taking it
// is the student's explicit choice („Erneut übertragen"). The same holds for a
// clear (`clearedFp`): a token I removed that comes back is contested, not mine
// to remove a second time.
//
// NOT FROM A STATE OLDER THAN MY PUSH (`awaitingOwnFp`). `lastOwnFp` is set
// the moment a push succeeds — the simulation below diverges without that —
// but the robot's state still shows the OLD slot for half a second or more
// (published right after the write, throttled on its way through rosbridge).
// Read naively, that stale state is „someone took the slot" and the card and
// the Aufnahme page flashed „taken over" after every push (review c,
// 2026-10-04). So while the push still waits to be SEEN (`awaitingOwnFp` equals
// the account's fingerprint; it ends when a state shows it or after
// WRITE_SETTLE_MS), a foreign slot proves nothing and the decision is
// WAIT/PUSH; the reconcile holds still until the wait ends.

export const DECISION = Object.freeze({
  NOOP: 'noop', PUSH: 'push', CLEAR: 'clear', WAIT: 'wait', TAKEN_OVER: 'taken_over',
});

const isPresent = (robot) => robot.present === true && typeof robot.fp === 'string' && robot.fp !== '';

/**
 * @param {object}  input
 * @param {boolean} input.enabled        the robot half is allowed to act at all
 * @param {object}  input.robot          `{known, accepts, present, fp, busy}` (hfToken.robot)
 * @param {string}  input.accountStatus  hfToken.account.status
 * @param {?string} input.accountFp      the account's fingerprint
 * @param {?string} input.lastOwnFp      the fingerprint of MY token the last time I saw it in the slot,
 *                                       or the one I just pushed (see awaitingOwnFp)
 * @param {?string} input.clearedFp      the fingerprint I removed from the slot last
 * @param {?string} input.awaitingOwnFp  the fingerprint I pushed and have not SEEN in the slot yet
 * @returns {'noop'|'push'|'clear'|'wait'|'taken_over'}
 */
export function decideSync({
  enabled = false,
  robot = null,
  accountStatus = 'unknown',
  accountFp = null,
  lastOwnFp = null,
  clearedFp = null,
  awaitingOwnFp = null,
} = {}) {
  if (!enabled) return DECISION.NOOP;
  if (!robot || robot.known !== true || robot.accepts !== true) return DECISION.NOOP;
  const present = isPresent(robot);
  if (accountStatus === 'stored') {
    if (present && robot.fp === accountFp) return DECISION.NOOP;
    const pushNotSeenYet = Boolean(awaitingOwnFp) && awaitingOwnFp === accountFp;
    if (present && lastOwnFp && lastOwnFp === accountFp && !pushNotSeenYet) return DECISION.TAKEN_OVER;
    return robot.busy === true ? DECISION.WAIT : DECISION.PUSH;
  }
  if (accountStatus === 'none' || accountStatus === 'unusable') {
    if (!present) return DECISION.NOOP;
    if (accountStatus === 'unusable' && accountFp && robot.fp === accountFp) return DECISION.NOOP;
    if (clearedFp && clearedFp === robot.fp) return DECISION.TAKEN_OVER;
    return robot.busy === true ? DECISION.WAIT : DECISION.CLEAR;
  }
  return DECISION.NOOP;
}

export const START_BLOCK = Object.freeze({
  NONE: 'none',
  UNUSABLE: 'unusable',
  TRANSFER: 'transfer',
  BUSY: 'busy',
  FAILED: 'failed',
  TAKEN_OVER: 'taken_over',
});

/**
 * Why the Aufnahme page must refuse Start for the token's sake, or null.
 *
 * Unknown NEVER blocks: an account state that has not answered (unknown,
 * loading, error, unsupported, unavailable) and a robot that has not said
 * whether it takes a personal token (older image, Jetson) both return null. The
 * Benutzer-ID list that decides WHICH namespace a recording uploads under is
 * kept honest at its own choke point (hooks/useHfUserList), not by blocking
 * Start on a guess (owner decision C-1).
 *
 * `busy` (review b, 2026-10-04): the robot is recording or talking to Hugging
 * Face and refuses a token change for now (the reconcile WAITs); it is not
 * „being transferred" and nothing on the Startseite can speed it up.
 *
 * @returns {null|'none'|'unusable'|'transfer'|'busy'|'failed'|'taken_over'}
 */
export function hfTokenStartBlock({
  accountStatus = 'unknown',
  accountFp = null,
  robot = null,
  syncPhase = 'idle',
  lastOwnFp = null,
  clearedFp = null,
  awaitingOwnFp = null,
} = {}) {
  if (!robot || robot.known !== true || robot.accepts !== true) return null;
  const present = isPresent(robot);
  if (accountStatus === 'none') return START_BLOCK.NONE;
  if (accountStatus === 'unusable') {
    return present && accountFp && robot.fp === accountFp ? null : START_BLOCK.UNUSABLE;
  }
  if (accountStatus !== 'stored') return null;
  if (present && robot.fp === accountFp) return null;
  const d = decideSync({
    enabled: true, robot, accountStatus, accountFp, lastOwnFp, clearedFp, awaitingOwnFp,
  });
  if (d === DECISION.TAKEN_OVER) return START_BLOCK.TAKEN_OVER;
  // While busy a failed count is moot: nothing is tried until the robot is idle.
  if (d === DECISION.WAIT) return START_BLOCK.BUSY;
  if (syncPhase === 'failed') return START_BLOCK.FAILED;
  return START_BLOCK.TRANSFER;
}

/** Retry schedule after a failed push, clear or account load: 2 / 5 / 15 / 30 s, then 30 s. */
export const BACKOFF_MS = Object.freeze([2000, 5000, 15000, 30000]);
/** While the robot is busy (recording / a Hugging-Face transfer) look again after this long. */
export const WAIT_RECHECK_MS = 5000;
/**
 * After a SUCCESSFUL push or clear the robot's own state message still shows
 * the old slot for a moment (it is published right after the write, then
 * throttled to 2 per second on its way through rosbridge). Without a pause the
 * reconcile would read that stale state and write a second time. The pause ends
 * early the instant a state message shows the student's token.
 */
export const WRITE_SETTLE_MS = 2500;
/** failures >= this => the card and the start block say „fehlgeschlagen" (no red flash on one busy race). */
export const FAILED_VISIBLE_AFTER = 2;
export const backoffDelayMs = (failures) => BACKOFF_MS[Math.min(Math.max(failures, 1), BACKOFF_MS.length) - 1];

/**
 * Circuit breaker (audit M10). The dampener above bounds the usual ping-pong,
 * but a random schedule over three clients still reached 59 slot writes. So a
 * client that has made BREAKER_MAX_WRITES successful automatic writes within
 * BREAKER_WINDOW_MS stops ALL automatic action and shows „Übertragung
 * fehlgeschlagen" with „Erneut übertragen" until the student presses it.
 * Failed calls do not count: they change nothing on the robot and are already
 * throttled by BACKOFF_MS.
 */
export const BREAKER_MAX_WRITES = 3;
export const BREAKER_WINDOW_MS = 60000;
/** A push/clear that has not finished after this long is declared failed (audit M10). */
export const SYNC_WATCHDOG_MS = 15000;
