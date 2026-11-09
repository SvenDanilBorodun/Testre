// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// setRosHost derives the LOCAL rosbridge URL through utils/piMode
// localRosbridgeUrl, which since 2026-08-06 is the same-origin /rosbridge
// nginx proxy on EVERY local rig (Orange Pi and Windows student PC alike) —
// see that module for the two reasons. setRosHost is the only way the URL is
// set.

import reducer, { setRosHost } from '../rosSlice';

vi.mock('../../../utils/piMode', () => ({
  __esModule: true,
  // Same-origin on every local rig now, so the mock takes no arguments —
  // there is no Pi/non-Pi branch left to simulate.
  localRosbridgeUrl: () => `ws://${window.location.host}/rosbridge`,
}));

describe('rosSlice — same-origin rosbridge URL derivation', () => {
  it('setRosHost builds the same-origin /rosbridge proxy URL', () => {
    const state = reducer(undefined, setRosHost('student-pc'));
    expect(state.rosHost).toBe('student-pc');
    expect(state.rosbridgeUrl).toBe(`ws://${window.location.host}/rosbridge`);
  });

  it('setRosHost is Pi-agnostic — the same URL on a Pi hostname', () => {
    const state = reducer(undefined, setRosHost('edubotics-42.local'));
    expect(state.rosHost).toBe('edubotics-42.local');
    expect(state.rosbridgeUrl).toBe(`ws://${window.location.host}/rosbridge`);
  });

  it('setRosHost never derives a URL naming the unpublished :9090', () => {
    // The direct port is gone from docker-compose.yml. A derivation that
    // re-introduced it would both bypass the nginx Origin allowlist and
    // point at a closed port.
    const state = reducer(undefined, setRosHost('student-pc'));
    expect(state.rosbridgeUrl).not.toContain('9090');
  });
});
