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
import { useSelector, useDispatch } from 'react-redux';
import { setHeartbeatStatus } from '../features/tasks/taskSlice';

/**
 * App-global ROS liveness watchdog.
 *
 * The `/heartbeat` topic callback (useRosTopicSubscription) dispatches
 * 'connected' + lastHeartbeatTime on each beat, but the time-based transitions
 * OUT of 'connected' (-> 'timeout' -> 'disconnected') were only computed inside
 * the <HeartbeatStatus> pill — which is mounted on Home/Record/Training/
 * Inference but NOT on Roboter Studio (WorkshopPage) or the Daten tab. On those
 * pages a node restart that did NOT close the websocket produced no
 * 'disconnected'->'connected' EDGE, so nothing keyed off liveness recovered.
 * Mounting this watchdog once at the app shell guarantees accurate liveness
 * state (pill, HomePage bridgeReady gate) on EVERY page. Robot identity no
 * longer depends on this edge: the server resolves EDUBOTICS_ROBOT_TYPE at
 * boot and its idle identity tick re-delivers robot_type + capabilities over
 * /task/status within ~2-3 s of any (re)connect.
 *
 * <HeartbeatStatus> keeps its own equivalent interval for the pages it renders
 * on; both only dispatch on a real status CHANGE (idempotent), so the overlap is
 * harmless — this hook simply extends the coverage to the pages with no pill.
 *
 * @param {{enabled?: boolean, timeoutMs?: number, disconnectTimeoutMs?: number}} opts
 *   `enabled` is false in cloud-only mode (no local rosbridge / heartbeat).
 */
export function useHeartbeatWatchdog({
  enabled = true,
  timeoutMs = 3000,
  disconnectTimeoutMs = 10000,
} = {}) {
  const dispatch = useDispatch();
  const heartbeatStatus = useSelector((s) => s.tasks.heartbeatStatus);
  const lastHeartbeatTime = useSelector((s) => s.tasks.lastHeartbeatTime);

  // Read the latest values inside the interval via refs so we never tear down
  // and recreate the 1 s interval on every heartbeat tick.
  const lastHbRef = useRef(lastHeartbeatTime);
  lastHbRef.current = lastHeartbeatTime;
  const statusRef = useRef(heartbeatStatus);
  statusRef.current = heartbeatStatus;

  useEffect(() => {
    if (!enabled) return undefined;
    const id = setInterval(() => {
      const lastHb = lastHbRef.current;
      const cur = statusRef.current;
      let next;
      if (!lastHb) {
        next = 'disconnected';
      } else {
        const dt = Date.now() - lastHb;
        next =
          dt >= disconnectTimeoutMs ? 'disconnected' : dt >= timeoutMs ? 'timeout' : 'connected';
      }
      if (next !== cur) dispatch(setHeartbeatStatus(next));
    }, 1000);
    return () => clearInterval(id);
  }, [enabled, timeoutMs, disconnectTimeoutMs, dispatch]);
}
