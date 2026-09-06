// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The STUDENT/MACHINE key registry for browser storage, and the scrub half of
// sign-out. The whole sign-out sequence — Jetson release, this scrub, the
// Supabase revoke, the `session/signedOut` broadcast — lives in
// `utils/signOut::signOutStudent`; nothing outside it should call
// `clearStudentScopedStorage` directly.
//
// WHY THIS EXISTS. A German school runs EduBotics on Windows student PCs under
// ONE shared Windows account. One Windows account means one WebView2 profile,
// which means ONE localStorage shared by every student who ever sits down at
// that PC. `edubotics_userId` — documented in taskSlice as "the HF account/org
// the student records under" — was seeded into Redux initial state and
// re-persisted on every setTaskInfo, and NOTHING cleared it on sign-out. The
// next student inherited the previous one's Hugging Face identity as the
// default their recordings upload under.
//
// THE OPERATIVE RULE, stated once. Clear what belongs to the PERSON — their
// identity, their work, and their choices about their work. Keep what belongs
// to the MACHINE: the rig's configuration, the shared PC's window furniture,
// and settings a TEACHER applies to the room rather than a student to
// themselves. Two places the rule needs its tie-breaks spelled out:
//   * the panel-open keys look alike, so ask what the panel SHOWS — the
//     generated-code view shows the STUDENT'S OWN PROGRAM, while the dock and
//     the URDF twin show the rig;
//   * `edubotics_audio_muted` and `edubotics:workshop:theme` are both comfort
//     settings, and they split on WHO sets them: muting is what a teacher does
//     to a classroom of PCs, an editor theme is what a student does to their
//     own workspace.
//
// It is an explicit ALLOWLIST of literal key names, never a prefix sweep:
// `edubotics_workshop_dock_*` shares its prefix with workshop keys that DO get
// cleared, `edubotics:workshop:theme` would be missed by that prefix entirely,
// and a future key must be classified by a human rather than swept along by
// whichever list its name happens to match. `sessionScope.test.js` scans src/
// for storage keys and fails on any `edubotics*` key that appears in none of
// the three lists below; `ci.yml::react-tests` runs it, so that is a gate and
// not only a local habit. What bounds it is REACH, not enforcement: it
// understands Web Storage and the `idb-keyval` aliases the repo imports, it
// resolves a receiver spelled literally or aliased once
// (`const ls = window.localStorage`), and it REFUSES anything else that yields
// a Storage object rather than passing it silently. Two receiver shapes remain
// the residual, and the second is the less obvious one: a receiver arriving by a
// route no pattern can follow (a function return, a property on an object built
// elsewhere), and a TWO-HOP alias — `const a = localStorage; const b = a;
// b.setItem(…)`, where `b`'s initializer carries no Storage token at all, so it
// is neither resolved NOR refused. Both are recorded in docs/KNOWN-ISSUES.md;
// closing them means transitive alias resolution.
//
// Every access is try/catch'd — the codebase's existing pattern for storage,
// because a WebView2 with storage disabled throws on the bare property access.

/**
 * Keys that belong to the PERSON in front of the machine. Cleared on sign-out.
 */
export const STUDENT_SCOPED_KEYS = Object.freeze([
  // The HF account/org recordings upload under. THE leak this module exists for.
  'edubotics_userId',
  // The student's last training job (id, policy, dataset) — their work, and it
  // drives the Training tab's live-progress card on load.
  'edubotics_trainingInfo',
  // Roboter Studio run speed: a choice about how fast the arm executes THEIR
  // program. Inheriting someone else's 2x is a physical surprise, not a
  // cosmetic one.
  'edubotics_workshop_tempo',
  // Whether the generated-Python view is open. A view of the student's own
  // program, not of the rig — a beginner who never asked for generated code
  // should not inherit it from the previous student's lesson.
  'edubotics_workshop_code_open',
  // Startseite hero view — „3D-Modell" or „Kamera". A personal choice about
  // their own workspace, same class as the editor theme below: the student
  // sets it for themselves, not a teacher for the room. Also the reason it
  // is not machine-scoped despite looking like window furniture — it costs
  // the next student nothing to lose, and inheriting someone else's camera
  // view silently opens a 5-8 Mbps stream they did not ask for.
  'edubotics_home_view',
  // Blockly editor theme — a personal choice about their own workspace.
  // (Colon-delimited: the one key an `edubotics_workshop_` prefix match would
  // miss, which is one more reason this list is explicit.)
  'edubotics:workshop:theme',
  // Blockly's CROSS-TAB CLIPBOARD — the blocks the student last copied, i.e.
  // their program, in plain text. Written by
  // `@mit-app-inventor/blockly-plugin-workspace-multiselect`'s
  // `dataCopyToStorage`, and READ by Ctrl+V, so without these three a new
  // student could paste the previous one's blocks.
  //
  // THREE THINGS MAKE THESE THE EASIEST KEYS IN THIS FILE TO MISS, which is why
  // they get the longest comment:
  //   1. they are written by a DEPENDENCY, not by `src/`, and
  //      `sessionScope.test.js`'s coverage scan walks `src/` with node_modules
  //      excluded — so the scan structurally cannot see them;
  //   2. they do not start with `edubotics`, and that scan's `isEdubotics`
  //      filter judges only keys that do;
  //   3. the feature is on by DEFAULT — `Multiselect`'s constructor sets
  //      `useCopyPasteCrossTab_ = true` and only an explicit
  //      `multiselectCopyPaste.crossTab === false` turns it off, which
  //      `BlocklyWorkspace.jsx`'s `ms.init({})` never provides.
  // `bootScrub.crossTab.test.js` therefore asserts the plugin still writes
  // exactly these names, so a dependency bump that renames them fails loudly
  // instead of silently reopening the leak.
  'blocklyStashMulti',
  'blocklyStashConnection',
  'blocklyStashTime',
]);

/**
 * Keys that belong to the MACHINE / the rig. Never cleared on sign-out.
 * Exported so the test can assert the two sets stay disjoint and complete.
 */
export const MACHINE_SCOPED_KEYS = Object.freeze([
  // Rig configuration. Clearing it costs the next student an arm re-scan.
  'edubotics_robotType',
  // A classroom setting a teacher applies to the room, not a student
  // preference — see the WHO-sets-it tie-break in the operative rule above.
  'edubotics_audio_muted',
  // Window furniture of the shared PC — which dock panels are open, and the
  // dock's geometry. Same class as the browser window size.
  'edubotics_workshop_dock_open',
  'edubotics_workshop_dock_collapsed',
  'edubotics_workshop_dock_width',
  'edubotics_workshop_dock_split',
  // The Record page's URDF panel toggle: a view of the RIG, and layout like the
  // dock keys above.
  'edubotics_urdf_open',
]);

/**
 * Storage keys that are deliberately neither cleared here nor "machine" state,
 * each with the reason. A reason-map rather than a list because "ignored" is
 * the category that rots: a bare name says nothing about whether the next
 * reader may re-classify it. `Object.keys()` feeds the coverage comparison
 * unchanged, and the test asserts every value is a non-empty string.
 *
 * Not listed, for a different reason: the Supabase session
 * (`sb-<project-ref>-auth-token`, written from inside supabase-js).
 * `persistSession: true` is deliberate — supabaseClient's `syncRealtimeAuth`
 * exists precisely because sessions are restored on INITIAL_SESSION — so it
 * must be CLEARED, never disabled, and the thing that normally clears it is
 * `supabase.auth.signOut()`, which also revokes it server-side. Deleting the
 * key by hand would skip the revoke, which is why it is not on any list here.
 * `clearSupabaseSessionKeys` below is the exception, and it is reached only
 * once the revoke has already been ATTEMPTED and reported failure.
 *
 * Tutorial progress has no browser-storage key at all: it lives server-side via
 * `tutorialApi::updateTutorialProgress`, and the Redux `activeTutorialId` /
 * `activeTutorialStep` are not persisted — the `session/signedOut` handler in
 * workshopSlice resets those in memory. When a key does appear for it, it
 * belongs in STUDENT_SCOPED_KEYS.
 */
export const IGNORED_STORAGE_KEYS = Object.freeze({
  'edubotics.jetson.reconnect':
    'sessionStorage, owned by features/jetson/sessionReset::clearReconnectMarker, '
    + 'which resetJetsonOnLogout — the FIRST step of signOutStudent — calls. '
    + 'Student-scoped, but two owners for one key is how one of them quietly '
    + 'stops being called.',
  __edubotics_version_reload_at:
    'sessionStorage, the loop guard for useVersionCheck’s self-reload. Not '
    + 'student state: clearing it would defeat the guard it exists to be.',
  edubotics_boot_scrub:
    'sessionStorage, owned by utils/bootScrub. It holds the `?fresh=<nonce>` a '
    + 'freshly spawned window carried, so the boot scrub runs once per WINDOW '
    + 'instead of once per document load — useVersionCheck reloads the page on '
    + 'an image update, and a re-fired scrub would sign a student out '
    + 'mid-lesson. Same category as __edubotics_version_reload_at: it is the '
    + 'guard, not student state, and clearing it would defeat what it exists to '
    + 'be. It is also unreachable from clearStudentScopedStorage, which is a '
    + 'localStorage loop.',
  'edubotics:workshop:autosave':
    'IndexedDB (idb-keyval), not Web Storage, so clearStudentScopedStorage — a '
    + 'synchronous localStorage loop — structurally cannot clear it. It is '
    + 'student work, but it is already per-student: components/Workshop/'
    + 'useAutosave namespaces EVERY bucket, with `:<supabase user id>` when '
    + 'signed in and `:<browser-session id>` when not — the latter narrowed by '
    + 'the student login gate (utils/authGate) to the „Ohne Anmeldung '
    + 'fortfahren" offline escape alone, but still reachable — so one student '
    + 'never reads another’s, and deleting a student’s crash-recovery cache on '
    + 'sign-out would destroy work to prevent a disclosure the namespacing '
    + 'already prevents. The bare name itself is written by nothing any more; '
    + 'useAutosave deletes a pre-namespacing leftover once on mount.',
  edubotics_workshop_autosave_session:
    'sessionStorage, owned by components/Workshop/useAutosave::'
    + 'autosaveSessionScope. It is the ANONYMOUS half of the identity that '
    + '`edubotics_userId` carries when a student IS signed in — since the '
    + 'student login gate reachable only via the „Ohne Anmeldung fortfahren" '
    + 'offline escape, which is rarer but not gone, so this entry STAYS — so '
    + 'by the operative rule it is student-scoped, but it cannot go in '
    + 'STUDENT_SCOPED_KEYS, because clearStudentScopedStorage is a '
    + 'localStorage loop and would silently no-op on it while the test that '
    + 'seeds keys into localStorage passed. Same reason '
    + 'edubotics.jetson.reconnect sits here. It does not NEED the sign-out '
    + 'scrub either: it is per browser session by construction, which for the '
    + 'student surface is the WebView2 window, so the handover it has to '
    + 'survive ends it. The residual — a handover WITHOUT closing that window, '
    + 'after which the session and therefore the bucket carry over — is in '
    + 'docs/KNOWN-ISSUES.md.',
});

/**
 * Remove every student-scoped key from localStorage.
 *
 * Best-effort and never throws: a failed scrub must not be able to block a
 * sign-out, which is the more important of the two.
 *
 * Storage is only HALF the leak — the same values are live in Redux, where
 * `setTaskInfo` re-persists `edubotics_userId` on the next keystroke. That is
 * why this is not exported as a sign-out step in its own right; call
 * `utils/signOut::signOutStudent`, which does both and cannot do one without
 * the other.
 */
export function clearStudentScopedStorage() {
  for (const key of STUDENT_SCOPED_KEYS) {
    try {
      localStorage.removeItem(key);
    } catch (_) {
      /* storage unavailable / disabled — nothing to scrub */
    }
  }
}

/**
 * The persisted Supabase session, by PATTERN. `sb-<project-ref>-auth-token` and
 * the PKCE sibling `…-auth-token-code-verifier` are named after the project
 * ref, which this layer does not know, so a pattern is the only form available
 * — the one place the allowlist doctrine above cannot apply, and it applies to
 * keys this app does not own rather than to its own.
 *
 * ONLY for a revoke that reported failure. `supabase.auth.signOut()` normally
 * removes both, but `@supabase/auth-js::_signOut` returns `{ error }` and skips
 * `_removeSession()` for any admin-signOut status other than 404/401/403 — the
 * promise RESOLVES, nothing throws, and the session key SURVIVES. On a
 * classroom PC behind a captive portal answering 502 that leaves the previous
 * student's session restorable, which the page reload then restores while
 * presenting a completed handover.
 *
 * Best-effort and never throws, like every other access in this module.
 *
 * Enumerated through the spec'd `length` / `key(i)` pair rather than
 * `Object.keys(localStorage)`: both work in a browser, but only the former
 * works against `setupTests.js`'s in-memory Storage, whose keys live in a Map
 * and not as own enumerable properties — and a sweep no test can exercise is
 * not a sweep. Names are snapshotted first because deleting shifts the indices
 * of everything after the deleted key.
 */
export function clearSupabaseSessionKeys() {
  try {
    const names = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      // `-user` joins the pattern: @supabase/auth-js writes `sb-<ref>-auth-token-user`
      // (id, email, user_metadata) from `_saveSession` when `settings.userStorage`
      // is set. We pass no options today so it is not written — but this sweep
      // exists precisely for the path where `_removeSession()` was SKIPPED, which
      // is the path that would leave it behind, and matching it is free.
      //
      // Because it is unwritten today, no ordinary fixture exercises it: deleting
      // the `-user` alternative left the whole React suite green (measured
      // 2026-08-31). It is therefore fenced BY NAME in
      // `__tests__/sessionScope.test.js` — if you remove it here, that test is
      // the one that goes red, and it is not stale.
      if (typeof key === 'string' && /^sb-.+-auth-token(-code-verifier|-user)?$/.test(key)) {
        names.push(key);
      }
    }
    for (const key of names) localStorage.removeItem(key);
  } catch (_) {
    /* storage unavailable / disabled — nothing to sweep */
  }
}

export default clearStudentScopedStorage;
