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
  syncSettleElapsed,
  syncWatchdogFired,
} from '../hfTokenSlice';
import {
  selectHfAccount,
  selectHfAwaitingOwnPush,
  selectHfDecision,
  selectHfEpoch,
  selectHfInSync,
  selectHfKick,
  selectHfListReloadAllowed,
  selectHfOfflineEscape,
  selectHfRecordHint,
  selectHfRobot,
  selectHfStartBlock,
  selectHfSync,
  selectHfSyncFailed,
} from '../hfTokenSelectors';
import { BREAKER_MAX_WRITES, BREAKER_WINDOW_MS, WRITE_SETTLE_MS } from '../syncDecision';
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
      clearedFp: null, awaitingOwnFp: null, awaitingUntil: null, lastMessage: null, autoWrites: [],
      breakerOpen: false,
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
      phase: 'idle', failures: 0, lastOwnFp: A, clearedFp: null, lastMessage: null,
    });
  });

  it('after a successful write the reconcile pauses until the robot\'s state has caught up', () => {
    const pushed = reducer(initial(), syncPushed({ fp: A }, 50_000));
    expect(pushed.sync.nextAttemptAt).toBe(50_000 + WRITE_SETTLE_MS);
    const cleared = reducer(initial(), syncCleared({ fp: B }, 50_000));
    expect(cleared.sync.nextAttemptAt).toBe(50_000 + WRITE_SETTLE_MS);
    // ... and the pause ends the moment a state message shows my token.
    const seen = run(initial(), stored(A), syncPushed({ fp: A }, 50_000), robotMsg({ present: true, fp: A }));
    expect(seen.sync.nextAttemptAt).toBeNull();
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

describe('no „taken over" from a state older than my push (review c, 2026-10-04)', () => {
  const wrap = (hfToken, extra = {}) => ({ hfToken, ...extra });
  // The previous student's token B is in the slot; I push A at t = 50 s.
  const pushedOverB = () => run(initial(), stored(A), robotMsg({ present: true, fp: B }), syncPushed({ fp: A }, 50_000));

  it('a successful push waits to be SEEN, and keeps the dampener memory', () => {
    const s = pushedOverB();
    expect(s.sync).toMatchObject({ lastOwnFp: A, awaitingOwnFp: A, awaitingUntil: 50_000 + WRITE_SETTLE_MS });
    expect(selectHfAwaitingOwnPush(wrap(s))).toBe(true);
  });

  it('the stale slot right after the push is neither taken over on the card nor on the Aufnahme page', () => {
    const s = pushedOverB();
    expect(selectHfDecision(wrap(s))).toBe('push');
    expect(selectHfStartBlock(wrap(s))).toBe('transfer');
    // ... nor after another stale state message
    const again = reducer(s, robotMsg({ present: true, fp: B }));
    expect(selectHfDecision(wrap(again))).toBe('push');
    expect(selectHfStartBlock(wrap(again))).toBe('transfer');
  });

  it('a state that shows my token ends the wait, and a later foreign slot IS a takeover at once', () => {
    const seen = reducer(pushedOverB(), robotMsg({ present: true, fp: A }));
    expect(seen.sync).toMatchObject({ awaitingOwnFp: null, awaitingUntil: null, lastOwnFp: A });
    expect(selectHfDecision(wrap(seen))).toBe('noop');
    const taken = reducer(seen, robotMsg({ present: true, fp: B }));
    expect(selectHfDecision(wrap(taken))).toBe('taken_over');
    expect(selectHfStartBlock(wrap(taken))).toBe('taken_over');
  });

  it('a push never shown within the wait turns into the dampener, not into another write', () => {
    const s = pushedOverB();
    const early = reducer(s, syncSettleElapsed(50_000 + WRITE_SETTLE_MS - 1)); // a stale timer
    expect(early.sync.awaitingOwnFp).toBe(A);
    const late = reducer(s, syncSettleElapsed(50_000 + WRITE_SETTLE_MS));
    expect(late.sync).toMatchObject({ awaitingOwnFp: null, awaitingUntil: null, lastOwnFp: A });
    expect(selectHfDecision(wrap(late))).toBe('taken_over');
  });

  it('every reset of the bookkeeping ends the wait', () => {
    for (const action of [syncCleared({ fp: B }), syncForceRetransfer(), accountRemoved(), signedOut(),
      identityChanged(), syncReset()]) {
      const s = reducer(pushedOverB(), action);
      expect(s.sync.awaitingOwnFp ?? null).toBeNull();
      expect(s.sync.awaitingUntil ?? null).toBeNull();
    }
  });

  it('settling with nothing awaited is a no-op', () => {
    const s = run(initial(), stored(A));
    expect(reducer(s, syncSettleElapsed(1)).sync).toEqual(s.sync);
  });
});

describe('the busy start block (review b, 2026-10-04)', () => {
  const wrap = (hfToken, extra = {}) => ({ hfToken, ...extra });

  it('a robot that refuses for now (recording / transfer) is "busy", not "transfer"', () => {
    const s = run(initial(), stored(A), robotMsg({ present: true, fp: B, busy: true }));
    expect(selectHfDecision(wrap(s))).toBe('wait');
    expect(selectHfStartBlock(wrap(s))).toBe('busy');
    const empty = run(initial(), stored(A), robotMsg({ busy: true }));
    expect(selectHfStartBlock(wrap(empty))).toBe('busy');
  });

  it('busy wins over a failure count (nothing is tried while busy), taken over wins over busy', () => {
    const failed = run(initial(), stored(A), robotMsg({ present: true, fp: B, busy: true }),
      syncFailed({}), syncFailed({}));
    expect(selectHfStartBlock(wrap(failed))).toBe('busy');
    const taken = run(initial(), stored(A), robotMsg({ present: true, fp: A }),
      robotMsg({ present: true, fp: B, busy: true }));
    expect(selectHfStartBlock(wrap(taken))).toBe('taken_over');
  });

  it('in sync while busy blocks nothing', () => {
    const s = run(initial(), stored(A), robotMsg({ present: true, fp: A, busy: true }));
    expect(selectHfStartBlock(wrap(s))).toBeNull();
  });
});

describe('selectHfRecordHint — the non-blocking Aufnahme hint (owner decision S2)', () => {
  const signedIn = { isLoading: false, isAuthenticated: true };
  const offline = { isLoading: false, isAuthenticated: false };
  const wrap = (hfToken, auth = signedIn, extra = {}) => ({ hfToken, auth, ...extra });
  const accepting = (state) => reducer(state, robotMsg({ accepts: true }));

  it('names an account state that answers nothing a decision could use', () => {
    expect(selectHfRecordHint(wrap(accepting(run(initial(), accountFailed('error')))))).toBe('error');
    expect(selectHfRecordHint(wrap(accepting(run(initial(), accountFailed('unavailable')))))).toBe('unavailable');
    expect(selectHfRecordHint(wrap(accepting(run(initial(), accountFailed('unsupported')))))).toBe('unsupported');
  });

  it('says nothing for a usable account state or one still loading', () => {
    for (const s of [accepting(run(initial(), stored(A))), accepting(run(initial(), accountLoaded({ status: 'none' }))),
      accepting(initial()), accepting(run(initial(), accountLoading()))]) {
      expect(selectHfRecordHint(wrap(s))).toBeNull();
    }
  });

  it('needs a robot known to take a personal token (except offline)', () => {
    const err = run(initial(), accountFailed('error'));
    expect(selectHfRecordHint(wrap(err))).toBeNull();                                  // robot unknown
    expect(selectHfRecordHint(wrap(reducer(err, robotMsg({ accepts: false }))))).toBeNull();
  });

  it('the offline login escape is its own hint, robot state or not', () => {
    expect(selectHfOfflineEscape({ auth: offline })).toBe(true);
    expect(selectHfOfflineEscape({ auth: { isLoading: true, isAuthenticated: false } })).toBe(false);
    expect(selectHfOfflineEscape({ auth: signedIn })).toBe(false);
    expect(selectHfOfflineEscape({})).toBe(false);
    expect(selectHfRecordHint(wrap(initial(), offline))).toBe('offline');
  });

  it('only while the Benutzer-ID list on screen is empty (review g)', () => {
    // The hint says why there is NO Benutzer-ID. A list loaded earlier stays
    // on screen (state.ui.hfUserList) and is still usable, so the hint would
    // contradict it: it says nothing then, in every state it would name.
    const err = accepting(run(initial(), accountFailed('error')));
    expect(selectHfRecordHint(wrap(err, signedIn, { ui: { hfUserList: ['anna'] } }))).toBeNull();
    expect(selectHfRecordHint(wrap(initial(), offline, { ui: { hfUserList: ['anna', 'schule'] } }))).toBeNull();
    // an empty, absent or malformed list is "no Benutzer-ID": the hint stays
    expect(selectHfRecordHint(wrap(err, signedIn, { ui: { hfUserList: [] } }))).toBe('error');
    expect(selectHfRecordHint(wrap(err, signedIn, { ui: {} }))).toBe('error');
    expect(selectHfRecordHint(wrap(err, signedIn, { ui: { hfUserList: null } }))).toBe('error');
    expect(selectHfRecordHint(wrap(initial(), offline, { ui: { hfUserList: [] } }))).toBe('offline');
  });

  it('never without the slice, and never under a claimed Jetson', () => {
    expect(selectHfRecordHint({ auth: offline })).toBeNull();
    const err = accepting(run(initial(), accountFailed('error')));
    expect(selectHfRecordHint(wrap(err, signedIn, { jetson: { status: 'connected' } }))).toBeNull();
    expect(selectHfRecordHint(wrap(initial(), offline, { jetson: { status: 'connected' } }))).toBeNull();
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
