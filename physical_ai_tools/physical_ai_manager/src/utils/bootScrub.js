// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// STUDENT HANDOVER, browser half. One shared Windows account means one WebView2
// profile, which means ONE localStorage for every student who ever sits at that
// PC. Student A closes the window and walks away without clicking „Abmelden" —
// the path students actually take — and A's Supabase session (auth-js defaults
// BOTH `persistSession` and `autoRefreshToken`, i.e. a durable self-renewing
// credential) is still there when B clicks „Web-Oberfläche öffnen".
//
// WHY THIS RUNS AT BOOT AND NOT AT CLOSE. `webview_window.destroy_all` cannot
// deliver a close hook reliably, and a close can be a crash or a power cut
// anyway. Spawn always runs. So the GUI marks each freshly spawned window with
// `?fresh=<nonce>` (gui_app.py::_open_webview) and this module answers it.
//
// WHY NOT AN rmtree OF THE WHOLE PROFILE. That was the first shape of this fix
// and it was a regression: the WebView2 user-data folder holds localStorage AND
// IndexedDB, so deleting it destroyed the Blockly crash-recovery autosave and
// every MACHINE_SCOPED_KEYS entry — `edubotics_robotType`, whose own comment
// says clearing it costs the next student an arm re-scan, the four dock keys,
// `edubotics_urdf_open`, `edubotics_audio_muted`. It also fired for a student
// who merely closed the window and re-opened it mid-lesson. utils/sessionScope
// already expresses exactly the partition this needs, so the scrub is that
// partition — the person's keys and the Supabase session go, the rig's stay.
//
// NOT `supabase.auth.signOut()`. That call belongs to utils/signOut and to
// nothing else (`sessionScope.test.js` pins it to one non-test file). It is also
// the wrong instrument here: it is a network round trip on a boot path that must
// not block, and there is no session object to revoke on behalf of a student who
// has already walked away — the credential we can reach is the persisted one.
// The server-side refresh token therefore stays valid until it expires; that
// residual is recorded in docs/KNOWN-ISSUES.md and is unchanged from the rmtree.

import clearStudentScopedStorage, { clearSupabaseSessionKeys } from './sessionScope';

/** Query parameter the GUI stamps onto a FRESHLY SPAWNED window. */
export const BOOT_SCRUB_PARAM = 'fresh';

/**
 * sessionStorage latch. Registered in sessionScope's IGNORED_STORAGE_KEYS with
 * the same reasoning as `__edubotics_version_reload_at`: it is the guard, not
 * student state, and clearing it would defeat what it exists to be.
 */
export const BOOT_SCRUB_KEY = 'edubotics_boot_scrub';

/**
 * ONCE PER WINDOW, not once per document load — and the nonce is what makes
 * that true in the SAFE direction.
 *
 * `useVersionCheck` reloads the page whenever `/version.json` advertises a new
 * buildId, i.e. after every image update. `window.location.reload()` keeps the
 * query string, so a flag alone would re-fire the scrub and sign a student out
 * mid-lesson. sessionStorage has exactly the right lifetime to stop that: it
 * survives a reload and dies with the window. The repo already leans on both
 * halves of that — `__edubotics_version_reload_at` is useVersionCheck's own
 * reload-loop guard, and `edubotics_workshop_autosave_session` is documented as
 * per-window because "for the student surface the tab IS the WebView2 window".
 *
 * But a latch alone fails UNSAFE if that lifetime is ever wrong: were
 * sessionStorage to survive a process restart (Chromium does persist it for
 * session restore, and WebView2's behaviour here is not something this repo can
 * prove), a bare flag would scrub once and never again — the next student
 * inherits everything, silently. So the latch stores the NONCE and compares it:
 * a reload carries the same nonce and is a no-op, while a new spawn carries a
 * new one and scrubs regardless of what survived. The remaining dependency runs
 * only in the harmless direction (a sessionStorage that did NOT survive a
 * reload would merely scrub again).
 *
 * @returns {'no-param'|'already'|'scrubbed'|'failed'} for tests and diagnostics.
 */
export function bootScrubOnce() {
  let nonce = '';
  try {
    if (typeof window === 'undefined') return 'no-param';
    nonce = new URLSearchParams(window.location.search).get(BOOT_SCRUB_PARAM) || '';
  } catch (_) {
    return 'no-param';
  }
  if (!nonce) return 'no-param';

  try {
    if (sessionStorage.getItem(BOOT_SCRUB_KEY) === nonce) return 'already';
  } catch (_) {
    // sessionStorage unavailable/disabled. Fall through and scrub: without the
    // latch we cannot prove this window was already handled, and scrubbing an
    // already-scrubbed window costs a re-login while skipping it leaks a
    // session.
  }

  try {
    clearStudentScopedStorage();
    clearSupabaseSessionKeys();
  } catch (_) {
    // Both helpers swallow their own storage errors, so reaching here means
    // something structural. Deliberately do NOT set the latch: re-scrubbing on
    // the next load is a recoverable annoyance, whereas marking a scrub that did
    // not happen hands the next student the previous one's session — the exact
    // defect this module exists for. Fail towards scrubbing again.
    return 'failed';
  }

  try {
    sessionStorage.setItem(BOOT_SCRUB_KEY, nonce);
  } catch (_) {
    // Same direction: an unrecorded scrub re-runs, it does not leak.
  }
  return 'scrubbed';
}

export default bootScrubOnce;
