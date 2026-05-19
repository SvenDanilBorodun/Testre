// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

import { useEffect, useRef, useState } from 'react';
import ROSLIB from 'roslib';
import rosConnectionManager from '../utils/rosConnectionManager';

// True liveness for camera topics. The `/image/get_available_list` ROS
// service reports CONFIGURED cameras (from omx_f_config.yaml) regardless
// of whether usb_cam actually launched or is publishing — so the old
// "N Kameras aktiv" chip would happily report 2 cameras while both USB
// devices were unplugged. This hook subscribes to each candidate topic
// at a low rate, records the last-arrival timestamp, and exposes the
// set of topics that have produced a message within LIVENESS_TIMEOUT_MS.
//
// Bandwidth budget: throttle_rate=4000 ms + queue_length=1 → at most one
// CompressedImage every 4 s per camera. For a 640×480 JPEG @ quality 70
// (~40 KB), that's ~10 KB/s/camera. Negligible.

const LIVENESS_TIMEOUT_MS = 8000;
const TICK_MS = 2000;
const SUBSCRIBE_THROTTLE_MS = 4000;

/**
 * @param {object} opts
 * @param {string[]} opts.topics              Topic names from /image/get_available_list
 *                                            (server-side strip already removed `/compressed`).
 * @param {string}   opts.rosbridgeUrl        Current rosbridge URL. Re-binding on URL
 *                                            change tears down + re-subscribes.
 * @param {boolean}  opts.enabled             Set false to disable the hook entirely
 *                                            (no subscriptions, returns empty set).
 * @returns {{ liveTopics: string[], liveCount: number, allConfiguredAlive: boolean }}
 */
export function useCameraLiveness({ topics, rosbridgeUrl, enabled }) {
  const lastMsgRef = useRef(new Map()); // topic -> ms
  const subsRef = useRef([]);           // active ROSLIB.Topic handles
  const [liveTopics, setLiveTopics] = useState([]);

  // (Re)bind subscriptions when topic list, URL, or enable flag changes.
  useEffect(() => {
    if (!enabled || !rosbridgeUrl || !topics || topics.length === 0) {
      // Tear down any leftover subscriptions and reset state.
      subsRef.current.forEach((t) => {
        try { t.unsubscribe(); } catch (_) { /* swallow */ }
      });
      subsRef.current = [];
      lastMsgRef.current = new Map();
      setLiveTopics([]);
      return undefined;
    }

    let cancelled = false;

    (async () => {
      let ros;
      try {
        ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      } catch (err) {
        console.warn('useCameraLiveness: rosbridge not connectable yet:', err.message);
        return;
      }
      if (cancelled) return;

      // Fresh subscription set — drop any previous handles before re-binding.
      subsRef.current.forEach((t) => {
        try { t.unsubscribe(); } catch (_) { /* swallow */ }
      });
      subsRef.current = [];
      lastMsgRef.current = new Map();

      topics.forEach((bareTopic) => {
        // Service strips trailing `/compressed`; add it back so we subscribe
        // to the actual transport that usb_cam advertises.
        const fullTopic = bareTopic.endsWith('/compressed')
          ? bareTopic
          : `${bareTopic}/compressed`;
        try {
          const sub = new ROSLIB.Topic({
            ros,
            name: fullTopic,
            messageType: 'sensor_msgs/msg/CompressedImage',
            throttle_rate: SUBSCRIBE_THROTTLE_MS,
            queue_length: 1,
          });
          sub.subscribe(() => {
            // We don't care about the payload — only that it arrived.
            lastMsgRef.current.set(bareTopic, Date.now());
          });
          subsRef.current.push(sub);
        } catch (err) {
          console.warn(`useCameraLiveness: failed to subscribe to ${fullTopic}:`, err);
        }
      });
    })();

    return () => {
      cancelled = true;
      subsRef.current.forEach((t) => {
        try { t.unsubscribe(); } catch (_) { /* swallow */ }
      });
      subsRef.current = [];
      lastMsgRef.current = new Map();
    };
  }, [enabled, rosbridgeUrl, topics]);

  // Tick: derive liveTopics from refs. Done separately from subscription
  // setup so a quiet camera doesn't keep the state stuck on a stale value.
  useEffect(() => {
    if (!enabled) {
      setLiveTopics([]);
      return undefined;
    }
    const compute = () => {
      const now = Date.now();
      const alive = [];
      lastMsgRef.current.forEach((ms, topic) => {
        if (now - ms <= LIVENESS_TIMEOUT_MS) {
          alive.push(topic);
        }
      });
      // Only update if the membership changed — avoids re-render storms.
      setLiveTopics((prev) => {
        if (prev.length !== alive.length) return alive;
        const prevSorted = [...prev].sort();
        const aliveSorted = [...alive].sort();
        for (let i = 0; i < prevSorted.length; i += 1) {
          if (prevSorted[i] !== aliveSorted[i]) return alive;
        }
        return prev;
      });
    };
    compute(); // immediate so the chip doesn't wait TICK_MS on first mount
    const id = setInterval(compute, TICK_MS);
    return () => clearInterval(id);
  }, [enabled]);

  const liveCount = liveTopics.length;
  const allConfiguredAlive =
    !!topics && topics.length > 0 && liveCount === topics.length;
  return { liveTopics, liveCount, allConfiguredAlive };
}

export default useCameraLiveness;
