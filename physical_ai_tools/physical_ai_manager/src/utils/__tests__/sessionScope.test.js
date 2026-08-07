/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// A German school runs EduBotics on Windows student PCs under ONE shared
// Windows account: one WebView2 profile, one localStorage, many students.
// `edubotics_userId` — the HF account recordings upload under — was seeded into
// Redux initial state and never cleared on sign-out, so the next student
// inherited the previous one's Hugging Face identity as their default.
//
// Four properties are pinned here, and they fail for different reasons:
//   1. the SPLIT — student keys go, rig/layout keys stay. Clearing too much
//      costs the next student an arm re-scan; clearing too little is the leak.
//      The LITERAL names are pinned, because every other assertion derives its
//      expectation from the exported collections: deleting 'edubotics_userId' —
//      the key the module exists for — from STUDENT_SCOPED_KEYS left the whole
//      suite passing, since a list cannot contradict itself.
//   2. COVERAGE — every `edubotics*` persistence key reachable from src/
//      appears in one of the three collections. Two-way set equality, the
//      compose-env-parity idiom: an unclassified new key fails, and so does a
//      listed key that no longer exists anywhere.
//   3. ONE sign-out — `supabase.auth.signOut()` appears in exactly one
//      non-test file, `utils/signOut.js`.
//   4. EVERY SLICE ANSWERS — each reducer in the real store either declares a
//      `session/signedOut` handler or sits on the reasoned exemption list
//      below. Storage coverage cannot reach student state that never came from
//      storage (an e-mail, a dataset list, a fetched roster), and that is the
//      shape that let `workshop` survive two review rounds.

import fs from 'node:fs';
import path from 'node:path';
import {
  clearStudentScopedStorage,
  clearSupabaseSessionKeys,
  STUDENT_SCOPED_KEYS,
  MACHINE_SCOPED_KEYS,
  IGNORED_STORAGE_KEYS,
} from '../sessionScope';

const SRC = path.resolve(__dirname, '..', '..');

// The one file allowed to call supabase.auth.signOut().
const SIGN_OUT_MODULE = 'utils/signOut.js';

// Scanning the definition site itself is meaningless: its storage call is
// `localStorage.removeItem(key)` over a loop variable, and its key names live
// in the collections the scan is checking AGAINST.
const SCAN_EXCLUDE = new Set(['utils/sessionScope.js']);

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__tests__' || entry.name === 'node_modules') continue;
      walk(abs, out);
    } else if (/\.jsx?$/.test(entry.name)) {
      out.push(abs);
    }
  }
  return out;
}

// Comment-only lines are dropped before ANY pattern runs. This file's own prose
// quotes storage calls, and so does sessionScope.js's; a scan that reads them
// demands the reader classify a key that does not exist. It fails safe, but a
// guard that cries wolf gets switched off. (Line-oriented, matching the repo's
// `//` and `*`-continuation comment style.)
const COMMENT_LINE_RE = /^\s*(?:\/\/|\*|\/\*)/;
const stripComments = (text) =>
  text
    .split('\n')
    .filter((ln) => !COMMENT_LINE_RE.test(ln))
    .join('\n');

// `const FOO_KEY = 'literal'` — the indirection every non-slice persistence
// call uses (TEMPO_STORAGE_KEY, DOCK_WIDTH_KEY, RECONNECT_MARKER_KEY, …). A
// scan that only understood inline literals would see 5 of the real keys.
const CONST_LITERAL_RE = /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(['"`])([^'"`\n]*)\2/g;
// `const storageKey = scopeKey ? `${STORAGE_KEY}:${scopeKey}` : STORAGE_KEY` —
// the NAMESPACED-key idiom. The composed name is not a literal anywhere, so the
// scan resolves such an identifier to every literal const its initializer
// mentions: the base key, which is what has to be classified.
const CONST_EXPR_RE = /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)/g;
const IDENT_RE = /[A-Za-z_$][\w$]*/g;

// The RECEIVER of a storage call, spelled literally. This used to be the whole
// story, which made `const ls = window.localStorage; ls.setItem('edubotics_x')`
// invisible — not discovered AND not reported as unresolvable, i.e. the exact
// silent miss the module claims to fail loudly on. Two additions below close it:
// aliases are RESOLVED, and any other expression that yields a Storage object is
// REFUSED.
const LITERAL_RECEIVERS = ['localStorage', 'sessionStorage'];
const STORAGE_METHOD = String.raw`\s*\.\s*(?:get|set|remove)Item\s*\(\s*([^,)]+?)\s*[,)]`;
const storageCallRe = (receivers) =>
  new RegExp(
    `(?:^|[^\\w$.])(?:${receivers
      .map((r) => r.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
      .join('|')})${STORAGE_METHOD}`,
    'g'
  );
// `const ls = window.localStorage` / `let s = sessionStorage`. Nothing more
// elaborate is resolved on purpose — see STORAGE_DECL_RE below, which REFUSES
// every other hand-off instead of resolving it.
// `(?![\w$])` rather than a `[;\n)]` lookahead: this pattern is also tested
// against a SLICE of the source (the declaration STORAGE_DECL_RE captured, which
// stops before the `;`), so requiring a following delimiter made a real alias
// read as an unrecognised receiver. A token-boundary check works on both, and
// still rejects `window.localStorageFoo`.
const ALIAS_DECL_RE =
  /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:(?:window|globalThis|self)\.)?(?:local|session)Storage(?![\w$])/g;
// Any OTHER declaration mentioning a Storage object on either side: a
// destructure (`const { localStorage: ls } = window`), a property read, a
// function result. Those are refused rather than missed. A declaration whose
// initializer is itself a storage USE (`const raw = localStorage.getItem(k)`)
// is not a receiver hand-off and is filtered out below.
const STORAGE_DECL_RE =
  /(?:const|let|var)\s+([^=\n;]*?)=\s*([^;\n]*)/g;
const STORAGE_TOKEN_RE = /(?:local|session)Storage/;
const STORAGE_USE_RE = /(?:local|session)Storage\s*\.\s*(?:getItem|setItem|removeItem|clear|key|length)/;
// IndexedDB via idb-keyval. Web Storage is not the only place the app persists
// per-student data — `components/Workshop/useAutosave` puts the whole Blockly
// workspace in IndexedDB — and a scan blind to it reports full coverage while
// missing the largest artefact a student leaves behind.
const IDB_CALL_RE = /\bidb(?:Get|Set|Del)\s*\(\s*([^,)]+?)\s*[,)]/g;

const SOURCE_FILES = walk(SRC).map((abs) => ({
  rel: path.relative(SRC, abs).split(path.sep).join('/'),
  text: fs.readFileSync(abs, 'utf8'),
}));

/** Every persistence key literally reachable from src/, plus anything unresolvable. */
function scanStorageKeys() {
  const keys = new Set();
  const unresolved = [];
  for (const { rel, text: raw } of SOURCE_FILES) {
    if (SCAN_EXCLUDE.has(rel)) continue;
    const text = stripComments(raw);
    const consts = new Map();
    for (const m of text.matchAll(CONST_LITERAL_RE)) consts.set(m[1], m[3]);
    const exprs = new Map();
    for (const m of text.matchAll(CONST_EXPR_RE)) {
      if (!consts.has(m[1])) exprs.set(m[1], m[2]);
    }
    // Receivers: the two literal names, plus every alias declared in this file.
    const aliases = [...text.matchAll(ALIAS_DECL_RE)].map((m) => m[1]);
    const receivers = [
      ...LITERAL_RECEIVERS,
      ...LITERAL_RECEIVERS.map((r) => `window.${r}`),
      ...LITERAL_RECEIVERS.map((r) => `globalThis.${r}`),
      ...aliases,
    ];
    // Refuse an unrecognised hand-off rather than miss it silently.
    for (const m of text.matchAll(STORAGE_DECL_RE)) {
      const decl = m[0];
      if (!STORAGE_TOKEN_RE.test(decl)) continue;
      if (ALIAS_DECL_RE.test(decl)) { ALIAS_DECL_RE.lastIndex = 0; continue; }
      ALIAS_DECL_RE.lastIndex = 0;
      if (STORAGE_USE_RE.test(m[2])) continue;   // a read, not an alias
      unresolved.push(`${rel}: unrecognised Storage receiver in \`${decl.trim()}\``);
    }
    for (const re of [storageCallRe(receivers), IDB_CALL_RE]) {
      for (const m of text.matchAll(re)) {
        const arg = m[1].trim();
        const literal = /^(['"`])(.*)\1$/.exec(arg);
        if (literal) {
          keys.add(literal[2]);
        } else if (consts.has(arg)) {
          keys.add(consts.get(arg));
        } else if (exprs.has(arg)) {
          const via = (exprs.get(arg).match(IDENT_RE) || []).filter((n) =>
            consts.has(n)
          );
          if (via.length) via.forEach((n) => keys.add(consts.get(n)));
          else unresolved.push(`${rel}: ${arg}`);
        } else {
          unresolved.push(`${rel}: ${arg}`);
        }
      }
    }
  }
  return { keys, unresolved };
}

const isEdubotics = (k) => /^_*edubotics/i.test(k);
const classifiedKeys = () => [
  ...STUDENT_SCOPED_KEYS,
  ...MACHINE_SCOPED_KEYS,
  ...Object.keys(IGNORED_STORAGE_KEYS),
];

describe('clearStudentScopedStorage', () => {
  beforeEach(() => {
    try {
      localStorage.clear();
    } catch {
      /* jsdom always has it */
    }
  });

  it('lists the exact student keys by name', () => {
    // Pinned literally. Every other assertion in this file derives its
    // expectation from these collections, so without this one they could be
    // emptied and the suite would still pass.
    expect([...STUDENT_SCOPED_KEYS].sort()).toEqual([
      'edubotics:workshop:theme',
      'edubotics_trainingInfo',
      'edubotics_userId',
      'edubotics_workshop_code_open',
      'edubotics_workshop_tempo',
    ]);
  });

  it('lists the exact machine keys by name', () => {
    expect([...MACHINE_SCOPED_KEYS].sort()).toEqual([
      'edubotics_audio_muted',
      'edubotics_robotType',
      'edubotics_urdf_open',
      'edubotics_workshop_dock_collapsed',
      'edubotics_workshop_dock_open',
      'edubotics_workshop_dock_split',
      'edubotics_workshop_dock_width',
    ]);
  });

  it('gives every ignored key a written reason', () => {
    // "Ignored" is the category that rots. A bare name tells the next reader
    // nothing about whether they may re-classify it, so the reason travels
    // with the key and is enforced to be present.
    const entries = Object.entries(IGNORED_STORAGE_KEYS);
    expect(entries.length).toBeGreaterThan(0);
    for (const [key, why] of entries) {
      // The key is spelled into the failure by hand: this project's eslint
      // config bans expect()'s second message argument (jest/valid-expect).
      if (typeof why !== 'string' || why.trim().length <= 20) {
        throw new Error(`IGNORED_STORAGE_KEYS['${key}'] has no written reason`);
      }
    }
  });

  it('removes every student-scoped key', () => {
    STUDENT_SCOPED_KEYS.forEach((k) => localStorage.setItem(k, 'x'));
    clearStudentScopedStorage();
    STUDENT_SCOPED_KEYS.forEach((k) => {
      expect(localStorage.getItem(k)).toBeNull();
    });
  });

  it('leaves every machine-scoped key untouched', () => {
    // Rig configuration must survive a student change: clearing
    // edubotics_robotType would cost the next student an arm re-scan, and the
    // dock geometry is the shared PC's window layout, not anyone's data.
    MACHINE_SCOPED_KEYS.forEach((k) => localStorage.setItem(k, 'keep-me'));
    clearStudentScopedStorage();
    MACHINE_SCOPED_KEYS.forEach((k) => {
      expect(localStorage.getItem(k)).toBe('keep-me');
    });
  });

  it('clears EXACTLY the student set — no more, no less', () => {
    const all = [...STUDENT_SCOPED_KEYS, ...MACHINE_SCOPED_KEYS];
    all.forEach((k) => localStorage.setItem(k, 'v'));
    clearStudentScopedStorage();
    const survivors = all.filter((k) => localStorage.getItem(k) !== null);
    expect(survivors.sort()).toEqual([...MACHINE_SCOPED_KEYS].sort());
  });

  it('carries the key that would be missed by a prefix sweep', () => {
    // `edubotics:workshop:theme` is colon-delimited, so an
    // `edubotics_workshop_` prefix match skips it — which is one of the reasons
    // the list is an explicit allowlist.
    expect(STUDENT_SCOPED_KEYS).toContain('edubotics:workshop:theme');
    // ... and the dock keys DO share that prefix while being machine-scoped,
    // which is the other reason. A prefix sweep would be wrong both ways.
    expect(MACHINE_SCOPED_KEYS.some((k) => k.startsWith('edubotics_workshop_'))).toBe(
      true
    );
  });

  it('never lists the same key in two categories', () => {
    const all = classifiedKeys();
    expect(new Set(all).size).toBe(all.length);
  });

  it('does not touch the Supabase session key', () => {
    // persistSession is deliberate (supabaseClient::syncRealtimeAuth exists
    // because sessions are restored on INITIAL_SESSION). It must be cleared by
    // supabase.auth.signOut, which also revokes it server-side — deleting the
    // key by hand would skip the revoke.
    const sbKey = 'sb-fnnbysrjkfugsqzwcksd-auth-token';
    localStorage.setItem(sbKey, '{"access_token":"x"}');
    clearStudentScopedStorage();
    expect(localStorage.getItem(sbKey)).not.toBeNull();
    STUDENT_SCOPED_KEYS.forEach((k) => expect(k.startsWith('sb-')).toBe(false));
  });

  it('swallows a storage that throws instead of blocking the sign-out', () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('storage disabled');
      },
    });
    try {
      expect(() => clearStudentScopedStorage()).not.toThrow();
    } finally {
      if (original) Object.defineProperty(globalThis, 'localStorage', original);
      else delete globalThis.localStorage;
    }
  });

  it('sweeps the Supabase session by pattern, and only that', () => {
    // The project ref is not known at this layer, so the session key can only be
    // matched by shape — the one place the literal-allowlist rule cannot apply,
    // and it applies to keys this app does not own. Both members are swept: the
    // token and the PKCE `-code-verifier` sibling auth-js skips in the same
    // early return.
    localStorage.setItem('sb-fnnbysrjkfugsqzwcksd-auth-token', '{"access_token":"x"}');
    localStorage.setItem('sb-fnnbysrjkfugsqzwcksd-auth-token-code-verifier', 'pkce');
    localStorage.setItem('sb-something-else', 'keep-me');
    localStorage.setItem('edubotics_robotType', 'keep-me');
    clearSupabaseSessionKeys();
    expect(localStorage.getItem('sb-fnnbysrjkfugsqzwcksd-auth-token')).toBeNull();
    expect(
      localStorage.getItem('sb-fnnbysrjkfugsqzwcksd-auth-token-code-verifier')
    ).toBeNull();
    expect(localStorage.getItem('sb-something-else')).toBe('keep-me');
    expect(localStorage.getItem('edubotics_robotType')).toBe('keep-me');
  });

  it('anchors the session pattern at BOTH ends', () => {
    // `^sb-.+-auth-token(-code-verifier)?$` is the ONE pattern in this module —
    // the project ref is not knowable at this layer, so the allowlist doctrine
    // cannot apply and the ANCHORS are the entire bound. Measured: dropping both
    // left 488/488 green, because every fixture in the tree was a real session
    // key. An unanchored sweep deletes any key that merely CONTAINS the shape,
    // and this module's whole point is that it removes only what it means to.
    localStorage.setItem('not-really-sb-ref-auth-token', 'keep-me');       // ^
    localStorage.setItem('sb-ref-auth-token-something-else', 'keep-me');   // $
    localStorage.setItem('sb-ref-auth-token', 'go');
    clearSupabaseSessionKeys();
    expect(localStorage.getItem('not-really-sb-ref-auth-token')).toBe('keep-me');
    expect(localStorage.getItem('sb-ref-auth-token-something-else')).toBe('keep-me');
    // Not vacuous: the real key IS swept in the same call.
    expect(localStorage.getItem('sb-ref-auth-token')).toBeNull();
  });

  it('sweeps every session key present, not just the first', () => {
    // Deleting shifts the indices of everything after the deleted key, so the
    // names are snapshotted before the removals — a walk that deletes as it goes
    // skips its own neighbours.
    for (const ref of ['aaa', 'bbb', 'ccc']) {
      localStorage.setItem(`sb-${ref}-auth-token`, 'x');
    }
    clearSupabaseSessionKeys();
    for (const ref of ['aaa', 'bbb', 'ccc']) {
      expect(localStorage.getItem(`sb-${ref}-auth-token`)).toBeNull();
    }
  });

  it('is idempotent on an empty store', () => {
    expect(() => clearStudentScopedStorage()).not.toThrow();
    clearStudentScopedStorage();
    STUDENT_SCOPED_KEYS.forEach((k) => expect(localStorage.getItem(k)).toBeNull());
  });
});

describe('every edubotics persistence key in src/ is classified', () => {
  it('actually scanned the tree', () => {
    // Zero-file floor, the repo's standing rule for scan-based guards: a
    // directory move would otherwise make everything below pass having read
    // nothing.
    expect(SOURCE_FILES.length).toBeGreaterThan(100);
    expect(scanStorageKeys().keys.size).toBeGreaterThan(5);
  });

  it('understands every persistence call it finds', () => {
    // A call whose key the scan cannot resolve is a HOLE, not a pass: refuse
    // it loudly rather than compare an incomplete picture (the same stance
    // .github/scripts/compose_env_parity.py takes on an unparsed compose).
    expect(scanStorageKeys().unresolved).toEqual([]);
  });

  it('sees the IndexedDB keys too, not just Web Storage', () => {
    // The autosave bucket is the biggest per-student artefact in the browser
    // and it is not in localStorage at all. Before the scan understood
    // idb-keyval, this assertion was the thing that could not be written.
    expect([...scanStorageKeys().keys]).toContain('edubotics:workshop:autosave');
  });

  it('only recognises idb-keyval under the aliases it scans for', () => {
    // The IDB pattern keys on the repo's single import convention. If a file
    // ever imports idb-keyval under other names the scan goes blind, so the
    // convention itself is asserted rather than assumed.
    const importers = SOURCE_FILES.filter(({ text }) => /'idb-keyval'/.test(stripComments(text)));
    expect(importers.length).toBeGreaterThan(0);
    for (const { rel, text } of importers) {
      if (!/get as idbGet[\s\S]*?from 'idb-keyval'/.test(stripComments(text))) {
        throw new Error(`${rel} imports idb-keyval under names the scan cannot see`);
      }
    }
  });

  it('classifies every discovered key in exactly one collection', () => {
    const discovered = [...scanStorageKeys().keys].filter(isEdubotics).sort();
    // Two-way: an UNCLASSIFIED key is the leak this module exists to prevent,
    // and a classified key nothing uses any more is a stale entry that makes
    // the lists lie about what the app stores.
    expect(discovered).toEqual(classifiedKeys().sort());
  });
});

describe('sign-out lives in exactly one place', () => {
  // Whole-file, not line-by-line: `await supabase\n.auth\n.signOut()` is the
  // same blind spot the previous hard-coded caller list had. Comment lines are
  // stripped first so sessionScope.js's own prose about WHY the Supabase key
  // must be cleared by signOut does not read as a second implementation.
  const SIGN_OUT_CALL_RE = /supabase\s*\.\s*auth\s*\.\s*signOut\s*\(/;
  const callers = SOURCE_FILES.filter(({ text }) =>
    SIGN_OUT_CALL_RE.test(stripComments(text))
  ).map(({ rel }) => rel);

  it('is called from utils/signOut.js and nowhere else', () => {
    expect(callers).toEqual([SIGN_OUT_MODULE]);
  });

  it('is reachable from every page, and from exactly one control', () => {
    // The only "Abmelden" controls used to live on TrainingPage and
    // InferencePage, so a student on Start / Aufnahme / Roboter Studio could
    // not hand the PC over at all. StudentApp's own chrome — the desktop rail
    // and the mobile header, both rendered independently of `page` — carries
    // it now, which also means a page-level copy is a duplicate button AND a
    // second identity readout on the same screen.
    const app = SOURCE_FILES.find((f) => f.rel === 'StudentApp.js').text;
    expect(app.match(/onClick=\{handleLogout\}/g)).toHaveLength(2);
    expect(app).toContain('dispatch(signOutStudent(');
    // Both instances are reachable without sight: the rail one is icon+label
    // in a collapsed sidebar, so it needs the aria-label the mobile one has.
    expect(app.match(/aria-label="Abmelden"/g)).toHaveLength(2);

    for (const rel of ['pages/TrainingPage.js', 'pages/InferencePage.js']) {
      const src = SOURCE_FILES.find((f) => f.rel === rel).text;
      expect(src).not.toContain('>Abmelden<');
      expect(src).not.toMatch(/<span>Abmelden<\/span>/);
      expect(src).not.toContain('handleLogout');
    }
  });

  it('feeds the block gate all four inputs, both surfaces and the German title', () => {
    // The DECISION and its wording are unit-tested in signOut.test.js. What
    // cannot be tested there is the WIRING: a gate that stops being asked about
    // Roboter Studio, or about liveness, leaves the pure function perfectly
    // green. So every input is pinned at the call site.
    const app = SOURCE_FILES.find((f) => f.rel === 'StudentApp.js').text;
    const call = /logoutBlockReason\(\{([\s\S]*?)\}\)/.exec(app);
    if (!call) throw new Error('StudentApp does not call logoutBlockReason({…})');
    const args = call[1];
    // Liveness — a rosbridge drop must not be able to latch the control shut.
    expect(args).toMatch(/robotLinkLive:\s*heartbeatStatus === 'connected'/);
    expect(args).toMatch(/taskRunning:\s*Boolean\(taskStatus\.running\)/);
    // Roboter Studio is a SECOND surface (state.workshop), not tasks.taskStatus.
    expect(args).toContain('workflowRunning');
    expect(args).toMatch(/calibrationCapturing:[\s\S]*calibrationCapturing/);
    expect(app).toContain('const logoutBlocked = blockReason !== null');
    expect(app).toContain('LOGOUT_BLOCK_TITLES_DE[blockReason]');
    // Both surfaces disable and both explain — a dead button with no reason
    // reads as a broken app.
    expect(app.match(/disabled=\{logoutBlocked\}/g)).toHaveLength(2);
    expect(app.match(/title=\{logoutTitle\}/g)).toHaveLength(2);
  });

  it('names the signed-in student in VISIBLE text on both chrome surfaces', () => {
    // On the shared Windows account this whole module exists for, "am I signed
    // in as me?" must be answerable at a glance. No page HEADER carries the
    // e-mail any more (one prose sentence on the Training tab is all that is
    // left), and a title= attribute is not an answer — so the rail and the
    // mobile header each render it as text, outside the button, which greys out
    // exactly when a student wants to check.
    const app = SOURCE_FILES.find((f) => f.rel === 'StudentApp.js').text;
    expect(app.match(/\{showLogout && identity && \(/g)).toHaveLength(2);
    expect(app.match(/title=\{identity\}/g)).toHaveLength(2);
    // A line that is ONLY `{identity}` is a rendered TEXT node — `title=` and
    // `Avatar name=` uses of the same value are not, and a tooltip is exactly
    // what this asserts is not sufficient.
    expect(app.match(/^\s*\{identity\}$/gm)).toHaveLength(2);
  });

  it('keeps the control reachable after a declined reload', () => {
    // The default sign-out ends in a reload, but `beforeunload` lets a student
    // mid-recording DECLINE it. The session is then already null, so a control
    // gated on the e-mail alone would vanish and strand them half-signed-out
    // with no way to retry.
    const app = SOURCE_FILES.find((f) => f.rel === 'StudentApp.js').text;
    expect(app).toMatch(/const showLogout = Boolean\(identity\) \|\| handoverStarted/);
    expect(app.match(/\{showLogout && \(/g)).toHaveLength(2);
    expect(app).toContain('setHandoverStarted(true)');
  });

  it('runs the sign-out steps in exactly one order', () => {
    // The EXACT sequence, not pairwise index comparisons — the previous form
    // asserted `broadcast > revoke`, i.e. it PINNED the defect: during the
    // awaited revoke `isAuthenticated` was still true with storage already
    // scrubbed, so one /task/status tick re-persisted the id and the reload
    // re-hydrated it.
    //
    // Everything LOCAL and SYNCHRONOUS now precedes the single remote call, and
    // the scrub repeats after it because the await is a window in which every
    // timer and pending handler in the document keeps running. `jetson` stays
    // first for its own reason: the release beacon needs the JWT the revoke is
    // about to invalidate.
    //
    // Comment lines are stripped, because signOut.js's header narrates this same
    // sequence in prose.
    const src = stripComments(
      SOURCE_FILES.find((f) => f.rel === SIGN_OUT_MODULE).text
    );
    const MARKERS = [
      [/resetJetsonOnLogout\(dispatch/g, 'jetson'],
      [/dispatch\(signedOut\(\)\)/g, 'broadcast'],
      [/clearStudentScopedStorage\(\)/g, 'scrub'],
      [/await supabase\s*\.\s*auth\s*\.\s*signOut\s*\(/g, 'revoke'],
      [/clearSupabaseSessionKeys\(\)/g, 'sweep'],
      [/window\.location\.reload\(\)/g, 'reload'],
    ];
    const seen = [];
    for (const [re, name] of MARKERS) {
      for (const m of src.matchAll(re)) seen.push([m.index, name]);
    }
    seen.sort((a, b) => a[0] - b[0]);
    expect(seen.map(([, name]) => name)).toEqual([
      'jetson',
      'broadcast',
      'scrub',
      'revoke',
      'sweep',
      'scrub',
      'reload',
    ]);
  });

  it('passes reload:false from exactly the three involuntary paths', () => {
    // The reload is the completeness BACKSTOP: a fresh document re-runs every
    // slice's module-level storage seed against the scrubbed storage and drops
    // every timer, poll and socket closure holding the old session. Suppressing
    // it means the enumeration above has to be complete on its own, so it is
    // allowed for exactly one reason — the path has just shown a German toast
    // the student must read, and a reload destroys it. All three are
    // INVOLUNTARY: nobody asked to be signed out.
    //
    // A voluntary logout must never join them, and until this test that was
    // fenced by nothing: turning WebApp's ordinary teacher logout into
    // `reload: false` left 488/488 green. Counted PER FILE for that reason — a
    // set of filenames would not notice a second call inside an allowed file.
    const RELOAD_FALSE_PATHS = Object.freeze({
      'StudentApp.js': 'wrong-role bounce: a teacher opened the student URL',
      'WebApp.js': 'wrong-role bounce: a student opened the teacher URL',
      'hooks/useMeProfile.js': 'a 401/403 from /me mid-session',
    });
    const RELOAD_FALSE_RE = /signOutStudent\(\s*\{\s*reload:\s*false\s*\}\s*\)/g;
    const found = {};
    for (const { rel, text } of SOURCE_FILES) {
      const hits = stripComments(text).match(RELOAD_FALSE_RE);
      if (hits) found[rel] = hits.length;
    }
    const expected = Object.fromEntries(
      Object.keys(RELOAD_FALSE_PATHS).map((rel) => [rel, 1])
    );
    expect(found).toEqual(expected);
    // Not vacuous: there ARE other sign-out call sites, and they reload.
    const plain = SOURCE_FILES.flatMap(({ text }) =>
      stripComments(text).match(/dispatch\(signOutStudent\(\)\)/g) || []
    );
    expect(plain.length).toBeGreaterThan(0);
  });

  it('does not import a hook — dependencies run one way', () => {
    // utils/signOut used to import resetJetsonOnLogout from hooks/, pulling
    // react-redux, react-hot-toast and the rosbridge singleton into the util
    // layer. The Jetson reset now lives in features/jetson/sessionReset, which
    // both the util and the hook import.
    const src = SOURCE_FILES.find((f) => f.rel === SIGN_OUT_MODULE).text;
    expect(src).not.toMatch(/from '\.\.\/hooks\//);
    expect(src).toContain("from '../features/jetson/sessionReset'");
  });

  it('clears the Supabase session key only through the pattern sweep', () => {
    // The sweep has exactly TWO callers and each is named with its reason —
    // hand-deleting the key anywhere else would skip the server-side revoke
    // (see sessionScope's comment). Enumerated, not counted: a third caller
    // must be a decision, not an accident.
    //
    //   utils/signOut.js    — a revoke that RESOLVED but reported failure;
    //                         auth-js skips _removeSession() for most statuses.
    //   utils/bootScrub.js  — student handover. The previous student is gone,
    //                         so there is no session object to revoke on their
    //                         behalf and no network call may block boot; the
    //                         persisted credential is the one thing reachable.
    const owners = SOURCE_FILES.filter(({ text }) =>
      /clearSupabaseSessionKeys\s*\(/.test(stripComments(text))
    ).map(({ rel }) => rel).sort();
    expect(owners).toEqual([
      'utils/bootScrub.js',
      'utils/sessionScope.js',
      SIGN_OUT_MODULE,
    ]);
    const src = stripComments(
      SOURCE_FILES.find((f) => f.rel === SIGN_OUT_MODULE).text
    );
    expect(src).toMatch(/if \(revokeFailed\) \{\s*clearSupabaseSessionKeys\(\);/);
  });
});

// ── Every slice answers for itself ──────────────────────────────────────────
// The storage-coverage scan above can only see state that CAME FROM STORAGE. A
// slice holding student data fetched over the network — an e-mail, a dataset
// list, a teacher's roster — is invisible to it, and so is a slice that hydrates
// from a listed key but has no handler. Both were measured to pass the whole
// suite. This is the structural half: every reducer in the REAL store either
// declares a `session/signedOut` handler or appears below with a reason.
//
// The exemption list is a reason-MAP for the same argument IGNORED_STORAGE_KEYS
// is: a bare name tells the next reader nothing about whether they may
// re-classify it. It follows `.github/scripts/compose_env_parity.py`'s
// INTENTIONAL allowlist, including its rule that a STALE entry is an error too.
const SIGNED_OUT_EXEMPT = Object.freeze({
  jetson:
    'Handled IMPERATIVELY and FIRST, not reactively: features/jetson/'
    + 'sessionReset::resetJetsonOnLogout dispatches clearJetson() as step 1 of '
    + 'signOutStudent, because it also fires an authenticated release beacon '
    + 'that needs the JWT the revoke is about to invalidate. A signedOut '
    + 'handler would run too late to be that.',
  ros:
    'Rosbridge URL, host and the image-topic list — facts about which ROBOT this '
    + 'browser talks to. Identical for every student at the PC, and clearing it '
    + 'would tear down the connection the next student needs. The Jetson '
    + 'override is not student state either: resetJetsonOnLogout points '
    + 'rosbridge back at the local rig itself.',
  ui:
    'Which tab is open, plus ui.hfUserList — and the list is the giveaway. It is '
    + 'the accounts and orgs reachable with THIS RIG\'s $HF_TOKEN, fetched once '
    + 'per ROS connect, so it describes the machine and not the person. It is '
    + 'also what keeps the never-signed-in recording path working: recording '
    + 'needs a Benutzer-ID and no cloud login, so clearing the list on sign-out '
    + 'would empty the selector for a student who never signs in at all.',
});

describe('every slice in the real store answers session/signedOut', () => {
  // Derived from store.js, never restated: the store's own import map is the
  // only authority on which reducer file backs which key.
  const storeSrc = SOURCE_FILES.find((f) => f.rel === 'store/store.js').text;
  const imports = new Map(
    [...storeSrc.matchAll(/import\s+([A-Za-z_$][\w$]*)\s+from\s+'([^']+)'/g)]
      .map((m) => [m[1], m[2]])
  );
  const reducerBlock = /reducer:\s*\{([\s\S]*?)\n\s*\}/.exec(storeSrc);
  const sliceFiles = new Map(
    [...(reducerBlock ? reducerBlock[1] : '').matchAll(
      /([A-Za-z_$][\w$]*)\s*:\s*([A-Za-z_$][\w$]*)\s*,/g
    )].map(([, key, ident]) => {
      const spec = imports.get(ident);
      if (!spec) return [key, null];
      // store.js lives in src/store/, so its specifiers resolve from there.
      const rel = path.posix.normalize(path.posix.join('store', spec));
      return [key, `${rel}.js`];
    })
  );

  it('actually parsed the store', () => {
    // Zero-slice floor: a rename of store.js or of `reducer: {` would otherwise
    // make everything below pass having found nothing.
    expect(sliceFiles.size).toBeGreaterThanOrEqual(10);
    for (const [key, rel] of sliceFiles) {
      if (!rel || !SOURCE_FILES.some((f) => f.rel === rel)) {
        throw new Error(`store key '${key}' does not resolve to a file (got ${rel})`);
      }
    }
  });

  it('handles it, or is exempted with a written reason', () => {
    const missing = [];
    for (const [key, rel] of sliceFiles) {
      const text = stripComments(SOURCE_FILES.find((f) => f.rel === rel).text);
      const handles =
        /addCase\(\s*signedOut/.test(text) || /addMatcher[\s\S]*signedOut/.test(text);
      const exempt = Object.prototype.hasOwnProperty.call(SIGNED_OUT_EXEMPT, key);
      if (handles && exempt) {
        throw new Error(
          `slice '${key}' both handles session/signedOut and is exempted — drop the exemption`
        );
      }
      if (!handles && !exempt) missing.push(`${key} (${rel})`);
    }
    expect(missing).toEqual([]);
  });

  it('has no stale exemption, and every reason is written out', () => {
    for (const [key, why] of Object.entries(SIGNED_OUT_EXEMPT)) {
      if (!sliceFiles.has(key)) {
        throw new Error(`SIGNED_OUT_EXEMPT['${key}'] names no slice in the store`);
      }
      if (typeof why !== 'string' || why.trim().length <= 40) {
        throw new Error(`SIGNED_OUT_EXEMPT['${key}'] has no written reason`);
      }
    }
  });
});
