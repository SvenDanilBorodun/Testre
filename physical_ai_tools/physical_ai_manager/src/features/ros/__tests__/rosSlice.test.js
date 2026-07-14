// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// setRosHost derives the rosbridge URL Pi-aware (utils/piMode
// localRosbridgeUrl): same-origin /rosbridge proxy on the Orange Pi, classic
// direct ws://<host>:9090 everywhere else. The non-Pi shape is load-bearing —
// the Windows rig and the Jetson swap-back both depend on it.

import reducer, { setRosHost, setRosbridgeUrl } from '../rosSlice';

let mockPi = false;
vi.mock('../../../utils/piMode', () => ({
  __esModule: true,
  localRosbridgeUrl: (host, piMode) => {
    const isPi = piMode === undefined ? mockPi : piMode;
    return isPi ? `ws://${window.location.host}/rosbridge` : `ws://${host}:9090`;
  },
}));

beforeEach(() => {
  mockPi = false;
});

describe('rosSlice — Pi-aware rosbridge URL derivation', () => {
  it('setRosHost builds the classic direct :9090 URL outside Pi mode', () => {
    const state = reducer(undefined, setRosHost('student-pc'));
    expect(state.rosHost).toBe('student-pc');
    expect(state.rosbridgeUrl).toBe('ws://student-pc:9090');
  });

  it('setRosHost builds the same-origin /rosbridge proxy URL in Pi mode', () => {
    mockPi = true;
    const state = reducer(undefined, setRosHost('edubotics-42.local'));
    expect(state.rosHost).toBe('edubotics-42.local');
    expect(state.rosbridgeUrl).toBe(`ws://${window.location.host}/rosbridge`);
  });

  it('setRosbridgeUrl still overrides the URL verbatim (Jetson :9091 swap)', () => {
    const seeded = reducer(undefined, setRosHost('student-pc'));
    const state = reducer(seeded, setRosbridgeUrl('ws://jetson-lan:9091'));
    expect(state.rosbridgeUrl).toBe('ws://jetson-lan:9091');
    expect(state.rosHost).toBe('student-pc');
  });
});
