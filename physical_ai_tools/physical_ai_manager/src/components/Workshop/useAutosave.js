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
 * @param {string|null} options.scopeKey - extra namespace (e.g. user id)
 *   so two students sharing a browser don't see each other's autosave.
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

  const storageKey = scopeKey ? `${STORAGE_KEY}:${scopeKey}` : STORAGE_KEY;

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
      toast.error(DE.AUTOSAVE_QUOTA_FULL, { id: 'autosave-too-big' });
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
