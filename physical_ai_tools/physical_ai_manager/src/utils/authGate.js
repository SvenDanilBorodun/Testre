// Pure decision helpers for the STUDENT LOGIN GATE in StudentApp. Extracted for
// the same reason as navGating.js: the decision is unit-testable without
// rendering the full StudentApp graph, and a source-text fence in
// __tests__/authGate.test.js pins the call site so the pure function cannot stay
// green while StudentApp stops asking it the right question.
//
// WHY THE GATE EXISTS. A German school runs EduBotics on Windows student PCs
// under ONE shared Windows account. „Umgebung starten" spawns a fresh WebView2
// window with a `?fresh=` nonce, and utils/bootScrub wipes the previous
// student's Supabase session before Redux or supabase-js ever read localStorage
// — so the window is guaranteed to arrive signed out. Until this gate, the
// student could then jog, hand-guide, program in Roboter Studio and RECORD
// entirely logged out: only Training and Inferenz asked for a session. Every
// handover now starts from a known identity.

import PageType from '../constants/pageType';

/**
 * Which screen StudentApp's <main> should render.
 *
 * @param {object} o
 * @param {boolean} o.isAuthLoading  state.auth.isLoading — the Supabase session
 *   lookup is still in flight.
 * @param {boolean} o.isAuthenticated state.auth.isAuthenticated.
 * @param {boolean} o.offlineOverride the student pressed „Ohne Anmeldung
 *   fortfahren", which only appears after a login attempt PROVED the auth
 *   service unreachable (see isCloudUnreachableAuthError).
 * @param {boolean} o.piMode          this browser is the Orange Pi's own UI.
 * @param {string}  o.page            the current PageType.
 * @returns {'app'|'loading'|'login'}
 */
export function studentAuthGateDecision({
  isAuthLoading,
  isAuthenticated,
  offlineOverride,
  piMode,
  page,
}) {
  // FIRST, so nothing can shadow it. The Orange Pi's System tab is the whole
  // setup wizard AND the only repair surface — Modus/Robotertyp, „Arme
  // scannen", HF-Token, „Umgebung starten", Update, Reset, Protokoll,
  // Netzwerk-Check. docker-compose.opi.yml boots the manager with the robot
  // tier DOWN BY DESIGN, so a student who cannot reach that tab has a brick,
  // and the Netzwerk-Check is precisely the tool that diagnoses WHY a login
  // fails (down link, filtered DNS, skewed clock breaking JWT validation).
  // components/StartupGate.js carves the same page out of its own overlay for
  // the same reason. Everything else on a Pi IS gated.
  if (piMode && page === PageType.SYSTEM) return 'app';
  if (isAuthenticated) return 'app';
  // Above isAuthLoading on purpose: once the escape is taken, re-entering the
  // bootstrap must not flash a spinner over a student who is already working.
  if (offlineOverride) return 'app';
  if (isAuthLoading) return 'loading';
  return 'login';
}

/**
 * Did the auth service fail to ANSWER, rather than answer about this request?
 *
 * Reached ⇒ offer the offline escape. Not reached ⇒ the service made a
 * judgement about THIS request, so the student's credentials are the problem
 * and an escape would just be a bypass button.
 *
 * The classification follows @supabase/auth-js's own (lib/fetch.js):
 *   * fetch itself rejects (no network, DNS, CORS) → AuthRetryableFetchError,
 *     status 0 — MEASURED against an unreachable host, alongside a 10.5 s wait;
 *   * 502/503/504 → AuthRetryableFetchError with that status;
 *   * any other non-OK HTTP → AuthApiError with that status (400 wrong
 *     password, 429 rate limit, 5xx a paused/broken project);
 *   * lib/supabaseClient's buildStub() — an image built with no Supabase args —
 *     throws synchronously with NO status at all. A mis-built image must not
 *     brick a classroom; BuildConfigBanner is already screaming in red.
 *
 * @param {*} error
 * @returns {boolean}
 */
export function isCloudUnreachableAuthError(error) {
  if (!error) return false;
  const status = error && error.status;
  // A 4xx is the auth service making a judgement about this request: wrong
  // password (400), rate limit (429), disabled user (403). The cloud is
  // reachable; an escape here would just be a bypass button.
  if (Number.isInteger(status) && status >= 400 && status < 500) return false;
  // Everything else: fetch rejected (AuthRetryableFetchError, status 0), a
  // 5xx / 502-504 gateway, the unconfigured-build stub's bare throw (no status
  // at all). All of them mean "we could not ask".
  return true;
}
