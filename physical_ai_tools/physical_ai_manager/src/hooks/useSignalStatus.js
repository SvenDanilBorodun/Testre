// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The rig's signal facts for the Aufnahme page: `/edubotics/signal_status`
// (std_msgs/String, JSON schema v1, latched + 1 Hz; spec §2.4).
//
// Local state, not Redux: one page reads it, and it is a fact about the rig
// that is worthless a few seconds later (utils/signalStatus judges staleness
// from `receivedAt`, a `performance.now()` reading). An older server image
// has no such topic, so `payload` simply stays null and the page shows no
// badge, no banner and refuses nothing on these grounds.

import { useEffect, useState } from 'react';
import { useSelector } from 'react-redux';
import ROSLIB from 'roslib';

import rosConnectionManager from '../utils/rosConnectionManager';
import { SIGNAL_STATUS_TOPIC, parseSignalStatus } from '../utils/signalStatus';

const THROTTLE_MS = 500;
const EMPTY = Object.freeze({ payload: null, receivedAt: null });

/**
 * @param {{enabled?: boolean}} [opts]
 * @returns {{payload: ?object, receivedAt: ?number}}
 */
export default function useSignalStatus({ enabled = true } = {}) {
  const rosbridgeUrl = useSelector((s) => s.ros.rosbridgeUrl);
  const [state, setState] = useState(EMPTY);

  useEffect(() => {
    if (!enabled || !rosbridgeUrl) {
      setState(EMPTY);
      return undefined;
    }
    let cancelled = false;
    let topic = null;

    (async () => {
      let ros;
      try {
        ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      } catch {
        return;
      }
      if (cancelled || !ros) return;
      topic = new ROSLIB.Topic({
        ros,
        name: SIGNAL_STATUS_TOPIC,
        messageType: 'std_msgs/msg/String',
        throttle_rate: THROTTLE_MS,
        queue_length: 1,
      });
      topic.subscribe((msg) => {
        if (cancelled) return;
        const payload = parseSignalStatus(msg?.data);
        if (payload) setState({ payload, receivedAt: performance.now() });
      });
    })();

    return () => {
      cancelled = true;
      if (topic) {
        try { topic.unsubscribe(); } catch { /* the socket may already be gone */ }
        topic = null;
      }
      setState(EMPTY);
    };
  }, [enabled, rosbridgeUrl]);

  return state;
}
