// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The actions of the per-student Hugging-Face token: load / save / remove /
// verify the account's token in the cloud, and push it to (or clear it from) the
// robot's slot.
//
// PLAIN THUNKS — `(dispatch, getState) => …` — NEVER Redux Toolkit's
// `createAsyncThunk`. That helper dispatches a `pending` action whose
// `meta.arg` is the thunk's argument, i.e. the token would ride in an action
// that the devtools, every middleware and any future logger can see. Here the
// token is a function argument or a local variable for the length of ONE await
// and is handed to exactly two things: the cloud service call (PUT) and the
// rosbridge service call (robotChannel). Every action dispatched below carries
// a fingerprint, a flag, a count or a German sentence.
//
// The reconcile that decides WHEN to push or clear is hooks/useHfTokenSync.js
// (with features/hfToken/syncDecision.js). These thunks are the verbs.

import * as api from '../../services/hfTokenApi';
import { setHfUsername } from '../auth/authSlice';
import { setHfUserList } from '../ui/uiSlice';
import { hfCopy } from './hfTokenCopy';
import {
  accountFailed,
  accountLoaded,
  accountLoading,
  accountRemoved,
  accountSaved,
  kicked,
  syncCleared,
  syncFailed,
  syncForceRetransfer,
  syncPhaseSet,
  syncPushed,
} from './hfTokenSlice';
import { setRobotToken } from './robotChannel';

const IN_FLIGHT = new Set(['pushing', 'clearing']);
const FP_SHAPE = /^[0-9a-f]{16}$/;
const MAX_DETAIL_CHARS = 400;
// A server `detail` is shown only when it is German. Our own API's details all
// are; a proxy in front of it (captive portal, gateway timeout) answers in
// English, and `apiClient` falls back to the HTTP status text then.
const GERMAN_HINT = /[äöüÄÖÜß]|\b(?:nicht|bitte|Bitte|konnte|wurde|kein|keine|ist|der|die|das|Die|Der|Das|Dein|dein|Dieses|Nur)\b/;
// Whatever a server (or a proxy in front of it) puts into a sentence, nothing
// shaped like a token ever reaches the state or the screen.
const TOKEN_IN_TEXT = /hf_[A-Za-z0-9_-]{8,}/g;
const scrub = (text) => (typeof text === 'string' ? text.replace(TOKEN_IN_TEXT, 'hf_***') : null);

const accessTokenOf = (state) => state.auth?.session?.access_token ?? null;

/** The German sentence for a failed cloud call: the server's own, else `fallback`. */
export function sentenceFromError(err, fallback = hfCopy('card.errorGeneric')) {
  const detail = err?.detail;
  if (typeof detail === 'string' && detail.length > 0 && detail.length <= MAX_DETAIL_CHARS
      && GERMAN_HINT.test(detail)) {
    return scrub(detail);
  }
  // FastAPI's own 422 can make `detail` an ARRAY of validation records; a
  // non-string is never rendered.
  return fallback;
}

/** `{status, hfUsername, hint, fp, role, validatedAt}` from a GET/PUT/verify body, or null if unusable. */
export function accountFromResponse(body) {
  if (!body || typeof body !== 'object') return null;
  if (body.stored !== true) {
    return { status: 'none', hfUsername: null, hint: null, fp: null, role: null, validatedAt: null };
  }
  // A stored row without a fingerprint is a contract violation: the reconcile
  // compares fingerprints, so there is nothing safe to do with it.
  if (typeof body.fp !== 'string' || !FP_SHAPE.test(body.fp)) return null;
  return {
    // Absent `usable` (a server that predates the key) reads as usable.
    status: body.usable === false ? 'unusable' : 'stored',
    hfUsername: typeof body.hf_username === 'string' ? body.hf_username : null,
    hint: typeof body.hint === 'string' ? body.hint : null,
    fp: body.fp,
    role: typeof body.role === 'string' ? body.role : null,
    validatedAt: typeof body.validated_at === 'string' ? body.validated_at : null,
  };
}

/** The `accountFailed` payload for a failed GET: 404 = no such route, 503 = no key on the server. */
export function failureFromError(err) {
  const status = err?.status;
  if (status === 404) return { kind: 'unsupported', message: null };
  if (status === 503) return { kind: 'unavailable', message: null };
  return { kind: 'error', message: sentenceFromError(err, hfCopy('card.loadError')) };
}

const hfStateOf = (getState) => getState().hfToken;

// Ordering of the cloud calls. A GET that was issued BEFORE a save/remove/verify
// and answers after it would overwrite the fresh result with the old one, and an
// older GET answering after a newer one would do the same. So a GET result is
// applied only if no newer GET started and no mutation started or finished since
// it was issued; a mutation's own result always applies (it is the newest truth
// about the account). The `kick` the mutation bumps re-reads the account anyway.
let getGeneration = 0;
let mutationGeneration = 0;

// A call belongs to the student it started for. `signedOut` and an identity
// change bump the epoch; a call that finds the epoch moved drops what it holds.
const epochMoved = (getState, epoch) => hfStateOf(getState)?.epoch !== epoch;

// ── the account (cloud) side ─────────────────────────────────────────────────

/** GET /me/hf-token → account state. Resolves to true when the account was read. */
export const refreshHfTokenAccount = () => async (dispatch, getState) => {
  const state = getState();
  const accessToken = accessTokenOf(state);
  if (!accessToken || !state.hfToken) return false;
  const epoch = state.hfToken.epoch;
  const myGet = ++getGeneration;
  const myMutations = mutationGeneration;
  const superseded = () => epochMoved(getState, epoch)
    || myGet !== getGeneration || myMutations !== mutationGeneration;
  dispatch(accountLoading());
  try {
    const mapped = accountFromResponse(await api.getHfToken(accessToken));
    if (superseded()) return false;
    if (!mapped) {
      dispatch(accountFailed({ kind: 'error', message: hfCopy('card.loadError') }));
      return false;
    }
    dispatch(accountLoaded(mapped));
    return true;
  } catch (err) {
    if (superseded()) return false;
    dispatch(accountFailed(failureFromError(err)));
    return false;
  }
};

/**
 * PUT /me/hf-token. The server checks the token against Hugging Face, so every
 * refusal (read-only token, rejected, system account) arrives as a German 422.
 * Resolves to `{ok, error, accountChanged}` — never the token.
 */
export const saveHfToken = (token) => async (dispatch, getState) => {
  const state = getState();
  const accessToken = accessTokenOf(state);
  if (!accessToken) return { ok: false, error: hfCopy('card.errorGeneric'), accountChanged: false };
  const epoch = state.hfToken?.epoch;
  const trimmed = typeof token === 'string' ? token.trim() : '';
  mutationGeneration += 1;
  try {
    const body = await api.putHfToken(accessToken, trimmed);
    mutationGeneration += 1;
    if (epochMoved(getState, epoch)) return { ok: false, error: null, accountChanged: false };
    const mapped = accountFromResponse(body);
    if (!mapped || mapped.status === 'none') {
      return { ok: false, error: hfCopy('card.errorGeneric'), accountChanged: false };
    }
    const accountChanged = body.account_changed === true;
    dispatch(accountSaved({ ...mapped, accountChanged, usable: mapped.status === 'stored' }));
    // The proven name replaces the old, unverified auto-link: the cloud set
    // `users.hf_username` from the token's own whoami in the same transaction.
    if (mapped.hfUsername) dispatch(setHfUsername(mapped.hfUsername));
    dispatch(kicked());
    return { ok: true, error: null, accountChanged };
  } catch (err) {
    mutationGeneration += 1;
    return { ok: false, error: sentenceFromError(err), accountChanged: false };
  }
};

/** DELETE /me/hf-token; the reconcile then clears the robot's slot. Resolves to `{ok, error}`. */
export const removeHfToken = () => async (dispatch, getState) => {
  const state = getState();
  const accessToken = accessTokenOf(state);
  if (!accessToken) return { ok: false, error: hfCopy('card.removeError') };
  const epoch = state.hfToken?.epoch;
  mutationGeneration += 1;
  try {
    await api.deleteHfToken(accessToken);
    mutationGeneration += 1;
    if (epochMoved(getState, epoch)) return { ok: false, error: null };
    dispatch(accountRemoved());
    dispatch(kicked());
    return { ok: true, error: null };
  } catch (err) {
    mutationGeneration += 1;
    return { ok: false, error: sentenceFromError(err, hfCopy('card.removeError')) };
  }
};

/** POST /me/hf-token/verify: ask Hugging Face again. Resolves to `{ok, error}`. */
export const verifyHfToken = () => async (dispatch, getState) => {
  const state = getState();
  const accessToken = accessTokenOf(state);
  if (!accessToken) return { ok: false, error: hfCopy('card.errorGeneric') };
  const epoch = state.hfToken?.epoch;
  mutationGeneration += 1;
  try {
    const body = await api.verifyHfToken(accessToken);
    mutationGeneration += 1;
    if (epochMoved(getState, epoch)) return { ok: false, error: null };
    const mapped = accountFromResponse(body);
    if (!mapped || mapped.status === 'none') return { ok: false, error: hfCopy('card.errorGeneric') };
    dispatch(accountSaved({
      ...mapped,
      accountChanged: body.account_changed === true,
      usable: mapped.status === 'stored',
    }));
    dispatch(kicked());
    return { ok: true, error: null };
  } catch (err) {
    mutationGeneration += 1;
    return { ok: false, error: sentenceFromError(err) };
  }
};

// ── the robot side ───────────────────────────────────────────────────────────

// Why an automatic push/clear may not start right now, or null. The reconcile
// hook gates on the same things to schedule its retry; this is the guard that
// holds even when something else calls the thunk.
function automaticBlock(sync, now) {
  if (IN_FLIGHT.has(sync.phase)) return 'in_flight';
  if (sync.breakerOpen) return 'breaker';
  if (sync.nextAttemptAt !== null && now < sync.nextAttemptAt) return 'backoff';
  return null;
}

/**
 * Reveal the stored token from the cloud and put it in the robot's slot.
 *
 * `automatic` is the reconcile (it is throttled by the backoff and counted by
 * the circuit breaker); a push the student asked for passes `{automatic:false}`.
 *
 * The token lives in the local `token` for the span between `reveal` and the
 * rosbridge call and is dropped before anything else is awaited. After the
 * `reveal` await the call re-checks that it still belongs to the same student
 * (epoch), is still the same attempt (the watchdog or an observed in-sync state
 * may have retired it) and that the account still stores THIS token.
 *
 * @returns {Promise<{ok: boolean, skipped?: string, dropped?: boolean}>}
 */
export const pushHfTokenToRobot = ({ automatic = true } = {}) => async (dispatch, getState) => {
  const state = getState();
  const accessToken = accessTokenOf(state);
  const h0 = state.hfToken;
  if (!accessToken || !h0 || state.auth?.isAuthenticated === false) return { ok: false, skipped: 'signed_out' };
  if (automatic) {
    const blocked = automaticBlock(h0.sync, Date.now());
    if (blocked) return { ok: false, skipped: blocked };
  } else if (IN_FLIGHT.has(h0.sync.phase)) {
    return { ok: false, skipped: 'in_flight' };
  }
  const epoch = h0.epoch;
  dispatch(syncPhaseSet('pushing'));
  const attempt = hfStateOf(getState).sync.attempt;
  const alive = () => {
    const h = hfStateOf(getState);
    return Boolean(h) && h.epoch === epoch && h.sync.attempt === attempt
      && getState().auth?.isAuthenticated !== false;
  };
  let token = null;
  try {
    const revealed = await api.revealHfToken(accessToken);
    token = typeof revealed?.token === 'string' ? revealed.token : null;
    const revealedFp = typeof revealed?.fp === 'string' ? revealed.fp : null;
    if (!alive()) return { ok: false, dropped: true };
    if (!token || !revealedFp || revealedFp !== hfStateOf(getState).account.fp) {
      // The cloud holds another token than the one this page last read (it was
      // replaced elsewhere): drop the one in hand and read the account again.
      token = null;
      dispatch(syncFailed({ message: null }));
      dispatch(refreshHfTokenAccount());
      return { ok: false, dropped: true };
    }
    const result = await setRobotToken(token);
    token = null; // not needed past the call
    if (!alive()) return { ok: false, dropped: true };
    if (result?.success === true) {
      dispatch(syncPushed({ fp: revealedFp, automatic }));
      return { ok: true };
    }
    dispatch(syncFailed({ message: scrub(result?.message) }));
    return { ok: false };
  } catch {
    token = null;
    if (alive()) dispatch(syncFailed({ message: null }));
    return { ok: false };
  } finally {
    token = null;
    // The phase must never be left on „pushing" by a path nobody thought of
    // (audit M10): whatever happened, THIS attempt's phase ends idle.
    const h = hfStateOf(getState);
    if (h && h.epoch === epoch && h.sync.attempt === attempt && h.sync.phase === 'pushing') {
      dispatch(syncPhaseSet('idle'));
    }
  }
};

/**
 * Clear the robot's slot. Same contract as the push: `automatic` is throttled
 * and counted; a clear the student asked for passes `{automatic:false}`.
 * After a confirmed clear the shared Benutzer-ID list is emptied too — it was
 * the account and organisations of the token that is gone.
 *
 * @returns {Promise<{ok: boolean, skipped?: string, dropped?: boolean}>}
 */
export const clearHfTokenOnRobot = ({ automatic = true } = {}) => async (dispatch, getState) => {
  const state = getState();
  const h0 = state.hfToken;
  if (!h0) return { ok: false, skipped: 'no_slice' };
  if (automatic) {
    const blocked = automaticBlock(h0.sync, Date.now());
    if (blocked) return { ok: false, skipped: blocked };
  } else if (IN_FLIGHT.has(h0.sync.phase)) {
    return { ok: false, skipped: 'in_flight' };
  }
  const epoch = h0.epoch;
  const clearedRobotFp = h0.robot.fp;
  dispatch(syncPhaseSet('clearing'));
  const attempt = hfStateOf(getState).sync.attempt;
  const alive = () => {
    const h = hfStateOf(getState);
    return Boolean(h) && h.epoch === epoch && h.sync.attempt === attempt;
  };
  try {
    const result = await setRobotToken('');
    if (!alive()) return { ok: false, dropped: true };
    if (result?.success === true) {
      dispatch(syncCleared({ fp: clearedRobotFp, automatic }));
      dispatch(setHfUserList([]));
      return { ok: true };
    }
    dispatch(syncFailed({ message: scrub(result?.message) }));
    return { ok: false };
  } catch {
    if (alive()) dispatch(syncFailed({ message: null }));
    return { ok: false };
  } finally {
    const h = hfStateOf(getState);
    if (h && h.epoch === epoch && h.sync.attempt === attempt && h.sync.phase === 'clearing') {
      dispatch(syncPhaseSet('idle'));
    }
  }
};

/**
 * „Erneut übertragen": forget every memory that makes the browser hold back
 * (the taken-over dampener, the circuit breaker, the retry backoff) and run the
 * reconcile once more. The student pressed it, so it is a decision, not a loop.
 */
export const forceRetransfer = () => (dispatch) => {
  dispatch(syncForceRetransfer());
};
