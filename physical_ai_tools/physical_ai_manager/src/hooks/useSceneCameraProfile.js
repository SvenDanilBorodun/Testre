// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The robot's own statement of its scene camera (decision D58): the arm
// container publishes `{"v":1,"profile":"legacy"|"tower_v1"}` on
// SCENE_CAMERA_PROFILE_TOPIC (std_msgs/String, latched + 1 Hz). The Inferenz
// page compares it with the camera the reserved model was trained with.
//
// UNKNOWN IS NOT LEGACY: until a statement arrives (an older robot image, a
// robot whose publisher is not running) the answer is null, for as long as the
// silence lasts. No timer turns silence into „legacy". A statement this page
// cannot read is null too.
//
// The names below equal physical_ai_server/scene_provenance.py's PROFILE_TOPIC
// and PROFILES (tests/test_scene_provenance_lockstep.py). Local state only: a
// fact about the robot, read by one view, never stored.

import { useEffect, useState } from 'react';
import { useSelector } from 'react-redux';
import ROSLIB from 'roslib';

import rosConnectionManager from '../utils/rosConnectionManager';

export const SCENE_CAMERA_PROFILE_TOPIC = '/edubotics/scene_camera_profile';
export const SCENE_CAMERA_PROFILES = Object.freeze(['legacy', 'tower_v1']);

// Fast enough to catch the 1 Hz re-publish when rosbridge does not request a
// matching durability for the latched message.
const THROTTLE_MS = 500;

/** A known scene-camera profile, else null. */
export function sceneCameraProfileOf(value) {
  return typeof value === 'string' && SCENE_CAMERA_PROFILES.includes(value) ? value : null;
}

/**
 * The profile a statement names, or null for anything else.
 *
 * @param {string} data raw std_msgs/String payload
 */
export function parseSceneCameraStatement(data) {
  if (typeof data !== 'string' || !data) return null;
  let raw;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.v !== 1) return null;
  return sceneCameraProfileOf(raw.profile);
}

/**
 * @param {{enabled?: boolean}} [opts] subscribe only while the cloud view is shown
 * @returns {?string} 'legacy' | 'tower_v1' | null (unknown)
 */
export default function useSceneCameraProfile({ enabled = true } = {}) {
  const rosbridgeUrl = useSelector((s) => s.ros.rosbridgeUrl);
  const [profile, setProfile] = useState(null);

  useEffect(() => {
    if (!enabled || !rosbridgeUrl) {
      setProfile(null);
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
        name: SCENE_CAMERA_PROFILE_TOPIC,
        messageType: 'std_msgs/msg/String',
        throttle_rate: THROTTLE_MS,
        queue_length: 1,
      });
      topic.subscribe((msg) => {
        if (!cancelled) setProfile(parseSceneCameraStatement(msg?.data));
      });
    })();

    return () => {
      cancelled = true;
      if (topic) {
        try { topic.unsubscribe(); } catch { /* the socket may already be gone */ }
        topic = null;
      }
      setProfile(null);
    };
  }, [enabled, rosbridgeUrl]);

  return enabled ? profile : null;
}
