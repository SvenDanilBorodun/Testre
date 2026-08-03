// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// THE sign-out. One thunk, every caller, no ordering for anyone to get wrong.
//
// It replaces six hand-written copies of the same four-step sequence spread
// over five files (StudentApp ×1, WebApp ×2, TrainingPage ×1, InferencePage ×1,
// useMeProfile ×1), whose ORDER was documented as prose in two other modules
// and enforced nowhere.
//
// THE ORDERING RULE: everything LOCAL and SYNCHRONOUS completes before the one
// remote call, and the remote call's own failure is handled rather than assumed
// away. The single `await` is a window in which the whole document keeps
// running — every timer, socket callback and pending promise — so anything left
// on the far side of it is state the previous student's browser can re-establish
// while the sign-out is still in flight.
//
//   1. `resetJetsonOnLogout` FIRST, and it is the ONE step that must precede
//      the revoke for a reason of its own: the beacon-style Jetson release is
//      authenticated with the JWT step 4 revokes. Skipping it leaks the
//      classroom-Jetson lock for the full 5-minute sweeper window. The token is
//      read out of `getState()` before anything resets, so step 2 cannot
//      invalidate it.
//   2. `session/signedOut` — the in-memory half, and the half that was missing.
//      `clearStudentScopedStorage` deletes `edubotics_userId` from
//      localStorage, but `state.tasks.taskInfo.userId` is untouched, and
//      `useRosServiceCaller` sends exactly that value as `user_id` on
//      START_RECORD. Storage-only meant the next student recorded under the
//      previous student's Hugging Face account — and their first keystroke in
//      InfoPanel re-dispatched `setTaskInfo`, whose reducer re-persists a
//      truthy `userId`, writing the deleted key straight back.
//      It runs BEFORE the await, not after: while `auth.isAuthenticated` was
//      still true and storage was already scrubbed, one `/task/status` tick
//      (~2 s cadence, against a revoke of hundreds of ms) re-adopted the id
//      into Redux AND back into storage, and the reload then re-hydrated it.
//   3. the storage scrub, so it cannot be skipped by a signOut that throws or
//      by a navigation the auth-state change fires.
//   4. `supabase.auth.signOut()` — AWAITED, and its RESULT is read.
//      `@supabase/auth-js::_signOut` returns `{ error }` and skips
//      `_removeSession()` for any admin-signOut status other than 404/401/403:
//      the promise RESOLVES, nothing throws, and `sb-<ref>-auth-token`
//      SURVIVES. A captive portal answering 502 is the realistic classroom
//      case, and the reload would then restore the previous student's session
//      while presenting a completed handover. `scope: 'local'` is not an
//      escape hatch — `_signOut` calls `admin.signOut()` before it looks at
//      the scope. So a reported failure sweeps the key itself.
//   5. the scrub AGAIN, for whatever the await window re-persisted.
//
// Step 2 is ONE action every slice answers for itself
// (`features/session/sessionActions`), not a list of resets kept here: a list
// in this file is a list of "which slices hold student data" maintained by the
// file that owns none of them.
//
// THE RELOAD is the completeness backstop, and it is deliberately not
// unconditional — see `reload` below.

import { supabase } from '../lib/supabaseClient';
import { signedOut } from '../features/session/sessionActions';
import { resetJetsonOnLogout } from '../features/jetson/sessionReset';
import { clearStudentScopedStorage, clearSupabaseSessionKeys } from './sessionScope';

/**
 * Drop every scrap of the signed-in student and end the session.
 *
 * A redux THUNK: the access token and the claimed Jetson id come from
 * `getState()` rather than being threaded through by each of the five call
 * sites — three of which used to get one or both of them wrong or omit them.
 *
 * The teacher web uses it too. The student-scoped resets are no-ops for a
 * teacher, and the shared-browser-profile argument applies there just as much:
 * on a classroom PC the teacher signs in and out of the same browser the
 * students use. There is deliberately no „Abgemeldet" success toast any more —
 * the reload lands on the login form, which says it better and cannot be
 * missed, and a toast would be destroyed by that reload anyway.
 *
 * @param {object}  opts
 * @param {boolean} opts.reload  reload the page afterwards (default true).
 *                               Pass FALSE from a path that has just shown a
 *                               German toast the student has to read — a
 *                               reload destroys it. That is the only reason to
 *                               pass false.
 * @returns {Function} a thunk; `dispatch(...)` returns a Promise<void>.
 */
export const signOutStudent = ({ reload = true } = {}) => async (dispatch, getState) => {
  const state = getState() || {};
  const accessToken = state.auth?.session?.access_token ?? null;
  const jetsonId = state.jetson?.jetsonId ?? null;

  resetJetsonOnLogout(dispatch, accessToken, jetsonId);
  dispatch(signedOut());
  clearStudentScopedStorage();

  let revokeFailed = false;
  try {
    const result = await supabase.auth.signOut();
    // A resolved promise carrying an `error` is auth-js reporting that it gave
    // up BEFORE `_removeSession()` — see step 4 in the header. Treated exactly
    // like a throw.
    revokeFailed = Boolean(result && result.error);
  } catch (_) {
    revokeFailed = true;
  }
  if (revokeFailed) {
    clearSupabaseSessionKeys();
  }
  // The await window is over; re-scrub. Redux was already clean and the
  // `/task/status` adopt is identity-gated, but `setTaskInfo` re-persists a
  // truthy `userId` from whatever object ITS caller captured, and every pending
  // handler in the document survived the await. Closing the window costs five
  // `removeItem` calls; reasoning about which callers are still alive does not
  // stay correct.
  clearStudentScopedStorage();

  if (!reload) return;
  // The blunt-but-complete step, and the reason the broadcast does not have to
  // reach every last derived value: a fresh document re-runs every slice's
  // module-level `localStorage.getItem` seed against the storage this function
  // just scrubbed, and drops every timer, poll and websocket closure still
  // holding the old session. On a shared classroom kiosk a sign-out happens
  // once a lesson, so the cost is one page load at exactly the moment the
  // student is leaving. It is a BACKSTOP, never the mechanism — a student can
  // end up here with no reload at all (StudentApp's `handoverStarted` names the
  // two routes), and everything above has to be complete on its own by then.
  // try/catch because an embedded host may refuse navigation.
  try {
    if (typeof window !== 'undefined' && window.location
        && typeof window.location.reload === 'function') {
      window.location.reload();
    }
  } catch (_) {
    /* navigation refused — the broadcast above is what remains */
  }
};

/**
 * WHY a sign-out is refused right now, or null. German text per reason, because
 * a disabled control with no explanation reads as a broken app.
 *
 * Frozen and exported so the wording lives beside the decision instead of in
 * the component that renders it.
 */
export const LOGOUT_BLOCK_TITLES_DE = Object.freeze({
  task: 'Abmelden nicht möglich, solange eine Aufnahme oder Inferenz läuft — bitte zuerst beenden.',
  workflow:
    'Abmelden nicht möglich, solange ein Programm im Roboter Studio läuft — bitte zuerst stoppen.',
  // Names the ESCAPE, because „bitte zuerst abschließen" alone can be advice a
  // student cannot take. The calibration branch is true for the whole normal
  // three-step run AND stays true forever if the run is abandoned:
  // `framesCaptured` LATCHES (only the next step's reset or „Kalibrierung neu
  // starten" zeroes it) and with EDUBOTICS_FORCE_RECALIBRATION on — the default
  // — `!calibrated` holds from session start. A student with no ChArUco board,
  // or in the wrong room, cannot finish. The escape is real and reachable: the
  // block is ANDed with being on „Roboter Studio", and CalibrationWizard renders
  // as an in-page panel rather than a fixed overlay, so the nav rail stays
  // clickable throughout. The guard itself is deliberately NOT weakened — a
  // mid-calibration reload must still not happen by accident.
  calibration:
    'Abmelden nicht möglich, solange die Kalibrierung läuft — bitte zuerst '
    + 'abschließen. Wenn das gerade nicht geht: „Roboter Studio" verlassen und '
    + 'z. B. auf „Start" wechseln, dann kannst du dich abmelden.',
});

/**
 * Pure decision: `'task' | 'workflow' | 'calibration' | null`.
 *
 * `robotLinkLive` gates ALL THREE, and that is the load-bearing part. Every
 * input below is written only by a rosbridge message — `taskStatus.running` by a
 * `/task/status` tick, `workshop.runState` by a `/workflow/status` tick — and
 * NOTHING clears them when the socket dies (`resetTaskStatus` exists and is
 * dispatched nowhere; the heartbeat watchdog writes only `heartbeatStatus`). So
 * a drop while the last tick said RECORDING used to leave „Abmelden" disabled
 * for the life of the document, telling the student in German to stop a
 * recording they can no longer reach — on the shared classroom PC this whole
 * mechanism exists to let them hand over.
 *
 * A stale report is not evidence, so the gate keys on whether the report can
 * still be OBSERVED. The alternative — clearing `running` on the
 * connected→disconnected edge — was rejected: `taskStatus.running` is read by
 * `beforeunload`, by the InfoPanel/InferencePanel editability gates and by
 * RecordPanel, and the server-side recorder keeps recording whether or not the
 * browser can see it, so publishing "no task is running" to all of them would
 * be a lie told to widen one button's availability.
 *
 * Roboter Studio activity is `state.workshop`, never `tasks.taskStatus` — the
 * two are separate surfaces and a guard that checks one misses the other.
 */
export function logoutBlockReason({
  robotLinkLive,
  taskRunning,
  workflowRunning,
  calibrationCapturing,
}) {
  if (!robotLinkLive) return null;
  if (taskRunning) return 'task';
  if (workflowRunning) return 'workflow';
  if (calibrationCapturing) return 'calibration';
  return null;
}

export default signOutStudent;
