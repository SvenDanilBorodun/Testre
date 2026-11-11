// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// Locks the two pure sidebar-gating decisions StudentApp uses:
//  - isCapabilityVisible: the capability filter (null caps hide nothing;
//    explicit `false` hides).
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

// The 6-tab nav map (HOME untagged). Inferenz carries NO capabilityKey —
// mirrors StudentApp: on a robot that cannot run models the cloud panel says
// why instead of the tab vanishing.
const NAV = [
  { key: 'home' },
  { key: 'record', capabilityKey: 'recordable' },
  { key: 'training', capabilityKey: 'trainable' },
  { key: 'inference' },
  { key: 'edit', capabilityKey: 'editable' },
  { key: 'workshop', capabilityKey: 'roboter_studio' },
];

function visibleKeys(opts) {
  return NAV.filter((n) => isCapabilityVisible(n, opts)).map((n) => n.key);
}

describe('isCapabilityVisible — capability nav filter', () => {
  it('null caps hide NOTHING (pre-first-tick / cloud / old image)', () => {
    expect(visibleKeys({ caps: null })).toEqual([
      'home', 'record', 'training', 'inference', 'edit', 'workshop',
    ]);
  });

  it('omx_full (all caps true) hides nothing', () => {
    expect(visibleKeys({ caps: OMX_FULL })).toEqual([
      'home', 'record', 'training', 'inference', 'edit', 'workshop',
    ]);
  });

  it('omx_follower hides Aufnahme/Training/Daten, keeps Inferenz + Roboter Studio', () => {
    expect(visibleKeys({ caps: OMX_FOLLOWER })).toEqual([
      'home', 'inference', 'workshop',
    ]);
  });

  it('an untagged item (HOME) is always visible', () => {
    expect(isCapabilityVisible({ key: 'home' }, { caps: OMX_FOLLOWER })).toBe(true);
  });

  it('only an EXPLICIT false hides — a missing/undefined cap key does not', () => {
    expect(
      isCapabilityVisible({ capabilityKey: 'recordable' }, { caps: {} })
    ).toBe(true);
  });

  it('Inferenz (no capabilityKey) stays visible even against inferable:false', () => {
    // The invariant "Inferenz is always visible": the nav item carries no
    // capabilityKey, so no manifest — however restrictive — can hide it.
    expect(
      isCapabilityVisible({ key: 'inference' }, {
        caps: { ...OMX_FOLLOWER, inferable: false },
      })
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
