// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Jetson half of sign-out, plus the refresh-vs-close marker it consumes.
//
// It lives here rather than in `hooks/useJetsonConnection` because
// `utils/signOut` needs it, and a util importing a hook pulls react-redux,
// react-hot-toast and the rosbridge singleton into the util layer. Dependencies
// run one way: the hook imports this, `utils/signOut` imports this, and this
// imports neither.
//
// It is deliberately NOT a `session/signedOut` listener like every other slice.
// It is not a reset — it is an authenticated network CALL that has to happen
// while the JWT is still valid, so it is ordered explicitly as the first step
// of `signOutStudent` instead of reacting to the action that ends the session.

import { clearJetson } from '../../store/jetsonSlice';
import { releaseJetsonBeacon } from '../../services/jetsonClient';
import rosConnectionManager from '../../utils/rosConnectionManager';

// Refresh-vs-close discriminator (M4). On unload while connected the hook drops
// this sessionStorage marker and then fires the release beacon. sessionStorage
// survives a same-tab refresh but is wiped on a real tab close, so on the next
// mount the discovery effect can tell a refresh (→ auto-reclaim the lock the
// beacon just freed) from a genuine close (→ leave the lock freed for the next
// student).
export const RECONNECT_MARKER_KEY = 'edubotics.jetson.reconnect';

export function readReconnectMarker() {
  try {
    const raw = window.sessionStorage.getItem(RECONNECT_MARKER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;  // sessionStorage unavailable / malformed — treat as no marker
  }
}

export function writeReconnectMarker(classroomId) {
  try {
    window.sessionStorage.setItem(
      RECONNECT_MARKER_KEY,
      JSON.stringify({ classroomId, at: Date.now() })
    );
  } catch (_) {
    /* best-effort: sessionStorage unavailable / full */
  }
}

export function clearReconnectMarker() {
  try {
    window.sessionStorage.removeItem(RECONNECT_MARKER_KEY);
  } catch (_) {
    /* best-effort */
  }
}

/**
 * Reset everything Jetson-side on logout. MUST run BEFORE
 * `supabase.auth.signOut()` invalidates the JWT, otherwise the release call
 * cannot authenticate and the lock leaks for the full 5-min sweeper window.
 * `utils/signOut::signOutStudent` is what guarantees that order.
 *
 * Optional jetsonId + accessToken drive a best-effort beacon release (matching
 * the tab-close path); without both, the slice clear still happens.
 */
export function resetJetsonOnLogout(dispatch, accessToken = null, jetsonId = null) {
  // Best-effort beacon release so the lock frees immediately on explicit logout
  // instead of waiting 5 min for the sweeper. The beacon path revalidates the
  // JWT server-side, so a slightly-stale token still works as long as it hasn't
  // expired.
  if (accessToken && jetsonId) {
    try { releaseJetsonBeacon(accessToken, jetsonId); } catch (_) { /* swallow */ }
  }
  // Drop any pending refresh-reconnect marker so a later sign-in to the same
  // classroom doesn't auto-reclaim on a stale marker.
  clearReconnectMarker();
  rosConnectionManager.setAuthToken(null);
  dispatch(clearJetson());
}

export default resetJetsonOnLogout;
