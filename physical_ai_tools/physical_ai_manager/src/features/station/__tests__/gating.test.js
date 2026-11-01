// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The pure station decisions other pages read (station spec v6.13, §9.1 step
// 4, §9.2): the claim counts from `enter` to the end of `leave`; „Station
// verlassen" and sign-out follow the server's `workflow_active`.

import {
  stationBlocksLogout, stationClaimHeld, stationLeaveDisabled, stationStartBlock,
} from '../gating';
import { STATION_DE } from '../stationCopy';
import { STATION_PROTOCOL, isStationRig } from '../stationContract';

describe('station gating', () => {
  it('the claim is held while entering, held and leaving', () => {
    expect(stationClaimHeld(null)).toBe(false);
    expect(stationClaimHeld({ held: false, phase: 'idle' })).toBe(false);
    expect(stationClaimHeld({ held: false, phase: 'entering' })).toBe(true);
    expect(stationClaimHeld({ held: true, phase: 'held' })).toBe(true);
    expect(stationClaimHeld({ held: false, phase: 'leaving' })).toBe(true);
  });

  it('the start block carries the sentence and the button state', () => {
    expect(stationStartBlock({ held: false, phase: 'idle' })).toBe(null);
    expect(stationStartBlock({ held: true, phase: 'held', workflowActive: false })).toEqual({
      textDe: STATION_DE.busyElsewhere, leaveDisabled: false, leaveTitle: '',
    });
    expect(stationStartBlock({ held: true, phase: 'held', workflowActive: true })).toEqual({
      textDe: STATION_DE.busyElsewhere, leaveDisabled: true, leaveTitle: STATION_DE.stopProgramFirst,
    });
    // A leave on its way: nothing to press.
    expect(stationStartBlock({ held: false, phase: 'leaving' }).leaveDisabled).toBe(true);
  });

  it('„Station verlassen" and sign-out follow the server\'s workflow_active, not the page', () => {
    expect(stationLeaveDisabled({ held: true, phase: 'held', workflowActive: false })).toBe(false);
    expect(stationLeaveDisabled({ held: true, phase: 'held', workflowActive: true })).toBe(true);
    expect(stationBlocksLogout({ held: true, workflowActive: true })).toBe(true);
    expect(stationBlocksLogout({ held: false, workflowActive: true })).toBe(false);
    expect(stationBlocksLogout({ held: true, workflowActive: false })).toBe(false);
  });

  it('a station rig is capabilities.station_protocol === 6 and nothing else', () => {
    expect(STATION_PROTOCOL).toBe(6);
    expect(isStationRig({ station_protocol: 6 })).toBe(true);
    expect(isStationRig({ station_protocol: 5 })).toBe(false);
    expect(isStationRig({ station_protocol: '6' })).toBe(false);
    expect(isStationRig(null)).toBe(false);
    expect(isStationRig(undefined)).toBe(false);
  });
});
