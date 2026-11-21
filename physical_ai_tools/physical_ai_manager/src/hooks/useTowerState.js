// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The V1's camera-tower verdicts, as the robot states them (spec 40 WP-HINT,
// 52 §5.1; decision D56). The page never computes the rule:
//   * /edubotics/tower_state — the V1 DRIVER's own verdict for the activation
//     glide from the live pose (WP-T1, run_home's check) and where a limp arm
//     would fall: `{v: 1, glide, fall, part, gen}`.
//   * /edubotics/home_glide_state — the SERVER's verdict for Roboter Studio's
//     manual glide to the Grundstellung (WP-12, the planner the press runs):
//     `{v: 1, glide, gen}`.
// Both are std_msgs/String, latched and re-sent at 2 Hz, and exist only for
// `v1_studio` under the tower camera (`tower_v1`); every other profile and the
// legacy camera see no topic at all (D1). Callers subscribe only on the V1.
//
// NO TOPIC IS TODAY'S UI. Until a verdict arrives the answer is null and every
// surface behaves as before the hint existed: the driver and the server refuse
// at the press anyway (refuse-only, D53). A message this page cannot read is
// null too. Local state only: a fact about the robot, never stored.
//
// The names and the two vocabularies equal the driver's (feetech_arm_node.py
// TOWER_STATE_TOPIC, feetech_geometry.py TOWER_CLEAR/… and
// limp_fall_direction) and the server's (physical_ai_server.py
// _HOME_GLIDE_STATE_TOPIC); hooks/__tests__/useTowerState.test.js reads both.

import {
  createContext, useContext, useEffect, useState, useSyncExternalStore,
} from 'react';
import * as reactRedux from 'react-redux';
import ROSLIB from 'roslib';

import rosConnectionManager from '../utils/rosConnectionManager';

export const TOWER_STATE_TOPIC = '/edubotics/tower_state';
export const HOME_GLIDE_STATE_TOPIC = '/edubotics/home_glide_state';

// The one robot the tower exists for.
export const TOWER_ARM_PROFILE = 'v1_studio';

export const GLIDE_CLEAR = 'clear';
export const GLIDE_REFUSED = 'refused';
export const GLIDE_UNJUDGEABLE = 'unjudgeable';
export const TOWER_GLIDES = Object.freeze([GLIDE_CLEAR, GLIDE_REFUSED, GLIDE_UNJUDGEABLE]);

export const FALL_FORWARD = 'forward';
export const FALL_BACK = 'back';
export const FALL_UNKNOWN = 'unknown';
export const TOWER_FALLS = Object.freeze([FALL_FORWARD, FALL_BACK, FALL_UNKNOWN]);

// The robot re-sends every 500 ms; a shorter throttle lets each one through
// (rosbridge does not always ask for the latched message).
const THROTTLE_MS = 250;

function readJson(data) {
  if (typeof data !== 'string' || !data) return null;
  let raw;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.v !== 1) return null;
  if (!TOWER_GLIDES.includes(raw.glide)) return null;
  return raw;
}

const genOf = (raw) => (Number.isInteger(raw.gen) ? raw.gen : null);

/**
 * The driver's statement on /edubotics/tower_state, or null when it cannot be
 * read. A `fall` it does not know is `unknown` — the direction the stop
 * dialog treats as „ask first".
 *
 * @param {string} data raw std_msgs/String payload
 * @returns {?{glide: string, fall: string, part: ?string, gen: ?number}}
 */
export function parseTowerState(data) {
  const raw = readJson(data);
  if (!raw) return null;
  return {
    glide: raw.glide,
    fall: TOWER_FALLS.includes(raw.fall) ? raw.fall : FALL_UNKNOWN,
    part: typeof raw.part === 'string' && raw.part ? raw.part : null,
    gen: genOf(raw),
  };
}

/**
 * The server's statement on /edubotics/home_glide_state, or null.
 *
 * @param {string} data raw std_msgs/String payload
 * @returns {?{glide: string, gen: ?number}}
 */
export function parseHomeGlideState(data) {
  const raw = readJson(data);
  if (!raw) return null;
  return { glide: raw.glide, gen: genOf(raw) };
}

/**
 * Whether „Umgebung stoppen" must ask first: everything but a verdict that the
 * limp arm falls forward, no verdict included (52 §5.3 (c), §6.3).
 */
export function stopNeedsTowerWarning(state) {
  return !state || state.fall !== FALL_FORWARD;
}

function useVerdictTopic(topicName, parse, enabled, rosbridgeUrl) {
  const [state, setState] = useState(null);
  const active = !!enabled && !!rosbridgeUrl;

  useEffect(() => {
    if (!active) {
      setState(null);
      return undefined;
    }
    let cancelled = false;
    let topic = null;

    (async () => {
      let ros;
      try {
        ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      } catch {
        return; // unreachable is the heartbeat chip's story
      }
      if (cancelled || !ros) return;
      topic = new ROSLIB.Topic({
        ros,
        name: topicName,
        messageType: 'std_msgs/msg/String',
        throttle_rate: THROTTLE_MS,
        queue_length: 1,
      });
      topic.subscribe((msg) => {
        if (!cancelled) setState(parse(msg?.data));
      });
    })();

    return () => {
      cancelled = true;
      if (topic) {
        try { topic.unsubscribe(); } catch { /* the socket may already be gone */ }
        topic = null;
      }
      setState(null);
    };
  }, [active, topicName, parse, rosbridgeUrl]);

  return active ? state : null;
}

/**
 * The driver's tower verdict for the live pose (/edubotics/tower_state).
 *
 * @param {{enabled?: boolean, rosbridgeUrl?: string}} opts subscribe only on
 *   the V1 (and, where the page knows it, under `tower_v1`)
 * @returns {?{glide: string, fall: string, part: ?string, gen: ?number}}
 */
export default function useTowerState({ enabled = true, rosbridgeUrl = '' } = {}) {
  return useVerdictTopic(TOWER_STATE_TOPIC, parseTowerState, enabled, rosbridgeUrl);
}

/**
 * The server's verdict for the manual glide to the Grundstellung
 * (/edubotics/home_glide_state).
 *
 * @param {{enabled?: boolean, rosbridgeUrl?: string}} opts
 * @returns {?{glide: string, gen: ?number}}
 */
export function useHomeGlideState({ enabled = true, rosbridgeUrl = '' } = {}) {
  return useVerdictTopic(HOME_GLIDE_STATE_TOPIC, parseHomeGlideState, enabled, rosbridgeUrl);
}

const NO_STORE_SUBSCRIBE = () => () => {};
const NO_URL = () => '';

// react-redux's context, read as an OPTIONAL export: the Roboter-Studio page
// tests stand react-redux in with useSelector and useDispatch only, and a
// missing export of such a stand-in throws when it is read (a presence check
// does not). Without it there is no store to read, as without a Provider.
const STORE_CONTEXT = 'ReactReduxContext' in reactRedux
  ? reactRedux.ReactReduxContext
  : createContext(null);

/**
 * The store's rosbridge URL, or '' where no Redux Provider is mounted (the
 * home-glide prompt and the System tab render standalone in their tests). The
 * context, not useSelector(): that one throws without a Provider
 * (useSmoothPhaseClock's rule).
 */
export function useOptionalRosbridgeUrl() {
  const store = useContext(STORE_CONTEXT)?.store ?? null;
  const read = store ? () => store.getState().ros?.rosbridgeUrl || '' : NO_URL;
  return useSyncExternalStore(store ? store.subscribe : NO_STORE_SUBSCRIBE, read, read);
}
