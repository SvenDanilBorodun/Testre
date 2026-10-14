// Copyright 2025 EduBotics
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
//
// Author: Kiwoong Park

import React, { useRef, useEffect } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import { setHeartbeatStatus } from '../features/tasks/taskSlice';
import { usePiMode, PI_PORT_BLOCKED_HINT } from '../utils/piMode';
import Led from './sucher/Led';

/**
 * HeartbeatStatus Component
 *
 * Displays the ROS connection status as a „Sucher" readout: a light (green and
 * breathing while connected, amber on a timeout, red when disconnected), the
 * status word and the latency („VERBUNDEN 2MS"; upper case comes from CSS).
 * `size`: 'sm' | 'md' (default; the older 'small' / 'medium' / 'large' map onto
 * them). `dark` is kept for its callers and has no visual effect.
 */
export default function HeartbeatStatus({
  timeoutMs = 3000,
  disconnectTimeoutMs = 10000,
  className = '',
  showLabel = true,
  dark = false, // kept for its callers; no visual effect in the readout look
  size = 'md',
}) {
  const dispatch = useDispatch();
  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const lastHeartbeatTime = useSelector((state) => state.tasks.lastHeartbeatTime);
  // Pi mode: distinguish a crashed stack from a port-filtered network. When the
  // agent REPORTS the robot tier running but rosbridge (the same-origin
  // /rosbridge proxy on the Pi) is unreachable
  // from the browser, that is the VLAN-ACL / port-filter case — say so, never
  // the Windows-era „Docker prüfen" text (usePiMode returns a non-Pi default
  // outside a Pi rig, so this is inert everywhere else).
  const { piMode, agentStatus } = usePiMode();
  const portBlocked =
    piMode &&
    heartbeatStatus === 'disconnected' &&
    !!(agentStatus && agentStatus.robot_tier_up);

  const intervalRef = useRef(null);
  const lastHeartbeatTimeRef = useRef(lastHeartbeatTime);
  lastHeartbeatTimeRef.current = lastHeartbeatTime;

  const getStatusInfo = () => {
    switch (heartbeatStatus) {
      case 'connected':
        return {
          led: 'ok',
          pulse: 'breathe',
          label: 'Verbunden',
          tone: 'success',
        };
      case 'timeout':
        return {
          led: 'warn',
          pulse: 'none',
          label: 'Zeitüberschreitung',
          tone: 'amber',
        };
      case 'disconnected':
      default:
        return {
          led: 'rec',
          pulse: 'none',
          label: 'Getrennt',
          tone: 'danger',
        };
    }
  };

  useEffect(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
    }

    intervalRef.current = setInterval(() => {
      const now = Date.now();
      const lastHb = lastHeartbeatTimeRef.current;

      if (!lastHb) {
        if (heartbeatStatus !== 'disconnected') {
          dispatch(setHeartbeatStatus('disconnected'));
        }
        return;
      }

      const timeSinceLastHeartbeat = now - lastHb;

      if (timeSinceLastHeartbeat >= disconnectTimeoutMs) {
        if (heartbeatStatus !== 'disconnected') {
          dispatch(setHeartbeatStatus('disconnected'));
        }
      } else if (timeSinceLastHeartbeat >= timeoutMs) {
        if (heartbeatStatus !== 'timeout') {
          dispatch(setHeartbeatStatus('timeout'));
        }
      } else {
        if (heartbeatStatus !== 'connected') {
          dispatch(setHeartbeatStatus('connected'));
        }
      }
    }, 1000);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [timeoutMs, disconnectTimeoutMs, dispatch]); // eslint-disable-line react-hooks/exhaustive-deps

  const statusInfo = getStatusInfo();

  // Compute latency ms (approx — interval between now and last heartbeat time)
  const latencyMs = lastHeartbeatTime ? Math.max(0, Date.now() - lastHeartbeatTime) : null;
  const shownLatency =
    heartbeatStatus === 'connected' && latencyMs != null && latencyMs < 2000
      ? `${Math.min(999, latencyMs)}ms`
      : '—';

  const small = size === 'sm' || size === 'small';

  const pill = (
    <div
      className={clsx('su-read', small ? 'su-read--sm' : 'su-read--md', 'su-hb', className)}
      title={portBlocked ? PI_PORT_BLOCKED_HINT : undefined}
    >
      <Led tone={statusInfo.led} pulse={statusInfo.pulse} size={small ? 8 : 9} />
      {showLabel && <span className="whitespace-nowrap">{statusInfo.label}</span>}
      <span className="su-hb__latency">{shownLatency}</span>
    </div>
  );

  // Non-Pi (and the healthy Pi paths) keep the bare readout.
  if (!portBlocked) return pill;

  // Pi port-blocked: pair the „Getrennt" pill with the network hint.
  return (
    <div className="inline-flex flex-col items-end gap-1">
      {pill}
      <span className="text-[10px] font-medium text-[color:var(--danger)] leading-tight text-right max-w-[240px]">
        {PI_PORT_BLOCKED_HINT}
      </span>
    </div>
  );
}
