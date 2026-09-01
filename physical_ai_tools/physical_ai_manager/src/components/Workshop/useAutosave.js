/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import * as Blockly from 'blockly/core';
import { get as idbGet, set as idbSet, del as idbDel } from 'idb-keyval';
import toast from 'react-hot-toast';
import { DE } from './blocks/messages_de';

const STORAGE_KEY = 'edubotics:workshop:autosave';
// Names the BROWSER SESSION, so the un-signed-in autosave bucket is per-session
// instead of one bucket shared by everyone who ever sat at the PC. Since the
// student login gate (utils/authGate) that bucket is reached by ONE path only —
// the „Ohne Anmeldung fortfahren" offline escape — but it is still reachable,
// so the fallback stays. See autosaveSessionScope.
const SESSION_SCOPE_KEY = 'edubotics_workshop_autosave_session';
const SAVE_INTERVAL_MS = 15_000;
const DEBOUNCE_MS = 750;
// Mirror the server-side validate_blockly_json byte ceiling so we
// don't autosave a payload that the cloud API will later reject.
const MAX_JSON_BYTES = 256 * 1024;

function debounce(fn, wait) {
  let t = null;
  const wrapped = (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => {
      t = null;
      fn(...args);
    }, wait);
  };
  wrapped.cancel = () => {
    if (t) {
      clearTimeout(t);
      t = null;
    }
  };
  return wrapped;
}

function nowMs() {
  return Date.now();
}

// Last resort for a browser that throws on the bare sessionStorage property
// access (a WebView2 with storage disabled — the codebase's standing assumption
// for every storage touch). Per DOCUMENT rather than per session: a reload then
// loses crash recovery, which is strictly better than sharing one bucket.
let memoryScopeId = null;

function newScopeId() {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch (e) {
    /* no crypto — fall through to the time+random form below */
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * An opaque id naming THIS browser session, minted once and kept in
 * sessionStorage.
 *
 * WHY. A German school runs EduBotics on Windows student PCs under ONE shared
 * Windows account: one WebView2 profile, one IndexedDB, many students. The
 * student app used to be fully usable without a cloud login — only Training and
 * Inferenz gated on one — so `scopeKey` (the Supabase user id) was frequently
 * null, and the old `scopeKey ? … : STORAGE_KEY` fallback put EVERY such session
 * into the SAME bucket. The next student's Roboter Studio then restored the
 * previous one's workspace on mount.
 *
 * The student login gate (utils/authGate) NARROWED that population without
 * removing it: `scopeKey` is null only on the „Ohne Anmeldung fortfahren"
 * offline escape now, which a student reaches after a login attempt has proven
 * the auth service unreachable. Rarer, still live — do not delete the
 * fallback.
 *
 * sessionStorage is the carrier because of what has to survive and what must
 * not. Crash recovery — the entire point of autosave — has to survive a RELOAD,
 * which a module-level variable would not; and the bucket must not survive the
 * WINDOW, which localStorage would. sessionStorage is scoped to exactly the tab,
 * which for the student surface is the WebView2 window `EduBotics.exe --webview`
 * spawns, so closing the app ends the session and the next student starts clean.
 *
 * RESIDUAL, stated because it is inherent to "per session" and not to this
 * implementation: two students who hand the PC over WITHOUT closing that window
 * share the session, hence the bucket. Recorded in docs/KNOWN-ISSUES.md.
 */
export function autosaveSessionScope() {
  try {
    const existing = sessionStorage.getItem(SESSION_SCOPE_KEY);
    if (existing) return existing;
    const minted = newScopeId();
    sessionStorage.setItem(SESSION_SCOPE_KEY, minted);
    return minted;
  } catch (e) {
    if (!memoryScopeId) memoryScopeId = newScopeId();
    return memoryScopeId;
  }
}

/**
 * Format a timestamp relative to now in German.
 *   < 5 s        → "gerade eben"
 *   < 60 s       → "vor X s"
 *   < 60 min     → "vor X min"
 *   otherwise    → ISO-ish locale time
 */
export function formatAutosaveAge(ts) {
  if (!ts) return DE.AUTOSAVE_NEVER;
  const ageMs = nowMs() - ts;
  if (ageMs < 5_000) return DE.AUTOSAVE_JUST_NOW;
  if (ageMs < 60_000) {
    return DE.AUTOSAVE_SECONDS_AGO.replace('%1', Math.round(ageMs / 1000));
  }
  if (ageMs < 60 * 60_000) {
    return DE.AUTOSAVE_MINUTES_AGO.replace('%1', Math.round(ageMs / 60_000));
  }
  try {
    return new Date(ts).toLocaleTimeString('de-DE');
  } catch (e) {
    return '';
  }
}

/**
 * Persist Blockly workspace JSON to IndexedDB. The hook returns a
 * status object the toolbar can render plus a manual-save callback
 * (e.g. when student presses Ctrl+S).
 *
 * @param {object} options
 * @param {Blockly.WorkspaceSvg | null} options.workspace
 * @param {boolean} options.enabled - false on cloud-only mode if you
 *   want to disable autosave (we still enable on cloud-only since the
 *   workflow JSON is the same shape).
 * @param {string|null} options.scopeKey - extra namespace (the Supabase user
 *   id) so two students sharing a browser don't see each other's autosave. When
 *   null — since the login gate, only a student on the „Ohne Anmeldung
 *   fortfahren" offline escape — the bucket is namespaced by
 *   `autosaveSessionScope()` instead, never shared.
 * @param {(json: object) => void} options.onRestore - called once on
 *   mount with the restored payload (caller can decide to apply it).
 */
export function useAutosave({
  workspace,
  enabled = true,
  scopeKey = null,
  onRestore = null,
} = {}) {
  const [lastSavedAt, setLastSavedAt] = useState(null);
  const [hasRestored, setHasRestored] = useState(false);
  const restoreCalledRef = useRef(false);
  const loadingFlagRef = useRef(false);

  // ALWAYS namespaced. The signed-in path is byte-identical to before
  // (`${STORAGE_KEY}:${supabase user id}`); the un-scoped case falls back to the
  // browser-session id rather than to the bare key, which every signed-out
  // session used to share. Kept on one line and mentioning STORAGE_KEY on
  // purpose: sessionScope.test.js's coverage scan resolves a namespaced key by
  // reading the literal consts its SINGLE-LINE initializer names, and refuses —
  // rather than silently skips — an initializer it cannot resolve.
  const storageKey = `${STORAGE_KEY}:${scopeKey || autosaveSessionScope()}`;

  // Save current workspace state. Called by debounced listener,
  // periodic timer, and manual save action.
  const save = useCallback(async () => {
    if (!enabled || !workspace) return;
    if (loadingFlagRef.current) return;
    let state;
    try {
      state = Blockly.serialization.workspaces.save(workspace);
    } catch (e) {
      console.error('useAutosave: serialize failed', e);
      return;
    }
    let serialized;
    try {
      serialized = JSON.stringify(state);
    } catch (e) {
      console.error('useAutosave: stringify failed', e);
      return;
    }
    // Measure UTF-8 bytes (what the server's 256KB cap actually
    // checks), not JS string-length, so we don't autosave a payload
    // that would be rejected by the server. Audit §J6.
    const utf8Bytes =
      typeof TextEncoder !== 'undefined'
        ? new TextEncoder().encode(serialized).length
        : serialized.length;
    if (utf8Bytes > MAX_JSON_BYTES) {
      // Audit §autosave-r1: distinguish "workflow exceeds the 256 KB
      // server cap" from "IndexedDB quota exceeded". They are two
      // different failure modes with two different remedies — telling
      // a student their browser is full when the actual issue is a
      // bloated workflow sends them down the wrong recovery path.
      toast.error(DE.AUTOSAVE_TOO_BIG, { id: 'autosave-too-big' });
      return;
    }
    try {
      await idbSet(storageKey, { state, ts: nowMs() });
      setLastSavedAt(nowMs());
    } catch (e) {
      const msg = (e && e.name) || '';
      if (msg === 'QuotaExceededError') {
        toast.error(DE.AUTOSAVE_QUOTA_FULL, { id: 'autosave-quota' });
      } else {
        console.error('useAutosave: idb-set failed', e);
      }
    }
  }, [enabled, workspace, storageKey]);

  // Restore on mount.
  useEffect(() => {
    if (!enabled || !workspace) return;
    if (restoreCalledRef.current) return;
    restoreCalledRef.current = true;

    // Reclaim the PRE-namespacing bucket. Fielded machines carry a bare
    // `edubotics:workshop:autosave` written by every signed-out session that
    // ever ran here, and `storageKey` above can no longer produce that name — so
    // it is unreachable work, not live state, and deleting it removes the
    // disclosure instead of destroying anything a student can still get to.
    // Exactly the ONE legacy name, deliberately not an `idb-keyval` keys()
    // enumeration: a pattern sweep over this namespace could match a real
    // `:<user id>` bucket and destroy work. Fire-and-forget so it cannot delay
    // the restore below.
    idbDel(STORAGE_KEY).catch(() => undefined);

    let cancelled = false;
    (async () => {
      try {
        const cached = await idbGet(storageKey);
        if (cancelled) return;
        if (!cached || !cached.state) {
          setHasRestored(true);
          return;
        }
        if (typeof onRestore === 'function') {
          // Defer to the parent so it can decide whether to clobber an
          // already-loaded server workflow with the autosaved version.
          onRestore(cached.state);
        } else {
          // No parent handler — we apply directly.
          loadingFlagRef.current = true;
          try {
            Blockly.serialization.workspaces.load(cached.state, workspace);
          } finally {
            loadingFlagRef.current = false;
          }
          toast(DE.AUTOSAVE_RESTORED, { icon: '💾' });
        }
        setLastSavedAt(cached.ts || null);
        setHasRestored(true);
      } catch (e) {
        console.error('useAutosave: idb-get failed', e);
        setHasRestored(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled, workspace, storageKey, onRestore]);

  // Wire the change listener + periodic timer.
  useEffect(() => {
    if (!enabled || !workspace) return undefined;
    const debouncedSave = debounce(() => {
      save();
    }, DEBOUNCE_MS);
    const listener = () => {
      if (loadingFlagRef.current) return;
      debouncedSave();
    };
    workspace.addChangeListener(listener);
    const interval = setInterval(() => {
      save();
    }, SAVE_INTERVAL_MS);
    return () => {
      workspace.removeChangeListener(listener);
      clearInterval(interval);
      debouncedSave.cancel();
    };
  }, [enabled, workspace, save]);

  const clearAutosave = useCallback(async () => {
    try {
      await idbDel(storageKey);
      setLastSavedAt(null);
    } catch (e) {
      console.error('useAutosave: idb-del failed', e);
    }
  }, [storageKey]);

  return { lastSavedAt, save, clearAutosave, hasRestored };
}
