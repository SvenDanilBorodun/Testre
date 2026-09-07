// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// „Ist der Roboter aktiviert?" — and the one call that activates him.
//
// WHY THIS EXISTS AT ALL. Bringing the environment up no longer moves the arm:
// the container comes up torqued and silent, and the home glide, the leader
// sync and the teleop spawn wait for /edubotics/activate. The only thing that
// calls it is the Startseite's „Roboter aktivieren" button, which lives inside
// StudentApp's authenticated <main> — so "a logged-in student asked for it" is
// what a moving arm means.
//
// THREE-STATE, exactly like hooks/useJointLiveness:
//   null  — nothing has answered. UNKNOWN. Renders „—", never „nicht
//           aktiviert", because an older server image has no agent at all and
//           accusing that rig of being inactive is a claim nobody checked.
//   obj   — the agent's own JSON.
// The Start page's whole doctrine is that unknown is not zero; a `|| false`
// here is the same defect in a different file.
//
// NOT A REDUX SLICE. One page reads this. Putting it in the store would mean a
// `session/signedOut` handler and an entry in sessionScope's reasoned lists for
// a value that is a fact about the RIG, not about the student — and it would
// still have to be re-read on the next connect anyway.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import ROSLIB from 'roslib';

import rosConnectionManager from '../utils/rosConnectionManager';

export const ACTIVATION_TOPIC = '/edubotics/activation_state';
export const ACTIVATION_SERVICE = '/edubotics/activate';

export const IDLE = 'idle';
export const ACTIVATING = 'activating';
export const ACTIVE = 'active';
export const FAILED = 'failed';

const VALID_STATES = [IDLE, ACTIVATING, ACTIVE, FAILED];

// The agent latches the state (TRANSIENT_LOCAL) AND republishes at 1 Hz.
// rosbridge does not always request a matching durability for its own
// subscriptions, so the throttle must stay fast enough to catch the periodic
// tick — this is the belt to the agent's braces.
const THROTTLE_MS = 500;

// The service returns immediately (the agent runs the sequence on a worker
// thread), so this timeout covers the round trip only, never the ~10 s of
// motion. Progress arrives on the topic.
const CALL_TIMEOUT_MS = 10000;

/**
 * Normalise one agent payload. Returns null for anything we cannot trust — a
 * malformed message must leave the page on „unknown", never crash it and never
 * be mistaken for „not activated".
 *
 * @param {string} data raw std_msgs/String payload
 */
export function parseActivationState(data) {
  if (typeof data !== 'string' || !data) return null;
  let raw;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object') return null;
  if (!VALID_STATES.includes(raw.state)) return null;
  return {
    state: raw.state,
    step: typeof raw.step === 'string' ? raw.step : '',
    message: typeof raw.message === 'string' ? raw.message : '',
    robotType: typeof raw.robot_type === 'string' ? raw.robot_type : '',
    // THREE-STATE, and the third value is the point. An older agent that omits
    // the key has not told us; guessing „leader" was wrong on the fleet (three
    // of the four shipped profiles are follower-only) and guessing „no leader"
    // is unsafe (it would offer a re-home into a live teleop rail). `null`
    // hands the question to ActivationCard::resolveHasLeader, which asks the
    // capability manifest — a CHECKED answer the app already has on the wire —
    // before falling back to the safe assumption.
    hasLeader: typeof raw.has_leader === 'boolean' ? raw.has_leader : null,
    // Absent ⇒ the gate is on. Same direction: assume the safe world.
    required: raw.required !== false,
  };
}

/**
 * @param {{enabled?: boolean}} [opts]
 * @returns {{status: ?object, activate: function, calling: boolean, error: ?string}}
 */
export default function useRobotActivation({ enabled = true } = {}) {
  const rosbridgeUrl = useSelector((s) => s.ros.rosbridgeUrl);
  const [status, setStatus] = useState(null);
  const [calling, setCalling] = useState(false);
  const [error, setError] = useState(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (!enabled || !rosbridgeUrl) {
      setStatus(null);
      return undefined;
    }
    let cancelled = false;
    let subscription = null;
    setStatus(null);

    (async () => {
      let ros;
      try {
        ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      } catch {
        // Unreachable is the heartbeat's story, not this hook's.
        return;
      }
      if (cancelled) return;
      subscription = new ROSLIB.Topic({
        ros,
        name: ACTIVATION_TOPIC,
        messageType: 'std_msgs/msg/String',
        throttle_rate: THROTTLE_MS,
        queue_length: 1,
      });
      subscription.subscribe((msg) => {
        if (cancelled) return;
        const parsed = parseActivationState(msg?.data);
        if (parsed) setStatus(parsed);
      });
    })();

    return () => {
      cancelled = true;
      if (subscription) {
        try { subscription.unsubscribe(); } catch { /* swallow */ }
        subscription = null;
      }
    };
  }, [enabled, rosbridgeUrl]);

  const activate = useCallback(async () => {
    if (!rosbridgeUrl) return false;
    setError(null);
    setCalling(true);
    try {
      const ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      const result = await new Promise((resolve, reject) => {
        const service = new ROSLIB.Service({
          ros,
          name: ACTIVATION_SERVICE,
          serviceType: 'std_srvs/srv/Trigger',
        });
        const timer = setTimeout(
          () => reject(new Error('Zeitüberschreitung')), CALL_TIMEOUT_MS,
        );
        service.callService(
          new ROSLIB.ServiceRequest({}),
          (res) => { clearTimeout(timer); resolve(res); },
          (err) => { clearTimeout(timer); reject(err instanceof Error ? err : new Error(String(err))); },
        );
      });
      if (mountedRef.current && result && result.success === false) {
        // „already activating" is a double-click, not a failure. The state
        // topic is throttled at 500 ms, so `busy` can still be false while the
        // agent is already running the sequence; surfacing that as a red line
        // under a activation that IS starting would be a lie. Every other
        // refusal (teleop already live, and whatever a future agent adds) is
        // real and shown.
        const benign = /wird bereits aktiviert/.test(result.message || '');
        if (!benign) setError(result.message || 'Die Aktivierung wurde abgelehnt.');
      }
      return !!(result && result.success);
    } catch (e) {
      if (mountedRef.current) {
        setError('Der Roboter konnte nicht erreicht werden. Läuft die '
          + 'Umgebung noch?');
      }
      return false;
    } finally {
      if (mountedRef.current) setCalling(false);
    }
  }, [rosbridgeUrl]);

  return { status, activate, calling, error };
}
