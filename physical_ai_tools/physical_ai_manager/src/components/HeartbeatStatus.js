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

/**
 * HeartbeatStatus Component
 *
 * Displays ROS connection status as a compact pill with a live blinking dot.
 * Matches the EduBotics design: small rounded chip, mono latency badge.
 */
export default function HeartbeatStatus({
  timeoutMs = 3000,
  disconnectTimeoutMs = 10000,
  className = '',
  showLabel = true,
  dark = false,
  size = 'medium',
}) {
  const dispatch = useDispatch();
  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const lastHeartbeatTime = useSelector((state) => state.tasks.lastHeartbeatTime);

  const intervalRef = useRef(null);
  const lastHeartbeatTimeRef = useRef(lastHeartbeatTime);
  lastHeartbeatTimeRef.current = lastHeartbeatTime;

  const getStatusInfo = () => {
    switch (heartbeatStatus) {
      case 'connected':
        return {
          color: 'var(--success)',
          label: 'Verbunden',
          tone: 'success',
        };
      case 'timeout':
        return {
          color: 'var(--amber)',
          label: 'Timeout',
          tone: 'amber',
        };
      case 'disconnected':
      default:
        return {
          color: 'var(--danger)',
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

  const sizeClass =
    size === 'small' ? 'h-7 px-2.5 text-[10px]' : size === 'large' ? 'h-9 px-3.5 text-xs' : 'h-8 px-3 text-[11px]';

  const containerClasses = clsx(
    'inline-flex items-center gap-2 rounded-full font-mono',
    sizeClass,
    dark
      ? 'bg-white/[0.08] border border-white/15 text-white/80'
      : 'bg-white border border-[var(--line)] text-[var(--ink-2)]',
    className
  );

  const dotWrap = (
    <span className="relative inline-flex">
      <span
        className="w-2 h-2 rounded-full block"
        style={{ background: statusInfo.color }}
      />
      {heartbeatStatus === 'connected' && (
        <span
          className="absolute inset-0 w-2 h-2 rounded-full eb-blink"
          style={{ background: statusInfo.color }}
        />
      )}
    </span>
  );

  return (
    <div className={containerClasses}>
      {dotWrap}
      {showLabel && <span className="whitespace-nowrap">{statusInfo.label}</span>}
      <span
        className={clsx(
          'px-1.5 py-0.5 rounded text-[10px]',
          dark ? 'bg-white/10' : 'bg-[var(--bg-sunk)]'
        )}
      >
        {shownLatency}
      </span>
    </div>
  );
}
