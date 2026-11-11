// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The browser's wire to the robot for Cloud-Inferenz (spec §6.5).
//
// THE TWO CREDENTIALS GO THROUGH ROSLIB.Service HERE AND NOT THROUGH
// useRosServiceCaller: that hook logs the requests it sends. Nothing in this
// folder calls console.* (a test greps it), and neither credential is ever
// stored, formatted into a message or put into an error. rosbridge itself never
// logs a service request (rosbridge_library/capabilities/call_service.py).
//
// Only the LOCAL rosbridge is ever called: no other rosbridge is ever sent a
// student's cloud credentials.
//
// ENCRYPTED FOR A REMOTE ROBOT (verification round C, VC-06, 2026-11-15). On a
// Pi every account that may use the robot holds a rosbridge session, and any
// of them can advertise /edubotics/cloud_inference/start and take the two
// tokens (VB-03's root cause). So on a remote page (utils/robotLocality: the
// marker says Pi, or the page's own host is not exactly a loopback host) both
// travel in the HF token's `enc1:` envelope, encrypted for the key the pi-agent
// serves behind the robot login (hfToken/robotChannel::readAgentRobotKey), the
// connect token under CLOUD_SESSION_CONTEXT and the edge token under
// CLOUD_EDGE_CONTEXT, so neither opens as the other or as an HF token. Without
// that key nothing is sent and the page says why in German. The robot opens
// each value once (VC-05) and validates the tokens as before; an older robot
// refuses an `enc1:` value as a malformed session (cloud_texts_de.BAD_SESSION_DE).
// The Windows GUI's loopback page sends the tokens as they are, as before.
//
// Named exports, no hooks: controller.js (a module, not a hook) imports these.

import ROSLIB from 'roslib';

import rosConnectionManager, { isLocalRosbridgeUrl } from '../../utils/rosConnectionManager';
import { robotIsRemote } from '../../utils/robotLocality';
import { cloudCopy } from '../../components/CloudInference/cloudCopy';
import { readAgentRobotKey } from '../hfToken/robotChannel';
import {
  CLOUD_EDGE_CONTEXT, CLOUD_SESSION_CONTEXT, encryptForRobot, SEAL_UNAVAILABLE,
} from '../hfToken/hfTokenCrypto';
import {
  START_SERVICE,
  START_SERVICE_TYPE,
  STATE_TOPIC,
  STOP_SERVICE,
  STOP_SERVICE_TYPE,
  parseCloudState,
} from './robotContract';

export const START_TIMEOUT_MS = 10000;
export const STOP_TIMEOUT_MS = 3000;
const THROTTLE_MS = 500;
const OLD_IMAGE = /does not exist/i;

function localConnection() {
  // getCurrentConnection never connects: no live socket means nowhere to send.
  const ros = rosConnectionManager.getCurrentConnection();
  if (!ros || !isLocalRosbridgeUrl(rosConnectionManager.url)) return null;
  return ros;
}

function errorText(err) {
  if (typeof err === 'string') return err;
  if (err && typeof err.message === 'string') return err.message;
  return '';
}

function withoutSecrets(text, secrets) {
  let out = typeof text === 'string' ? text : '';
  for (const secret of secrets) {
    if (typeof secret === 'string' && secret) out = out.split(secret).join('***');
  }
  return out;
}

/**
 * Start a cloud run on the robot.
 *
 * Never rejects. `{success, message, oldImage, timedOut}`:
 *   - success: the robot accepted the run;
 *   - a refusal carries the robot's own German sentence (shown as-is);
 *   - an image without the service makes rosbridge answer „Service … does not
 *     exist" at once: `oldImage` (the page says „Roboter-Software aktualisieren");
 *   - no answer within START_TIMEOUT_MS: `timedOut` (the robot MAY have started:
 *     the caller stops it and ends the session);
 *   - any other failure: our sentence, rosbridge's English text dropped.
 *
 * On a remote robot both tokens are encrypted first (VC-06, module comment); a
 * key that cannot be had is a refusal with our German sentence and sends nothing.
 *
 * @param {{task_info: object, session_token: string, edge_token: string}} request
 * @returns {Promise<{success: boolean, message: string, oldImage: boolean, timedOut: boolean}>}
 */
export function startCloudInference(request) {
  if (!robotIsRemote()) return callStart(request, request);
  return sealForRemoteRobot(request).then((sealed) => (
    sealed.ok ? callStart(sealed.request, request) : refused(sealed.message)
  ));
}

function refused(message) {
  return { success: false, message, oldImage: false, timedOut: false };
}

/**
 * Both tokens of `request` encrypted for the key the pi-agent serves (one
 * bounded read). `{ok: true, request}` or `{ok: false, message}`.
 */
async function sealForRemoteRobot(request) {
  const served = await readAgentRobotKey();
  if (served.status === 'legacy') return { ok: false, message: cloudCopy.errors.robotOld };
  if (served.status === 'unreachable') return { ok: false, message: cloudCopy.errors.robotKeyUnreachable };
  if (served.status !== 'ok') return { ok: false, message: cloudCopy.errors.robotKeyMissing };
  try {
    const sessionToken = await encryptForRobot(request?.session_token, served.key, { context: CLOUD_SESSION_CONTEXT });
    const edgeToken = await encryptForRobot(request?.edge_token, served.key, { context: CLOUD_EDGE_CONTEXT });
    return { ok: true, request: { ...request, session_token: sessionToken, edge_token: edgeToken } };
  } catch (err) {
    const unloaded = Boolean(err) && err.message === SEAL_UNAVAILABLE;
    return { ok: false, message: unloaded ? cloudCopy.errors.sealUnavailable : cloudCopy.errors.encryptFailed };
  }
}

/**
 * The service call itself. `original` carries the plain tokens, so neither
 * ever comes back in a message, even inside an envelope's refusal.
 */
function callStart(request, original) {
  return new Promise((resolve) => {
    const secrets = [
      request?.session_token, request?.edge_token, original?.session_token, original?.edge_token,
    ];
    const ros = localConnection();
    if (!ros) {
      resolve({ success: false, message: cloudCopy.errors.robotUnreachable, oldImage: false, timedOut: false });
      return;
    }
    let settled = false;
    let timer = null;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    timer = setTimeout(
      () => finish({ success: false, message: cloudCopy.errors.robotUnreachable, oldImage: false, timedOut: true }),
      START_TIMEOUT_MS,
    );
    try {
      const service = new ROSLIB.Service({ ros, name: START_SERVICE, serviceType: START_SERVICE_TYPE });
      service.callService(
        new ROSLIB.ServiceRequest({
          task_info: request.task_info,
          session_token: request.session_token,
          edge_token: request.edge_token,
        }),
        (res) => {
          const ok = res?.success === true;
          const message = ok ? '' : withoutSecrets(res?.message, secrets);
          finish({ success: ok, message: ok ? '' : (message || cloudCopy.errors.robotFailed), oldImage: false, timedOut: false });
        },
        (err) => {
          const old = OLD_IMAGE.test(errorText(err));
          finish({
            success: false,
            message: old ? cloudCopy.errors.robotOld : cloudCopy.errors.robotFailed,
            oldImage: old,
            timedOut: false,
          });
        },
      );
    } catch {
      finish({ success: false, message: cloudCopy.errors.robotFailed, oldImage: false, timedOut: false });
    }
  });
}

/**
 * Stop a cloud run on the robot (std_srvs/Trigger; ends ONLY a cloud run).
 * Never rejects; settles within STOP_TIMEOUT_MS. True when the robot answered success.
 *
 * @returns {Promise<boolean>}
 */
export function stopCloudInference() {
  return new Promise((resolve) => {
    const ros = localConnection();
    if (!ros) {
      resolve(false);
      return;
    }
    let settled = false;
    let timer = null;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    timer = setTimeout(() => finish(false), STOP_TIMEOUT_MS);
    try {
      const service = new ROSLIB.Service({ ros, name: STOP_SERVICE, serviceType: STOP_SERVICE_TYPE });
      service.callService(
        new ROSLIB.ServiceRequest({}),
        (res) => finish(res?.success === true),
        () => finish(false),
      );
    } catch {
      finish(false);
    }
  });
}

/**
 * Subscribe to the robot's cloud state (latched + 1 Hz). Resolves to an
 * unsubscribe function; rejects when the connection cannot be made.
 *
 * @param {string} rosbridgeUrl
 * @param {(state: object) => void} onState called with every VALID payload
 * @returns {Promise<() => void>}
 */
export async function subscribeCloudState(rosbridgeUrl, onState) {
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
    const parsed = parseCloudState(msg?.data);
    if (parsed) onState(parsed);
  });
  return () => {
    live = false;
    try { topic.unsubscribe(); } catch { /* swallow */ }
  };
}
