/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Two robot facts Roboter Studio needs, carried into the workshop slice while
// the page is open (spec 40 S8):
//   * the calibration service's state, /edubotics/calibration_state
//     (std_msgs/String JSON, latched + 1 Hz; autocal/service.py) — the editor on
//     the real robot unlocks only while it says `ok`;
//   * the scene-camera profile the arm container STATES (D63) — the simulator
//     and the twin draw the tower only for `tower_v1`; silence is null, never
//     `legacy`.
// A message this page cannot read is dropped (the last good one stays); leaving
// the page forgets both, so the next entry starts from „wird geprüft". The
// store read of the robot link is guarded: the page's tests mount it with a
// partial store.

import { useEffect } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import ROSLIB from 'roslib';

import rosConnectionManager from '../../../utils/rosConnectionManager';
import {
  SCENE_CAMERA_PROFILE_TOPIC,
  parseSceneCameraStatement,
} from '../../../hooks/useSceneCameraProfile';
import { setCalibrationState, setSceneCameraProfile } from '../../../features/workshop/workshopSlice';
import { parseCalibrationState } from './calibrationModel';

export const CALIBRATION_STATE_TOPIC = '/edubotics/calibration_state';

// Both topics are latched AND re-published at 1 Hz; rosbridge does not always
// ask for a matching durability, so the throttle stays below the 1 Hz tick.
const THROTTLE_MS = 500;

/**
 * @param {{enabled?: boolean}} [opts] subscribe only while Roboter Studio is open
 */
export default function useCalibrationState({ enabled = true } = {}) {
  const dispatch = useDispatch();
  const rosbridgeUrl = useSelector((s) => (s.ros ? s.ros.rosbridgeUrl : null));

  useEffect(() => {
    if (!enabled || !rosbridgeUrl) return undefined;
    let cancelled = false;
    const topics = [];

    (async () => {
      let ros;
      try {
        ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      } catch {
        return; // unreachable is the heartbeat chip's story
      }
      if (cancelled || !ros) return;
      const listen = (name, onData) => {
        const topic = new ROSLIB.Topic({
          ros,
          name,
          messageType: 'std_msgs/msg/String',
          throttle_rate: THROTTLE_MS,
          queue_length: 1,
        });
        topic.subscribe((msg) => { if (!cancelled) onData(msg && msg.data); });
        topics.push(topic);
      };
      listen(CALIBRATION_STATE_TOPIC, (data) => {
        const parsed = parseCalibrationState(data);
        if (parsed) dispatch(setCalibrationState(parsed));
      });
      listen(SCENE_CAMERA_PROFILE_TOPIC, (data) => {
        dispatch(setSceneCameraProfile(parseSceneCameraStatement(data)));
      });
    })();

    return () => {
      cancelled = true;
      topics.forEach((topic) => {
        try { topic.unsubscribe(); } catch { /* the socket may already be gone */ }
      });
      dispatch(setCalibrationState(null));
      dispatch(setSceneCameraProfile(null));
    };
  }, [enabled, rosbridgeUrl, dispatch]);
}
