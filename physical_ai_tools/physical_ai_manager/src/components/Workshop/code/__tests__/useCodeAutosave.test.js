/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code document's crash-recovery draft, at the level WorkshopPage cannot
// reach: the bucket's NAME, and the rule that the bucket exists only while the
// open document is code.
//
// The namespace rule is `useAutosave`'s, and the reason is the same: a German
// school runs EduBotics on Windows student PCs under ONE shared Windows
// account — one WebView2 profile, one IndexedDB, many students. The signed-in
// path uses the Supabase user id; the signed-out one (since the login gate,
// the „Ohne Anmeldung fortfahren" offline escape alone) uses the browser
// session, never a shared bare name.

import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import toast from 'react-hot-toast';
import { useCodeAutosave } from '../useCodeAutosave';
import { DE } from '../../blocks/messages_de';

const idb = vi.hoisted(() => ({
  get: vi.fn(async () => undefined),
  set: vi.fn(async () => undefined),
  del: vi.fn(async () => undefined),
}));
vi.mock('idb-keyval', () => ({ get: idb.get, set: idb.set, del: idb.del }));
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

const FILES = { 'main.py': 'import robot\n' };

beforeEach(() => {
  idb.get.mockReset();
  idb.get.mockImplementation(async () => undefined);
  idb.set.mockReset();
  idb.set.mockImplementation(async () => undefined);
  idb.del.mockClear();
  toast.error.mockClear();
  try { sessionStorage.clear(); } catch (_) { /* jsdom without storage */ }
});

describe('the bucket name', () => {
  it('is namespaced by the signed-in student', async () => {
    renderHook(() => useCodeAutosave({ language: 'python', files: FILES, scopeKey: 'user-7' }));
    await waitFor(() => expect(idb.set).toHaveBeenCalled());
    expect(idb.set.mock.calls[0][0]).toBe('edubotics:workshop:code-autosave:user-7');
  });

  it('falls back to the browser session, never to a shared bare name', async () => {
    renderHook(() => useCodeAutosave({ language: 'python', files: FILES, scopeKey: null }));
    await waitFor(() => expect(idb.set).toHaveBeenCalled());
    const key = idb.set.mock.calls[0][0];
    expect(key.startsWith('edubotics:workshop:code-autosave:')).toBe(true);
    expect(key).not.toBe('edubotics:workshop:code-autosave');
    expect(key.length).toBeGreaterThan('edubotics:workshop:code-autosave:'.length);
  });
});

describe('what is written', () => {
  it('is the language and the files, with a timestamp', async () => {
    renderHook(() => useCodeAutosave({ language: 'java', files: { 'Main.java': 'class Main {}' }, scopeKey: 'u' }));
    await waitFor(() => expect(idb.set).toHaveBeenCalled());
    const payload = idb.set.mock.calls[0][1];
    expect(payload.state).toEqual({ language: 'java', files: { 'Main.java': 'class Main {}' } });
    expect(typeof payload.ts).toBe('number');
  });

  it('is nothing at all while the open document is not code', async () => {
    renderHook(() => useCodeAutosave({ language: '', files: null, scopeKey: 'u' }));
    await waitFor(() => expect(idb.get).toHaveBeenCalled());
    // Past the debounce, not merely before it: asserting on the next tick
    // passes on a hook that has no guard at all, because the write is 750 ms
    // away either way.
    await new Promise((r) => { setTimeout(r, 900); });
    expect(idb.set).not.toHaveBeenCalled();
  });

  it('says so in German when the browser store is full', async () => {
    const err = new Error('quota');
    err.name = 'QuotaExceededError';
    idb.set.mockImplementation(async () => { throw err; });
    renderHook(() => useCodeAutosave({ language: 'python', files: FILES, scopeKey: 'u' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(
      DE.AUTOSAVE_QUOTA_FULL, { id: 'autosave-quota' },
    ));
    idb.set.mockImplementation(async () => undefined);
  });

  it('is nothing while the hook is disabled', async () => {
    renderHook(() => useCodeAutosave({ language: 'python', files: FILES, scopeKey: 'u', enabled: false }));
    await new Promise((r) => { setTimeout(r, 900); });
    expect(idb.set).not.toHaveBeenCalled();
    expect(idb.get).not.toHaveBeenCalled();
  });
});

describe('the restore', () => {
  it('hands a stored draft to the caller, once', async () => {
    idb.get.mockImplementation(async () => ({ state: { language: 'python', files: FILES }, ts: 1 }));
    const onRestore = vi.fn();
    const { rerender } = renderHook(
      () => useCodeAutosave({ language: '', files: null, scopeKey: 'u', onRestore }),
    );
    await waitFor(() => expect(onRestore).toHaveBeenCalledTimes(1));
    expect(onRestore).toHaveBeenCalledWith({ language: 'python', files: FILES });
    rerender();
    rerender();
    expect(onRestore).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['no payload', undefined],
    ['an empty record', {}],
    ['a language this editor does not have', { state: { language: 'rust', files: FILES } }],
    ['files that are not a project', { state: { language: 'python', files: ['main.py'] } }],
    ['no files at all', { state: { language: 'python' } }],
  ])('refuses %s rather than opening it as a document', async (_label, cached) => {
    idb.get.mockImplementation(async () => cached);
    const onRestore = vi.fn();
    renderHook(() => useCodeAutosave({ language: '', files: null, scopeKey: 'u', onRestore }));
    await waitFor(() => expect(idb.get).toHaveBeenCalled());
    await new Promise((r) => { setTimeout(r, 0); });
    expect(onRestore).not.toHaveBeenCalled();
  });

  it('settles even when the READ throws synchronously (no IndexedDB at all)', async () => {
    // idb-keyval's getDB() throws SYNCHRONOUSLY in a browser with no
    // IndexedDB — a WebView2 with storage disabled, the standing assumption
    // behind every storage touch here — so it never produces a promise for
    // `.catch()` to attach to. Unhandled, the restore never settles, and the
    // bucket could then never be dropped either: the observable damage is the
    // MISSING delete below, not a visible exception.
    idb.get.mockImplementation(() => { throw new ReferenceError('indexedDB is not defined'); });
    const onRestore = vi.fn();

    renderHook(() => useCodeAutosave({ language: '', files: null, scopeKey: 'u', onRestore }));

    await waitFor(() => expect(idb.del).toHaveBeenCalledWith('edubotics:workshop:code-autosave:u'));
    expect(onRestore).not.toHaveBeenCalled();
  });

  it('reports a WRITE that throws synchronously instead of letting it escape', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    idb.set.mockImplementation(() => { throw new ReferenceError('indexedDB is not defined'); });

    renderHook(() => useCodeAutosave({ language: 'python', files: FILES, scopeKey: 'u' }));

    await waitFor(() => expect(spy).toHaveBeenCalledWith(
      'useCodeAutosave: idb-set failed', expect.any(ReferenceError),
    ));
    spy.mockRestore();
  });

  it('survives a read that rejects and still lets the bucket be dropped', async () => {
    // The delete waits on the restore attempt SETTLING. A read that threw must
    // therefore still settle it, or a corrupt bucket could never be cleared.
    idb.get.mockImplementation(async () => { throw new Error('IndexedDB kaputt'); });
    const onRestore = vi.fn();
    renderHook(() => useCodeAutosave({ language: '', files: null, scopeKey: 'u', onRestore }));
    await waitFor(() => expect(idb.del).toHaveBeenCalledWith('edubotics:workshop:code-autosave:u'));
    expect(onRestore).not.toHaveBeenCalled();
  });
});

describe('the bucket exists only while the open document is code', () => {
  it('is dropped once a non-code document is open', async () => {
    renderHook(() => useCodeAutosave({ language: '', files: null, scopeKey: 'u' }));
    await waitFor(() => expect(idb.del).toHaveBeenCalledWith('edubotics:workshop:code-autosave:u'));
  });

  it('is NOT dropped before the restore has had its chance to read it', async () => {
    // `language` is '' until the restore (or the cloud hydrate) says
    // otherwise, so a delete that did not wait would erase the draft it is
    // about to be handed.
    let release;
    idb.get.mockImplementation(() => new Promise((resolve) => { release = () => resolve(undefined); }));
    renderHook(() => useCodeAutosave({ language: '', files: null, scopeKey: 'u' }));
    await waitFor(() => expect(idb.get).toHaveBeenCalled());
    expect(idb.del).not.toHaveBeenCalled();
    release();
    await waitFor(() => expect(idb.del).toHaveBeenCalled());
  });

  it('is kept while a code document is open', async () => {
    renderHook(() => useCodeAutosave({ language: 'python', files: FILES, scopeKey: 'u' }));
    await waitFor(() => expect(idb.set).toHaveBeenCalled());
    expect(idb.del).not.toHaveBeenCalled();
  });
});
