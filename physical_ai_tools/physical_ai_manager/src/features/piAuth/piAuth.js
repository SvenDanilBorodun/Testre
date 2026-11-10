// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The Orange Pi's ROBOT LOGIN, browser side (2026-11-10).
//
// On a Pi every robot transport on port 80 — rosbridge, the camera stream, the
// dataset sidecar and the agent's /api/system — needs a ROBOT SESSION: an
// HttpOnly, SameSite=Strict cookie the pi-agent mints after an EduBotics login
// (pi_agent/pi_auth.py). This module gets and keeps one:
//
//   * probe   GET  /api/system/auth/state  (public): is the Pi set up, does
//             THIS browser hold a live session? A 404 is an agent from before
//             the login (`legacy`: version skew, see piLoginOf);
//   * exchange POST /api/system/auth/session with the Supabase access token
//             (Authorization header). The agent asks the cloud's GET /me with
//             it once and forgets it; the cookie comes back. Again at 60 % of
//             the session's life, on every Supabase token refresh
//             (setPiAccessToken) and after any 401 (noteUnauthorized);
//   * claim   POST /api/system/auth/claim — a teacher/admin + the one-time
//             setup code set an UNCLAIMED Pi up (PiClaimScreen);
//   * end     POST /api/system/auth/logout (keepalive) — signOutStudent.
//
// A module-level store (useSyncExternalStore), deliberately NOT Redux and NOT
// browser storage: the cookie is the only persistent piece and the browser
// keeps it where script cannot read it. Nothing here is a no-op off a Pi only
// by accident: everything stays in `off` until StudentApp calls startPiAuth().

import { useSyncExternalStore } from 'react';

export const PI_AUTH_PHASE = Object.freeze({
  OFF: 'off', // not a Pi (or not started): no gate at all
  PROBING: 'probing', // first /auth/state on its way
  UNREACHABLE: 'unreachable', // the agent does not answer (boot, crash)
  LEGACY: 'legacy', // an agent from before the robot login (404)
  UNCLAIMED: 'unclaimed', // the Pi is not set up yet
  LOCKED: 'locked', // set up, no session in this browser
  EXCHANGING: 'exchanging', // token → session in flight
  SESSION: 'session', // a live robot session
  ERROR: 'error', // the exchange was refused / could not be checked
});

const STATE_URL = '/api/system/auth/state';
const SESSION_URL = '/api/system/auth/session';
const CLAIM_URL = '/api/system/auth/claim';
const LOGOUT_URL = '/api/system/auth/logout';

const PROBE_BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];
// Re-exchange at this fraction of the session's life, never sooner than this.
export const REFRESH_FRACTION = 0.6;
const MIN_REFRESH_MS = 20000;
// A session seen alive this recently is trusted by the rosbridge gate without
// asking the agent again.
const VERIFY_FRESH_MS = 10000;
const ERROR_RETRY_MS = 15000;
const UNAUTHORIZED_DEBOUNCE_MS = 500;
// VA-03 (2026-11-12): every exchange costs the cloud a GET /me (or the Pi's
// login budget), and on a school network every browser shares ONE address
// towards the cloud. After one of these answers the gate (the rosbridge
// reconnect loop asks every ≤ 2 s), a 401 and a probe wait out ERROR_RETRY_MS
// before they exchange again; only a new token or the scheduled retry ends the
// wait. `network` (the agent itself did not answer) costs the cloud nothing and
// keeps the fast retry.
const BACKOFF_CODES = new Set(['rate_limited', 'cloud_unreachable']);

// German sentences for the codes the agent answers (its own `message` wins).
export const PI_AUTH_TEXT_DE = Object.freeze({
  token_rejected:
    'Der Roboter hat die Anmeldung nicht angenommen. Bitte abmelden und erneut anmelden.',
  token_missing: 'Die Anmeldung fehlt. Bitte die Seite neu laden und anmelden.',
  cloud_unreachable:
    'Der Roboter erreicht die EduBotics-Cloud gerade nicht und kann die Anmeldung deshalb '
    + 'nicht prüfen. Bitte die Internetverbindung des Pis prüfen (System → Netzwerk-Check).',
  rate_limited: 'Zu viele Anmeldeversuche — bitte eine Minute warten.',
  network: 'Der Roboter antwortet gerade nicht. Bitte kurz warten.',
  legacy:
    'Die Pi-Software ist älter als diese Weboberfläche. Roboter, Kamera und Daten bleiben '
    + 'gesperrt, bis sie aktualisiert ist: bitte im System-Fenster „Aktualisieren“ ausführen.',
});

function initialState() {
  return {
    phase: PI_AUTH_PHASE.OFF,
    claimed: null,
    hostname: '',
    role: null,
    expiresAt: 0,
    setupCode: null, // 'none' | 'active' | 'void' (unclaimed only)
    error: null, // { code, message }
    resetCode: null, // a code a factory reset just answered (memory only)
  };
}

let state = initialState();
const listeners = new Set();
let started = false;
let fetchImpl = (...args) => fetch(...args);
let accessToken = null;
let authResolved = false;
let hygieneDone = false;
let lastVerifiedAt = 0;
let probeAttempt = 0;
let probeTimer = null;
let refreshTimer = null;
let errorTimer = null;
let suspectTimer = null;
let probeInFlight = null;
let exchangeInFlight = null;
// The access token an exchange was refused for with `token_rejected`. Never
// sent again: only a different token (setPiAccessToken) is tried. Memory
// only, never browser storage (VA-03).
let rejectedToken = null;
let lastRefusedAt = 0;

const now = () => Date.now();

function setState(patch) {
  state = { ...state, ...patch };
  listeners.forEach((fn) => {
    try {
      fn();
    } catch (_) {
      /* a listener never breaks the store */
    }
  });
}

function clearTimer(t) {
  if (t) clearTimeout(t);
  return null;
}

export function getPiAuthState() {
  return state;
}

export function subscribePiAuth(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function usePiAuth() {
  return useSyncExternalStore(subscribePiAuth, getPiAuthState, getPiAuthState);
}

/**
 * What the robot login means for the login gate (utils/authGate):
 *   'none'      no robot login here: not a Pi, an agent that does not answer,
 *               or one from before the login (legacy) — today's behaviour;
 *   'pending'   the first answer is on its way;
 *   'unclaimed' the Pi is not set up: the claim screen, on every page;
 *   'required'  set up: every tab, the System tab included, needs a login.
 */
export function piLoginOf(s) {
  switch (s && s.phase) {
    case PI_AUTH_PHASE.PROBING:
      return 'pending';
    case PI_AUTH_PHASE.UNCLAIMED:
      return 'unclaimed';
    case PI_AUTH_PHASE.LOCKED:
    case PI_AUTH_PHASE.EXCHANGING:
    case PI_AUTH_PHASE.SESSION:
    case PI_AUTH_PHASE.ERROR:
      return 'required';
    default:
      return 'none';
  }
}

async function readJson(res) {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

function scheduleRefresh(expiresInS) {
  refreshTimer = clearTimer(refreshTimer);
  const ms = Math.max(MIN_REFRESH_MS, Math.floor(expiresInS * 1000 * REFRESH_FRACTION));
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    if (accessToken) exchange();
    else probe();
  }, ms);
}

function scheduleProbeRetry() {
  probeTimer = clearTimer(probeTimer);
  const delay = PROBE_BACKOFF_MS[Math.min(probeAttempt, PROBE_BACKOFF_MS.length - 1)];
  probeAttempt += 1;
  probeTimer = setTimeout(() => {
    probeTimer = null;
    probe();
  }, delay);
}

function scheduleErrorRetry() {
  errorTimer = clearTimer(errorTimer);
  errorTimer = setTimeout(() => {
    errorTimer = null;
    if (accessToken && state.phase === PI_AUTH_PHASE.ERROR) exchange({ retry: true });
  }, ERROR_RETRY_MS);
}

/** True when the current token is the one the cloud refused (VA-03). */
function tokenRejected() {
  return accessToken !== null && accessToken === rejectedToken;
}

/** True while a refusal that a new attempt cannot fix yet is fresh (VA-03). */
function inBackoff() {
  return state.phase === PI_AUTH_PHASE.ERROR
    && BACKOFF_CODES.has(state.error && state.error.code)
    && now() - lastRefusedAt < ERROR_RETRY_MS;
}

function applySession(expiresIn, role) {
  const secs = Number.isFinite(expiresIn) && expiresIn > 0 ? expiresIn : 60;
  lastVerifiedAt = now();
  setState({
    phase: PI_AUTH_PHASE.SESSION,
    claimed: true,
    role: role || state.role,
    expiresAt: now() + secs * 1000,
    error: null,
    resetCode: null,
  });
  scheduleRefresh(secs);
}

/** GET /auth/state once (single-flight) and move the phase accordingly. */
export function probe() {
  if (!started) return Promise.resolve(state);
  if (probeInFlight) return probeInFlight;
  probeInFlight = (async () => {
    let res;
    try {
      res = await fetchImpl(`${STATE_URL}?_=${now()}`, {
        cache: 'no-store',
        credentials: 'same-origin',
      });
    } catch {
      res = null;
    }
    if (!res) {
      if (state.phase === PI_AUTH_PHASE.PROBING || state.phase === PI_AUTH_PHASE.OFF) {
        setState({ phase: PI_AUTH_PHASE.UNREACHABLE });
      }
      scheduleProbeRetry();
      return state;
    }
    if (res.status === 404) {
      // An agent from before the robot login. This manager's nginx gates the
      // transports on a session that agent cannot mint (version skew): say so.
      probeAttempt = 0;
      setState({ phase: PI_AUTH_PHASE.LEGACY, error: null });
      return state;
    }
    const json = res.ok ? await readJson(res) : null;
    if (!json || json.auth !== 1) {
      setState({ phase: PI_AUTH_PHASE.UNREACHABLE });
      scheduleProbeRetry();
      return state;
    }
    probeAttempt = 0;
    const hostname = typeof json.hostname === 'string' ? json.hostname : '';
    if (!json.claimed) {
      refreshTimer = clearTimer(refreshTimer);
      setState({
        phase: PI_AUTH_PHASE.UNCLAIMED,
        claimed: false,
        hostname,
        role: null,
        expiresAt: 0,
        setupCode: typeof json.setup_code === 'string' ? json.setup_code : null,
      });
      return state;
    }
    if (json.session) {
      // Hygiene: a session cookie in a browser with NO EduBotics login belongs
      // to whoever used this browser before (a sign-out whose logout never
      // reached the Pi). End it instead of adopting it.
      if (authResolved && !accessToken) {
        hygieneDone = true;
        setState({ hostname, claimed: true });
        endPiSession();
        return state;
      }
      setState({ hostname });
      applySession(Number(json.expires_in), json.role);
      return state;
    }
    refreshTimer = clearTimer(refreshTimer);
    setState({
      phase: state.phase === PI_AUTH_PHASE.ERROR ? PI_AUTH_PHASE.ERROR : PI_AUTH_PHASE.LOCKED,
      claimed: true,
      hostname,
      role: null,
      expiresAt: 0,
    });
    if (accessToken) exchange();
    return state;
  })().finally(() => {
    probeInFlight = null;
  });
  return probeInFlight;
}

/**
 * Trade the Supabase access token for a robot session (single-flight).
 * A token the cloud rejected is never sent again (only a new one is), and
 * after a refusal in BACKOFF_CODES nothing is sent for ERROR_RETRY_MS unless
 * the token changed or the scheduled retry asks (VA-03).
 * @param {object} [opts]
 * @param {boolean} [opts.retry] the error retry or a new token: skip the back-off
 */
export function exchange({ retry = false } = {}) {
  if (!started || !accessToken) return Promise.resolve(state);
  if (exchangeInFlight) return exchangeInFlight;
  if (tokenRejected()) return Promise.resolve(state);
  if (!retry && inBackoff()) return Promise.resolve(state);
  const token = accessToken;
  if (state.phase !== PI_AUTH_PHASE.SESSION) setState({ phase: PI_AUTH_PHASE.EXCHANGING });
  exchangeInFlight = (async () => {
    let res;
    try {
      res = await fetchImpl(SESSION_URL, {
        method: 'POST',
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: '{}',
      });
    } catch {
      res = null;
    }
    if (!res) {
      setState({
        phase: PI_AUTH_PHASE.ERROR,
        error: { code: 'network', message: PI_AUTH_TEXT_DE.network },
      });
      scheduleErrorRetry();
      return state;
    }
    const json = (await readJson(res)) || {};
    if (res.ok && json.ok) {
      applySession(Number(json.expires_in), json.role);
      return state;
    }
    if (res.status === 404) {
      setState({ phase: PI_AUTH_PHASE.LEGACY, error: null });
      return state;
    }
    if (json.code === 'unclaimed') {
      exchangeInFlight = null;
      return probe();
    }
    const code = typeof json.code === 'string' ? json.code : 'network';
    const message = typeof json.message === 'string' && json.message
      ? json.message
      : PI_AUTH_TEXT_DE[code] || PI_AUTH_TEXT_DE.network;
    refreshTimer = clearTimer(refreshTimer);
    lastRefusedAt = now();
    setState({ phase: PI_AUTH_PHASE.ERROR, error: { code, message }, expiresAt: 0 });
    if (code === 'token_rejected') {
      // The cloud judged THIS token: asking again with it can only be refused
      // again. Wait for supabase-js to deliver another one (VA-03).
      if (accessToken === token) rejectedToken = token;
      errorTimer = clearTimer(errorTimer);
    } else {
      scheduleErrorRetry();
    }
    return state;
  })().finally(() => {
    exchangeInFlight = null;
  });
  return exchangeInFlight;
}

/**
 * Start the robot login (Pi mode only; StudentApp). Idempotent.
 * @param {object} [opts]
 * @param {Function} [opts.fetch] injected fetch (tests)
 */
export function startPiAuth({ fetch: injected } = {}) {
  if (injected) fetchImpl = injected;
  if (started) return;
  started = true;
  setState({ phase: PI_AUTH_PHASE.PROBING });
  probe();
}

/** Tests only: forget everything. */
export function resetPiAuthForTests() {
  probeTimer = clearTimer(probeTimer);
  refreshTimer = clearTimer(refreshTimer);
  errorTimer = clearTimer(errorTimer);
  suspectTimer = clearTimer(suspectTimer);
  started = false;
  fetchImpl = (...args) => fetch(...args);
  accessToken = null;
  authResolved = false;
  hygieneDone = false;
  lastVerifiedAt = 0;
  probeAttempt = 0;
  probeInFlight = null;
  exchangeInFlight = null;
  rejectedToken = null;
  lastRefusedAt = 0;
  state = initialState();
  listeners.forEach((fn) => fn());
}

/**
 * The current Supabase access token (or null), pushed by StudentApp on every
 * change. A NEW token re-exchanges at once (the requirement: re-exchange on
 * token refresh). A token going to null does NOT end the session here: the
 * sign-out ends it itself, AFTER the robot's token slot is cleared over the
 * open websocket (utils/signOut).
 */
export function setPiAccessToken(token, { resolved = true } = {}) {
  const next = typeof token === 'string' && token ? token : null;
  const changed = next !== accessToken;
  accessToken = next;
  if (changed && next !== rejectedToken) rejectedToken = null;
  if (resolved) authResolved = true;
  if (!started) return;
  if (resolved && !hygieneDone) {
    hygieneDone = true;
    if (!next && state.phase === PI_AUTH_PHASE.SESSION) {
      endPiSession();
      return;
    }
  }
  if (!next || !changed) return;
  if (
    state.phase === PI_AUTH_PHASE.LOCKED
    || state.phase === PI_AUTH_PHASE.ERROR
    || state.phase === PI_AUTH_PHASE.SESSION
  ) {
    exchange({ retry: true });
  }
}

/**
 * Something answered 401 (the agent, nginx in front of the camera or the
 * dataset API): the session may be gone (an agent restart wipes them all).
 * Re-check soon — debounced, so a burst of refusals costs one probe.
 */
export function noteUnauthorized() {
  if (!started) return;
  lastVerifiedAt = 0;
  if (suspectTimer) return;
  suspectTimer = setTimeout(() => {
    suspectTimer = null;
    probe();
  }, UNAUTHORIZED_DEBOUNCE_MS);
}

export class PiLoginError extends Error {
  constructor(reason) {
    super(`pi_login:${reason}`);
    this.name = 'PiLoginError';
    this.reason = reason;
  }
}

/**
 * The rosbridge connect gate (utils/rosConnectionManager): resolves when this
 * browser holds a robot session, rejects with PiLoginError otherwise. Off a Pi
 * (phase `off`) it resolves at once.
 *
 * The browser hides the HTTP status of a refused websocket handshake, so a
 * session that died (an agent restart wipes them all) cannot be told from a
 * robot tier that is down. Hence a session is trusted without asking only for
 * VERIFY_FRESH_MS: the reconnect loop then re-checks it at most every ~10 s.
 */
export async function ensurePiSession() {
  if (!started || state.phase === PI_AUTH_PHASE.OFF) return;
  if (state.phase === PI_AUTH_PHASE.SESSION) {
    if (now() - lastVerifiedAt < VERIFY_FRESH_MS && state.expiresAt - now() > 5000) return;
    await probe();
    if (state.phase === PI_AUTH_PHASE.SESSION) return;
  } else if (
    state.phase === PI_AUTH_PHASE.PROBING
    || state.phase === PI_AUTH_PHASE.UNREACHABLE
  ) {
    await probe();
    if (state.phase === PI_AUTH_PHASE.SESSION) return;
  }
  if (state.phase === PI_AUTH_PHASE.LEGACY) throw new PiLoginError('agent_outdated');
  if (state.phase === PI_AUTH_PHASE.UNCLAIMED) throw new PiLoginError('unclaimed');
  if (state.phase === PI_AUTH_PHASE.UNREACHABLE) throw new PiLoginError('unreachable');
  // VA-03: a token the cloud rejected is not sent again, and a fresh refusal
  // the next attempt cannot fix is waited out (the error retry asks at 15 s).
  if (tokenRejected()) throw new PiLoginError('login_required');
  if (inBackoff()) throw new PiLoginError('retry_later');
  if (accessToken) {
    await exchange();
    if (state.phase === PI_AUTH_PHASE.SESSION) return;
  }
  throw new PiLoginError('login_required');
}

/**
 * Claim an unclaimed Pi with the setup code (a teacher/admin login).
 * @returns {Promise<{ok: boolean, code?: string, message?: string}>}
 */
export async function claimPi(code) {
  if (!accessToken) {
    return { ok: false, code: 'token_missing', message: 'Bitte zuerst anmelden.' };
  }
  let res;
  try {
    res = await fetchImpl(CLAIM_URL, {
      method: 'POST',
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: String(code || '') }),
    });
  } catch {
    return { ok: false, code: 'network', message: PI_AUTH_TEXT_DE.network };
  }
  const json = (await readJson(res)) || {};
  if (res.ok && json.ok) {
    applySession(Number(json.expires_in), json.role);
    return { ok: true, message: json.message || '' };
  }
  if (json.code === 'already_claimed') probe();
  return {
    ok: false,
    code: typeof json.code === 'string' ? json.code : 'network',
    message: typeof json.message === 'string' && json.message
      ? json.message
      : PI_AUTH_TEXT_DE.network,
  };
}

/**
 * End THIS browser's robot session (keepalive: survives the reload that
 * follows a sign-out). Never throws, never waits on the network for the local
 * part: the phase drops at once.
 */
export function endPiSession() {
  refreshTimer = clearTimer(refreshTimer);
  errorTimer = clearTimer(errorTimer);
  if (state.phase === PI_AUTH_PHASE.SESSION || state.phase === PI_AUTH_PHASE.EXCHANGING
      || state.phase === PI_AUTH_PHASE.ERROR) {
    setState({ phase: PI_AUTH_PHASE.LOCKED, role: null, expiresAt: 0, error: null });
  }
  lastVerifiedAt = 0;
  try {
    return Promise.resolve(fetchImpl(LOGOUT_URL, {
      method: 'POST',
      cache: 'no-store',
      credentials: 'same-origin',
      keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })).then(() => true, () => false);
  } catch {
    return Promise.resolve(false);
  }
}

/** signOutStudent's step: a no-op off a Pi or before the login started. */
export function endPiSessionOnLogout() {
  if (!started || state.phase === PI_AUTH_PHASE.OFF || state.phase === PI_AUTH_PHASE.LEGACY) {
    return Promise.resolve(false);
  }
  accessToken = null;
  rejectedToken = null;
  return endPiSession();
}

/** A factory reset answered a new setup code: keep it (memory only) for the
 * claim screen, and re-read the state (the Pi is unclaimed now). */
export function setPiResetCode(code) {
  setState({ resetCode: typeof code === 'string' && code ? code : null });
  probe();
}
