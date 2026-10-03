// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The `hfToken` slice and its selectors: metadata only, answers sign-out,
// tolerates a store without it, and carries the S1 gate on the Benutzer-ID list.

import reducer, {
  ACCOUNT_UNKNOWN,
  ROBOT_UNKNOWN,
  SYNC_INITIAL,
  accountFailed,
  accountLoaded,
  accountLoading,
  accountRemoved,
  accountSaved,
  identityChanged,
  kicked,
  robotLegacyConfirmed,
  robotStateLost,
  robotStateReceived,
  syncCleared,
  syncFailed,
  syncForceRetransfer,
  syncPhaseSet,
  syncPushed,
  syncReset,
  syncWatchdogFired,
} from '../hfTokenSlice';
import {
  selectHfAccount,
  selectHfDecision,
  selectHfEpoch,
  selectHfInSync,
  selectHfKick,
  selectHfListReloadAllowed,
  selectHfRobot,
  selectHfStartBlock,
  selectHfSync,
  selectHfSyncFailed,
} from '../hfTokenSelectors';
import { BREAKER_MAX_WRITES, BREAKER_WINDOW_MS } from '../syncDecision';
import { signedOut } from '../../session/sessionActions';

const A = 'aaaaaaaaaaaaaaaa';
const B = 'bbbbbbbbbbbbbbbb';

const initial = () => reducer(undefined, { type: '@@INIT' });
const run = (state, ...actions) => actions.reduce((s, a) => reducer(s, a), state);
const stored = (fp = A, extra = {}) => accountLoaded({
  status: 'stored', hfUsername: 'anna', hint: 'hf_…aaaa', fp, role: 'write', validatedAt: '2026-10-03T10:00:00Z', ...extra,
});
const robotMsg = (o = {}) => robotStateReceived(
  { v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false, ...o },
  1000,
);

describe('hfToken slice — initial state', () => {
  it('has the contracted shape', () => {
    const s = initial();
    expect(s.account).toEqual({
      status: 'unknown', hfUsername: null, hint: null, fp: null, role: null, validatedAt: null,
      accountChanged: false, error: null, failures: 0,
    });
    expect(s.robot).toEqual({
      known: false, accepts: null, present: false, fp: null, busy: false, seq: null, receivedAt: null, legacy: false,
    });
    expect(s.sync).toEqual({
      phase: 'idle', phaseSince: null, attempt: 0, failures: 0, nextAttemptAt: null, lastOwnFp: null,
      clearedFp: null, lastMessage: null, autoWrites: [], breakerOpen: false,
    });
    expect(s.epoch).toBe(0);
    expect(s.kick).toBe(0);
  });

  it('holds no field that could carry a token', () => {
    // Every key of the state tree is a metadata name; none is named for a
    // secret. (The secrecy fence proves the values against the real store.)
    const names = [];
    const walk = (o) => Object.entries(o).forEach(([k, v]) => {
      names.push(k);
      if (v && typeof v === 'object' && !Array.isArray(v)) walk(v);
    });
    walk(initial());
    expect(names.filter((n) => /^(token|secret|password|plain)/i.test(n))).toEqual([]);
  });
});

describe('hfToken slice — sign-out and identity', () => {
  it('session/signedOut returns the initial state with epoch + 1, whatever was there', () => {
    let s = run(
      initial(), stored(), robotMsg({ present: true, fp: A }), syncPushed({ fp: A }), kicked(),
      syncFailed({ message: 'x' }),
    );
    s = reducer(s, signedOut());
    expect(s).toEqual({ ...initial(), epoch: 1 });
    expect(reducer(s, signedOut()).epoch).toBe(2);
  });

  it('an identity change resets the account and the bookkeeping, keeps the robot, bumps the epoch', () => {
    const s = run(initial(), stored(), robotMsg({ present: true, fp: B, seq: 7 }), syncPushed({ fp: A }));
    const next = reducer(s, identityChanged());
    expect(next.epoch).toBe(1);
    expect(next.account).toEqual(initial().account);
    expect(next.sync).toEqual(initial().sync);
    expect(next.robot).toEqual(s.robot);
    expect(next.robot.known).toBe(true);
  });
});

describe('hfToken slice — the account half', () => {
  it('loading only degrades unknown, never a known account', () => {
    expect(reducer(initial(), accountLoading()).account.status).toBe('loading');
    const known = run(initial(), stored());
    expect(reducer(known, accountLoading()).account.status).toBe('stored');
  });

  it('loads stored / none / unusable', () => {
    const s = run(initial(), stored(A));
    expect(s.account).toMatchObject({ status: 'stored', fp: A, hfUsername: 'anna', hint: 'hf_…aaaa', role: 'write' });
    const none = reducer(s, accountLoaded({ status: 'none' }));
    expect(none.account).toMatchObject({ status: 'none', fp: null, hfUsername: null, hint: null });
    const unusable = reducer(s, accountLoaded({ status: 'unusable', fp: A, hfUsername: 'anna', hint: 'hf_…aaaa' }));
    expect(unusable.account.status).toBe('unusable');
  });

  it('ignores a load with a status it does not know', () => {
    const s = run(initial(), stored());
    expect(reducer(s, accountLoaded({ status: 'bogus' })).account).toEqual(s.account);
  });

  it('a failed refresh keeps the last known account and records the sentence', () => {
    const s = run(initial(), stored(), accountFailed({ kind: 'error', message: 'Nicht erreichbar.' }));
    expect(s.account.status).toBe('stored');
    expect(s.account.error).toBe('Nicht erreichbar.');
    expect(s.account.failures).toBe(1);
  });

  it('a failure with nothing known yet degrades to error, and counts', () => {
    const fromUnknown = run(initial(), accountFailed('error'));
    expect(fromUnknown.account.status).toBe('error');
    const fromLoading = run(initial(), accountLoading(), accountFailed({ kind: 'error' }), accountFailed('error'));
    expect(fromLoading.account.status).toBe('error');
    expect(fromLoading.account.failures).toBe(2);
  });

  it('404 and 503 set unsupported / unavailable even over a known account', () => {
    expect(run(initial(), stored(), accountFailed({ kind: 'unsupported' })).account.status).toBe('unsupported');
    expect(run(initial(), accountFailed('unavailable')).account.status).toBe('unavailable');
  });

  it('a successful load clears the error and the failure count', () => {
    const s = run(initial(), accountFailed({ kind: 'error', message: 'x' }), stored());
    expect(s.account.error).toBeNull();
    expect(s.account.failures).toBe(0);
  });

  it('accountChanged survives a reload of the same token and clears for another', () => {
    let s = run(initial(), accountSaved({ fp: A, hfUsername: 'anna', accountChanged: true }));
    expect(s.account.accountChanged).toBe(true);
    s = reducer(s, stored(A));
    expect(s.account.accountChanged).toBe(true);
    s = reducer(s, stored(B));
    expect(s.account.accountChanged).toBe(false);
  });

  it('a saved token that is not usable reads as unusable', () => {
    expect(run(initial(), accountSaved({ fp: A, usable: false })).account.status).toBe('unusable');
    expect(run(initial(), accountSaved({ fp: A })).account.status).toBe('stored');
  });

  it('removal leaves "none" and forgets what the browser remembered about my token', () => {
    const s = run(initial(), stored(), syncPushed({ fp: A }), accountRemoved());
    expect(s.account).toMatchObject({ status: 'none', fp: null, hfUsername: null });
    expect(s.sync.lastOwnFp).toBeNull();
  });
});

describe('hfToken slice — the robot half', () => {
  it('records a state message, stamping the arrival time through the prepare callback', () => {
    const s = run(initial(), robotMsg({ accepts: true, present: true, fp: A, busy: true, seq: 9 }));
    expect(s.robot).toEqual({
      known: true, accepts: true, present: true, fp: A, busy: true, seq: 9, receivedAt: 1000, legacy: false,
    });
  });

  it('stamps Date.now() when no time is given', () => {
    const before = Date.now();
    const s = reducer(initial(), robotStateReceived({ v: 1, seq: 1, accepts: true, present: false, fp: null, busy: false }));
    expect(s.robot.receivedAt).toBeGreaterThanOrEqual(before);
  });

  it('a state message clears legacy', () => {
    let s = run(initial(), robotLegacyConfirmed());
    expect(s.robot.legacy).toBe(true);
    s = reducer(s, robotMsg());
    expect(s.robot.legacy).toBe(false);
    expect(s.robot.known).toBe(true);
  });

  it('legacy is confirmed only while no state was ever seen', () => {
    const s = run(initial(), robotMsg(), robotLegacyConfirmed());
    expect(s.robot.legacy).toBe(false);
  });

  it('losing the robot returns it to unknown, legacy included', () => {
    const s = run(initial(), robotLegacyConfirmed(), robotStateLost());
    expect(s.robot).toEqual(initial().robot);
    const t = run(initial(), robotMsg({ present: true, fp: A }), robotStateLost());
    expect(t.robot).toEqual(initial().robot);
  });

  it('observing my own token in the slot remembers it and forgets every failure', () => {
    const s = run(
      initial(), stored(A),
      syncFailed({ message: 'busy' }), syncFailed({ message: 'busy' }),
      robotMsg({ present: true, fp: A }),
    );
    expect(s.sync.lastOwnFp).toBe(A);
    expect(s.sync.failures).toBe(0);
    expect(s.sync.nextAttemptAt).toBeNull();
    expect(s.sync.lastMessage).toBeNull();
    expect(s.sync.phase).toBe('idle');
  });

  it('observing my token while a push is in flight retires that push', () => {
    const pushing = run(initial(), stored(A), syncPhaseSet('pushing'));
    const attempt = pushing.sync.attempt;
    const s = reducer(pushing, robotMsg({ present: true, fp: A }));
    expect(s.sync.phase).toBe('idle');
    expect(s.sync.attempt).toBe(attempt + 1);
  });

  it('another token in the slot changes nothing about my memory', () => {
    const s = run(initial(), stored(A), syncPushed({ fp: A }), robotMsg({ present: true, fp: B }));
    expect(s.sync.lastOwnFp).toBe(A);
  });
});

describe('hfToken slice — sync bookkeeping', () => {
  it('a starting push or clear gets a new attempt number and a start time', () => {
    const s = reducer(initial(), { ...syncPhaseSet('pushing', 5000) });
    expect(s.sync).toMatchObject({ phase: 'pushing', attempt: 1, phaseSince: 5000 });
    const t = run(s, syncPhaseSet('idle'), syncPhaseSet('clearing', 6000));
    expect(t.sync).toMatchObject({ phase: 'clearing', attempt: 2, phaseSince: 6000 });
    expect(reducer(t, syncPhaseSet('idle')).sync.phaseSince).toBeNull();
  });

  it('a successful push remembers my fingerprint and forgets failures', () => {
    const s = run(initial(), syncFailed({ message: 'x' }), syncPhaseSet('pushing'), syncPushed({ fp: A }));
    expect(s.sync).toMatchObject({
      phase: 'idle', failures: 0, nextAttemptAt: null, lastOwnFp: A, clearedFp: null, lastMessage: null,
    });
  });

  it('a successful clear remembers what it cleared and forgets my own fingerprint', () => {
    const s = run(initial(), syncPushed({ fp: A }), syncCleared({ fp: B }));
    expect(s.sync.clearedFp).toBe(B);
    expect(s.sync.lastOwnFp).toBeNull();
  });

  it('a failure counts, keeps the robot\'s own sentence and schedules the retry by the backoff', () => {
    let s = reducer(initial(), syncFailed({ message: 'Während einer Aufnahme …' }, 10_000));
    expect(s.sync.failures).toBe(1);
    expect(s.sync.lastMessage).toBe('Während einer Aufnahme …');
    expect(s.sync.nextAttemptAt).toBe(12_000);
    s = reducer(s, syncFailed({ message: null }, 20_000));
    expect(s.sync.failures).toBe(2);
    expect(s.sync.nextAttemptAt).toBe(25_000);
    expect(s.sync.lastMessage).toBeNull();
  });

  it('the watchdog retires a call that never came back, once, and only that call', () => {
    const pushing = run(initial(), syncPhaseSet('pushing', 100));
    const attempt = pushing.sync.attempt;
    const fired = reducer(pushing, syncWatchdogFired(attempt, 16_000));
    expect(fired.sync.phase).toBe('idle');
    expect(fired.sync.failures).toBe(1);
    expect(fired.sync.attempt).toBe(attempt + 1); // the zombie finds its attempt gone
    // A second firing for the same attempt is a no-op.
    expect(reducer(fired, syncWatchdogFired(attempt, 17_000))).toEqual(fired);
    // A stale attempt number never touches a newer call.
    const newer = run(fired, syncPhaseSet('pushing', 18_000));
    expect(reducer(newer, syncWatchdogFired(attempt, 19_000))).toEqual(newer);
  });

  it('the watchdog ignores a phase that is not in flight', () => {
    const s = initial();
    expect(reducer(s, syncWatchdogFired(0, 1))).toEqual(s);
  });

  it('force-retransfer forgets the dampener, the breaker and the backoff, and kicks once', () => {
    let s = run(initial(), syncPushed({ fp: A }), syncCleared({ fp: B }), syncFailed({ message: 'x' }));
    s = reducer(s, syncForceRetransfer());
    expect(s.sync).toMatchObject({
      lastOwnFp: null, clearedFp: null, failures: 0, nextAttemptAt: null, lastMessage: null,
      autoWrites: [], breakerOpen: false,
    });
    expect(s.kick).toBe(1);
  });

  it('kicked and syncReset do what they say', () => {
    expect(reducer(initial(), kicked()).kick).toBe(1);
    const s = run(initial(), syncFailed({ message: 'x' }), syncReset());
    expect(s.sync).toEqual(initial().sync);
  });
});

describe('hfToken slice — the circuit breaker (audit M10)', () => {
  const t0 = 1_000_000;
  const push = (offsetMs, automatic = true) => syncPushed({ fp: A, automatic }, t0 + offsetMs);

  it(`opens after ${BREAKER_MAX_WRITES} automatic writes inside the window`, () => {
    let s = run(initial(), push(0), push(10_000));
    expect(s.sync.breakerOpen).toBe(false);
    s = reducer(s, push(20_000));
    expect(s.sync.breakerOpen).toBe(true);
    expect(s.sync.autoWrites).toHaveLength(3);
  });

  it('counts pushes and clears alike', () => {
    const s = run(
      initial(),
      push(0), syncCleared({ fp: B, automatic: true }, t0 + 1000), push(2000),
    );
    expect(s.sync.breakerOpen).toBe(true);
  });

  it('forgets writes that fall out of the window', () => {
    const s = run(initial(), push(0), push(10_000), push(BREAKER_WINDOW_MS + 5_000));
    expect(s.sync.breakerOpen).toBe(false);
    expect(s.sync.autoWrites).toHaveLength(2);
  });

  it('does not count a write the student asked for', () => {
    const s = run(initial(), push(0, false), push(1000, false), push(2000, false), push(3000, false));
    expect(s.sync.breakerOpen).toBe(false);
    expect(s.sync.autoWrites).toEqual([]);
  });

  it('does not count a failure', () => {
    const s = run(initial(), syncFailed({}, t0), syncFailed({}, t0 + 1), syncFailed({}, t0 + 2), syncFailed({}, t0 + 3));
    expect(s.sync.breakerOpen).toBe(false);
  });

  it('stays open through an in-sync observation (else a ping-pong would never trip it)', () => {
    const s = run(initial(), stored(A), push(0), push(1), push(2), robotMsg({ present: true, fp: A }));
    expect(s.sync.breakerOpen).toBe(true);
  });

  it('closes on a manual card action and on force-retransfer', () => {
    const open = run(initial(), push(0), push(1), push(2));
    expect(open.sync.breakerOpen).toBe(true);
    expect(reducer(open, accountSaved({ fp: A })).sync.breakerOpen).toBe(false);
    expect(reducer(open, accountRemoved()).sync.breakerOpen).toBe(false);
    expect(reducer(open, syncForceRetransfer()).sync.breakerOpen).toBe(false);
  });
});

describe('selectors on a store WITHOUT the slice', () => {
  const bare = { tasks: {}, auth: {} };

  it('answer unknown, with module-level constants and the same reference every time', () => {
    expect(selectHfAccount(bare)).toBe(ACCOUNT_UNKNOWN);
    expect(selectHfRobot(bare)).toBe(ROBOT_UNKNOWN);
    expect(selectHfSync(bare)).toBe(SYNC_INITIAL);
    expect(selectHfAccount(bare)).toBe(selectHfAccount({}));
    expect(selectHfEpoch(bare)).toBe(0);
    expect(selectHfKick(bare)).toBe(0);
    expect(selectHfInSync(bare)).toBe(false);
    expect(selectHfDecision(bare)).toBe('noop');
    expect(selectHfSyncFailed(bare)).toBe(false);
    expect(selectHfStartBlock(bare)).toBeNull();
  });

  it('the constants are frozen', () => {
    expect(Object.isFrozen(ACCOUNT_UNKNOWN)).toBe(true);
    expect(Object.isFrozen(ROBOT_UNKNOWN)).toBe(true);
    expect(Object.isFrozen(SYNC_INITIAL)).toBe(true);
  });

  it('return stable references from a store WITH the slice, until it changes', () => {
    const s = { hfToken: run(initial(), stored()) };
    expect(selectHfAccount(s)).toBe(selectHfAccount(s));
    expect(selectHfRobot(s)).toBe(selectHfRobot(s));
  });
});

describe('selectHfInSync / selectHfDecision / selectHfStartBlock on real state', () => {
  const wrap = (hfToken, extra = {}) => ({ hfToken, ...extra });

  it('in sync only for a stored account and the same fingerprint in the slot', () => {
    expect(selectHfInSync(wrap(run(initial(), stored(A), robotMsg({ present: true, fp: A }))))).toBe(true);
    expect(selectHfInSync(wrap(run(initial(), stored(A), robotMsg({ present: true, fp: B }))))).toBe(false);
    expect(selectHfInSync(wrap(run(initial(), stored(A), robotMsg())))).toBe(false);
    expect(selectHfInSync(wrap(run(initial(), accountLoaded({ status: 'none' }), robotMsg())))).toBe(false);
  });

  it('decides from the slice, and steps aside while a Jetson is claimed', () => {
    const state = run(initial(), stored(A), robotMsg());
    expect(selectHfDecision(wrap(state))).toBe('push');
    expect(selectHfDecision(wrap(state, { jetson: { status: 'connected' } }))).toBe('noop');
    expect(selectHfStartBlock(wrap(state))).toBe('transfer');
    expect(selectHfStartBlock(wrap(state, { jetson: { status: 'connected' } }))).toBeNull();
  });

  it('says "failed" after two failures or an open breaker', () => {
    const one = run(initial(), syncFailed({}));
    expect(selectHfSyncFailed(wrap(one))).toBe(false);
    expect(selectHfSyncFailed(wrap(reducer(one, syncFailed({}))))).toBe(true);
    const open = run(initial(), syncPushed({ fp: A }), syncPushed({ fp: A }), syncPushed({ fp: A }));
    expect(selectHfSyncFailed(wrap(open))).toBe(true);
  });

  it('start block "failed" follows the failure count', () => {
    const base = run(initial(), stored(A), robotMsg({ present: true, fp: B }), syncFailed({}), syncFailed({}));
    expect(selectHfStartBlock(wrap(base))).toBe('failed');
  });
});

describe('selectHfListReloadAllowed — the Benutzer-ID gate (audit S1)', () => {
  const wrap = (hfToken, extra = {}) => ({ hfToken, ...extra });

  it('allows a store without the slice (page tests, builds without the feature)', () => {
    expect(selectHfListReloadAllowed({})).toBe(true);
  });

  it('blocks while the robot holds a foreign token and the account has none', () => {
    const s = run(initial(), accountLoaded({ status: 'none' }), robotMsg({ present: true, fp: B }));
    expect(selectHfListReloadAllowed(wrap(s))).toBe(false);
  });

  it('blocks while the robot holds a foreign token and the account state failed to load', () => {
    const s = run(initial(), accountFailed('error'), robotMsg({ present: true, fp: B }));
    expect(selectHfListReloadAllowed(wrap(s))).toBe(false);
  });

  it('blocks while a stored token is still on its way to the robot', () => {
    const s = run(initial(), stored(A), robotMsg({ present: true, fp: B }));
    expect(selectHfListReloadAllowed(wrap(s))).toBe(false);
  });

  it('allows once the robot holds the account\'s token', () => {
    const s = run(initial(), stored(A), robotMsg({ present: true, fp: A }));
    expect(selectHfListReloadAllowed(wrap(s))).toBe(true);
  });

  it('blocks while the robot state is unknown (offline escape, the first seconds)', () => {
    expect(selectHfListReloadAllowed(wrap(initial()))).toBe(false);
  });

  it('allows an old image once it proved silent', () => {
    expect(selectHfListReloadAllowed(wrap(run(initial(), robotLegacyConfirmed())))).toBe(true);
  });

  it('allows a robot that takes no personal token (the Jetson image)', () => {
    const s = run(initial(), accountLoaded({ status: 'none' }), robotMsg({ accepts: false }));
    expect(selectHfListReloadAllowed(wrap(s))).toBe(true);
  });

  it('allows a claimed Jetson whatever the local slot says', () => {
    const s = run(initial(), accountLoaded({ status: 'none' }), robotMsg({ present: true, fp: B }));
    expect(selectHfListReloadAllowed(wrap(s, { jetson: { status: 'connected' } }))).toBe(true);
    expect(selectHfListReloadAllowed(wrap(initial(), { jetson: { status: 'connected' } }))).toBe(true);
  });
});
