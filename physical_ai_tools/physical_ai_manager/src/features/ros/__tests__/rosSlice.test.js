// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// setRosHost derives the LOCAL rosbridge URL through utils/piMode
// localRosbridgeUrl, which since 2026-08-06 is the same-origin /rosbridge
// nginx proxy on EVERY local rig (Orange Pi and Windows student PC alike) —
// see that module for the two reasons. What is load-bearing HERE is the split
// between the two reducers: setRosHost DERIVES, setRosbridgeUrl is a verbatim
// PASSTHROUGH, and the Jetson's JWT-gated ws://<ip>:9091 rides the latter. So
// the security change to the derivation provably cannot reach the Jetson path.

import reducer, { setRosHost, setRosbridgeUrl } from '../rosSlice';

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

  it('setRosbridgeUrl still overrides the URL verbatim (Jetson :9091 swap)', () => {
    const seeded = reducer(undefined, setRosHost('student-pc'));
    const state = reducer(seeded, setRosbridgeUrl('ws://jetson-lan:9091'));
    expect(state.rosbridgeUrl).toBe('ws://jetson-lan:9091');
    expect(state.rosHost).toBe('student-pc');
  });
});
