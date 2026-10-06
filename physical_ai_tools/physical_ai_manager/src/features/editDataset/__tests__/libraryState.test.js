// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The page's library copy (spec §J.4.1, H-2, U-3, T-1, §C1): replies never go
// backwards, a hub=0 reply never replaces the hub view, a foreign hub part is
// dropped, and the crashed card needs a reply sent after the newest busy change.

import { describe, expect, it } from 'vitest';
import {
  applyLibraryReply, busyChanges, cardBusyKind, cardPhase, emptyLibrary, hubEntryLacksNumbers, libraryCards,
  onlineCardsWithoutNumbers, stampBusyChange, syncAfterLocalEdit,
} from '../model/libraryState';

const FP = 'fp-lena';

const local = (id, patch = {}) => ({
  id, ns: id.split('/')[0], name: id.split('/')[1], state: 'ok', meta_digest: `d-${id}`,
  total_episodes: 10, hint_episodes: 2, modified_at: '2026-10-03T14:12:00Z', ...patch,
});

const hub1 = (locals, entries, sync, hubPatch = {}) => ({
  v: 1,
  robot_type: 'omx_f',
  local: locals,
  hub: {
    state: 'ok', token_fp: FP, account: 'lena', fetched_at: 'x', hidden_count: 0, complete_ns: ['lena'],
    entries, ...hubPatch,
  },
  sync,
});

const hub0 = (locals, sync) => ({ v: 1, robot_type: 'omx_f', local: locals, hub: { state: 'skipped' }, sync });

describe('applyLibraryReply — H-2: a hub=0 reply never replaces the hub view', () => {
  const first = applyLibraryReply(emptyLibrary(), hub1(
    [local('lena/omx_f_a'), local('lena/omx_f_b')],
    [{ id: 'lena/omx_f_a', head: 'h1', private: false }, { id: 'lena/omx_f_becher', head: 'h9', private: true }],
    {
      'lena/omx_f_a': { state: 'current', reason: null, head: 'h1' },
      'lena/omx_f_b': { state: 'local', reason: null, head: null },
      'lena/omx_f_becher': { state: 'online', reason: null, head: 'h9' },
    },
  ), { seq: 1, hub: true, ids: null }, { accountFp: FP, nowMs: 1000 });

  it('takes the hub=1 reply whole', () => {
    expect(Object.keys(first.local).sort()).toEqual(['lena/omx_f_a', 'lena/omx_f_b']);
    expect(first.hub.state).toBe('ok');
    expect(Object.keys(first.hub.entries).sort()).toEqual(['lena/omx_f_a', 'lena/omx_f_becher']);
    expect(first.sync['lena/omx_f_a'].state).toBe('current');
    expect(first.hubAt).toBe(1000);
  });

  it('a later hub=0 reply keeps every badge and the „Nur online" card', () => {
    const next = applyLibraryReply(first, hub0(
      [local('lena/omx_f_a', { hint_episodes: 3 }), local('lena/omx_f_b')],
      {
        'lena/omx_f_a': { state: 'unknown', reason: 'not_asked', head: null },
        'lena/omx_f_b': { state: 'unknown', reason: 'not_asked', head: null },
      },
    ), { seq: 2, hub: false, ids: null }, { accountFp: FP });
    expect(next.sync['lena/omx_f_a'].state).toBe('current');
    expect(next.sync['lena/omx_f_b'].state).toBe('local');
    expect(next.sync['lena/omx_f_becher'].state).toBe('online');
    expect(next.hub.entries['lena/omx_f_becher']).toBeTruthy();
    expect(next.local['lena/omx_f_a'].hint_episodes).toBe(3); // the hints DO come in
    const cards = libraryCards(next, ['lena']);
    expect(cards.map((c) => c.id).sort()).toEqual(['lena/omx_f_a', 'lena/omx_f_b', 'lena/omx_f_becher']);
  });

  it('… except a dataset whose meta_digest changed (its sync from the hub=0 reply)', () => {
    const next = applyLibraryReply(first, hub0(
      [local('lena/omx_f_a', { meta_digest: 'new' }), local('lena/omx_f_b')],
      { 'lena/omx_f_a': { state: 'changed', reason: null, head: 'h1' } },
    ), { seq: 2, hub: false, ids: null }, { accountFp: FP });
    expect(next.sync['lena/omx_f_a'].state).toBe('changed');
  });

  // T2-5: an online card the hub could not read just now (V2-10) keeps its
  // `unknown/unreachable` verdict through the 5 s hint poll — never „Nur online".
  it('… and an online card\'s hub verdict too, unknown/unreachable included (T2-5)', () => {
    const withUnreadable = applyLibraryReply(first, hub1(
      [local('lena/omx_f_a'), local('lena/omx_f_b')],
      [{ id: 'lena/omx_f_a', head: 'h1', private: false }, {
        id: 'lena/omx_f_becher', head: 'h9', private: true, total_episodes: null, duration_s: null,
      }],
      {
        'lena/omx_f_a': { state: 'current', reason: null, head: 'h1' },
        'lena/omx_f_b': { state: 'local', reason: null, head: null },
        'lena/omx_f_becher': { state: 'unknown', reason: 'unreachable', head: 'h9' },
      },
    ), { seq: 2, hub: true, ids: null }, { accountFp: FP, nowMs: 2000 });
    const polled = applyLibraryReply(withUnreadable, hub0(
      [local('lena/omx_f_a'), local('lena/omx_f_b', { hint_episodes: 1 })],
      {},
    ), { seq: 3, hub: false, ids: null }, { accountFp: FP });
    expect(polled.sync['lena/omx_f_becher']).toEqual({ state: 'unknown', reason: 'unreachable', head: 'h9' });
    const becher = libraryCards(polled, ['lena']).find((c) => c.id === 'lena/omx_f_becher');
    expect(becher.sync.state).toBe('unknown');
  });

  it('… and one gone locally falls back to „Nur online" when the hub lists it, else is dropped', () => {
    const next = applyLibraryReply(first, hub0([], {}), { seq: 2, hub: false, ids: null }, { accountFp: FP });
    expect(next.local['lena/omx_f_a']).toBeUndefined();
    expect(next.sync['lena/omx_f_a']).toEqual({ state: 'online', reason: null, head: 'h1' });
    expect(next.sync['lena/omx_f_b']).toBeUndefined();
  });
});

describe('applyLibraryReply — rule 1: an older reply never replaces a newer one (U-3)', () => {
  it('per dataset, by request number, whatever order the replies arrive in', () => {
    const newer = applyLibraryReply(emptyLibrary(), hub1([local('lena/omx_f_a')], [], {}), { seq: 7, hub: true, ids: ['lena/omx_f_a'] }, { accountFp: FP });
    const older = applyLibraryReply(newer, hub1([local('lena/omx_f_a', { state: 'in_session' })], [], {}), { seq: 5, hub: true, ids: null }, { accountFp: FP });
    expect(older.local['lena/omx_f_a'].state).toBe('ok');
    expect(older.entrySeq['lena/omx_f_a']).toBe(7);
  });
});

describe('applyLibraryReply — rule 3: a hub part read with another token is dropped', () => {
  it('no online entries, verdicts that need the hub read unknown, local changes stay', () => {
    const lib = applyLibraryReply(emptyLibrary(), hub1(
      [local('lena/omx_f_a'), local('lena/omx_f_b')],
      [{ id: 'x/omx_f_c', head: 'h' }],
      {
        'lena/omx_f_a': { state: 'current', reason: null, head: 'h1' },
        'lena/omx_f_b': { state: 'conflict', reason: null, head: 'h2' },
        'x/omx_f_c': { state: 'online', reason: null, head: 'h' },
      },
      { token_fp: 'fp-someone-else' },
    ), { seq: 1, hub: true, ids: null }, { accountFp: FP });
    expect(lib.hub.state).toBe('token_changed');
    expect(lib.hub.entries).toEqual({});
    expect(lib.sync['lena/omx_f_a']).toEqual({ state: 'unknown', reason: 'not_asked', head: null });
    expect(lib.sync['lena/omx_f_b'].state).toBe('changed');
    expect(lib.sync['x/omx_f_c']).toBeUndefined();
  });

  it('a failed hub listing keeps its state for the banner', () => {
    const lib = applyLibraryReply(emptyLibrary(), hub1([local('lena/omx_f_a')], [], {
      'lena/omx_f_a': { state: 'unknown', reason: 'unreachable', head: null },
    }, { state: 'unreachable', token_fp: FP }), { seq: 1, hub: true, ids: null }, { accountFp: FP });
    expect(lib.hub.state).toBe('unreachable');
    expect(lib.sync['lena/omx_f_a'].state).toBe('unknown');
  });

  it('V2-9: the robot\'s `unreachable` reason survives an unusable hub part (its tooltip is the unreachable sentence)', () => {
    const failed = applyLibraryReply(emptyLibrary(), hub1([local('lena/omx_f_a')], [], {
      'lena/omx_f_a': { state: 'unknown', reason: 'unreachable', head: null },
    }, { state: 'unreachable', token_fp: FP }), { seq: 1, hub: true, ids: null }, { accountFp: FP });
    expect(failed.sync['lena/omx_f_a']).toEqual({ state: 'unknown', reason: 'unreachable', head: null });
    // the same for an ids reply
    const ids = applyLibraryReply(emptyLibrary(), hub1([local('lena/omx_f_a')], [], {
      'lena/omx_f_a': { state: 'unknown', reason: 'unreachable', head: null },
    }, { state: 'unreachable', token_fp: FP }), { seq: 2, hub: true, ids: ['lena/omx_f_a'] }, { accountFp: FP });
    expect(ids.sync['lena/omx_f_a'].reason).toBe('unreachable');
    // a verdict made with ANOTHER token says nothing about this account: not asked
    const foreign = applyLibraryReply(emptyLibrary(), hub1([local('lena/omx_f_a'), local('lena/omx_f_b')], [], {
      'lena/omx_f_a': { state: 'current', reason: null, head: 'h' },
      'lena/omx_f_b': { state: 'unknown', reason: 'not_visible', head: null },
    }, { token_fp: 'fp-someone-else' }), { seq: 1, hub: true, ids: null }, { accountFp: FP });
    expect(foreign.sync['lena/omx_f_a'].reason).toBe('not_asked');
    expect(foreign.sync['lena/omx_f_b'].reason).toBe('not_asked');
  });
});

describe('an ids reply touches only its datasets', () => {
  it('replaces their local entry, sync and hub entry', () => {
    const base = applyLibraryReply(emptyLibrary(), hub1(
      [local('lena/omx_f_a'), local('lena/omx_f_b')],
      [{ id: 'lena/omx_f_a', head: 'h1' }, { id: 'lena/omx_f_b', head: 'h2' }],
      { 'lena/omx_f_a': { state: 'current', head: 'h1' }, 'lena/omx_f_b': { state: 'current', head: 'h2' } },
    ), { seq: 1, hub: true, ids: null }, { accountFp: FP });
    const next = applyLibraryReply(base, hub1(
      [local('lena/omx_f_b', { meta_digest: 'n' })],
      [{ id: 'lena/omx_f_b', head: 'h3' }],
      { 'lena/omx_f_b': { state: 'newer', head: 'h3' } },
    ), { seq: 2, hub: true, ids: ['lena/omx_f_b'] }, { accountFp: FP });
    expect(next.sync['lena/omx_f_a'].state).toBe('current');
    expect(next.sync['lena/omx_f_b']).toEqual({ state: 'newer', reason: null, head: 'h3' });
    expect(next.hub.entries['lena/omx_f_b'].head).toBe('h3');
    expect(next.hub.entries['lena/omx_f_a'].head).toBe('h1');
  });
});

describe('the crashed card (H-1, T-1, U-3)', () => {
  const crashed = local('lena/omx_f_k', { state: 'in_session' });

  it('busy changes: a kind that disappeared; a kind that appeared, or another order, is none (C-2)', () => {
    expect(busyChanges({ a: ['record'] }, { a: ['upload'] })).toEqual(['a']);
    expect(busyChanges({ a: ['upload'] }, {})).toEqual(['a']);
    expect(busyChanges({}, { a: ['record'] })).toEqual([]);
    expect(busyChanges({ a: ['record'] }, { a: ['record'] })).toEqual([]);
    // a Start waited for the upload; the upload ended, the recording began
    expect(busyChanges({ a: ['record', 'upload'] }, { a: ['record'] })).toEqual(['a']);
    expect(busyChanges({ a: ['record'] }, { a: ['record', 'upload'] })).toEqual([]);
    expect(busyChanges({ a: ['record', 'upload'] }, { a: ['record', 'upload'] })).toEqual([]);
  });

  it('cardBusyKind: a transfer or an edit beside `record` is what runs (a Start still waits for it)', () => {
    expect(cardBusyKind(['record', 'upload'])).toBe('upload');
    expect(cardBusyKind(['upload', 'record'])).toBe('upload');
    expect(cardBusyKind(['record'])).toBe('record');
    expect(cardBusyKind(['edit'])).toBe('edit');
    expect(cardBusyKind(['record', 'download'])).toBe('download');
    expect(cardBusyKind([])).toBeNull();
    expect(cardBusyKind(undefined)).toBeNull();
  });

  it('a live recording is never the crashed card — in either wire order', () => {
    expect(cardPhase(crashed, { busyKinds: ['record'], stateSeen: true, stamp: 3, entrySeq: 4 })).toBe('live');
    expect(cardPhase(crashed, { busyKinds: ['upload', 'record'], stateSeen: true, stamp: 3, entrySeq: 4 })).toBe('live');
  });

  it('before the first daten_state message an in_session card is neutral', () => {
    expect(cardPhase(crashed, { busyKinds: [], stateSeen: false, stamp: undefined, entrySeq: 4 })).toBe('refreshing');
  });

  it('crashed ONLY from a reply whose request was sent after the newest busy change', () => {
    expect(cardPhase(crashed, { busyKinds: [], stateSeen: true, stamp: 5, entrySeq: 6 })).toBe('crashed');
    expect(cardPhase(crashed, { busyKinds: [], stateSeen: true, stamp: 5, entrySeq: 5 })).toBe('refreshing');
    expect(cardPhase(crashed, { busyKinds: [], stateSeen: true, stamp: 5, entrySeq: 2 })).toBe('refreshing');
  });

  it('any card waiting for its re-fetch after a busy change is neutral', () => {
    expect(cardPhase(local('lena/omx_f_a'), { busyKinds: ['upload'], stateSeen: true, stamp: 5, entrySeq: 5 })).toBe('refreshing');
    expect(cardPhase(local('lena/omx_f_a'), { busyKinds: ['upload'], stateSeen: true, stamp: 5, entrySeq: 6 })).toBeNull();
  });

  it('two replies in reverse order: never crashed from the late old one', () => {
    let lib = stampBusyChange(emptyLibrary(), ['lena/omx_f_k'], 5);
    // the reply to request 7 (sent after the change: the marker is gone)
    lib = applyLibraryReply(lib, hub1([local('lena/omx_f_k')], [], {}), { seq: 7, hub: true, ids: ['lena/omx_f_k'] }, { accountFp: FP });
    // the late reply to request 4 (sent before the change: still in_session)
    lib = applyLibraryReply(lib, hub0([crashed], {}), { seq: 4, hub: false, ids: null }, { accountFp: FP });
    const entry = lib.local['lena/omx_f_k'];
    expect(entry.state).toBe('ok');
    expect(cardPhase(entry, { busyKinds: [], stateSeen: true, stamp: lib.stamps['lena/omx_f_k'], entrySeq: lib.entrySeq['lena/omx_f_k'] })).toBeNull();
  });
});

describe('syncAfterLocalEdit (V2-5): a local edit without the robot\'s re-read', () => {
  it('a synced copy becomes changed, a newer one a conflict; local and unknown stay', () => {
    expect(syncAfterLocalEdit('current')).toBe('changed');
    expect(syncAfterLocalEdit('changed')).toBe('changed');
    expect(syncAfterLocalEdit('newer')).toBe('conflict');
    expect(syncAfterLocalEdit('conflict')).toBe('conflict');
    expect(syncAfterLocalEdit('local')).toBe('local');
    expect(syncAfterLocalEdit('unknown')).toBe('unknown');
    expect(syncAfterLocalEdit(undefined)).toBe('unknown');
  });
});

describe('online cards without their numbers (V2-14)', () => {
  it('an ids reply\'s hub entry has none; the whole list\'s has them', () => {
    expect(hubEntryLacksNumbers({ id: 'a', head: 'h' })).toBe(true);
    expect(hubEntryLacksNumbers({ id: 'a', head: 'h', total_episodes: null })).toBe(true);
    expect(hubEntryLacksNumbers({ id: 'a', head: 'h', total_episodes: 0 })).toBe(false);
    expect(hubEntryLacksNumbers(null)).toBe(false);
  });

  it('only ids that are online-only now AND lack numbers', () => {
    const lib = {
      local: { 'lena/omx_f_l': local('lena/omx_f_l') },
      hub: { entries: {
        'lena/omx_f_l': { id: 'lena/omx_f_l', head: 'h' },
        'lena/omx_f_gone': { id: 'lena/omx_f_gone', head: 'h' },
        'lena/omx_f_full': { id: 'lena/omx_f_full', head: 'h', total_episodes: 4 },
      } },
    };
    expect(onlineCardsWithoutNumbers(lib, ['lena/omx_f_l', 'lena/omx_f_gone', 'lena/omx_f_full', 'lena/omx_f_x']))
      .toEqual(['lena/omx_f_gone']);
    expect(onlineCardsWithoutNumbers(emptyLibrary(), ['a'])).toEqual([]);
  });
});
