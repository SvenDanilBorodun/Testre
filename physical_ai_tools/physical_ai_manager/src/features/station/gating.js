// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Pure decisions other pages derive from the station snapshot (station spec
// v6.13, §9.1 step 4, §9.2). Every input is a value.

import { STATION_DE } from './stationCopy';

/**
 * This tab holds the station claim (or is taking or giving it up): the server
 * refuses recording and inference starts meanwhile (§5.3), so the pages say so
 * before the student tries (D13).
 */
export function stationClaimHeld(snapshot) {
  if (!snapshot) return false;
  return Boolean(snapshot.held) || snapshot.phase === 'entering' || snapshot.phase === 'leaving';
}

/**
 * The Aufnahme / Inferenz block, or null: the sentence, and whether
 * „Station verlassen" is offered disabled (a real program runs, §9.1 step 4:
 * from the server's `workflow_active`, never from the page's own run state).
 * @returns {null | {textDe: string, leaveDisabled: boolean, leaveTitle: string}}
 */
export function stationStartBlock(snapshot) {
  if (!stationClaimHeld(snapshot)) return null;
  const leaveDisabled = Boolean(snapshot.workflowActive) || snapshot.phase !== 'held';
  return {
    textDe: STATION_DE.busyElsewhere,
    leaveDisabled,
    leaveTitle: snapshot.workflowActive ? STATION_DE.stopProgramFirst : '',
  };
}

/** Sign-out is refused while a real program runs in this tab's session (§9.1 step 4). */
export function stationBlocksLogout(snapshot) {
  return Boolean(snapshot && snapshot.held && snapshot.workflowActive);
}

/** „Station verlassen" is disabled: a real program runs or a leave is on its way. */
export function stationLeaveDisabled(snapshot) {
  return !snapshot || !snapshot.held || Boolean(snapshot.workflowActive) || snapshot.phase !== 'held';
}
