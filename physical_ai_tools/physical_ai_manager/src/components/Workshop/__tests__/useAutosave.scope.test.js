/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The autosave bucket's NAMESPACE, and only that.
//
// A German school runs EduBotics on Windows student PCs under ONE shared Windows
// account: one WebView2 profile, one IndexedDB, many students. The student app is
// fully usable without a cloud login — only Training and Inferenz gate on one —
// so `scopeKey` (the Supabase user id) is frequently null, and the old
// `scopeKey ? `${STORAGE_KEY}:${scopeKey}` : STORAGE_KEY` fallback put EVERY such
// session into the SAME bucket. The next student's Roboter Studio restored the
// previous one's workspace on mount.
//
// Four properties are pinned, and they fail for different reasons:
//   1. the SIGNED-IN path is unchanged — the fix must not cost a student who does
//      log in their crash recovery;
//   2. two sessions with no user id get DIFFERENT buckets;
//   3. ONE session keeps ONE bucket across a reload, because surviving a reload
//      is what autosave is FOR;
//   4. the pre-namespacing bare bucket is deleted, exactly once and by NAME.

import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const idb = vi.hoisted(() => ({
  get: vi.fn(async () => undefined),
  set: vi.fn(async () => undefined),
  del: vi.fn(async () => undefined),
}));

vi.mock('idb-keyval', () => ({
  get: idb.get,
  set: idb.set,
  del: idb.del,
}));

vi.mock('react-hot-toast', () => ({
  default: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

const BARE_KEY = 'edubotics:workshop:autosave';
const SESSION_SCOPE_KEY = 'edubotics_workshop_autosave_session';

// A workspace stand-in: the hook only needs the change-listener pair and, for a
// save, Blockly.serialization. These tests never trigger a save.
function fakeWorkspace() {
  return { addChangeListener: vi.fn(), removeChangeListener: vi.fn() };
}

/** Import a FRESH copy of the module, i.e. simulate a new document. */
async function freshModule() {
  vi.resetModules();
  return import('../useAutosave');
}

/** The key the hook actually reads on mount. */
async function mountedKey(useAutosave, scopeKey) {
  idb.get.mockClear();
  renderHook(() => useAutosave({ workspace: fakeWorkspace(), scopeKey }));
  expect(idb.get).toHaveBeenCalledTimes(1);
  return idb.get.mock.calls[0][0];
}

describe('autosave bucket namespace', () => {
  beforeEach(() => {
    sessionStorage.clear();
    idb.get.mockClear();
    idb.set.mockClear();
    idb.del.mockClear();
  });

  it('leaves the signed-in bucket byte-identical', async () => {
    // The fix is about the FALLBACK. A student who signs in must keep reading and
    // writing exactly the bucket they had before, or the change costs them the
    // crash recovery it exists to protect.
    const { useAutosave } = await freshModule();
    expect(await mountedKey(useAutosave, 'aaaa-1111')).toBe(`${BARE_KEY}:aaaa-1111`);
  });

  it('never uses the bare key for an un-signed-in session', async () => {
    const { useAutosave } = await freshModule();
    const key = await mountedKey(useAutosave, null);
    expect(key).not.toBe(BARE_KEY);
    expect(key.startsWith(`${BARE_KEY}:`)).toBe(true);
    // The suffix is the session id, so the bucket is nameable only from inside
    // this session.
    expect(key).toBe(`${BARE_KEY}:${sessionStorage.getItem(SESSION_SCOPE_KEY)}`);
  });

  it('gives the next browser session a DIFFERENT bucket', async () => {
    // THE regression: student A works without signing in, closes the window,
    // student B opens it. A new window is a new sessionStorage, so B must not be
    // able to name A's bucket.
    const first = await freshModule();
    const keyA = await mountedKey(first.useAutosave, null);
    sessionStorage.clear();          // window closed -> session gone
    const second = await freshModule();
    const keyB = await mountedKey(second.useAutosave, null);
    expect(keyB).not.toBe(keyA);
  });

  it('keeps ONE bucket across a reload of the same session', async () => {
    // Crash recovery is the whole point, and a reload is the commonest way to
    // reach it (useVersionCheck reloads the app on a new buildId). A module-level
    // id would satisfy the test above and fail this one.
    const first = await freshModule();
    const keyA = await mountedKey(first.useAutosave, null);
    const second = await freshModule();   // new document, same sessionStorage
    expect(await mountedKey(second.useAutosave, null)).toBe(keyA);
  });

  it('mints the session id once and reuses it', async () => {
    const { autosaveSessionScope } = await freshModule();
    const id = autosaveSessionScope();
    expect(id).toBeTruthy();
    expect(autosaveSessionScope()).toBe(id);
    expect(sessionStorage.getItem(SESSION_SCOPE_KEY)).toBe(id);
  });

  it('falls back to a per-document id when sessionStorage throws', async () => {
    // A WebView2 with storage disabled throws on the bare property access — the
    // codebase's standing assumption for every storage touch. Losing crash
    // recovery on reload is acceptable there; crashing the only Blockly surface,
    // or sharing one bucket again, is not.
    const original = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    Object.defineProperty(globalThis, 'sessionStorage', {
      configurable: true,
      get() { throw new Error('storage disabled'); },
    });
    try {
      const { autosaveSessionScope, useAutosave } = await freshModule();
      const id = autosaveSessionScope();
      expect(id).toBeTruthy();
      expect(autosaveSessionScope()).toBe(id);
      expect(await mountedKey(useAutosave, null)).toBe(`${BARE_KEY}:${id}`);
    } finally {
      if (original) Object.defineProperty(globalThis, 'sessionStorage', original);
      else delete globalThis.sessionStorage;
    }
  });

  it('deletes the pre-namespacing bucket, by name and only that', async () => {
    // Fielded machines carry a bare bucket written by every signed-out session
    // that ever ran there. Nothing can name it any more, so it is unreachable
    // work rather than live state — and it is removed by its LITERAL name, never
    // by an idb-keyval keys() sweep, which could match a real `:<user id>`
    // bucket and destroy work.
    const { useAutosave } = await freshModule();
    renderHook(() => useAutosave({ workspace: fakeWorkspace(), scopeKey: 'u1' }));
    expect(idb.del.mock.calls.map(([k]) => k)).toEqual([BARE_KEY]);
  });

  it('does not sweep when autosave is disabled or has no workspace', async () => {
    const { useAutosave } = await freshModule();
    renderHook(() => useAutosave({ workspace: null, scopeKey: 'u1' }));
    renderHook(() => useAutosave({ workspace: fakeWorkspace(), enabled: false }));
    expect(idb.del).not.toHaveBeenCalled();
    expect(idb.get).not.toHaveBeenCalled();
  });
});
