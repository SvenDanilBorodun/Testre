// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// useDatenSession (spec §J.B step 3): link tokens over rosbridge, the library
// with its merge rule (H-2), the re-poll of hints with hub=0, the re-fetch of
// a dataset whose busy entry changed (T-1 a) and the crashed card's sequence
// rule (U-3), driven with a mocked /daten/command and a mocked fetch.

import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import useDatenSession, { HINT_POLL_MS } from '../hooks/useDatenSession';
import { cardPhase } from '../model/libraryState';

const FP = 'fp-lena';

const local = (id, patch = {}) => ({
  id, ns: id.split('/')[0], name: id.split('/')[1], state: 'ok', meta_digest: `d-${id}`,
  total_episodes: 10, hint_episodes: 2, modified_at: '2026-10-03T14:12:00Z', ...patch,
});

function libraryReply(url, world) {
  const u = new URL(url, 'http://h');
  const hub = u.searchParams.get('hub') === '1';
  const ids = u.searchParams.get('ids') ? u.searchParams.get('ids').split(',') : null;
  const locals = world.local.filter((e) => !ids || ids.includes(e.id));
  return {
    v: 1,
    robot_type: 'omx_f',
    local: locals,
    hub: hub ? { state: 'ok', token_fp: FP, account: 'lena', complete_ns: ['lena'], hidden_count: 0, entries: world.hub.filter((e) => !ids || ids.includes(e.id)) } : { state: 'skipped' },
    sync: Object.fromEntries(Object.entries(hub ? world.syncHub : world.sync0).filter(([id]) => !ids || ids.includes(id))),
  };
}

let world;
let requests;
let gates; // url substring → deferred, to hold a reply back

function okResponse(body) {
  return { ok: true, status: 200, headers: { get: () => null }, json: () => Promise.resolve(body) };
}

beforeEach(() => {
  requests = [];
  gates = [];
  world = {
    local: [local('lena/omx_f_a'), local('lena/omx_f_k', { state: 'in_session' })],
    hub: [{ id: 'lena/omx_f_a', head: 'h1', private: false }, { id: 'lena/omx_f_becher', head: 'h9', private: true }],
    syncHub: {
      'lena/omx_f_a': { state: 'current', reason: null, head: 'h1' },
      'lena/omx_f_k': { state: 'current', reason: null, head: 'hk' },
      'lena/omx_f_becher': { state: 'online', reason: null, head: 'h9' },
    },
    sync0: {
      'lena/omx_f_a': { state: 'unknown', reason: 'not_asked', head: null },
      'lena/omx_f_k': { state: 'unknown', reason: 'not_asked', head: null },
    },
  };
  global.fetch = vi.fn((url) => {
    requests.push(url);
    const reply = libraryReply(url, world);
    const gate = gates.find((g) => !g.used && url.includes(g.match));
    if (gate) {
      gate.used = true;
      return gate.promise.then(() => okResponse(reply));
    }
    return Promise.resolve(okResponse(reply));
  });
});

afterEach(() => {
  vi.useRealTimers();
  delete global.fetch;
});

const command = vi.fn(async (action, args) => {
  if (action === 'link') {
    const tokens = {};
    (args.datasets || []).forEach((id) => { tokens[id] = `ds-${id}`; });
    return { ok: true, code: '', message: '', result: { ttl_s: 1800, library_token: 'LIB', tokens, missing: [] }, oldImage: false, unreachable: false };
  }
  return { ok: false, code: 'invalid', message: '', result: {} };
});

function hold(match) {
  let release;
  const promise = new Promise((r) => { release = r; });
  gates.push({ match, promise, used: false });
  return release;
}

function mount(initialState = { received: false, payload: null }) {
  return renderHook((props) => useDatenSession(props), {
    initialProps: {
      enabled: true, command, namespaces: ['lena'], inSync: true, accountFp: FP, datenState: initialState,
    },
  });
}

const phaseOf = (result, id, busyKind = null) => {
  const lib = result.current.lib;
  return cardPhase(lib.local[id], {
    busyKinds: busyKind ? [busyKind] : [], stateSeen: lib.stateSeen, stamp: lib.stamps[id], entrySeq: lib.entrySeq[id],
  });
};

const state = (busy) => ({ received: true, payload: { v: 1, seq: 1, busy, jobs: [], transfer: null } });

describe('useDatenSession', () => {
  it('mints a library token over rosbridge and loads the library with the hub (token in sync)', async () => {
    const { result } = mount();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(command).toHaveBeenCalledWith('link', { library: true, datasets: [] });
    expect(requests[0]).toBe('/daten-api/v1/lib/LIB/library?ns=lena&hub=1');
    expect(result.current.lib.sync['lena/omx_f_a'].state).toBe('current');
    // every listed local dataset gets a ds token (its thumbnail)
    await waitFor(() => expect(result.current.peekDsToken('lena/omx_f_a')).toBe('ds-lena/omx_f_a'));
  });

  it('H-2: a hub=0 reply arriving after the hub=1 reply turns no badge unknown and keeps „Nur online"', async () => {
    const { result } = mount();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    await act(async () => { await result.current.loadLibrary({ hub: false }); });
    expect(requests[requests.length - 1]).toBe('/daten-api/v1/lib/LIB/library?ns=lena&hub=0');
    expect(result.current.lib.sync['lena/omx_f_a'].state).toBe('current');
    expect(result.current.lib.sync['lena/omx_f_becher'].state).toBe('online');
  });

  it('re-polls with hub=0 every 5 s while a card\'s hints are still being computed', async () => {
    world.local = [local('lena/omx_f_a', { hint_episodes: null })];
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { result } = mount();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    const before = requests.length;
    world.local = [local('lena/omx_f_a', { hint_episodes: 3 })];
    await act(async () => { await vi.advanceTimersByTimeAsync(HINT_POLL_MS + 10); });
    await waitFor(() => expect(result.current.lib.local['lena/omx_f_a'].hint_episodes).toBe(3));
    expect(requests.slice(before).some((u) => u.includes('hub=0'))).toBe(true);
  });

  it('before the first daten_state message an in_session card is neutral; the first message re-fetches it', async () => {
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(phaseOf(result, 'lena/omx_f_k')).toBe('refreshing');
    const before = requests.length;
    rerender({ enabled: true, command, namespaces: ['lena'], inSync: true, accountFp: FP, datenState: state([]) });
    await waitFor(() => expect(requests.length).toBeGreaterThan(before));
    expect(requests[requests.length - 1]).toBe('/daten-api/v1/lib/LIB/library?ns=lena&hub=1&ids=lena%2Fomx_f_k');
    await waitFor(() => expect(phaseOf(result, 'lena/omx_f_k')).toBe('crashed'));
  });

  it('T-1 a: record → upload → gone re-fetches at each change and stays neutral until the reply', async () => {
    world.local = [local('lena/omx_f_w', { state: 'in_session' })];
    const props = (busy) => ({ enabled: true, command, namespaces: ['lena'], inSync: true, accountFp: FP, datenState: state(busy) });
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    rerender(props([{ id: 'lena/omx_f_w', kind: 'record' }]));
    await waitFor(() => expect(phaseOf(result, 'lena/omx_f_w', 'record')).toBe('live'));

    // the session ends: the marker goes, the auto-upload starts
    world.local = [local('lena/omx_f_w')];
    const release = hold('ids=lena%2Fomx_f_w');
    rerender(props([{ id: 'lena/omx_f_w', kind: 'upload' }]));
    await waitFor(() => expect(requests.some((u) => u.endsWith('ids=lena%2Fomx_f_w'))).toBe(true));
    expect(phaseOf(result, 'lena/omx_f_w', 'upload')).toBe('refreshing');
    await act(async () => { release(); });
    await waitFor(() => expect(phaseOf(result, 'lena/omx_f_w', 'upload')).toBeNull());

    const n = requests.filter((u) => u.endsWith('ids=lena%2Fomx_f_w')).length;
    rerender(props([]));
    await waitFor(() => expect(requests.filter((u) => u.endsWith('ids=lena%2Fomx_f_w')).length).toBe(n + 1));
  });

  it('C-2: the same busy kinds in another order are no change (no re-fetch, no neutral flash)', async () => {
    world.local = [local('lena/omx_f_w')];
    const props = (busy) => ({ enabled: true, command, namespaces: ['lena'], inSync: true, accountFp: FP, datenState: state(busy) });
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    rerender(props([{ id: 'lena/omx_f_w', kind: 'record' }, { id: 'lena/omx_f_w', kind: 'upload' }]));
    await waitFor(() => expect(result.current.lib.stateSeen).toBe(true));
    const n = requests.filter((u) => u.endsWith('ids=lena%2Fomx_f_w')).length;
    rerender(props([{ id: 'lena/omx_f_w', kind: 'upload' }, { id: 'lena/omx_f_w', kind: 'record' }]));
    await act(async () => { await Promise.resolve(); });
    expect(requests.filter((u) => u.endsWith('ids=lena%2Fomx_f_w')).length).toBe(n);
    expect(result.current.lib.stamps['lena/omx_f_w']).toBeUndefined();
    // the upload ends (the waiting Start records now): THAT is a change
    rerender(props([{ id: 'lena/omx_f_w', kind: 'record' }]));
    await waitFor(() => expect(requests.filter((u) => u.endsWith('ids=lena%2Fomx_f_w')).length).toBe(n + 1));
  });

  it('U-3: a reply to a request sent before the change never shows the crashed card', async () => {
    world.local = [local('lena/omx_f_w', { state: 'in_session' })];
    const props = (busy) => ({ enabled: true, command, namespaces: ['lena'], inSync: true, accountFp: FP, datenState: state(busy) });
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    rerender(props([{ id: 'lena/omx_f_w', kind: 'record' }]));
    await waitFor(() => expect(result.current.lib.stateSeen).toBe(true));
    // a hint poll sent while recording, held back …
    const releaseOld = hold('hub=0');
    let oldDone;
    act(() => { oldDone = result.current.loadLibrary({ hub: false }); });
    // … the session stops by a crash: the busy entry disappears, the marker stays
    const releaseNew = hold('ids=lena%2Fomx_f_w');
    rerender(props([]));
    await waitFor(() => expect(requests.some((u) => u.endsWith('ids=lena%2Fomx_f_w'))).toBe(true));
    // the OLD reply lands first: neutral, never crashed
    await act(async () => { releaseOld(); await oldDone; });
    expect(phaseOf(result, 'lena/omx_f_w')).toBe('refreshing');
    // the reply to the request sent after the change: crashed
    await act(async () => { releaseNew(); });
    await waitFor(() => expect(phaseOf(result, 'lena/omx_f_w')).toBe('crashed'));
  });

  it('a token error re-mints once and retries', async () => {
    let first = true;
    global.fetch = vi.fn((url) => {
      requests.push(url);
      if (first) {
        first = false;
        return Promise.resolve({ ok: false, status: 403, headers: { get: () => null }, json: () => Promise.resolve({ error: 'token_expired' }) });
      }
      return Promise.resolve(okResponse(libraryReply(url, world)));
    });
    const { result } = mount();
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(command.mock.calls.filter(([a, args]) => a === 'link' && args.datasets.length === 0).length).toBeGreaterThanOrEqual(2);
  });

  it('the old image (no /daten/command) is its own state', async () => {
    const old = vi.fn(async () => ({ ok: false, code: 'old_image', message: '', result: {}, oldImage: true, unreachable: false }));
    const { result } = renderHook(() => useDatenSession({
      enabled: true, command: old, namespaces: ['lena'], inSync: true, accountFp: FP, datenState: { received: false },
    }));
    await waitFor(() => expect(result.current.status).toBe('old_image'));
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('a sidecar that does not answer is „sidecar down" (retried)', async () => {
    global.fetch = vi.fn(() => Promise.resolve({ ok: false, status: 502, headers: { get: () => null }, json: () => Promise.reject(new Error('x')) }));
    const { result } = mount();
    await waitFor(() => expect(result.current.status).toBe('sidecar_down'));
  });
});
