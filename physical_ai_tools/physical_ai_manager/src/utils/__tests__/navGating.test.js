// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Locks the two pure sidebar-gating decisions StudentApp uses:
//  - isCapabilityVisible: the capability filter (null caps hide nothing;
//    explicit `false` hides; Jetson mode SKIPS the filter — D9).
//  - robotGateDecision: requireRobotOrRedirect (cloud mode passes straight
//    through; otherwise a robot identity is required to enter a hardware page).

import { isCapabilityVisible, robotGateDecision } from '../navGating';

const OMX_FULL = {
  recordable: true, editable: true, trainable: true,
  inferable: true, roboter_studio: true, has_leader: true,
};
const OMX_FOLLOWER = {
  recordable: false, editable: false, trainable: false,
  inferable: true, roboter_studio: true, has_leader: false,
};

// The 6-tab nav map (HOME untagged).
const NAV = [
  { key: 'home' },
  { key: 'record', capabilityKey: 'recordable' },
  { key: 'training', capabilityKey: 'trainable' },
  { key: 'inference', capabilityKey: 'inferable' },
  { key: 'edit', capabilityKey: 'editable' },
  { key: 'workshop', capabilityKey: 'roboter_studio' },
];

function visibleKeys(opts) {
  return NAV.filter((n) => isCapabilityVisible(n, opts)).map((n) => n.key);
}

describe('isCapabilityVisible — capability nav filter', () => {
  it('null caps hide NOTHING (pre-first-tick / cloud / old image)', () => {
    expect(visibleKeys({ jetsonConnected: false, caps: null })).toEqual([
      'home', 'record', 'training', 'inference', 'edit', 'workshop',
    ]);
  });

  it('omx_full (all caps true) hides nothing', () => {
    expect(visibleKeys({ jetsonConnected: false, caps: OMX_FULL })).toEqual([
      'home', 'record', 'training', 'inference', 'edit', 'workshop',
    ]);
  });

  it('omx_follower hides Aufnahme/Training/Daten, keeps Inferenz + Roboter Studio', () => {
    expect(visibleKeys({ jetsonConnected: false, caps: OMX_FOLLOWER })).toEqual([
      'home', 'inference', 'workshop',
    ]);
  });

  it('Jetson mode SKIPS the filter entirely — Training stays visible even with trainable:false (D9)', () => {
    expect(visibleKeys({ jetsonConnected: true, caps: OMX_FOLLOWER })).toEqual([
      'home', 'record', 'training', 'inference', 'edit', 'workshop',
    ]);
  });

  it('an untagged item (HOME) is always visible', () => {
    expect(isCapabilityVisible({ key: 'home' }, { jetsonConnected: false, caps: OMX_FOLLOWER })).toBe(true);
  });

  it('only an EXPLICIT false hides — a missing/undefined cap key does not', () => {
    expect(
      isCapabilityVisible({ capabilityKey: 'recordable' }, { jetsonConnected: false, caps: {} })
    ).toBe(true);
  });
});

describe('robotGateDecision — requireRobotOrRedirect', () => {
  it('cloud-only mode ALWAYS navigates (no rosbridge ever delivers identity)', () => {
    expect(robotGateDecision({ debug: false, cloudOnly: true, robotType: '' })).toBe('navigate');
  });

  it('debug flag forces navigation', () => {
    expect(robotGateDecision({ debug: true, cloudOnly: false, robotType: '' })).toBe('navigate');
  });

  it('a settled robotType navigates', () => {
    expect(robotGateDecision({ debug: false, cloudOnly: false, robotType: 'omx_f' })).toBe('navigate');
  });

  it('waits when the identity has not arrived yet (empty / whitespace)', () => {
    expect(robotGateDecision({ debug: false, cloudOnly: false, robotType: '' })).toBe('wait');
    expect(robotGateDecision({ debug: false, cloudOnly: false, robotType: '   ' })).toBe('wait');
  });
});
