// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The browser's wire to the robot for Cloud-Inferenz (spec §6.5).
//
// THE TWO CREDENTIALS GO THROUGH ROSLIB.Service HERE AND NOT THROUGH
// useRosServiceCaller: that hook logs the requests it sends. Nothing in this
// folder calls console.* (a test greps it), and neither credential is ever
// stored, formatted into a message or put into an error. rosbridge itself never
// logs a service request (rosbridge_library/capabilities/call_service.py).
//
// Only the LOCAL rosbridge is ever called: the classroom Jetson proxy is never
// sent a student's cloud credentials (the cloud target is not offered while a
// Jetson owns rosbridge anyway, spec §6.2).
//
// Named exports, no hooks: controller.js (a module, not a hook) imports these.

import ROSLIB from 'roslib';

import rosConnectionManager, { isLocalRosbridgeUrl } from '../../utils/rosConnectionManager';
import { cloudCopy } from '../../components/CloudInference/cloudCopy';
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
 * @param {{task_info: object, session_token: string, edge_token: string}} request
 * @returns {Promise<{success: boolean, message: string, oldImage: boolean, timedOut: boolean}>}
 */
export function startCloudInference(request) {
  return new Promise((resolve) => {
    const secrets = [request?.session_token, request?.edge_token];
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
