// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The per-student Hugging-Face token, as the browser knows it: METADATA ONLY.
//
//   account — what the cloud says about the student's stored token (a
//             fingerprint, the proven account name, a hint like „hf_…abcd").
//   robot   — what the robot says about its one token slot, from
//             /edubotics/hf_token_state (a fingerprint and three booleans).
//   sync    — the bookkeeping of keeping the two equal (features/hfToken/
//             syncDecision.js decides, hooks/useHfTokenSync.js acts).
//
// THE TOKEN ITSELF IS NEVER IN HERE. No reducer, action payload or field of
// this slice can carry it: it travels cloud → thunk local variable → rosbridge
// service call and nowhere else (hfTokenThunks.js, robotChannel.js), and
// hfTokenSecrecy.test.js proves it against the real store. Every payload below
// is a fingerprint, a flag, a count or a German sentence.
//
// The slice answers `session/signedOut` for itself, like every slice that
// holds a student's data, and bumps `epoch` so a push still awaiting its
// `reveal` can tell it belongs to a student who is gone and drops the token.

import { createSlice } from '@reduxjs/toolkit';

import { signedOut } from '../session/sessionActions';
import {
  BREAKER_MAX_WRITES,
  BREAKER_WINDOW_MS,
  WRITE_SETTLE_MS,
  backoffDelayMs,
} from './syncDecision';

// Module-level frozen constants: selectors on a store WITHOUT this slice (every
// page test builds its own) return these, so a selector never allocates and a
// useSelector on a slice-less store never re-renders.
export const ACCOUNT_UNKNOWN = Object.freeze({
  // 'unknown'|'loading'|'none'|'stored'|'unusable'|'unsupported'|'unavailable'|'error'
  status: 'unknown',
  hfUsername: null,
  hint: null,
  fp: null,
  role: null,
  validatedAt: null,
  accountChanged: false,
  error: null, // a German sentence, never a token
  failures: 0, // consecutive failed account loads (drives the auto-retry backoff)
});

export const ROBOT_UNKNOWN = Object.freeze({
  known: false,
  accepts: null,
  present: false,
  fp: null,
  busy: false,
  seq: null,
  receivedAt: null,
  // True only after the 8 s grace in useHfTokenSync proved an OLD image silent:
  // an image that predates the state topic. The one thing that lets the
  // Benutzer-ID list load while the robot's state is unknown (audit S1).
  legacy: false,
});

export const SYNC_INITIAL = Object.freeze({
  phase: 'idle', // 'idle'|'pushing'|'clearing'
  phaseSince: null,
  attempt: 0, // bumped whenever a push/clear starts or is abandoned
  failures: 0,
  nextAttemptAt: null,
  lastOwnFp: null,
  clearedFp: null,
  lastMessage: null, // the robot's own German answer to the last failed call
  autoWrites: Object.freeze([]), // timestamps of recent successful automatic writes
  breakerOpen: false,
});

const IN_FLIGHT = new Set(['pushing', 'clearing']);

const freshAccount = () => ({ ...ACCOUNT_UNKNOWN });
const freshRobot = () => ({ ...ROBOT_UNKNOWN });
const freshSync = () => ({ ...SYNC_INITIAL, autoWrites: [] });

const makeInitial = (epoch = 0) => ({
  account: freshAccount(),
  robot: freshRobot(),
  sync: freshSync(),
  epoch, // bumped by session/signedOut and by an identity change
  kick: 0, // bumped to re-run the reconcile effect
});

const initialState = makeInitial();

const ACCOUNT_STATUSES = new Set(['none', 'stored', 'unusable']);

// A manual action on the card (save, replace, remove) restarts the loop
// bookkeeping: the breaker counts what the machine did on its own.
function resetLoopBookkeeping(sync) {
  sync.failures = 0;
  sync.nextAttemptAt = null;
  sync.lastMessage = null;
  sync.autoWrites = [];
  sync.breakerOpen = false;
}

function recordAutoWrite(sync, at) {
  const kept = sync.autoWrites.filter((t) => t <= at && at - t < BREAKER_WINDOW_MS);
  kept.push(at);
  sync.autoWrites = kept;
  if (kept.length >= BREAKER_MAX_WRITES) sync.breakerOpen = true;
}

function settleIdle(sync) {
  sync.phase = 'idle';
  sync.phaseSince = null;
}

function failSync(sync, message, at) {
  sync.failures += 1;
  sync.lastMessage = typeof message === 'string' && message ? message : null;
  sync.nextAttemptAt = at + backoffDelayMs(sync.failures);
  settleIdle(sync);
}

const hfTokenSlice = createSlice({
  name: 'hfToken',
  initialState,
  reducers: {
    // ── the account (cloud) half ───────────────────────────────────────────
    accountLoading: (state) => {
      // Only from `unknown`: a refresh of a known account must not flash the
      // card back to „wird geladen".
      if (state.account.status === 'unknown') state.account.status = 'loading';
    },
    accountLoaded: (state, action) => {
      const p = action.payload || {};
      const a = state.account;
      if (!ACCOUNT_STATUSES.has(p.status)) return;
      const samePlace = a.fp !== null && a.fp === (p.fp ?? null);
      a.status = p.status;
      a.hfUsername = p.hfUsername ?? null;
      a.hint = p.hint ?? null;
      a.fp = p.fp ?? null;
      a.role = p.role ?? null;
      a.validatedAt = p.validatedAt ?? null;
      if (!samePlace) a.accountChanged = false;
      a.error = null;
      a.failures = 0;
    },
    // payload: a kind string, or {kind: 'unsupported'|'unavailable'|'error', message}
    accountFailed: (state, action) => {
      const raw = action.payload;
      const kind = typeof raw === 'string' ? raw : raw?.kind;
      const message = typeof raw === 'object' && raw ? raw.message : null;
      const a = state.account;
      a.failures += 1;
      a.error = typeof message === 'string' && message ? message : null;
      if (kind === 'unsupported' || kind === 'unavailable') {
        a.status = kind;
      } else if (a.status === 'unknown' || a.status === 'loading') {
        // A failed refresh keeps the last KNOWN account; only a state that never
        // knew anything degrades.
        a.status = 'error';
      }
    },
    // After PUT or POST /verify: {hfUsername, hint, fp, role, validatedAt, accountChanged, usable}
    accountSaved: (state, action) => {
      const p = action.payload || {};
      const a = state.account;
      a.status = p.usable === false ? 'unusable' : 'stored';
      a.hfUsername = p.hfUsername ?? null;
      a.hint = p.hint ?? null;
      a.fp = p.fp ?? null;
      a.role = p.role ?? null;
      a.validatedAt = p.validatedAt ?? null;
      a.accountChanged = p.accountChanged === true;
      a.error = null;
      a.failures = 0;
      state.sync.clearedFp = null;
      resetLoopBookkeeping(state.sync);
    },
    accountRemoved: (state) => {
      state.account = { ...freshAccount(), status: 'none' };
      state.sync.lastOwnFp = null;
      resetLoopBookkeeping(state.sync);
    },

    // ── the robot half ─────────────────────────────────────────────────────
    robotStateReceived: {
      reducer: (state, action) => {
        const p = action.payload;
        const r = state.robot;
        r.known = true;
        r.accepts = p.accepts === true;
        r.present = p.present === true;
        r.fp = typeof p.fp === 'string' && p.fp ? p.fp : null;
        r.busy = p.busy === true;
        r.seq = Number.isFinite(p.seq) ? p.seq : null;
        r.receivedAt = p.receivedAt;
        r.legacy = false;
        const a = state.account;
        if (a.status === 'stored' && r.present && r.fp !== null && r.fp === a.fp) {
          // Observed in sync: remember it is MY token in the slot (the dampener's
          // memory) and forget every failure.
          const s = state.sync;
          s.lastOwnFp = r.fp;
          s.failures = 0;
          s.nextAttemptAt = null;
          s.lastMessage = null;
          if (IN_FLIGHT.has(s.phase)) s.attempt += 1; // the in-flight call is moot now
          settleIdle(s);
        }
      },
      prepare: (parsed, receivedAt = Date.now()) => ({ payload: { ...parsed, receivedAt } }),
    },
    robotStateLost: (state) => {
      state.robot = freshRobot();
    },
    // Only while no state was ever seen: the robot image predates the topic.
    robotLegacyConfirmed: (state) => {
      if (!state.robot.known) state.robot.legacy = true;
    },

    // ── the sync bookkeeping ───────────────────────────────────────────────
    syncPhaseSet: {
      reducer: (state, action) => {
        const { phase, at } = action.payload;
        const s = state.sync;
        s.phase = phase;
        if (IN_FLIGHT.has(phase)) {
          s.attempt += 1;
          s.phaseSince = at;
        } else {
          s.phaseSince = null;
        }
      },
      prepare: (phase, at = Date.now()) => ({ payload: { phase, at } }),
    },
    // payload: {fp, automatic}
    syncPushed: {
      reducer: (state, action) => {
        const { fp, automatic, at } = action.payload;
        const s = state.sync;
        s.failures = 0;
        // Hold off until the robot's own state message has caught up with the
        // write (WRITE_SETTLE_MS); an in-sync message ends the pause early.
        s.nextAttemptAt = at + WRITE_SETTLE_MS;
        s.lastMessage = null;
        s.lastOwnFp = typeof fp === 'string' && fp ? fp : null;
        s.clearedFp = null;
        settleIdle(s);
        if (automatic) recordAutoWrite(s, at);
      },
      prepare: ({ fp = null, automatic = true } = {}, at = Date.now()) => ({
        payload: { fp, automatic, at },
      }),
    },
    // payload: {fp (what was in the slot before), automatic}
    syncCleared: {
      reducer: (state, action) => {
        const { fp, automatic, at } = action.payload;
        const s = state.sync;
        s.failures = 0;
        s.nextAttemptAt = at + WRITE_SETTLE_MS;
        s.lastMessage = null;
        s.clearedFp = typeof fp === 'string' && fp ? fp : null;
        s.lastOwnFp = null;
        settleIdle(s);
        if (automatic) recordAutoWrite(s, at);
      },
      prepare: ({ fp = null, automatic = true } = {}, at = Date.now()) => ({
        payload: { fp, automatic, at },
      }),
    },
    // payload: {message} — the robot's own German sentence, or none
    syncFailed: {
      reducer: (state, action) => {
        failSync(state.sync, action.payload.message, action.payload.at);
      },
      prepare: ({ message = null } = {}, at = Date.now()) => ({ payload: { message, at } }),
    },
    // The 15 s watchdog: declare a push/clear that never came back failed.
    // payload: {attempt} — a no-op unless that very call is still in flight.
    syncWatchdogFired: {
      reducer: (state, action) => {
        const s = state.sync;
        if (s.attempt !== action.payload.attempt || !IN_FLIGHT.has(s.phase)) return;
        s.attempt += 1; // the zombie call finds its attempt gone and drops its result
        failSync(s, null, action.payload.at);
      },
      prepare: (attempt, at = Date.now()) => ({ payload: { attempt, at } }),
    },
    // „Erneut übertragen": forget every memory that makes the browser hold back
    // (dampener, breaker, backoff) and re-run the reconcile once.
    syncForceRetransfer: (state) => {
      const s = state.sync;
      s.lastOwnFp = null;
      s.clearedFp = null;
      resetLoopBookkeeping(s);
      state.kick += 1;
    },
    syncReset: (state) => {
      state.sync = freshSync();
    },
    kicked: (state) => {
      state.kick += 1;
    },
    // Supabase can swap the session to another user in an already open tab
    // (a login in a second tab) without a sign-out. The account and the
    // bookkeeping belong to the previous user; the robot's facts do not.
    identityChanged: (state) => ({
      ...makeInitial(state.epoch + 1),
      robot: { ...state.robot },
    }),
  },
  extraReducers: (builder) => {
    builder.addCase(signedOut, (state) => makeInitial(state.epoch + 1));
  },
});

export const {
  accountLoading,
  accountLoaded,
  accountFailed,
  accountSaved,
  accountRemoved,
  robotStateReceived,
  robotStateLost,
  robotLegacyConfirmed,
  syncPhaseSet,
  syncPushed,
  syncCleared,
  syncFailed,
  syncWatchdogFired,
  syncForceRetransfer,
  syncReset,
  kicked,
  identityChanged,
} = hfTokenSlice.actions;

export default hfTokenSlice.reducer;
