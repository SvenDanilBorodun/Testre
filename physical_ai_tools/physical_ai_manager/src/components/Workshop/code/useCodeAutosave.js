/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The crash-recovery draft of a CODE document — the counterpart of
// `../useAutosave`, which cannot serve one.
//
// WHY A SECOND HOOK. `useAutosave` is keyed on the Blockly workspace: `save()`
// returns at `if (!enabled || !workspace) return;`, the restore effect has the
// same guard, and the change listener is registered ON the workspace. A code
// workflow renders `CodeWorkspace` instead of `BlocklyWorkspace`, whose unmount
// hands the page `onWorkspaceReady(null)` — so for a Python or Java student
// every one of those paths was a no-op and nothing was ever written. A reload,
// a WebView2 crash, „Neu ▾" or picking another workflow lost every edit since
// the last „Speichern". Making the Blockly hook polymorphic would have put a
// second document shape through a serializer path built for exactly one; this
// hook owns the code shape and leaves that one untouched.
//
// ITS OWN BUCKET, and the bucket exists only while the open document IS code.
// Two buckets under one namespace could otherwise both answer on mount, and a
// student who moved on to blocks would be pulled back into their old Python
// program. Deleting on a non-code document is therefore part of the contract,
// and it waits for the restore attempt to SETTLE — deleting first would erase
// the draft before it could be read (`language` is '' until the restore or the
// cloud hydrate says otherwise).
//
// SHARED LIMITATION, stated so it is not a surprise: like the Blockly draft,
// this one is offered back only when NO cloud workflow is selected. After a
// reload that is every time (`selectedWorkflowId` is not persisted), so the
// edits do come back — but as an UNSAVED document, and saving it then creates
// a new workflow instead of updating the one they came from. That is the
// Blockly path's behaviour too; closing it means matching a draft to a
// workflow id, which neither hook does today.
//
// The namespace rule is `useAutosave`'s, reused rather than restated: the
// Supabase user id when signed in, `autosaveSessionScope()` (the browser
// session) on the „Ohne Anmeldung fortfahren" offline escape. One shared
// Windows account is one WebView2 profile and one IndexedDB for a whole class.
// `utils/sessionScope.js` classifies the key.

import { useEffect, useState } from 'react';
import { get as idbGet, set as idbSet, del as idbDel } from 'idb-keyval';
import toast from 'react-hot-toast';
import { DE } from '../blocks/messages_de';
import { autosaveSessionScope } from '../useAutosave';
import { serializeState } from '../sammlung/destinationStore';
import { isCodeLanguage } from './codeProject';

const CODE_STORAGE_KEY = 'edubotics:workshop:code-autosave';
const DEBOUNCE_MS = 750;

/** A `{ path: content }` project, and nothing else. */
function isProject(files) {
  return !!files && typeof files === 'object' && !Array.isArray(files);
}

/**
 * Run an idb-keyval call and hand every failure to `onError`.
 *
 * `.catch()` alone is NOT enough: in a browser with no IndexedDB at all — a
 * WebView2 with storage disabled, the standing assumption behind every storage
 * touch in this codebase — `getDB()` throws SYNCHRONOUSLY, before idb-keyval
 * has a promise to reject. Crash recovery is a convenience; losing the editor
 * over its absence is not acceptable.
 */
function idbSafe(run, onError) {
  try {
    return Promise.resolve(run()).catch(onError);
  } catch (e) {
    onError(e);
    return Promise.resolve(undefined);
  }
}

/**
 * Persist the open code document to IndexedDB and offer the stored one back
 * once, on mount.
 *
 * @param {object} options
 * @param {string} options.language - '' for a Blockly document.
 * @param {object|null} options.files - `{ path: content }`.
 * @param {Array|null} options.destinations - the document's Ziele/Positionen
 *   (the entries of its store, migration 041); written as the serializer's
 *   state beside the files. Omitted (null) → the draft carries no key, the
 *   shape an older draft has, which still restores.
 * @param {boolean} options.enabled
 * @param {string|null} options.scopeKey - the Supabase user id.
 * @param {({language, files, destinations?}) => void} options.onRestore - the caller decides
 *   whether to apply it (a selected cloud workflow takes precedence, exactly
 *   as it does for the Blockly draft).
 */
export function useCodeAutosave({
  language = '',
  files = null,
  destinations = null,
  enabled = true,
  scopeKey = null,
  onRestore = null,
} = {}) {
  // The restore ATTEMPT has finished — not that it found anything. Held as
  // state, not a ref, because the delete effect below has to re-run on it.
  const [restoreSettled, setRestoreSettled] = useState(false);

  // Kept on one line and naming CODE_STORAGE_KEY for the same reason
  // useAutosave's is: sessionScope.test.js resolves a namespaced key by reading
  // the literal consts its single-line initializer names.
  const storageKey = `${CODE_STORAGE_KEY}:${scopeKey || autosaveSessionScope()}`;

  // One debounce, no interval: `files` is a new object on every edit, so the
  // prop IS the change signal — unlike the Blockly hook, which listens to a
  // workspace and needs the timer as a floor. The write is inline rather than
  // a `save` callback of its own: this effect would be its only caller, and a
  // callback repeating the guard on the line above it is a branch no test can
  // reach.
  useEffect(() => {
    if (!enabled || !isCodeLanguage(language) || !isProject(files)) return undefined;
    const state = Array.isArray(destinations)
      ? { language, files, destinations: serializeState(destinations) }
      : { language, files };
    const t = setTimeout(() => {
      idbSafe(
        () => idbSet(storageKey, { state, ts: Date.now() }),
        (e) => {
          if ((e && e.name) === 'QuotaExceededError') {
            toast.error(DE.AUTOSAVE_QUOTA_FULL, { id: 'autosave-quota' });
          } else {
            console.error('useCodeAutosave: idb-set failed', e);
          }
        },
      );
    }, DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [enabled, language, files, destinations, storageKey]);

  // Restore, once. `restoreSettled` is set on EVERY exit — a read that found
  // nothing, and a read that threw — because the delete below waits on it and
  // a bucket that can never be deleted is worse than one that was never read.
  useEffect(() => {
    if (!enabled || restoreSettled) return undefined;
    let cancelled = false;
    (async () => {
      const cached = await idbSafe(
        () => idbGet(storageKey),
        (e) => { console.error('useCodeAutosave: idb-get failed', e); },
      );
      const state = cached && cached.state;
      if (cancelled) return;
      if (state && isCodeLanguage(state.language) && isProject(state.files)
          && typeof onRestore === 'function') {
        const draft = { language: state.language, files: state.files };
        // An older draft has no Ziele: no key, and the page opens it with none.
        if (state.destinations && typeof state.destinations === 'object') {
          draft.destinations = state.destinations;
        }
        onRestore(draft);
      }
      setRestoreSettled(true);
    })();
    return () => { cancelled = true; };
    // onRestore is deliberately out of the deps: the page rebuilds it whenever
    // the open document changes, and a restore must happen once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, storageKey, restoreSettled]);

  useEffect(() => {
    if (!enabled || !restoreSettled || isCodeLanguage(language)) return;
    idbSafe(() => idbDel(storageKey), () => undefined);
  }, [enabled, restoreSettled, language, storageKey]);
}
