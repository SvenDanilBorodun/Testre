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
//             THIS browser hold a live session, and the Pi's id (`pi_id`)? A
//             404 is an agent from before the login (`legacy`: version skew,
//             see piLoginOf);
//   * exchange (SA-06, 2026-11-13) — the Supabase access token NEVER goes to
//             the Pi, which is reached over plain HTTP on the school network:
//             1. POST <cloud API>/pi/tickets over HTTPS with the token
//                (Authorization header) and the Pi's id → a one-time ticket
//                (60 s, single-use, valid at this Pi only);
//             2. POST /api/system/auth/session with ONLY that ticket in the
//                body. The agent redeems it at the cloud; the cookie comes
//                back. Again at 60 % of the session's life, on every Supabase
//                token refresh (setPiAccessToken) and after any 401
//                (noteUnauthorized);
//   * claim   POST /api/system/auth/claim — a teacher/admin + the one-time
//             setup code set an UNCLAIMED Pi up (PiClaimScreen), with a
//             ticket minted the same way;
//   * end     POST /api/system/auth/logout (keepalive) — signOutStudent.
//
// A module-level store (useSyncExternalStore), deliberately NOT Redux and NOT
// browser storage: the cookie is the only persistent piece and the browser
// keeps it where script cannot read it. Nothing here is a no-op off a Pi only
// by accident: everything stays in `off` until StudentApp calls startPiAuth().

import { useSyncExternalStore } from 'react';

import { CLOUD_API_URL } from '../../services/cloudConfig';

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
// The cloud route that mints a one-time login ticket (SA-06).
export const TICKET_PATH = '/pi/tickets';

const PROBE_BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];
// Re-exchange at this fraction of the session's life, never sooner than this.
export const REFRESH_FRACTION = 0.6;
const MIN_REFRESH_MS = 20000;
// A session seen alive this recently is trusted by the rosbridge gate without
// asking the agent again.
const VERIFY_FRESH_MS = 10000;
const ERROR_RETRY_MS = 15000;
const UNAUTHORIZED_DEBOUNCE_MS = 500;
// VA-03 (2026-11-12): every exchange costs the cloud a ticket mint and a
// redeem (or the Pi's login budget), and on a school network every browser
// shares ONE address towards the cloud. After one of these answers the gate
// (the rosbridge reconnect loop asks every ≤ 2 s), a 401 and a probe wait out
// ERROR_RETRY_MS before they exchange again; only a new token or the scheduled
// retry ends the wait. `network` (the agent itself did not answer) costs the
// cloud nothing and keeps the fast retry.
const BACKOFF_CODES = new Set(['rate_limited', 'cloud_unreachable']);
// Refusals the next attempt with the SAME Supabase token cannot fix: the cloud
// refused the token at the mint (`token_rejected`), or refused a ticket twice
// in a row (`ticket_rejected`, see exchange). Not sent again until supabase-js
// delivers another token (VA-03, adapted to the tickets).
const REJECTED_CODES = new Set(['token_rejected', 'ticket_rejected']);

// German sentences for the codes the agent answers (its own `message` wins).
export const PI_AUTH_TEXT_DE = Object.freeze({
  token_rejected:
    'Die EduBotics-Cloud hat die Anmeldung nicht angenommen. Bitte abmelden und erneut anmelden.',
  ticket_rejected:
    'Der Roboter hat die Anmeldung nicht angenommen. Bitte die Seite neu laden; hilft das nicht, '
    + 'abmelden und erneut anmelden.',
  ticket_missing: 'Die Anmeldung fehlt. Bitte die Seite neu laden und anmelden.',
  cloud_from_browser:
    'Die EduBotics-Cloud ist von diesem Gerät aus gerade nicht erreichbar, deshalb kann die '
    + 'Anmeldung am Roboter nicht vorbereitet werden. Bitte die Internetverbindung prüfen.',
  pi_id_missing:
    'Der Roboter hat keine Kennung gemeldet, die Anmeldung ist deshalb nicht möglich. Bitte die '
    + 'Pi-Software im System-Fenster aktualisieren („Aktualisieren“).',
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
    piId: null, // the Pi's id a login ticket is minted for (/auth/state)
    error: null, // { code, message }
    resetCode: null, // a code a factory reset just answered (memory only)
  };
}

let state = initialState();
const listeners = new Set();
let started = false;
let fetchImpl = (...args) => fetch(...args);
let cloudApiUrl = CLOUD_API_URL || '';
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
// The access token an exchange was refused for with `token_rejected` or a
// second `ticket_rejected`. Never used again: only a different token
// (setPiAccessToken) is tried. Memory only, never browser storage (VA-03).
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
    const piId = typeof json.pi_id === 'string' && json.pi_id ? json.pi_id : null;
    if (piId !== state.piId) setState({ piId });
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
 * Mint a one-time login ticket for THIS Pi from the cloud (SA-06): the only
 * request that carries the Supabase token, and it goes over HTTPS to the cloud
 * API, never to the Pi. No cookie (credentials: 'omit'); the cloud answers this
 * one path to any origin, since a Pi's LAN address cannot be listed.
 * @returns {Promise<{ok: true, ticket: string}|{ok: false, code: string, message: string}>}
 */
async function mintTicket(token) {
  const piId = state.piId;
  if (!piId) return { ok: false, code: 'pi_id_missing', message: PI_AUTH_TEXT_DE.pi_id_missing };
  if (!cloudApiUrl) {
    return { ok: false, code: 'cloud_unreachable', message: PI_AUTH_TEXT_DE.cloud_from_browser };
  }
  let res;
  try {
    res = await fetchImpl(`${cloudApiUrl}${TICKET_PATH}`, {
      method: 'POST',
      cache: 'no-store',
      credentials: 'omit',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ pi_id: piId }),
    });
  } catch {
    res = null;
  }
  if (!res) return { ok: false, code: 'cloud_unreachable', message: PI_AUTH_TEXT_DE.cloud_from_browser };
  const json = res.ok ? await readJson(res) : null;
  if (res.ok && json && typeof json.ticket === 'string' && json.ticket) {
    return { ok: true, ticket: json.ticket };
  }
  if (res.status === 401 || res.status === 403) {
    return { ok: false, code: 'token_rejected', message: PI_AUTH_TEXT_DE.token_rejected };
  }
  if (res.status === 429) {
    return { ok: false, code: 'rate_limited', message: PI_AUTH_TEXT_DE.rate_limited };
  }
  if (res.status === 400) probe(); // the Pi's id may have changed (a factory reset)
  return { ok: false, code: 'cloud_unreachable', message: PI_AUTH_TEXT_DE.cloud_from_browser };
}

/** Post a body to the agent; null when the agent could not be asked. */
async function postToAgent(url, body) {
  try {
    return await fetchImpl(url, {
      method: 'POST',
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    return null;
  }
}

function refuse(code, message, token) {
  refreshTimer = clearTimer(refreshTimer);
  lastRefusedAt = now();
  setState({ phase: PI_AUTH_PHASE.ERROR, error: { code, message }, expiresAt: 0 });
  if (REJECTED_CODES.has(code)) {
    // The same token can only be refused again: wait for supabase-js to
    // deliver another one (VA-03).
    if (accessToken === token) rejectedToken = token;
    errorTimer = clearTimer(errorTimer);
  } else if (code !== 'pi_id_missing') {
    scheduleErrorRetry();
  }
  return state;
}

/**
 * Trade the Supabase access token for a robot session (single-flight), through
 * a one-time ticket (SA-06): the token goes to the cloud, the ticket to the Pi.
 * A ticket the Pi's redeem refused (`ticket_rejected`: during a blue-green
 * deploy of the cloud API a ticket minted on the old instance is unknown to the
 * new one) is replaced by a freshly minted one ONCE; refused again, it is
 * handled like a rejected token. A token the cloud rejected is never sent
 * again (only a new one is), and after a refusal in BACKOFF_CODES nothing is
 * sent for ERROR_RETRY_MS unless the token changed or the scheduled retry asks
 * (VA-03).
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
    for (let attempt = 0; attempt < 2; attempt += 1) {
      // eslint-disable-next-line no-await-in-loop
      const minted = await mintTicket(token);
      if (!minted.ok) return refuse(minted.code, minted.message, token);
      // eslint-disable-next-line no-await-in-loop
      const res = await postToAgent(SESSION_URL, { ticket: minted.ticket });
      if (!res) {
        setState({
          phase: PI_AUTH_PHASE.ERROR,
          error: { code: 'network', message: PI_AUTH_TEXT_DE.network },
        });
        scheduleErrorRetry();
        return state;
      }
      // eslint-disable-next-line no-await-in-loop
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
      if (code === 'ticket_rejected' && attempt === 0) continue;
      const message = typeof json.message === 'string' && json.message
        ? json.message
        : PI_AUTH_TEXT_DE[code] || PI_AUTH_TEXT_DE.network;
      return refuse(code, message, token);
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
 * @param {string} [opts.cloudApiUrl] injected cloud API base (tests)
 */
export function startPiAuth({ fetch: injected, cloudApiUrl: injectedCloud } = {}) {
  if (injected) fetchImpl = injected;
  if (typeof injectedCloud === 'string') cloudApiUrl = injectedCloud;
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
  cloudApiUrl = CLOUD_API_URL || '';
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
 * Claim an unclaimed Pi with the setup code (a teacher/admin login). The login
 * travels as a one-time ticket (SA-06), minted from the cloud like an exchange's;
 * a ticket the Pi's redeem refused is replaced once by a fresh one.
 * @returns {Promise<{ok: boolean, code?: string, message?: string}>}
 */
export async function claimPi(code) {
  const token = accessToken;
  if (!token) {
    return { ok: false, code: 'ticket_missing', message: 'Bitte zuerst anmelden.' };
  }
  if (!state.piId) await probe();
  for (let attempt = 0; attempt < 2; attempt += 1) {
    // eslint-disable-next-line no-await-in-loop
    const minted = await mintTicket(token);
    if (!minted.ok) return { ok: false, code: minted.code, message: minted.message };
    // eslint-disable-next-line no-await-in-loop
    const res = await postToAgent(CLAIM_URL, { code: String(code || ''), ticket: minted.ticket });
    if (!res) return { ok: false, code: 'network', message: PI_AUTH_TEXT_DE.network };
    // eslint-disable-next-line no-await-in-loop
    const json = (await readJson(res)) || {};
    if (res.ok && json.ok) {
      applySession(Number(json.expires_in), json.role);
      return { ok: true, message: json.message || '' };
    }
    if (json.code === 'ticket_rejected' && attempt === 0) continue;
    if (json.code === 'already_claimed') probe();
    return {
      ok: false,
      code: typeof json.code === 'string' ? json.code : 'network',
      message: typeof json.message === 'string' && json.message
        ? json.message
        : PI_AUTH_TEXT_DE.network,
    };
  }
  return { ok: false, code: 'ticket_rejected', message: PI_AUTH_TEXT_DE.ticket_rejected };
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
