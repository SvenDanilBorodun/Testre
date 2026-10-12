/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The studio's mode badge: „ECHTER ROBOTER" (orange) or „SIMULATOR" (blue),
// beside the page title and on the Vormachen window. In real mode it also says
// whether a program is driving the arm right now — „Programm läuft" with a dot
// that pulses only while the robot link is alive (on a dead link the recorded
// run state has nothing behind it, so the dot stays still), „Angehalten" with
// a steady dot at a breakpoint. The badge itself never animates.
//
// `withRunState={false}` (the Vormachen window) renders no run state and reads
// no store, so the badge needs no Redux slice there.

import React from 'react';
import { useSelector } from 'react-redux';
import Icon from '../icons/Icon';
import { selectWorkflowRunning } from '../../features/workshop/workshopSlice';
import { MODE_DE } from './modeCopy';

// Its own component so the hook order never depends on the mode.
function RunDot() {
  const running = useSelector(selectWorkflowRunning);
  const paused = useSelector((s) => !!(s.workshop && s.workshop.paused));
  const linkAlive = useSelector((s) => s.tasks?.heartbeatStatus === 'connected');
  if (!running) return null;
  return (
    <>
      <span aria-hidden="true">{' · '}</span>
      <span className="inline-flex items-center gap-1 font-medium">
        <Icon
          name="dot"
          size="0.7em"
          className={!paused && linkAlive ? 'motion-safe:animate-pulse' : ''}
        />
        {paused ? MODE_DE.RUN_PAUSED : MODE_DE.RUN_LIVE}
      </span>
    </>
  );
}

export default function ModeBadge({ simMode, withRunState = true }) {
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-bold text-white whitespace-nowrap"
      style={{ background: simMode ? '#2563EB' : '#C2410C' }}
      title={simMode ? MODE_DE.BADGE_SIM_TITLE : MODE_DE.BADGE_REAL_TITLE}
      data-testid="mode-badge"
    >
      <Icon name={simMode ? 'simulator' : 'robotArm'} />
      <span>{simMode ? MODE_DE.BADGE_SIM : MODE_DE.BADGE_REAL}</span>
      {!simMode && withRunState && <RunDot />}
    </span>
  );
}
