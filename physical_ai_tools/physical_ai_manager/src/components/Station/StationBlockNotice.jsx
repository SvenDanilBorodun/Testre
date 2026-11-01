/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Roboter Studio nutzt gerade die Kamerastation." with „Station verlassen"
// (station spec v6.13, §9.2, D13): what the Inferenz page shows instead of
// starting while this tab holds the station claim. The server refuses every
// inference start meanwhile (§5.3); the button is the controller's leave,
// disabled while a real program runs (the server's `workflow_active`).

import React from 'react';
import toast from 'react-hot-toast';

import { Key } from '../sucher';
import Icon from '../icons/Icon';
import * as station from '../../features/station/controller';
import { useStationValue } from '../../features/station/useStation';
import { stationStartBlock } from '../../features/station/gating';
import { STATION_DE } from '../../features/station/stationCopy';

export default function StationBlockNotice({ className = '' }) {
  const blockKey = useStationValue((st) => {
    const b = stationStartBlock(st);
    if (!b) return '';
    return b.leaveDisabled ? 'held-locked' : 'held';
  });
  if (!blockKey) return null;
  const locked = blockKey === 'held-locked';
  const onLeave = async () => {
    const r = await station.leave();
    if (!r.left && r.message) toast.error(r.message);
  };
  return (
    <div
      role="status"
      data-testid="station-block-notice"
      className={`flex flex-wrap items-center gap-2 border-b border-[var(--line)] bg-[var(--amber-wash)] px-4 py-2 text-sm text-[var(--ink)] ${className}`}
    >
      <Icon name="camera" size={16} className="text-[color:var(--amber)]" />
      <span className="font-semibold">{STATION_DE.busyElsewhere}</span>
      <Key
        size="sm"
        icon="logout"
        onClick={onLeave}
        disabled={locked}
        title={locked ? STATION_DE.stopProgramFirst : undefined}
      >
        {STATION_DE.leave}
      </Key>
    </div>
  );
}
