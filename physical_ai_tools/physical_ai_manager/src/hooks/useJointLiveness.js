// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// „Antwortet der Arm?" — one bit, independent of what is on screen.
//
// WHY NOT REUSE THE TWIN'S SIGNAL. `UrdfTwin` already tracks exactly this as
// `hasJointData`, and lifting it out was the obvious first move. It is wrong:
// the twin is mounted only while the hero shows the 3D view, so the moment a
// student clicked „Kamera" the Health-Check would report „keine Gelenkdaten"
// — a claim about the ARM caused by a choice about the VIEW. A health check
// that changes its answer when you look away is worse than no health check.
//
// So this subscribes once, for the life of the page, at 1 Hz. That is one
// extra rosbridge topic at a tenth of the twin's rate; the message is read for
// its ARRIVAL, never its contents, so nothing is parsed and nothing rendered.
//
// THREE-STATE ON PURPOSE:
//   null  — not observing (no bridge URL / disabled). UNKNOWN, not "no".
//   false — subscribed, and nothing has arrived for STALE_MS.
//   true  — a JointState arrived inside the window.
// The middle value is the one that matters: `false` is a finding about the
// arm, `null` is the absence of one, and collapsing them is how a disconnected
// rig ends up accused of a dead arm.

import { useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import ROSLIB from 'roslib';

import rosConnectionManager from '../utils/rosConnectionManager';

// Matches UrdfTwin's JOINT_STALE_MS so the two surfaces agree on what "live"
// means. The real /joint_states runs at 100 Hz, so 3 s is ~300 missed messages
// — comfortably past any transient hiccup.
const STALE_MS = 3000;
const CHECK_MS = 1000;
// Liveness needs arrival, not resolution. 1 Hz is the cheapest rate that still
// answers within the 3 s window.
const THROTTLE_MS = 1000;

/**
 * @param {{enabled?: boolean, topic?: string}} [opts]
 * @returns {boolean|null} true / false / null — see the three-state note above.
 */
export default function useJointLiveness({ enabled = true, topic = '/joint_states' } = {}) {
  const rosbridgeUrl = useSelector((s) => s.ros.rosbridgeUrl);
  const [live, setLive] = useState(null);
  const lastAtRef = useRef(0);

  useEffect(() => {
    if (!enabled || !rosbridgeUrl) {
      lastAtRef.current = 0;
      setLive(null);
      return undefined;
    }

    let cancelled = false;
    let subscription = null;
    lastAtRef.current = 0;
    // Subscribed but nothing heard yet is already a finding: the bridge is up
    // and the arm is silent. Start at false rather than null so a rig whose
    // controller never came up does not sit on „—" forever.
    setLive(false);

    // A plain interval rather than a per-message timeout: re-arming a timer on
    // every message is the pattern UrdfTwin avoided for the same reason.
    const staleTimer = window.setInterval(() => {
      if (cancelled) return;
      const last = lastAtRef.current;
      if (last && Date.now() - last > STALE_MS) {
        lastAtRef.current = 0;
        setLive(false);
      }
    }, CHECK_MS);

    (async () => {
      let ros;
      try {
        ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      } catch (err) {
        // Not reachable is the heartbeat's story to tell, not this hook's.
        if (!cancelled) setLive(null);
        return;
      }
      if (cancelled) return;
      subscription = new ROSLIB.Topic({
        ros,
        name: topic,
        messageType: 'sensor_msgs/msg/JointState',
        throttle_rate: THROTTLE_MS,
        queue_length: 1,
      });
      subscription.subscribe(() => {
        if (cancelled) return;
        lastAtRef.current = Date.now();
        setLive(true);
      });
    })();

    return () => {
      cancelled = true;
      window.clearInterval(staleTimer);
      if (subscription) {
        try { subscription.unsubscribe(); } catch (_) { /* swallow */ }
        subscription = null;
      }
    };
  }, [enabled, rosbridgeUrl, topic]);

  return live;
}
