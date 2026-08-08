/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The student-handover boot scrub. Three properties, and they fail for
// different reasons:
//
//   1. WHAT IT CLEARS — the person's keys and the persisted Supabase session go;
//      the rig's keys and the Blockly autosave stay. This is the whole reason
//      the scrub replaced an rmtree of the WebView2 profile: that directory
//      holds localStorage AND IndexedDB, so it destroyed the crash-recovery
//      autosave and every MACHINE_SCOPED_KEYS entry along with the leak.
//   2. ONCE PER WINDOW — a reload (useVersionCheck fires one on every image
//      update) must NOT re-scrub, or a student is signed out mid-lesson; a new
//      spawn MUST scrub even if the latch somehow survived.
//   3. WHERE IT RUNS — first, before store/store.js and lib/supabaseClient.js
//      are evaluated. Both read localStorage at module-evaluation time, so a
//      scrub after them would run against state already handed to Redux and to
//      supabase-js. That is a position, not a behaviour, so it is pinned
//      structurally.

import fs from 'node:fs';
import path from 'node:path';
import {
  bootScrubOnce,
  BOOT_SCRUB_PARAM,
  BOOT_SCRUB_KEY,
} from '../bootScrub';
import {
  STUDENT_SCOPED_KEYS,
  MACHINE_SCOPED_KEYS,
  IGNORED_STORAGE_KEYS,
} from '../sessionScope';

const SRC = path.resolve(__dirname, '..', '..');
const readSrc = (rel) => fs.readFileSync(path.join(SRC, rel), 'utf8');

// Same line-oriented comment strip sessionScope.test.js uses, for the same
// reason: this module's header QUOTES the call it must not make, and a scan
// that reads prose only teaches the next author to rephrase the prose.
const COMMENT_LINE_RE = /^\s*(?:\/\/|\*|\/\*)/;
const stripComments = (text) =>
  text
    .split('\n')
    .filter((ln) => !COMMENT_LINE_RE.test(ln))
    .join('\n');

const SB_KEY = 'sb-fnnbysrjkfugsqzwcksd-auth-token';

/** Point window.location.search at a query string, restoring it afterwards. */
function withSearch(search, fn) {
  const original = Object.getOwnPropertyDescriptor(window, 'location');
  delete window.location;
  window.location = { ...original?.value, search };
  try {
    return fn();
  } finally {
    if (original) Object.defineProperty(window, 'location', original);
  }
}

const spawnUrl = (nonce) => `?_v=2.14.1&robot=omx_full&${BOOT_SCRUB_PARAM}=${nonce}`;

/** The full storage picture a student leaves behind on a shared PC. */
function seedEverything() {
  STUDENT_SCOPED_KEYS.forEach((k) => localStorage.setItem(k, 'student-A'));
  MACHINE_SCOPED_KEYS.forEach((k) => localStorage.setItem(k, 'rig'));
  localStorage.setItem(SB_KEY, '{"access_token":"A"}');
  localStorage.setItem(`${SB_KEY}-code-verifier`, 'pkce');
}

describe('bootScrubOnce', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it('does nothing at all without the spawn flag', () => {
    // A student who types http://localhost/ by hand, or any navigation the GUI
    // did not spawn, must not lose their session.
    seedEverything();
    expect(withSearch('?_v=2.14.1', bootScrubOnce)).toBe('no-param');
    STUDENT_SCOPED_KEYS.forEach((k) => expect(localStorage.getItem(k)).toBe('student-A'));
    expect(localStorage.getItem(SB_KEY)).not.toBeNull();
  });

  it('does nothing when the flag is present but empty', () => {
    seedEverything();
    expect(withSearch(`?${BOOT_SCRUB_PARAM}=`, bootScrubOnce)).toBe('no-param');
    expect(localStorage.getItem('edubotics_userId')).toBe('student-A');
  });

  it('clears the student keys and the persisted Supabase session', () => {
    seedEverything();
    expect(withSearch(spawnUrl('n1'), bootScrubOnce)).toBe('scrubbed');
    STUDENT_SCOPED_KEYS.forEach((k) => expect(localStorage.getItem(k)).toBeNull());
    expect(localStorage.getItem(SB_KEY)).toBeNull();
    expect(localStorage.getItem(`${SB_KEY}-code-verifier`)).toBeNull();
  });

  it('KEEPS every machine-scoped key — this is the rmtree regression', () => {
    // The rmtree deleted the WebView2 user-data folder, so it took these with
    // it. `edubotics_robotType`'s own comment in sessionScope says clearing it
    // costs the next student an arm re-scan.
    seedEverything();
    withSearch(spawnUrl('n1'), bootScrubOnce);
    MACHINE_SCOPED_KEYS.forEach((k) => expect(localStorage.getItem(k)).toBe('rig'));
  });

  it('touches nothing in IndexedDB — the Blockly autosave survives', () => {
    // The other half of the rmtree regression, and it is structural: the scrub
    // is two localStorage helpers, so it CANNOT reach idb-keyval. Asserted by
    // reading the source, because there is nothing behavioural to observe.
    const src = readSrc('utils/bootScrub.js');
    expect(src).not.toMatch(/idb-keyval|idbGet|idbSet|idbDel|indexedDB/);
    // ...and the autosave key is classified as ignored precisely because it is
    // already per-student, so nothing here needs to reach it.
    expect(Object.keys(IGNORED_STORAGE_KEYS)).toContain('edubotics:workshop:autosave');
  });

  it('is a no-op on a RELOAD of the same window', () => {
    // useVersionCheck reloads on every image update and location.reload() keeps
    // the query string. A flag without a latch would sign the student out
    // mid-lesson.
    withSearch(spawnUrl('n1'), bootScrubOnce);
    localStorage.setItem('edubotics_userId', 'student-B-signed-in-after-the-scrub');
    localStorage.setItem(SB_KEY, '{"access_token":"B"}');

    expect(withSearch(spawnUrl('n1'), bootScrubOnce)).toBe('already');
    expect(localStorage.getItem('edubotics_userId')).toBe(
      'student-B-signed-in-after-the-scrub'
    );
    expect(localStorage.getItem(SB_KEY)).toBe('{"access_token":"B"}');
  });

  it('scrubs again for a NEW spawn even though the latch survived', () => {
    // The fail-safe direction. If sessionStorage ever outlived the window
    // (Chromium persists it for session restore; WebView2's behaviour is not
    // something this repo can prove), a bare flag would scrub once and never
    // again. The nonce is what makes a new window a new decision.
    withSearch(spawnUrl('n1'), bootScrubOnce);
    seedEverything();
    expect(sessionStorage.getItem(BOOT_SCRUB_KEY)).toBe('n1');

    expect(withSearch(spawnUrl('n2'), bootScrubOnce)).toBe('scrubbed');
    expect(localStorage.getItem('edubotics_userId')).toBeNull();
    expect(localStorage.getItem(SB_KEY)).toBeNull();
    expect(sessionStorage.getItem(BOOT_SCRUB_KEY)).toBe('n2');
  });

  it('scrubs when sessionStorage is unavailable, rather than skipping', () => {
    // Without the latch we cannot prove the window was already handled.
    // Scrubbing twice costs a re-login; skipping leaks a session.
    seedEverything();
    const original = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
    Object.defineProperty(globalThis, 'sessionStorage', {
      configurable: true,
      get() {
        throw new Error('storage disabled');
      },
    });
    try {
      expect(withSearch(spawnUrl('n1'), bootScrubOnce)).toBe('scrubbed');
      expect(localStorage.getItem('edubotics_userId')).toBeNull();
    } finally {
      if (original) Object.defineProperty(globalThis, 'sessionStorage', original);
      else delete globalThis.sessionStorage;
    }
  });

  it('never latches a scrub that did not happen', async () => {
    // Fail towards scrubbing again. Marking a failed scrub as done hands the
    // next student the previous one's session — the exact defect this exists
    // for — while an unmarked successful scrub merely repeats.
    //
    // The scrub helpers swallow their own storage errors by design, so a
    // throwing localStorage does NOT reach this branch. The only way to reach
    // it is a structural failure of the helpers themselves, which is what this
    // mocks — otherwise the assertion would be conditional on a verdict the
    // test cannot force, i.e. vacuous.
    vi.resetModules();
    vi.doMock('../sessionScope', () => ({
      default: () => {
        throw new Error('structural failure');
      },
      clearStudentScopedStorage: () => {
        throw new Error('structural failure');
      },
      clearSupabaseSessionKeys: () => {},
    }));
    try {
      const mod = await import('../bootScrub');
      expect(withSearch(spawnUrl('n1'), mod.bootScrubOnce)).toBe('failed');
      expect(sessionStorage.getItem(mod.BOOT_SCRUB_KEY)).toBeNull();
    } finally {
      vi.doUnmock('../sessionScope');
      vi.resetModules();
    }
  });

  it('never throws, whatever the URL', () => {
    for (const search of ['', '?', '?fresh', '?fresh=&fresh=x', '?%']) {
      expect(() => withSearch(search, bootScrubOnce)).not.toThrow();
    }
  });
});

describe('the scrub runs before anything reads storage', () => {
  const indexSrc = readSrc('index.js');
  const importSpecs = [...indexSrc.matchAll(/^import\s+(?:.*?\s+from\s+)?'([^']+)';/gm)].map(
    (m) => m[1]
  );

  it('actually parsed index.js', () => {
    // Zero-import floor: a rewrite of index.js would otherwise make everything
    // below pass having found nothing.
    expect(importSpecs.length).toBeGreaterThanOrEqual(5);
    expect(importSpecs).toContain('./store/store');
  });

  it('imports the scrub FIRST', () => {
    // Not "before ./App and ./store/store" — those are the two modules that
    // read storage at evaluation time TODAY, and any other import could grow
    // such a read tomorrow. "First" is the only form of the guarantee that
    // stays true as the file changes.
    expect(importSpecs[0]).toBe('./utils/bootScrubOnLoad');
  });

  it('runs it as a side effect, not as a call in the body', () => {
    // Imports are hoisted: a call in index.js's body would execute after every
    // import has already been evaluated, which is exactly too late.
    expect(indexSrc).not.toMatch(/bootScrubOnce\s*\(/);
    expect(readSrc('utils/bootScrubOnLoad.js')).toMatch(/^bootScrubOnce\(\);$/m);
  });

  it('the two modules it must beat really do read storage at import time', () => {
    // Not vacuous: if these ever stopped reading storage during module
    // evaluation, the ordering rule above would be cargo-cult.
    expect(readSrc('features/tasks/taskSlice.js')).toMatch(
      /localStorage\.getItem\('edubotics_userId'\)/
    );
    expect(readSrc('features/training/trainingSlice.js')).toMatch(
      /localStorage\.getItem\('edubotics_trainingInfo'\)/
    );
    expect(readSrc('lib/supabaseClient.js')).toMatch(/supabase\.auth\s*\.\s*getSession\(\)/);
  });

  it('does not reach for supabase.auth.signOut', () => {
    // That call belongs to utils/signOut and to exactly one non-test file —
    // pinned by sessionScope.test.js. It is also the wrong instrument on a boot
    // path: a network round trip, on behalf of a student who has already left.
    //
    // Asserted as "never imports the client" rather than "the string is
    // absent": both modules DISCUSS signOut in their headers, and a substring
    // ban would only teach the next author to rephrase the comment. Without the
    // import the call is unreachable however it is spelled.
    for (const rel of ['utils/bootScrub.js', 'utils/bootScrubOnLoad.js']) {
      const code = stripComments(readSrc(rel));
      expect(code).not.toMatch(/from\s+'[^']*supabase/i);
      expect(code).not.toMatch(/supabase\s*\.\s*auth\s*\.\s*signOut\s*\(/);
    }
  });
});
