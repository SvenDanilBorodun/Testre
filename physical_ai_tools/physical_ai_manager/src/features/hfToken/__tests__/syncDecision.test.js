// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The reconcile decision, as a table. `decideSync` is pure, so the whole
// contract is these 28 rows (one per case the audit enumerated), the start-block
// asserts, the backoff schedule, and a multi-client simulation that proves the
// taken-over dampener: several students sharing one robot slot settle in a
// handful of writes instead of overwriting each other for ever.

import {
  BACKOFF_MS,
  BREAKER_MAX_WRITES,
  BREAKER_WINDOW_MS,
  DECISION,
  FAILED_VISIBLE_AFTER,
  START_BLOCK,
  SYNC_WATCHDOG_MS,
  WAIT_RECHECK_MS,
  WRITE_SETTLE_MS,
  backoffDelayMs,
  decideSync,
  hfTokenStartBlock,
} from '../syncDecision';

const R = (o = {}) => ({ known: true, accepts: true, present: false, fp: null, busy: false, ...o });
const A = 'aaaaaaaaaaaaaaaa';
const B = 'bbbbbbbbbbbbbbbb';
const C = 'cccccccccccccccc';

describe('decideSync — the 28-row truth table', () => {
  const rows = [
    ['1 disabled', { enabled: false, robot: R(), accountStatus: 'stored', accountFp: A }, 'noop'],
    ['2 robot state unknown', { robot: { known: false }, accountStatus: 'stored', accountFp: A }, 'noop'],
    ['3 robot null', { robot: null, accountStatus: 'stored', accountFp: A }, 'noop'],
    ['4 Jetson / no slot (accepts false)', { robot: R({ accepts: false }), accountStatus: 'stored', accountFp: A }, 'noop'],
    ['5 account unknown', { robot: R({ present: true, fp: B }), accountStatus: 'unknown' }, 'noop'],
    ['6 account loading', { robot: R({ present: true, fp: B }), accountStatus: 'loading' }, 'noop'],
    ['7 account error', { robot: R({ present: true, fp: B }), accountStatus: 'error' }, 'noop'],
    ['8 account unsupported', { robot: R(), accountStatus: 'unsupported' }, 'noop'],
    ['9 account unavailable', { robot: R(), accountStatus: 'unavailable' }, 'noop'],
    ['10 stored, robot empty', { robot: R(), accountStatus: 'stored', accountFp: A }, 'push'],
    ['11 stored, robot empty, busy', { robot: R({ busy: true }), accountStatus: 'stored', accountFp: A }, 'wait'],
    ['12 stored, same fp', { robot: R({ present: true, fp: A }), accountStatus: 'stored', accountFp: A }, 'noop'],
    ['13 stored, same fp, busy', { robot: R({ present: true, fp: A, busy: true }), accountStatus: 'stored', accountFp: A }, 'noop'],
    ['14 stored, other fp (hand-over), never saw my own', { robot: R({ present: true, fp: B }), accountStatus: 'stored', accountFp: A }, 'push'],
    ['15 stored, other fp, busy', { robot: R({ present: true, fp: B, busy: true }), accountStatus: 'stored', accountFp: A }, 'wait'],
    ['16 other fp after I was in sync', { robot: R({ present: true, fp: B }), accountStatus: 'stored', accountFp: A, lastOwnFp: A }, 'taken_over'],
    ['17 taken over AND busy stays taken_over', { robot: R({ present: true, fp: B, busy: true }), accountStatus: 'stored', accountFp: A, lastOwnFp: A }, 'taken_over'],
    ['18 emptied after I was in sync', { robot: R(), accountStatus: 'stored', accountFp: A, lastOwnFp: A }, 'push'],
    ['19 stale older lastOwnFp', { robot: R({ present: true, fp: B }), accountStatus: 'stored', accountFp: A, lastOwnFp: C }, 'push'],
    ['20 present but fp null counts as empty', { robot: R({ present: true, fp: null }), accountStatus: 'stored', accountFp: A }, 'push'],
    ['21 none, robot empty', { robot: R(), accountStatus: 'none' }, 'noop'],
    ['22 none, robot holds a token', { robot: R({ present: true, fp: B }), accountStatus: 'none' }, 'clear'],
    ['23 none, holds a token, busy', { robot: R({ present: true, fp: B, busy: true }), accountStatus: 'none' }, 'wait'],
    ['24 none, a token I cleared before reappeared', { robot: R({ present: true, fp: B }), accountStatus: 'none', clearedFp: B }, 'taken_over'],
    ['25 none, a different token than I cleared', { robot: R({ present: true, fp: A }), accountStatus: 'none', clearedFp: B }, 'clear'],
    ['26 unusable, robot empty', { robot: R(), accountStatus: 'unusable', accountFp: A }, 'noop'],
    ['27 unusable, robot has the stored fp', { robot: R({ present: true, fp: A }), accountStatus: 'unusable', accountFp: A }, 'noop'],
    ['28 unusable, robot has another token', { robot: R({ present: true, fp: B }), accountStatus: 'unusable', accountFp: A }, 'clear'],
  ];

  it('has exactly the 28 rows', () => {
    expect(rows).toHaveLength(28);
  });

  it.each(rows)('%s', (_name, input, expected) => {
    // `enabled` is true unless the row says otherwise (row 1).
    expect(decideSync({ enabled: true, ...input })).toBe(expected);
  });

  it('answers noop to a call with no arguments', () => {
    expect(decideSync()).toBe(DECISION.NOOP);
  });

  it('never decides anything for a robot that did not say it takes a personal token', () => {
    for (const accountStatus of ['stored', 'none', 'unusable', 'unknown']) {
      for (const robot of [null, { known: false }, R({ accepts: false }), R({ accepts: null })]) {
        expect(decideSync({ enabled: true, robot, accountStatus, accountFp: A })).toBe(DECISION.NOOP);
      }
    }
  });
});

describe('hfTokenStartBlock', () => {
  const sb = (o) => hfTokenStartBlock({ accountFp: A, ...o });

  it('never blocks on an account state that has not answered', () => {
    for (const accountStatus of ['unknown', 'loading', 'error', 'unsupported', 'unavailable']) {
      expect(sb({ accountStatus, robot: R() })).toBeNull();
      expect(sb({ accountStatus, robot: R({ present: true, fp: B }) })).toBeNull();
    }
  });

  it('never blocks on a robot that did not say it takes a personal token', () => {
    expect(sb({ accountStatus: 'none', robot: { known: false } })).toBeNull();
    expect(sb({ accountStatus: 'none', robot: null })).toBeNull();
    expect(sb({ accountStatus: 'none', robot: R({ accepts: false }) })).toBeNull();
    expect(sb({ accountStatus: 'stored', robot: { known: false } })).toBeNull();
  });

  it('none: blocks, whatever the robot holds', () => {
    expect(sb({ accountStatus: 'none', robot: R() })).toBe(START_BLOCK.NONE);
    expect(sb({ accountStatus: 'none', robot: R({ present: true, fp: B }) })).toBe(START_BLOCK.NONE);
  });

  it('unusable: blocks unless the robot still holds the stored token', () => {
    expect(sb({ accountStatus: 'unusable', robot: R() })).toBe(START_BLOCK.UNUSABLE);
    expect(sb({ accountStatus: 'unusable', robot: R({ present: true, fp: A }) })).toBeNull();
  });

  it('stored: free once the robot holds the account\'s token, "transfer" until then', () => {
    expect(sb({ accountStatus: 'stored', robot: R({ present: true, fp: A }) })).toBeNull();
    expect(sb({ accountStatus: 'stored', robot: R() })).toBe(START_BLOCK.TRANSFER);
    expect(sb({ accountStatus: 'stored', robot: R({ present: true, fp: B }) })).toBe(START_BLOCK.TRANSFER);
  });

  it('stored: "failed" when the sync phase says so, "taken_over" when the slot was taken from me', () => {
    const other = R({ present: true, fp: B });
    expect(sb({ accountStatus: 'stored', robot: other, syncPhase: 'failed' })).toBe(START_BLOCK.FAILED);
    expect(sb({ accountStatus: 'stored', robot: other, lastOwnFp: A })).toBe(START_BLOCK.TAKEN_OVER);
    // taken over wins over failed: the retry button is the remedy for both, but
    // the sentence must name the cause.
    expect(sb({ accountStatus: 'stored', robot: other, lastOwnFp: A, syncPhase: 'failed' }))
      .toBe(START_BLOCK.TAKEN_OVER);
  });
});

describe('the retry schedule', () => {
  it('backs off 2 / 5 / 15 / 30 s and then stays at 30 s', () => {
    expect([1, 2, 3, 4, 5, 9].map(backoffDelayMs)).toEqual([2000, 5000, 15000, 30000, 30000, 30000]);
    expect(BACKOFF_MS).toEqual([2000, 5000, 15000, 30000]);
  });

  it('treats 0 or a negative count as the first step', () => {
    expect(backoffDelayMs(0)).toBe(2000);
    expect(backoffDelayMs(-3)).toBe(2000);
  });

  it('pins the other tuning constants the hook and the card rely on', () => {
    expect(WAIT_RECHECK_MS).toBe(5000);
    expect(WRITE_SETTLE_MS).toBe(2500);
    expect(FAILED_VISIBLE_AFTER).toBe(2);
    expect(BREAKER_MAX_WRITES).toBe(3);
    expect(BREAKER_WINDOW_MS).toBe(60000);
    expect(SYNC_WATCHDOG_MS).toBe(15000);
  });
});

// ── several students, ONE slot ───────────────────────────────────────────────
// Each round every client looks at the slot and acts on `decideSync`. `lastOwnFp`
// is set on a successful push AND whenever the client observes its own token in
// the slot; `clearedFp` after a successful clear. Setting `lastOwnFp` only on
// observation (not on push) diverged (80 writes): that is the reason for both.
describe('several students sharing one robot slot settle instead of flapping', () => {
  function simulate({ clients, slot = null, maxRounds = 40 }) {
    let writes = 0;
    for (let round = 0; round < maxRounds; round += 1) {
      let acted = false;
      for (const c of clients) {
        const robot = R({ present: slot !== null, fp: slot });
        const decision = decideSync({
          enabled: true,
          robot,
          accountStatus: c.status,
          accountFp: c.fp,
          lastOwnFp: c.lastOwnFp,
          clearedFp: c.clearedFp,
        });
        if (decision === 'noop' && c.status === 'stored' && slot === c.fp) c.lastOwnFp = c.fp;
        if (decision === 'push') {
          slot = c.fp;
          c.lastOwnFp = c.fp;
          writes += 1;
          acted = true;
        }
        if (decision === 'clear') {
          c.clearedFp = slot;
          slot = null;
          writes += 1;
          acted = true;
        }
      }
      if (!acted) return { writes, slot, diverged: false };
    }
    return { writes, slot, diverged: true };
  }
  const client = (status, fp) => ({ status, fp, lastOwnFp: null, clearedFp: null });

  it('A and B both stored: 2 writes', () => {
    const r = simulate({ clients: [client('stored', A), client('stored', B)] });
    expect(r.diverged).toBe(false);
    expect(r.writes).toBe(2);
  });

  it('A stored and B without a token: 3 writes', () => {
    const r = simulate({ clients: [client('stored', A), client('none', null)] });
    expect(r.diverged).toBe(false);
    expect(r.writes).toBe(3);
  });

  it('the slot holds B, a B-less client and A: 4 writes', () => {
    const r = simulate({ clients: [client('none', null), client('stored', A)], slot: B });
    expect(r.diverged).toBe(false);
    expect(r.writes).toBe(4);
  });

  it('three clients (A, B, none): 6 writes', () => {
    const r = simulate({ clients: [client('stored', A), client('stored', B), client('none', null)] });
    expect(r.diverged).toBe(false);
    expect(r.writes).toBe(6);
  });

  it('without the dampener the same two students would never settle', () => {
    // The negative control that gives the numbers above meaning: with `lastOwnFp`
    // never remembered, A and B overwrite each other for as long as we look.
    let slot = null;
    let writes = 0;
    const clients = [client('stored', A), client('stored', B)];
    for (let round = 0; round < 40; round += 1) {
      for (const c of clients) {
        const d = decideSync({
          enabled: true,
          robot: R({ present: slot !== null, fp: slot }),
          accountStatus: c.status,
          accountFp: c.fp,
        });
        if (d === 'push') {
          slot = c.fp;
          writes += 1;
        }
      }
    }
    // 80 writes in 40 rounds: every client overwrites the other every round.
    expect(writes).toBe(80);
  });
});
