// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Daten page's link to the robot's dataset sidecar (spec §B5, §J.4,
// §G2): link tokens minted over rosbridge (`/daten/command` action `link`,
// renewed at 2/3 of their TTL, re-minted once on a 403), the library with its
// merge rules (model/libraryState.js), the 5 s hint re-poll while a card's
// hints are still being computed (at most 2 min), the re-fetch of a dataset
// whose /edubotics/daten_state busy entry changed (T-1 a), and the per-dataset
// reads (summary, hubstate, episode data, hub probe).
//
// The page's states: 'old_image' when the robot has no `/daten/command`
// (§B10: nothing else of the tab is shown), 'sidecar_down' when `link`
// answered but the sidecar does not (retried every 5 s), 'ready'.

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import { MAX_LINK_DATASETS, TOKEN_TTL_S } from '../datenContract';
import { DatenHttpError, dsUrl, getJson, isTokenError, libUrl } from '../api/datenHttp';
import {
  applyLibraryReply, busyChanges, emptyLibrary, stampBusyChange,
} from '../model/libraryState';
import { busyKindsById } from './useDatenState';

export const HINT_POLL_MS = 5000;
export const HINT_POLL_MAX_MS = 120000;
export const SIDECAR_RETRY_MS = 5000;
export const IDS_REFETCH_DELAYS_MS = Object.freeze([2000, 4000, 8000]);
const RENEW_FRACTION = 2 / 3;

function reducer(state, action) {
  switch (action.type) {
    case 'reply':
      return applyLibraryReply(state, action.reply, action.req, action.opts);
    case 'stamp':
      return stampBusyChange(state, action.ids, action.seq);
    case 'state_seen':
      return state.stateSeen ? state : { ...state, stateSeen: true };
    case 'reset':
      return emptyLibrary();
    default:
      return state;
  }
}

const tokenFresh = (t, nowMs) => !!(t && t.token && t.expiresAt - nowMs > (1 - RENEW_FRACTION) * TOKEN_TTL_S * 1000);

/**
 * @param {object} p
 * @param {boolean} p.enabled the page may talk to the robot (signed in, linked, connected)
 * @param {(action: string, args: object) => Promise<object>} p.command useDatenCommand's runner
 * @param {string[]} p.namespaces own + group (useGroupNamespaces)
 * @param {boolean} p.inSync the robot holds this student's token (selectHfInSync)
 * @param {string|null} p.accountFp the stored token's fingerprint (selectHfAccount(s).fp)
 * @param {{received: boolean, payload: object|null}} p.datenState useDatenState's snapshot
 */
export default function useDatenSession({
  enabled, command, namespaces, inSync, accountFp, datenState, now = Date.now,
}) {
  const [lib, rawDispatch] = useReducer(reducer, undefined, emptyLibrary);
  // The library as of the LAST action, before React renders it: a caller that
  // awaited `loadLibrary` reads the reply it waited for through `getLib()`
  // (a `lib` captured by the page is one render behind — V2-5's toast once
  // read the state from before the edit). The same pure reducer runs here and
  // in React, in the same order, so both arrive at the same state.
  const latestRef = useRef(null);
  if (latestRef.current === null) latestRef.current = lib;
  const dispatch = useCallback((action) => {
    latestRef.current = reducer(latestRef.current, action);
    rawDispatch(action);
  }, []);
  const getLib = useCallback(() => latestRef.current, []);
  const [status, setStatus] = useState('idle');
  const [hubLoading, setHubLoading] = useState(false);
  const seqRef = useRef(0);
  const libTokenRef = useRef(null); // {token, expiresAt}
  const dsTokensRef = useRef(new Map()); // id → {token, expiresAt}
  const mintingRef = useRef(null);
  const aliveRef = useRef(true);
  const nsKey = (namespaces || []).join(',');
  const nsRef = useRef(namespaces || []);
  nsRef.current = namespaces || [];
  const fpRef = useRef(accountFp);
  fpRef.current = accountFp;
  const inSyncRef = useRef(inSync);
  inSyncRef.current = inSync;
  const libRef = useRef(lib);
  libRef.current = lib;
  const [tokenEpoch, setTokenEpoch] = useState(0);

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  // ---- link tokens ---------------------------------------------------------
  const mint = useCallback(async (datasets = []) => {
    const ids = [...new Set(datasets)].slice(0, MAX_LINK_DATASETS);
    const r = await command('link', { library: true, datasets: ids });
    if (!aliveRef.current) return null;
    if (r.oldImage) {
      setStatus('old_image');
      return null;
    }
    if (!r.ok) {
      if (r.unreachable) setStatus((s) => (s === 'ready' ? s : 'unreachable'));
      return null;
    }
    const ttl = Math.max(60, Number(r.result.ttl_s) || TOKEN_TTL_S) * 1000;
    const expiresAt = now() + ttl;
    if (r.result.library_token) libTokenRef.current = { token: r.result.library_token, expiresAt };
    const tokens = r.result.tokens && typeof r.result.tokens === 'object' ? r.result.tokens : {};
    Object.entries(tokens).forEach(([id, token]) => {
      if (typeof token === 'string' && token) dsTokensRef.current.set(id, { token, expiresAt });
    });
    setTokenEpoch((e) => e + 1);
    return r.result;
  }, [command, now]);

  const libToken = useCallback(async (force = false) => {
    if (!force && tokenFresh(libTokenRef.current, now())) return libTokenRef.current.token;
    if (!mintingRef.current) {
      mintingRef.current = mint([]).finally(() => { mintingRef.current = null; });
    }
    await mintingRef.current;
    return libTokenRef.current ? libTokenRef.current.token : null;
  }, [mint, now]);

  /** A fresh link token for dataset `id` (minted when missing or old, or `force`). */
  const dsToken = useCallback(async (id, force = false) => {
    const t = dsTokensRef.current.get(id);
    if (!force && tokenFresh(t, now())) return t.token;
    await mint([id]);
    const fresh = dsTokensRef.current.get(id);
    return fresh ? fresh.token : null;
  }, [mint, now]);

  /** The token already held for `id` (for a thumbnail's first src), or null. */
  const peekDsToken = useCallback((id) => {
    const t = dsTokensRef.current.get(id);
    return t ? t.token : null;
    // tokenEpoch: re-read after every mint
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tokenEpoch]);

  // Mint the ds tokens of every listed local dataset that has none (the
  // thumbnails need one each), in batches of MAX_LINK_DATASETS.
  const mintMissing = useCallback(async (ids) => {
    const missing = ids.filter((id) => !tokenFresh(dsTokensRef.current.get(id), now()));
    for (let i = 0; i < missing.length; i += MAX_LINK_DATASETS) {
      // eslint-disable-next-line no-await-in-loop
      await mint(missing.slice(i, i + MAX_LINK_DATASETS));
    }
  }, [mint, now]);

  // ---- the library ---------------------------------------------------------
  /**
   * One `library` request. `hub` asks Hugging Face too; `ids` restricts it to
   * those datasets (and their hub repos). Resolves true when a reply landed.
   */
  const loadLibrary = useCallback(async ({ hub = false, ids = null } = {}) => {
    const seq = ++seqRef.current;
    const ns = nsRef.current;
    if (!ns.length) return false;
    const req = { seq, hub: !!hub, ids: ids && ids.length ? ids : null };
    if (hub && !req.ids) setHubLoading(true);
    try {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        // eslint-disable-next-line no-await-in-loop
        const token = await libToken(attempt > 0);
        if (!token || !aliveRef.current) return false;
        try {
          // eslint-disable-next-line no-await-in-loop
          const reply = await getJson(libUrl(token, 'library', {
            ns, hub: hub ? 1 : 0, ids: req.ids,
          }));
          if (!aliveRef.current) return false;
          dispatch({ type: 'reply', reply, req, opts: { accountFp: fpRef.current, nowMs: now() } });
          setStatus('ready');
          const localIds = Array.isArray(reply.local) ? reply.local.map((e) => e.id).filter(Boolean) : [];
          mintMissing(localIds);
          return true;
        } catch (err) {
          if (err instanceof DatenHttpError && isTokenError(err.code) && attempt === 0) continue;
          if (err instanceof DatenHttpError && err.code === 'sidecar_down') {
            if (aliveRef.current) setStatus((s) => (s === 'old_image' ? s : 'sidecar_down'));
          }
          return false;
        }
      }
      return false;
    } finally {
      if (hub && !req.ids && aliveRef.current) setHubLoading(false);
    }
  }, [libToken, mintMissing, now, dispatch]);

  /** „Aktualisieren", opening the tab, after a job: the whole list, the hub too when the token is this student's. */
  const refresh = useCallback(() => loadLibrary({ hub: !!inSyncRef.current }), [loadLibrary]);

  /** `library?ids=…&hub=1` for these datasets (a changed busy entry, a job's outputs), retried on failure. */
  const refetchIds = useCallback((ids) => {
    const list = [...new Set((ids || []).filter(Boolean))];
    if (!list.length) return;
    let attempt = 0;
    const run = async () => {
      const ok = await loadLibrary({ hub: true, ids: list });
      if (!ok && aliveRef.current && attempt < IDS_REFETCH_DELAYS_MS.length) {
        const delay = IDS_REFETCH_DELAYS_MS[attempt];
        attempt += 1;
        setTimeout(run, delay);
      }
    };
    run();
  }, [loadLibrary]);

  // Open the tab: the first load (and again when the namespaces or the token's
  // ownership change).
  useEffect(() => {
    if (!enabled || !nsKey) return undefined;
    refresh();
    return undefined;
  }, [enabled, nsKey, inSync, refresh]);

  // The sidecar did not answer: try again every 5 s (§B10).
  useEffect(() => {
    if (!enabled || (status !== 'sidecar_down' && status !== 'unreachable')) return undefined;
    const id = setInterval(() => { refresh(); }, SIDECAR_RETRY_MS);
    return () => clearInterval(id);
  }, [enabled, status, refresh]);

  // Renew every token at 2/3 of its life.
  useEffect(() => {
    if (!enabled || status !== 'ready') return undefined;
    const id = setInterval(() => {
      const ids = [...dsTokensRef.current.keys()];
      mint(ids.slice(0, MAX_LINK_DATASETS));
    }, TOKEN_TTL_S * 1000 * RENEW_FRACTION);
    return () => clearInterval(id);
  }, [enabled, status, mint]);

  // ---- the hint re-poll (R-6, §J.4.1) --------------------------------------
  const pending = useMemo(
    () => Object.values(lib.local).some((e) => e.hint_episodes === null && e.state === 'ok'),
    [lib.local],
  );
  const pollStartRef = useRef(null);
  useEffect(() => {
    if (!enabled || status !== 'ready' || !pending) {
      if (!pending) pollStartRef.current = null;
      return undefined;
    }
    if (pollStartRef.current === null) pollStartRef.current = now();
    if (now() - pollStartRef.current > HINT_POLL_MAX_MS) return undefined;
    const id = setTimeout(() => { loadLibrary({ hub: false }); }, HINT_POLL_MS);
    return () => clearTimeout(id);
  }, [enabled, status, pending, lib, loadLibrary, now]);

  // ---- busy changes → re-fetch (T-1 a, U-3) -------------------------------
  const prevBusyRef = useRef(null);
  const payload = datenState ? datenState.payload : null;
  const received = !!(datenState && datenState.received);
  useEffect(() => {
    if (!enabled || !received) {
      if (!received) prevBusyRef.current = null;
      return;
    }
    const nextBusy = busyKindsById(payload);
    const shown = (id) => !!(libRef.current.local[id] || (libRef.current.hub && libRef.current.hub.entries[id]));
    let changed;
    if (prevBusyRef.current === null) {
      // The FIRST message of this page counts as a change for every in_session card.
      changed = Object.values(libRef.current.local).filter((e) => e.state === 'in_session').map((e) => e.id);
      dispatch({ type: 'state_seen' });
    } else {
      changed = busyChanges(prevBusyRef.current, nextBusy).filter(shown);
    }
    prevBusyRef.current = nextBusy;
    if (changed.length) {
      dispatch({ type: 'stamp', ids: changed, seq: seqRef.current });
      refetchIds(changed);
    }
  }, [enabled, received, payload, refetchIds, dispatch]);

  // A local dataset the library reports in_session AFTER the first message (a
  // card that appeared later) is stamped too, so it is judged by a fresh reply.
  useEffect(() => {
    if (!enabled || !lib.stateSeen) return;
    const unstamped = Object.values(lib.local)
      .filter((e) => e.state === 'in_session' && lib.stamps[e.id] === undefined)
      .map((e) => e.id);
    if (unstamped.length) {
      dispatch({ type: 'stamp', ids: unstamped, seq: seqRef.current });
      refetchIds(unstamped);
    }
  }, [enabled, lib.stateSeen, lib.local, lib.stamps, refetchIds, dispatch]);

  // Reset when the page is disabled (signed out, link lost).
  useEffect(() => {
    if (enabled) return;
    dispatch({ type: 'reset' });
    prevBusyRef.current = null;
    pollStartRef.current = null;
    setStatus((s) => (s === 'old_image' ? s : 'idle'));
  }, [enabled, dispatch]);

  // ---- per-dataset reads ---------------------------------------------------
  /** GET `/ds/<T>/<path>` with one re-mint on a token error. */
  const getDs = useCallback(async (id, path, { signal } = {}) => {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      // eslint-disable-next-line no-await-in-loop
      const token = await dsToken(id, attempt > 0);
      if (!token) throw new DatenHttpError('unavailable', 0);
      try {
        // eslint-disable-next-line no-await-in-loop
        return await getJson(dsUrl(token, path), { signal });
      } catch (err) {
        if (err instanceof DatenHttpError && isTokenError(err.code) && attempt === 0) continue;
        throw err;
      }
    }
    throw new DatenHttpError('token_invalid', 403);
  }, [dsToken]);

  const fetchSummary = useCallback((id, opts) => getDs(id, 'summary', opts), [getDs]);
  const fetchHubState = useCallback((id, opts) => getDs(id, 'hubstate', opts), [getDs]);
  const fetchEpisodeData = useCallback((id, i, opts) => getDs(id, `episode/${Number(i)}/data`, opts), [getDs]);

  /** `hub/probe?repo=` (D12's „Suchen"). */
  const probeRepo = useCallback(async (repo, { signal } = {}) => {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      // eslint-disable-next-line no-await-in-loop
      const token = await libToken(attempt > 0);
      if (!token) throw new DatenHttpError('unavailable', 0);
      try {
        // eslint-disable-next-line no-await-in-loop
        return await getJson(libUrl(token, 'hub/probe', { repo }), { signal });
      } catch (err) {
        if (err instanceof DatenHttpError && isTokenError(err.code) && attempt === 0) continue;
        throw err;
      }
    }
    throw new DatenHttpError('token_invalid', 403);
  }, [libToken]);

  return {
    status,
    lib,
    getLib,
    hubLoading,
    refresh,
    refetchIds,
    loadLibrary,
    dsToken,
    peekDsToken,
    fetchSummary,
    fetchHubState,
    fetchEpisodeData,
    probeRepo,
    seqRef,
  };
}
