// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The browser's wire to the robot's ONE Hugging-Face token slot.
//
//   READ  /edubotics/hf_token_state  (std_msgs/String, JSON)
//         {"v":1,"seq":N,"accepts":bool,"present":bool,"fp":"16hex"|null,"busy":bool}
//         A fingerprint and three booleans. The robot never says the token.
//   WRITE /register_hf_user          (physical_ai_interfaces/srv/SetHFUser)
//         request.token: a token, or '' to clear the slot.
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
import { hfCopy } from './hfTokenCopy';

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
    if (parsed) onState(parsed);
  });
  return () => {
    live = false;
    try { topic.unsubscribe(); } catch { /* swallow */ }
  };
}

// ── the write side: ONE serial queue ──────────────────────────────────────────
// A clear must always follow a push that is already on its way: two overlapping
// service calls could land in either order, and „sign out" would then leave the
// next student's robot holding the previous student's token.
let tail = Promise.resolve();

function serial(task) {
  const result = tail.then(task);
  tail = result.then(() => undefined, () => undefined);
  return result;
}

function sanitizeMessage(text) {
  return typeof text === 'string' ? text.replace(TOKEN_IN_TEXT, 'hf_***') : '';
}

function callSetService(value) {
  return new Promise((resolve) => {
    // getCurrentConnection never connects: if there is no live socket there is
    // nowhere to send the token and the call is refused, not queued for later.
    // And only the LOCAL rosbridge ever gets a personal token: the classroom
    // Jetson proxy keeps its own read-only token.
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
  return serial(() => callSetService(value));
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
