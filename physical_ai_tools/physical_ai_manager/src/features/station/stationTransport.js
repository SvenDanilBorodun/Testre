// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The camera station's two wires (station spec v6.13, §3.3, §8, §9.1):
//
//   * the three ROS services on the robot's LOCAL rosbridge, through
//     ROSLIB.Service directly (the controller is a module, not a hook; the
//     classroom Jetson proxy is never asked — a claimed Jetson makes the
//     controller inactive, §9.1 step 6);
//   * the GUI's Studio-stream lease on the Windows control bridge
//     (rsControlBase(false) = http://localhost:8769), POSTed as a CORS-simple
//     `text/plain` body (the same shape `navigator.sendBeacon` sends on
//     `pagehide`, so one request shape reaches `/station/stream`, §3.3).
//
// Named exports, no hooks. Every call settles and never rejects: a timeout, a
// missing connection and a rosbridge error are answers the controller handles.

import ROSLIB from 'roslib';

import rosConnectionManager, { isLocalRosbridgeUrl } from '../../utils/rosConnectionManager';
import { rsControlBase } from '../../utils/piMode';
import {
  CHECK_SERVICE,
  CHECK_SERVICE_TYPE,
  COMMISSION_SERVICE,
  COMMISSION_SERVICE_TYPE,
  GUI_POST_TIMEOUT_MS,
  SESSION_SERVICE,
  SESSION_SERVICE_TYPE,
  STREAM_PATH,
} from './stationContract';

const OLD_IMAGE = /does not exist/i;

function localConnection() {
  // getCurrentConnection never connects: no live socket means nowhere to send.
  const ros = rosConnectionManager.getCurrentConnection();
  if (!ros || !ros.isConnected || !isLocalRosbridgeUrl(rosConnectionManager.url)) return null;
  return ros;
}

/** The local rosbridge is connected: the resume `status` can be asked (§9.1 step 1). */
export function isRosReady() {
  return localConnection() !== null;
}

function errorText(err) {
  if (typeof err === 'string') return err;
  if (err && typeof err.message === 'string') return err.message;
  return '';
}

/**
 * One service call. Resolves `{ok, res, timedOut, oldImage, unreachable}`:
 *   ok        — an answer arrived (`res` is it);
 *   timedOut  — no answer within `timeoutMs` (counts as „no answer", §9.1 step 6);
 *   oldImage  — rosbridge says the service does not exist (an older image);
 *   unreachable — no local rosbridge connection.
 */
function callService(name, type, request, timeoutMs) {
  return new Promise((resolve) => {
    const ros = localConnection();
    if (!ros) {
      resolve({ ok: false, res: null, timedOut: false, oldImage: false, unreachable: true });
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
    timer = setTimeout(
      () => finish({ ok: false, res: null, timedOut: true, oldImage: false, unreachable: false }),
      timeoutMs,
    );
    try {
      const service = new ROSLIB.Service({ ros, name, serviceType: type });
      service.callService(
        new ROSLIB.ServiceRequest(request),
        (res) => finish({ ok: true, res: res || {}, timedOut: false, oldImage: false, unreachable: false }),
        (err) => finish({
          ok: false, res: null, timedOut: false, oldImage: OLD_IMAGE.test(errorText(err)), unreachable: false,
        }),
      );
    } catch {
      finish({ ok: false, res: null, timedOut: false, oldImage: false, unreachable: false });
    }
  });
}

/** `/station/session` (§8.1). */
export function callSession(request, timeoutMs) {
  return callService(SESSION_SERVICE, SESSION_SERVICE_TYPE, {
    command: '',
    session_id: '',
    client_id: '',
    ...request,
  }, timeoutMs);
}

/** `/station/check` (§8.2). */
export function callCheck(request, timeoutMs) {
  return callService(CHECK_SERVICE, CHECK_SERVICE_TYPE, {
    command: '',
    session_id: '',
    client_id: '',
    ...request,
  }, timeoutMs);
}

/** `/station/commission` (§8.3); every request field is sent, with its default. */
export function callCommission(request, timeoutMs) {
  return callService(COMMISSION_SERVICE, COMMISSION_SERVICE_TYPE, {
    command: '',
    session_id: '',
    client_id: '',
    layout_id: '',
    commission_id: '',
    dot_id: 0,
    mark_id: '',
    cube_turn: 0,
    ...request,
  }, timeoutMs);
}

/** The Windows control bridge, never the Pi's (D1; §9.1: „not in Pi mode"). */
export function streamUrl() {
  return `${rsControlBase(false)}${STREAM_PATH}`;
}

/**
 * POST `/station/stream {"on": …}` (§3.3). Resolves
 * `{ok, status, body, unreachable}`; never rejects.
 */
export async function postStream(on) {
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), GUI_POST_TIMEOUT_MS) : null;
  try {
    const res = await fetch(streamUrl(), {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: JSON.stringify({ on: Boolean(on) }),
      signal: ctrl ? ctrl.signal : undefined,
    });
    let body = {};
    try {
      body = (await res.json()) || {};
    } catch {
      body = {};
    }
    return { ok: Boolean(res.ok), status: res.status, body, unreachable: false };
  } catch {
    return { ok: false, status: 0, body: {}, unreachable: true };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * `navigator.sendBeacon` of `{"on": false}` on `pagehide` (§9.1 step 5): a
 * CORS-simple `text/plain` POST that survives the page going away.
 * @returns {boolean} whether the browser queued it
 */
export function beaconStreamOff() {
  try {
    if (typeof navigator === 'undefined' || typeof navigator.sendBeacon !== 'function') return false;
    const blob = new Blob([JSON.stringify({ on: false })], { type: 'text/plain' });
    return navigator.sendBeacon(streamUrl(), blob);
  } catch {
    return false;
  }
}
