// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The verbs of the per-student token, against a REAL store (auth, hfToken, ui):
// the status mapping of the cloud answers, the save / remove / verify results,
// and the push and clear to the robot with everything that can go wrong around
// them — a sign-out in the middle, the watchdog, the breaker, a throw.

import { configureStore } from '@reduxjs/toolkit';

import authReducer, { setSession } from '../../auth/authSlice';
import uiReducer, { setHfUserList } from '../../ui/uiSlice';
import hfReducer, {
  accountLoaded,
  robotStateReceived,
  syncFailed,
  syncPhaseSet,
  syncPushed,
  syncWatchdogFired,
} from '../hfTokenSlice';
import { signedOut } from '../../session/sessionActions';
import * as api from '../../../services/hfTokenApi';
import { setRobotToken } from '../robotChannel';
import {
  accountFromResponse,
  clearHfTokenOnRobot,
  failureFromError,
  forceRetransfer,
  pushHfTokenToRobot,
  refreshHfTokenAccount,
  removeHfToken,
  saveHfToken,
  sentenceFromError,
  verifyHfToken,
} from '../hfTokenThunks';

vi.mock('../../../services/hfTokenApi', () => ({
  __esModule: true,
  getHfToken: vi.fn(),
  putHfToken: vi.fn(),
  deleteHfToken: vi.fn(),
  revealHfToken: vi.fn(),
  verifyHfToken: vi.fn(),
}));
vi.mock('../robotChannel', () => ({
  __esModule: true,
  setRobotToken: vi.fn(),
}));

// Low-entropy fixtures on purpose (the repository's secret scan covers history).
const TOKEN = `hf_${'a'.repeat(34)}`;
const FP = 'c1770a7966b0771e';
const OTHER_FP = 'bbbbbbbbbbbbbbbb';

const storedBody = (o = {}) => ({
  stored: true, usable: true, hf_username: 'anna', hint: 'hf_…aaaa', fp: FP, role: 'write',
  validated_at: '2026-10-03T10:00:00Z', ...o,
});

function makeStore({ signedIn = true } = {}) {
  const store = configureStore({
    reducer: { auth: authReducer, hfToken: hfReducer, ui: uiReducer },
  });
  if (signedIn) store.dispatch(setSession({ access_token: 'jwt-1', user: { id: 'u1' } }));
  return store;
}
const hf = (store) => store.getState().hfToken;

const apiError = (status, detail) => Object.assign(new Error(String(detail)), { status, detail });

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

// A store whose account stores FP and whose robot is empty and idle.
function readyToPush() {
  const store = makeStore();
  store.dispatch(accountLoaded({
    status: 'stored', hfUsername: 'anna', hint: 'hf_…aaaa', fp: FP, role: 'write', validatedAt: null,
  }));
  store.dispatch(robotStateReceived({ v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false }, 1));
  return store;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('accountFromResponse', () => {
  it('maps not stored / stored / not usable', () => {
    expect(accountFromResponse({ stored: false })).toMatchObject({ status: 'none', fp: null });
    expect(accountFromResponse(storedBody())).toEqual({
      status: 'stored', hfUsername: 'anna', hint: 'hf_…aaaa', fp: FP, role: 'write',
      validatedAt: '2026-10-03T10:00:00Z',
    });
    expect(accountFromResponse(storedBody({ usable: false })).status).toBe('unusable');
  });

  it('reads an absent `usable` (an older server) as usable', () => {
    const body = storedBody();
    delete body.usable;
    expect(accountFromResponse(body).status).toBe('stored');
  });

  it('refuses a stored row without a valid fingerprint and anything that is not an object', () => {
    expect(accountFromResponse(storedBody({ fp: null }))).toBeNull();
    expect(accountFromResponse(storedBody({ fp: 'ABC' }))).toBeNull();
    expect(accountFromResponse(null)).toBeNull();
    expect(accountFromResponse('x')).toBeNull();
  });
});

describe('sentenceFromError / failureFromError', () => {
  it('shows the server\'s own German detail', () => {
    const detail = 'Dieses Token hat nur Leserechte. Zum Hochladen braucht EduBotics ein Token mit Schreibrechten.';
    expect(sentenceFromError(apiError(422, detail))).toBe(detail);
  });

  it('never lets a token-shaped string through, even in a German sentence', () => {
    const shown = sentenceFromError(apiError(422, `Das Token ${TOKEN} ist ungültig.`));
    expect(shown).not.toContain(TOKEN);
    expect(shown).toBe('Das Token hf_*** ist ungültig.');
  });

  it('falls back to the generic sentence for an English status text, an array and nothing', () => {
    const generic = 'Das hat nicht geklappt. Bitte versuche es noch einmal.';
    expect(sentenceFromError(apiError(502, 'Bad Gateway'))).toBe(generic);
    expect(sentenceFromError(apiError(422, [{ type: 'json_invalid', msg: 'x' }]))).toBe(generic);
    expect(sentenceFromError(new Error('boom'))).toBe(generic);
    expect(sentenceFromError(undefined)).toBe(generic);
    expect(sentenceFromError(apiError(500, 'x'.repeat(500)))).toBe(generic);
    expect(sentenceFromError(apiError(500, 'Bad Gateway'), 'eigener Satz')).toBe('eigener Satz');
  });

  it('maps 404 to unsupported, 503 to unavailable and the rest to an error with a sentence', () => {
    expect(failureFromError(apiError(404, 'Not Found'))).toEqual({ kind: 'unsupported', message: null });
    expect(failureFromError(apiError(503, 'x'))).toEqual({ kind: 'unavailable', message: null });
    expect(failureFromError(apiError(500, 'Bad Gateway'))).toEqual({
      kind: 'error', message: 'Der Token-Status konnte nicht geladen werden.',
    });
    expect(failureFromError(new TypeError('Failed to fetch')).kind).toBe('error');
  });
});

describe('refreshHfTokenAccount — the status mapping', () => {
  it('not stored → none', async () => {
    const store = makeStore();
    api.getHfToken.mockResolvedValue({ stored: false, usable: false });
    await expect(store.dispatch(refreshHfTokenAccount())).resolves.toBe(true);
    expect(hf(store).account.status).toBe('none');
    expect(api.getHfToken).toHaveBeenCalledWith('jwt-1');
  });

  it('stored and usable → stored, with the metadata', async () => {
    const store = makeStore();
    api.getHfToken.mockResolvedValue(storedBody());
    await store.dispatch(refreshHfTokenAccount());
    expect(hf(store).account).toMatchObject({
      status: 'stored', hfUsername: 'anna', hint: 'hf_…aaaa', fp: FP, role: 'write',
    });
  });

  it('stored but not usable → unusable', async () => {
    const store = makeStore();
    api.getHfToken.mockResolvedValue(storedBody({ usable: false }));
    await store.dispatch(refreshHfTokenAccount());
    expect(hf(store).account.status).toBe('unusable');
  });

  it('404 → unsupported, 503 → unavailable, anything else → error', async () => {
    for (const [status, expected] of [[404, 'unsupported'], [503, 'unavailable'], [500, 'error']]) {
      const store = makeStore();
      api.getHfToken.mockRejectedValue(apiError(status, 'x'));
      await expect(store.dispatch(refreshHfTokenAccount())).resolves.toBe(false);
      expect(hf(store).account.status).toBe(expected);
      expect(hf(store).account.failures).toBe(1);
    }
  });

  it('a network failure with nothing known yet → error, with a German sentence', async () => {
    const store = makeStore();
    api.getHfToken.mockRejectedValue(new TypeError('Failed to fetch'));
    await store.dispatch(refreshHfTokenAccount());
    expect(hf(store).account.status).toBe('error');
    expect(hf(store).account.error).toBe('Der Token-Status konnte nicht geladen werden.');
  });

  it('a failed refresh keeps the last known account', async () => {
    const store = makeStore();
    api.getHfToken.mockResolvedValueOnce(storedBody());
    await store.dispatch(refreshHfTokenAccount());
    api.getHfToken.mockRejectedValueOnce(apiError(500, 'x'));
    await store.dispatch(refreshHfTokenAccount());
    expect(hf(store).account.status).toBe('stored');
    expect(hf(store).account.fp).toBe(FP);
    expect(hf(store).account.error).toBeTruthy();
  });

  it('a 401 or 403 never signs the student out from here', async () => {
    const store = makeStore();
    const seen = [];
    const original = store.dispatch;
    store.dispatch = (a) => { if (a && a.type) seen.push(a.type); return original(a); };
    for (const status of [401, 403]) {
      api.getHfToken.mockRejectedValueOnce(apiError(status, 'Sitzung abgelaufen'));
      await store.dispatch(refreshHfTokenAccount());
    }
    expect(seen).not.toContain('session/signedOut');
    expect(hf(store).account.status).toBe('error');
  });

  it('a malformed body is an error, not an account', async () => {
    const store = makeStore();
    api.getHfToken.mockResolvedValue({ stored: true, fp: null });
    await expect(store.dispatch(refreshHfTokenAccount())).resolves.toBe(false);
    expect(hf(store).account.status).toBe('error');
  });

  it('does nothing without a session', async () => {
    const store = makeStore({ signedIn: false });
    await expect(store.dispatch(refreshHfTokenAccount())).resolves.toBe(false);
    expect(api.getHfToken).not.toHaveBeenCalled();
    expect(hf(store).account.status).toBe('unknown');
  });

  it('shows "loading" only from unknown', async () => {
    const store = makeStore();
    const gate = deferred();
    api.getHfToken.mockReturnValueOnce(gate.promise);
    const first = store.dispatch(refreshHfTokenAccount());
    expect(hf(store).account.status).toBe('loading');
    gate.resolve(storedBody());
    await first;
    api.getHfToken.mockReturnValueOnce(gate.promise);
    const second = store.dispatch(refreshHfTokenAccount());
    expect(hf(store).account.status).toBe('stored');
    await second;
  });

  it('drops an answer that arrives after the student signed out', async () => {
    const store = makeStore();
    const gate = deferred();
    api.getHfToken.mockReturnValueOnce(gate.promise);
    const pending = store.dispatch(refreshHfTokenAccount());
    store.dispatch(signedOut());
    gate.resolve(storedBody());
    await pending;
    expect(hf(store).account.status).toBe('unknown');
    expect(hf(store).account.fp).toBeNull();
  });

  it('drops an older answer that arrives after a newer one', async () => {
    const store = makeStore();
    const oldGet = deferred();
    api.getHfToken.mockReturnValueOnce(oldGet.promise);
    const first = store.dispatch(refreshHfTokenAccount());
    api.getHfToken.mockResolvedValueOnce(storedBody({ fp: OTHER_FP }));
    await store.dispatch(refreshHfTokenAccount());
    oldGet.resolve(storedBody({ fp: FP }));
    await first;
    expect(hf(store).account.fp).toBe(OTHER_FP);
  });

  it('drops a GET that was issued before a save and answers after it', async () => {
    const store = makeStore();
    const oldGet = deferred();
    api.getHfToken.mockReturnValueOnce(oldGet.promise);
    const get = store.dispatch(refreshHfTokenAccount());
    api.putHfToken.mockResolvedValueOnce(storedBody({ account_changed: false }));
    await store.dispatch(saveHfToken(TOKEN));
    oldGet.resolve({ stored: false });
    await get;
    expect(hf(store).account.status).toBe('stored');
  });
});

describe('saveHfToken', () => {
  it('stores the account, links the proven name and kicks the reconcile', async () => {
    const store = makeStore();
    api.putHfToken.mockResolvedValue(storedBody({ account_changed: true }));
    const result = await store.dispatch(saveHfToken(TOKEN));
    expect(result).toEqual({ ok: true, error: null, accountChanged: true });
    expect(hf(store).account).toMatchObject({ status: 'stored', fp: FP, accountChanged: true, hfUsername: 'anna' });
    expect(store.getState().auth.hfUsername).toBe('anna');
    expect(hf(store).kick).toBe(1);
  });

  it('sends the trimmed token and nothing else', async () => {
    const store = makeStore();
    api.putHfToken.mockResolvedValue(storedBody());
    await store.dispatch(saveHfToken(`  ${TOKEN}\n`));
    expect(api.putHfToken).toHaveBeenCalledWith('jwt-1', TOKEN);
  });

  it('returns the server\'s German how-to for a refused token and changes nothing', async () => {
    const store = makeStore();
    const detail = 'Hugging Face hat dieses Token abgelehnt. Prüfe, ob du es vollständig kopiert hast.';
    api.putHfToken.mockRejectedValue(apiError(422, detail));
    const result = await store.dispatch(saveHfToken(TOKEN));
    expect(result).toEqual({ ok: false, error: detail, accountChanged: false });
    expect(hf(store).account.status).toBe('unknown');
    expect(hf(store).kick).toBe(0);
  });

  it('answers generically to an array detail or a network error', async () => {
    const store = makeStore();
    api.putHfToken.mockRejectedValueOnce(apiError(422, [{ msg: 'x' }]));
    expect((await store.dispatch(saveHfToken(TOKEN))).error).toBe('Das hat nicht geklappt. Bitte versuche es noch einmal.');
    api.putHfToken.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    expect((await store.dispatch(saveHfToken(TOKEN))).ok).toBe(false);
  });

  it('refuses a response that is not a stored account', async () => {
    const store = makeStore();
    api.putHfToken.mockResolvedValue({ stored: false });
    expect((await store.dispatch(saveHfToken(TOKEN))).ok).toBe(false);
    expect(hf(store).account.status).toBe('unknown');
  });

  it('does nothing without a session', async () => {
    const store = makeStore({ signedIn: false });
    expect((await store.dispatch(saveHfToken(TOKEN))).ok).toBe(false);
    expect(api.putHfToken).not.toHaveBeenCalled();
  });

  it('forgets the result when the student signed out meanwhile', async () => {
    const store = makeStore();
    const gate = deferred();
    api.putHfToken.mockReturnValueOnce(gate.promise);
    const pending = store.dispatch(saveHfToken(TOKEN));
    store.dispatch(signedOut());
    gate.resolve(storedBody());
    expect((await pending).ok).toBe(false);
    expect(hf(store).account.status).toBe('unknown');
  });
});

describe('removeHfToken / verifyHfToken', () => {
  it('remove: the account becomes "none" and the reconcile is kicked', async () => {
    const store = readyToPush();
    api.deleteHfToken.mockResolvedValue({ stored: false });
    await expect(store.dispatch(removeHfToken())).resolves.toEqual({ ok: true, error: null });
    expect(hf(store).account.status).toBe('none');
    expect(hf(store).kick).toBe(1);
  });

  it('remove: a failure keeps the account and says so in German', async () => {
    const store = readyToPush();
    api.deleteHfToken.mockRejectedValue(apiError(500, 'Bad Gateway'));
    const result = await store.dispatch(removeHfToken());
    expect(result).toEqual({ ok: false, error: 'Das Token konnte nicht entfernt werden. Bitte versuche es noch einmal.' });
    expect(hf(store).account.status).toBe('stored');
  });

  it('verify: refreshes the account from the answer', async () => {
    const store = readyToPush();
    api.verifyHfToken.mockResolvedValue(storedBody({ validated_at: '2026-10-04T08:00:00Z' }));
    await expect(store.dispatch(verifyHfToken())).resolves.toEqual({ ok: true, error: null });
    expect(hf(store).account.validatedAt).toBe('2026-10-04T08:00:00Z');
  });

  it('verify: a rejected token is the server\'s German sentence, and the account stays', async () => {
    const store = readyToPush();
    const detail = 'Hugging Face hat dein gespeichertes Token abgelehnt (widerrufen oder abgelaufen). Bitte speichere ein neues.';
    api.verifyHfToken.mockRejectedValue(apiError(422, detail));
    const result = await store.dispatch(verifyHfToken());
    expect(result).toEqual({ ok: false, error: detail });
    expect(hf(store).account.status).toBe('stored');
  });
});

describe('pushHfTokenToRobot', () => {
  it('reveals the token, hands it to the robot and remembers the fingerprint', async () => {
    const store = readyToPush();
    api.revealHfToken.mockResolvedValue({ token: TOKEN, fp: FP });
    setRobotToken.mockResolvedValue({ success: true, message: 'Dein Hugging-Face-Token ist auf dem Roboter aktiv.' });
    await expect(store.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: true });
    expect(api.revealHfToken).toHaveBeenCalledWith('jwt-1');
    expect(setRobotToken).toHaveBeenCalledTimes(1);
    expect(setRobotToken).toHaveBeenCalledWith(TOKEN);
    expect(hf(store).sync).toMatchObject({ phase: 'idle', lastOwnFp: FP, failures: 0 });
    expect(hf(store).sync.autoWrites).toHaveLength(1);
  });

  it('drops the token when the student signed out while the reveal was on its way', async () => {
    const store = readyToPush();
    const gate = deferred();
    api.revealHfToken.mockReturnValueOnce(gate.promise);
    const pending = store.dispatch(pushHfTokenToRobot());
    store.dispatch(signedOut());
    gate.resolve({ token: TOKEN, fp: FP });
    await expect(pending).resolves.toEqual({ ok: false, dropped: true });
    expect(setRobotToken).not.toHaveBeenCalled();
    expect(hf(store).sync.lastOwnFp).toBeNull();
  });

  it('drops the token when the session ended another way (auth cleared) during the reveal', async () => {
    const store = readyToPush();
    const gate = deferred();
    api.revealHfToken.mockReturnValueOnce(gate.promise);
    const pending = store.dispatch(pushHfTokenToRobot());
    store.dispatch(setSession(null));
    gate.resolve({ token: TOKEN, fp: FP });
    await expect(pending).resolves.toMatchObject({ ok: false, dropped: true });
    expect(setRobotToken).not.toHaveBeenCalled();
  });

  it('does not push a token whose fingerprint is not the one the account was read with', async () => {
    const store = readyToPush();
    api.revealHfToken.mockResolvedValue({ token: TOKEN, fp: OTHER_FP });
    api.getHfToken.mockResolvedValue(storedBody({ fp: OTHER_FP }));
    await expect(store.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: false, dropped: true });
    expect(setRobotToken).not.toHaveBeenCalled();
    // ... and the account is read again so the next round compares the right one.
    await vi.waitFor(() => expect(hf(store).account.fp).toBe(OTHER_FP));
  });

  it('does not push a reveal that carries no token', async () => {
    const store = readyToPush();
    api.revealHfToken.mockResolvedValue({ fp: FP });
    api.getHfToken.mockResolvedValue(storedBody());
    await store.dispatch(pushHfTokenToRobot());
    expect(setRobotToken).not.toHaveBeenCalled();
    expect(hf(store).sync.failures).toBe(1);
  });

  it('counts a refusal by the robot and keeps its own German sentence', async () => {
    const store = readyToPush();
    api.revealHfToken.mockResolvedValue({ token: TOKEN, fp: FP });
    setRobotToken.mockResolvedValue({ success: false, message: 'Während einer Aufnahme kann das Token nicht geändert werden.' });
    await expect(store.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: false });
    expect(hf(store).sync).toMatchObject({
      phase: 'idle', failures: 1, lastMessage: 'Während einer Aufnahme kann das Token nicht geändert werden.',
    });
    expect(hf(store).sync.nextAttemptAt).toBeGreaterThan(Date.now());
    expect(hf(store).sync.autoWrites).toEqual([]);
  });

  it('counts a failed reveal and a throwing robot call, and never leaves the phase on "pushing"', async () => {
    const store = readyToPush();
    api.revealHfToken.mockRejectedValueOnce(apiError(500, 'x'));
    await store.dispatch(pushHfTokenToRobot({ automatic: false }));
    expect(hf(store).sync).toMatchObject({ phase: 'idle', failures: 1 });
    api.revealHfToken.mockResolvedValueOnce({ token: TOKEN, fp: FP });
    setRobotToken.mockRejectedValueOnce(new Error('boom'));
    await store.dispatch(pushHfTokenToRobot({ automatic: false }));
    expect(hf(store).sync).toMatchObject({ phase: 'idle', failures: 2 });
  });

  it('is single-flight: a second push while one is in flight does not start', async () => {
    const store = readyToPush();
    const gate = deferred();
    api.revealHfToken.mockReturnValueOnce(gate.promise);
    const first = store.dispatch(pushHfTokenToRobot());
    expect(hf(store).sync.phase).toBe('pushing');
    await expect(store.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: false, skipped: 'in_flight' });
    await expect(store.dispatch(pushHfTokenToRobot({ automatic: false }))).resolves.toEqual({ ok: false, skipped: 'in_flight' });
    gate.resolve({ token: TOKEN, fp: FP });
    setRobotToken.mockResolvedValue({ success: true, message: '' });
    await first;
    expect(api.revealHfToken).toHaveBeenCalledTimes(1);
  });

  it('an automatic push waits out the backoff; one the student asked for does not', async () => {
    const store = readyToPush();
    store.dispatch(syncFailed({ message: 'x' }));
    await expect(store.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: false, skipped: 'backoff' });
    expect(api.revealHfToken).not.toHaveBeenCalled();
    api.revealHfToken.mockResolvedValue({ token: TOKEN, fp: FP });
    setRobotToken.mockResolvedValue({ success: true, message: '' });
    await expect(store.dispatch(pushHfTokenToRobot({ automatic: false }))).resolves.toEqual({ ok: true });
  });

  it('is retired by the watchdog: a late answer is dropped and writes nothing', async () => {
    const store = readyToPush();
    const gate = deferred();
    api.revealHfToken.mockReturnValueOnce(gate.promise);
    const pending = store.dispatch(pushHfTokenToRobot());
    store.dispatch(syncWatchdogFired(hf(store).sync.attempt));
    expect(hf(store).sync).toMatchObject({ phase: 'idle', failures: 1 });
    gate.resolve({ token: TOKEN, fp: FP });
    await expect(pending).resolves.toEqual({ ok: false, dropped: true });
    expect(setRobotToken).not.toHaveBeenCalled();
    expect(hf(store).sync.lastOwnFp).toBeNull();
  });

  it('is retired when the robot is seen holding the token before the call answers', async () => {
    const store = readyToPush();
    const gate = deferred();
    api.revealHfToken.mockReturnValueOnce(gate.promise);
    const pending = store.dispatch(pushHfTokenToRobot());
    store.dispatch(robotStateReceived({ v: 1, seq: 2, accepts: true, present: true, fp: FP, busy: false }, 2));
    gate.resolve({ token: TOKEN, fp: FP });
    await expect(pending).resolves.toEqual({ ok: false, dropped: true });
    expect(setRobotToken).not.toHaveBeenCalled();
    expect(hf(store).sync.lastOwnFp).toBe(FP);
  });

  it('opens the circuit breaker after three automatic writes and then refuses automatic ones', async () => {
    const store = readyToPush();
    api.revealHfToken.mockResolvedValue({ token: TOKEN, fp: FP });
    setRobotToken.mockResolvedValue({ success: true, message: '' });
    for (let i = 0; i < 3; i += 1) {
      await expect(store.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: true });
    }
    expect(hf(store).sync.breakerOpen).toBe(true);
    await expect(store.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: false, skipped: 'breaker' });
    expect(setRobotToken).toHaveBeenCalledTimes(3);
    // the student's own push is never held back by it
    await expect(store.dispatch(pushHfTokenToRobot({ automatic: false }))).resolves.toEqual({ ok: true });
    // and „Erneut übertragen" closes it
    store.dispatch(forceRetransfer());
    expect(hf(store).sync.breakerOpen).toBe(false);
    await expect(store.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: true });
  });

  it('does nothing without a session or without the slice', async () => {
    const out = makeStore({ signedIn: false });
    await expect(out.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: false, skipped: 'signed_out' });
    const bare = configureStore({ reducer: { auth: authReducer } });
    bare.dispatch(setSession({ access_token: 'jwt-1' }));
    await expect(bare.dispatch(pushHfTokenToRobot())).resolves.toEqual({ ok: false, skipped: 'signed_out' });
    expect(api.revealHfToken).not.toHaveBeenCalled();
  });
});

describe('clearHfTokenOnRobot', () => {
  const holdingForeignToken = () => {
    const store = makeStore();
    store.dispatch(accountLoaded({ status: 'none' }));
    store.dispatch(robotStateReceived({ v: 1, seq: 1, accepts: true, present: true, fp: OTHER_FP, busy: false }, 1));
    store.dispatch(setHfUserList(['vorheriger-schueler']));
    return store;
  };

  it('clears the slot, remembers what it cleared and empties the Benutzer-ID list', async () => {
    const store = holdingForeignToken();
    setRobotToken.mockResolvedValue({ success: true, message: 'Das Hugging-Face-Token wurde vom Roboter entfernt.' });
    await expect(store.dispatch(clearHfTokenOnRobot())).resolves.toEqual({ ok: true });
    expect(setRobotToken).toHaveBeenCalledWith('');
    expect(hf(store).sync).toMatchObject({ phase: 'idle', clearedFp: OTHER_FP, lastOwnFp: null });
    expect(store.getState().ui.hfUserList).toEqual([]);
  });

  it('counts a refusal and keeps the list', async () => {
    const store = holdingForeignToken();
    setRobotToken.mockResolvedValue({ success: false, message: 'Während einer Aufnahme …' });
    await expect(store.dispatch(clearHfTokenOnRobot())).resolves.toEqual({ ok: false });
    expect(hf(store).sync).toMatchObject({ phase: 'idle', failures: 1, lastMessage: 'Während einer Aufnahme …' });
    expect(store.getState().ui.hfUserList).toEqual(['vorheriger-schueler']);
  });

  it('survives a throwing robot call', async () => {
    const store = holdingForeignToken();
    setRobotToken.mockRejectedValue(new Error('boom'));
    await expect(store.dispatch(clearHfTokenOnRobot())).resolves.toEqual({ ok: false });
    expect(hf(store).sync).toMatchObject({ phase: 'idle', failures: 1 });
  });

  it('drops its result when the student signed out meanwhile', async () => {
    const store = holdingForeignToken();
    const gate = deferred();
    setRobotToken.mockReturnValueOnce(gate.promise);
    const pending = store.dispatch(clearHfTokenOnRobot());
    store.dispatch(signedOut());
    gate.resolve({ success: true, message: '' });
    await expect(pending).resolves.toEqual({ ok: false, dropped: true });
    expect(hf(store).sync.clearedFp).toBeNull();
  });

  it('is single-flight and honours the backoff and the breaker when automatic', async () => {
    const store = holdingForeignToken();
    store.dispatch(syncPhaseSet('pushing'));
    await expect(store.dispatch(clearHfTokenOnRobot())).resolves.toEqual({ ok: false, skipped: 'in_flight' });
    store.dispatch(syncPhaseSet('idle'));
    store.dispatch(syncFailed({ message: 'x' }));
    await expect(store.dispatch(clearHfTokenOnRobot())).resolves.toEqual({ ok: false, skipped: 'backoff' });
    expect(setRobotToken).not.toHaveBeenCalled();
  });

  it('counts an automatic clear towards the breaker', async () => {
    const store = holdingForeignToken();
    setRobotToken.mockResolvedValue({ success: true, message: '' });
    store.dispatch(syncPushed({ fp: FP }));
    store.dispatch(syncPushed({ fp: FP }));
    await store.dispatch(clearHfTokenOnRobot());
    expect(hf(store).sync.breakerOpen).toBe(true);
  });
});

describe('forceRetransfer', () => {
  it('forgets the dampener and the backoff and kicks once', () => {
    const store = readyToPush();
    store.dispatch(syncPushed({ fp: FP }));
    store.dispatch(syncFailed({ message: 'x' }));
    store.dispatch(forceRetransfer());
    expect(hf(store).sync).toMatchObject({ lastOwnFp: null, clearedFp: null, failures: 0, nextAttemptAt: null });
    expect(hf(store).kick).toBe(1);
  });
});
