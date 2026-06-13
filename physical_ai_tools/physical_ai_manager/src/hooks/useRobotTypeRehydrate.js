// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { useEffect, useRef } from 'react';
import { useSelector } from 'react-redux';
import { useRosServiceCaller } from './useRosServiceCaller';
import TaskPhase from '../constants/taskPhases';

/**
 * Re-establish the SERVER-side robot type after a physical_ai_server node
 * restart, so the student no longer has to re-select the robot on the Start
 * page after every disconnect.
 *
 * Why this is needed: the ROS node holds the selected robot type ONLY in RAM
 * (`/set_robot_type` -> init_ros_params). A node restart (crash/respawn/OOM)
 * loses it, while the browser still has the persisted value in Redux/
 * localStorage. The node now publishes its 1 Hz liveness heartbeat from
 * __init__ (decoupled from robot selection), so a restart makes the heartbeat
 * recover WITHOUT the websocket ever dropping — there is no reconnect event to
 * hang the re-issue off. We therefore watch the heartbeat status and, on the
 * recovery EDGE into 'connected', re-issue `/set_robot_type` from the persisted
 * value to re-initialise the server.
 *
 * Guards:
 *   - Only the EDGE into 'connected' (from 'disconnected'/'timeout') fires it —
 *     never repeatedly while steadily connected.
 *   - NEVER while a task is active (`running` or phase > READY): /set_robot_type
 *     runs clear_parameters + init_ros_params and would tear down a live
 *     recording/inference.
 *   - No-op when no robot type is persisted yet (first-ever run — the student
 *     picks one manually).
 *   - A single in-flight call at a time.
 *
 * @param {{enabled?: boolean}} opts disable in cloud-only / Jetson-routed modes
 *   (the local node isn't the rosbridge target there).
 */
export function useRobotTypeRehydrate({ enabled = true } = {}) {
  const { setRobotType } = useRosServiceCaller();
  const heartbeatStatus = useSelector((s) => s.tasks.heartbeatStatus);
  const robotType = useSelector((s) => s.tasks.taskStatus.robotType);
  const phase = useSelector((s) => s.tasks.taskStatus.phase);
  const running = useSelector((s) => s.tasks.taskStatus.running);
  // Roboter Studio activity lives in a SEPARATE slice (state.workshop), not in
  // tasks.taskStatus — a running Blockly workflow or an in-progress calibration
  // keeps tasks.running=false / phase=READY. /set_robot_type does
  // clear_parameters + init_ros_params (rebuilds the IK chain / joint topology),
  // which would abort a live workflow/calibration and jerk the follower, so we
  // must guard on it too. Optional-chained so the hook is safe if the workshop
  // slice isn't registered (e.g. in isolation tests).
  const workflowRunState = useSelector((s) => s.workshop?.runState);
  const calibState = useSelector((s) => s.workshop?.calibState);

  const prevStatusRef = useRef(heartbeatStatus);
  const inFlightRef = useRef(false);

  useEffect(() => {
    const prev = prevStatusRef.current;
    prevStatusRef.current = heartbeatStatus;

    if (!enabled) return;
    // Only on a recovery edge into 'connected'.
    if (heartbeatStatus !== 'connected' || prev === 'connected') return;
    if (!robotType || robotType.trim() === '') return;
    // Never clobber a live task (clear_parameters + init_ros_params).
    if (running || phase > TaskPhase.READY) return;
    // Never clobber a running Roboter Studio workflow or calibration.
    const workshopBusy =
      (workflowRunState && workflowRunState !== 'idle') ||
      (calibState && calibState !== 'idle');
    if (workshopBusy) return;
    if (inFlightRef.current) return;

    inFlightRef.current = true;
    setRobotType(robotType)
      .catch((e) => {
        // Best-effort: the manual RobotTypeSelector remains the fallback.
        console.warn('Robotertyp-Wiederherstellung fehlgeschlagen:', e?.message || e);
      })
      .finally(() => {
        inFlightRef.current = false;
      });
  }, [
    enabled,
    heartbeatStatus,
    robotType,
    running,
    phase,
    workflowRunState,
    calibState,
    setRobotType,
  ]);
}
