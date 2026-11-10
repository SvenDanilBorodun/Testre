// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The browser's wire to the robot's ONE Hugging-Face token slot.
//
//   READ  /edubotics/hf_token_state  (std_msgs/String, JSON)
//         {"v":1,"seq":N,"accepts":bool,"present":bool,"fp":"16hex"|null,"busy":bool}
//         A fingerprint and three booleans. The robot never says the token.
//   WRITE /register_hf_user          (physical_ai_interfaces/srv/SetHFUser)
//         request.token: a token, or '' to clear the slot.
//
// ENCRYPTED when the robot advertises a key (2026-11-13, hfTokenCrypto.js; the
// audited @noble libraries since 2026-11-14, a lazy chunk): the
// state's additive `enc`/`kid`/`pub` name the robot's ephemeral P-256 key, and
// the token goes out as `enc1:…` in the same field. On an Orange Pi (a plain
// LAN websocket) a token is NEVER sent in the clear: a robot that advertises no
// key gets nothing and the student a German sentence to update it. On Windows
// (loopback) a key is used whenever one is advertised, else the plain token.
// Clearing stays the empty string.
//
// ON A REMOTE ROBOT THE KEY COMES FROM THE AGENT (verification round B,
// VB-03, 2026-11-15). Every account that may use the robot holds a rosbridge session,
// and rosbridge lets any client publish /edubotics/hf_token_state and
// advertise /register_hf_user: a classmate could name their own key and read
// the next student's token. So on a Pi the page encrypts ONLY for the key the
// pi-agent serves behind the robot login (`GET /api/system/robot-key`, read
// from a file the node writes — pi_agent/robot_key.py), and sends nothing while
// the topic advertises another key id: it re-reads both a few times (the node
// may just have restarted: the file changes first, the topic a moment later)
// and then says so in German. A forged topic is ignored; a forged service gets
// a ciphertext for the node's real key, which it cannot open.
//
// „On a Pi" is decided FAIL-CLOSED (verification round C, VC-01): the Pi
// marker fails to non-Pi on a slow or failed fetch, so the robot counts as
// remote whenever the page's own host is not exactly a loopback host, whatever
// the marker said (utils/robotLocality.js::robotIsRemote). Only the Windows
// GUI's own page (`http://localhost/`) takes the loopback path.
//
// THE CLEAR IS NEVER HELD BY A PUSH THAT HAS NOT REACHED THE WIRE (VC-03): the
// service CALLS go through one serial queue (a clear follows a push already on
// the wire), but a push's preparation (the agent read, every read bounded by
// AGENT_KEY_TIMEOUT_MS, and the encryption) runs before its call is queued, and
// a clear asked for meanwhile drops that push instead of waiting for it.
//
// Named exports, no hooks: utils/signOut.js imports `clearRobotHfToken` and a
// util must not import from hooks/ (sessionScope.test.js fences that).
//
// THE TOKEN GOES THROUGH ROSLIB.Service HERE AND NOT THROUGH useRosServiceCaller:
// that hook's `callService` logs the request it sends, which for this one call
// is the student's token. Nothing in this file logs, formats or stores one.
//
// `STATE_TOPIC`, `SET_SERVICE`, `STATE_KEYS`, `STATE_SCHEMA_VERSION` and
// `STATE_STALE_MS` are the browser half of a contract the robot's
// data_processing/hf_token_store.py holds the other half of;
// robotis_ai_setup/tests/test_hf_token_state_lockstep.py reads both, so keep
// each as a plain `export const NAME = <literal>;`.

import ROSLIB from 'roslib';

import rosConnectionManager, { isLocalRosbridgeUrl } from '../../utils/rosConnectionManager';
import { robotIsRemote } from '../../utils/robotLocality';
import { noteUnauthorized } from '../piAuth/piAuth';
import { hfCopy } from './hfTokenCopy';
import {
  encryptForRobot, parseRobotKey, parseServedKey, SEAL_UNAVAILABLE,
} from './hfTokenCrypto';

export const STATE_TOPIC = '/edubotics/hf_token_state';
export const SET_SERVICE = '/register_hf_user';
export const SERVICE_TYPE = 'physical_ai_interfaces/srv/SetHFUser';
export const STATE_KEYS = Object.freeze(['v', 'seq', 'accepts', 'present', 'fp', 'busy']);
export const STATE_SCHEMA_VERSION = 1;
// The robot publishes at 1 Hz; a state older than this is no state at all
// (more than twice the period, with room for rosbridge's throttle).
export const STATE_STALE_MS = 6000;
export const CALL_TIMEOUT_MS = 8000;
export const CLEAR_BOUND_MS = 2000;
// VB-03: the agent's copy of the robot key (pi_agent/agent.py GET /robot-key).
export const AGENT_KEY_URL = '/api/system/robot-key';
// How often a Pi push reads the agent's key before it gives up, and the pause
// between two reads (the topic publishes at 1 Hz).
export const KEY_CHECKS = 3;
export const KEY_RECHECK_MS = 1000;
// VC-03: one agent read never takes longer than this (the agent is on the
// robot's own network; a stalled connection must not hold anything).
export const AGENT_KEY_TIMEOUT_MS = 4000;

const THROTTLE_MS = 500;
const FP_SHAPE = /^[0-9a-f]{16}$/;
// Defence in depth: the node's message is token-free by contract, but a
// sentence that is about to reach a student never carries anything shaped like one.
const TOKEN_IN_TEXT = /hf_[A-Za-z0-9_-]{8,}/g;

/**
 * Parse one /edubotics/hf_token_state payload.
 *
 * Returns null — UNKNOWN, never „no token" — for anything malformed: not JSON,
 * not an object, another schema version, a key of the wrong type. A null here
 * leaves the page exactly where it was, so a garbled message can never read as
 * „the robot holds nothing" and trigger a push.
 *
 * @param {string} data raw std_msgs/String payload
 * @returns {null|{v:number, seq:number, accepts:boolean, present:boolean, fp:?string, busy:boolean}}
 */
export function parseTokenState(data) {
  if (typeof data !== 'string' || !data) return null;
  let raw;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  for (const key of STATE_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(raw, key)) return null;
  }
  if (raw.v !== STATE_SCHEMA_VERSION) return null;
  if (!Number.isFinite(raw.seq)) return null;
  if (typeof raw.accepts !== 'boolean' || typeof raw.present !== 'boolean' || typeof raw.busy !== 'boolean') {
    return null;
  }
  if (raw.fp !== null && !(typeof raw.fp === 'string' && FP_SHAPE.test(raw.fp))) return null;
  return {
    v: raw.v,
    seq: raw.seq,
    accepts: raw.accepts,
    present: raw.present,
    fp: raw.fp,
    busy: raw.busy,
  };
}

// The robot's advertised key from the newest VALID state message (null: none).
// Not a secret; replaced by every message, so a robot restart's new key wins.
let robotKey = null;

/** The key the next push encrypts for (tests and the sync's diagnostics). */
export function getRobotKey() {
  return robotKey;
}

/** Tests only. */
export function setRobotKeyForTests(key) {
  robotKey = key || null;
}

let agentFetch = (...args) => fetch(...args);
let recheckMs = KEY_RECHECK_MS;
let agentTimeoutMs = AGENT_KEY_TIMEOUT_MS;

/** Tests only: the fetch the agent's key is read with, the pause and the bound. */
export function setAgentKeySourceForTests({ fetch: injected, recheckMs: ms, timeoutMs } = {}) {
  agentFetch = injected || ((...args) => fetch(...args));
  recheckMs = Number.isFinite(ms) ? ms : KEY_RECHECK_MS;
  agentTimeoutMs = Number.isFinite(timeoutMs) ? timeoutMs : AGENT_KEY_TIMEOUT_MS;
}

/**
 * The robot's key as the pi-agent serves it (VB-03). Never rejects, and
 * settles within AGENT_KEY_TIMEOUT_MS (VC-03): the request is aborted then,
 * and a fetch that ignores the abort is left behind, unanswered.
 * @returns {Promise<{status: 'ok', key: {kid: string, pub: string}}
 *   |{status: 'none'|'legacy'|'unreachable'}>}
 *   `none`: the agent has no key file (the robot tier is down or starting, or
 *   an image from before the file); `legacy`: an agent without the route.
 */
export function readAgentRobotKey() {
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  let timer = null;
  const timedOut = new Promise((resolve) => {
    timer = setTimeout(() => {
      if (ctrl) ctrl.abort();
      resolve({ status: 'unreachable' });
    }, agentTimeoutMs);
  });
  const read = readAgentRobotKeyOnce(ctrl ? ctrl.signal : null)
    .catch(() => ({ status: 'unreachable' }));
  return Promise.race([read, timedOut]).finally(() => clearTimeout(timer));
}

async function readAgentRobotKeyOnce(signal) {
  let res;
  try {
    res = await agentFetch(`${AGENT_KEY_URL}?_=${Date.now()}`, {
      cache: 'no-store',
      credentials: 'same-origin',
      ...(signal ? { signal } : {}),
    });
  } catch {
    return { status: 'unreachable' };
  }
  if (!res) return { status: 'unreachable' };
  if (res.status === 401) {
    noteUnauthorized(); // the robot session may be gone: the login re-checks
    return { status: 'unreachable' };
  }
  if (res.status === 404) return { status: 'legacy' };
  if (!res.ok) return { status: 'unreachable' };
  let json;
  try {
    json = await res.json();
  } catch {
    return { status: 'unreachable' };
  }
  if (!json || json.ok !== true) return { status: 'unreachable' };
  const key = parseServedKey(json.key);
  return key ? { status: 'ok', key } : { status: 'none' };
}

/**
 * Subscribe to the robot's token state. Like useRobotActivation: the singleton
 * connection, a throttled depth-1 topic. Resolves to an unsubscribe function;
 * rejects when the connection cannot be made (the heartbeat's story, not ours).
 *
 * @param {string} rosbridgeUrl
 * @param {(state: object) => void} onState called with every VALID payload
 * @returns {Promise<() => void>}
 */
export async function subscribeTokenState(rosbridgeUrl, onState) {
  const ros = await rosConnectionManager.getConnection(rosbridgeUrl);
  let live = true;
  const topic = new ROSLIB.Topic({
    ros,
    name: STATE_TOPIC,
    messageType: 'std_msgs/msg/String',
    throttle_rate: THROTTLE_MS,
    queue_length: 1,
  });
  topic.subscribe((msg) => {
    if (!live) return;
    const parsed = parseTokenState(msg?.data);
    if (parsed) {
      robotKey = parseRobotKey(msg?.data);
      onState(parsed);
    }
  });
  return () => {
    live = false;
    try { topic.unsubscribe(); } catch { /* swallow */ }
  };
}

// ── the write side ────────────────────────────────────────────────────────────
// Every SERVICE CALL goes through one serial queue: a clear must always follow a
// call that is already on its way (two overlapping calls could land in either
// order, and „sign out" would then leave the next student's robot holding the
// previous student's token). A push PREPARES first (the agent's key, the
// encryption) on a queue of its own, so pushes keep their order, and queues its
// call only when ready. A clear never waits for a push that is still preparing
// (VC-03: an agent that never answers would otherwise hold „Abmelden"): it bumps
// the clear epoch and goes on the call queue at once, and every push prepared
// under an older epoch is dropped instead of sent.
let callTail = Promise.resolve();
let prepTail = Promise.resolve();
let clearEpoch = 0;

function serialCall(task) {
  const result = callTail.then(task);
  callTail = result.then(() => undefined, () => undefined);
  return result;
}

function serialPrep(task) {
  const result = prepTail.then(task);
  prepTail = result.then(() => undefined, () => undefined);
  return result;
}

function sanitizeMessage(text) {
  return typeof text === 'string' ? text.replace(TOKEN_IN_TEXT, 'hf_***') : '';
}

function callSetService(value) {
  return new Promise((resolve) => {
    // getCurrentConnection never connects: if there is no live socket there is
    // nowhere to send the token and the call is refused, not queued for later.
    // And only the LOCAL rosbridge ever gets a personal token.
    const ros = rosConnectionManager.getCurrentConnection();
    if (!ros || !isLocalRosbridgeUrl(rosConnectionManager.url)) {
      resolve({ success: false, message: hfCopy('robot.noLink') });
      return;
    }
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const timer = setTimeout(
      () => finish({ success: false, message: hfCopy('robot.timeout') }),
      CALL_TIMEOUT_MS,
    );
    try {
      const service = new ROSLIB.Service({ ros, name: SET_SERVICE, serviceType: SERVICE_TYPE });
      service.callService(
        new ROSLIB.ServiceRequest({ token: value }),
        (res) => {
          const message = sanitizeMessage(res?.message);
          finish({
            success: res?.success === true,
            message: message || (res?.success === true ? '' : hfCopy('robot.failed')),
          });
        },
        // rosbridge's own failure text is English and unrelated to the student:
        // the sentence is ours, the text is dropped.
        () => finish({ success: false, message: hfCopy('robot.failed') }),
      );
    } catch {
      finish({ success: false, message: hfCopy('robot.failed') });
    }
  });
}

/**
 * Put a token in the robot's slot, or clear the slot with ''.
 * Never rejects: a refusal is `{success: false, message}` with a German sentence.
 *
 * @param {string} token a token, or '' to clear
 * @returns {Promise<{success: boolean, message: string}>}
 */
export function setRobotToken(token) {
  const value = typeof token === 'string' ? token : '';
  if (value === '') {
    clearEpoch += 1;
    return serialCall(() => callSetService(''));
  }
  const epoch = clearEpoch;
  return serialPrep(async () => {
    if (epoch !== clearEpoch) return superseded();
    // A remote robot (a Pi, or any page not opened at a loopback host) is
    // reached over a network others share: never the token in the clear, and
    // only for the key the agent serves (VB-03, VC-01).
    const prepared = robotIsRemote() ? await prepareRemote(value) : await prepareLocal(value);
    if (!prepared.ok) return prepared.result;
    // Checked and queued in one synchronous step: a clear asked for while this
    // push prepared has gone out already, and this push must not follow it.
    if (epoch !== clearEpoch) return superseded();
    return serialCall(() => callSetService(prepared.wire));
  });
}

function superseded() {
  return { success: false, message: hfCopy('robot.superseded') };
}

function refusal(copyKey) {
  return { ok: false, result: { success: false, message: hfCopy(copyKey) } };
}

/** `{ok: true, wire}` — the `enc1:` value of `value` for `key` — or `{ok: false, result}`. */
async function sealFor(value, key) {
  try {
    return { ok: true, wire: await encryptForRobot(value, key) };
  } catch (err) {
    const unloaded = Boolean(err) && err.message === SEAL_UNAVAILABLE;
    return refusal(unloaded ? 'robot.sealUnavailable' : 'robot.encryptFailed');
  }
}

/** The loopback robot (the Windows GUI's own page): the topic's key, else the plain token. */
async function prepareLocal(value) {
  const key = robotKey;
  if (key) return sealFor(value, key);
  return { ok: true, wire: value };
}

const pause = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

/**
 * A remote robot's push (VB-03, VC-01): encrypt for the AGENT's key, and only
 * while the topic advertises the same key id. Re-read both up to KEY_CHECKS
 * times, then refuse in German; never the plain token, never the topic's point.
 */
async function prepareRemote(value) {
  let served = null;
  let agentAnswered = false;
  for (let check = 0; check < KEY_CHECKS; check += 1) {
    // eslint-disable-next-line no-await-in-loop
    if (check > 0) await pause(recheckMs);
    // eslint-disable-next-line no-await-in-loop
    served = await readAgentRobotKey();
    if (served.status !== 'unreachable') agentAnswered = true;
    if (served.status === 'legacy') return refusal('robot.needsUpdate');
    const advertised = robotKey;
    if (served.status === 'ok' && advertised && advertised.kid === served.key.kid) {
      return sealFor(value, served.key);
    }
  }
  // The agent could not be asked once: say that, not that the key is missing.
  if (!agentAnswered) return refusal('robot.agentUnreachable');
  if (served && served.status === 'ok' && robotKey) return refusal('robot.keyMismatch');
  if (served && served.status === 'none' && !robotKey) {
    // Neither the agent nor the topic names a key: an image without one.
    return refusal('robot.needsUpdate');
  }
  return refusal('robot.keyUnavailable');
}

/**
 * Clear the robot's slot, bounded. Never rejects and always settles within
 * `timeoutMs` (the sign-out must not wait for a robot that is gone).
 *
 * @returns {Promise<boolean>} true when the robot confirmed the slot is empty
 */
export function clearRobotHfToken({ timeoutMs = CLEAR_BOUND_MS } = {}) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), timeoutMs);
    setRobotToken('').then(
      (result) => { clearTimeout(timer); resolve(Boolean(result && result.success)); },
      () => { clearTimeout(timer); resolve(false); },
    );
  });
}
