// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The reconnect policy of the rosbridge singleton (spec-r5 §2.3-3). The local,
// same-origin rosbridge (`ws(s)://<host>/rosbridge`, both platforms through
// nginx) comes back within 2 s and never gives up, so a page that lost its
// link during a recording sees the end within ~4 s instead of up to ~30 s. The
// Jetson proxy (`ws://<ip>:9091`) keeps its exponential backoff and its cap.

import rosConnectionManager, {
  LOCAL_RECONNECT_MAX_DELAY_MS,
  isLocalRosbridgeUrl,
  reconnectDelayMs,
} from '../rosConnectionManager';

describe('which rosbridge is local', () => {
  it.each([
    ['ws://localhost/rosbridge', true],
    ['wss://192.168.1.20/rosbridge', true],
    ['ws://edubotics-pi.local/rosbridge', true],
    ['ws://localhost:8080/rosbridge', true],
    ['ws://10.0.0.5:9091', false],
    ['ws://10.0.0.5:9091/rosbridge', false],
    ['ws://localhost:9090', false],
    ['', false],
    ['not a url', false],
  ])('%s → %s', (url, local) => {
    expect(isLocalRosbridgeUrl(url)).toBe(local);
  });
});

describe('the reconnect delay', () => {
  it('local: 1 s, then 2 s for ever', () => {
    expect(LOCAL_RECONNECT_MAX_DELAY_MS).toBe(2000);
    const delays = [0, 1, 2, 3, 10, 50].map((n) => reconnectDelayMs('ws://localhost/rosbridge', n));
    expect(delays).toEqual([1000, 2000, 2000, 2000, 2000, 2000]);
  });

  it('the Jetson proxy: unchanged exponential backoff up to 30 s', () => {
    const delays = [0, 1, 2, 3, 4, 5, 6, 20].map((n) => reconnectDelayMs('ws://10.0.0.5:9091', n));
    expect(delays).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  });
});

describe('_scheduleReconnect', () => {
  let delays;
  let timeoutSpy;

  beforeEach(() => {
    vi.useFakeTimers();
    rosConnectionManager.disconnect();
    delays = [];
    timeoutSpy = vi.spyOn(globalThis, 'setTimeout').mockImplementation((fn, ms) => {
      delays.push(ms);
      return delays.length;
    });
  });

  afterEach(() => {
    timeoutSpy.mockRestore();
    rosConnectionManager.disconnect();
    vi.useRealTimers();
  });

  function scheduleTimes(url, n) {
    rosConnectionManager.url = url;
    rosConnectionManager.intentionalDisconnect = false;
    for (let i = 0; i < n; i += 1) rosConnectionManager._scheduleReconnect();
  }

  it('local: no attempt cap — attempt 40 is still scheduled, 2 s apart', () => {
    scheduleTimes('ws://localhost/rosbridge', 40);
    expect(delays).toHaveLength(40);
    expect(delays.slice(0, 3)).toEqual([1000, 2000, 2000]);
    expect(new Set(delays.slice(1))).toEqual(new Set([2000]));
  });

  it('the Jetson proxy still gives up after its 30 attempts', () => {
    scheduleTimes('ws://10.0.0.5:9091', 40);
    expect(delays).toHaveLength(30);
    expect(delays.slice(0, 6)).toEqual([1000, 2000, 4000, 8000, 16000, 30000]);
  });

  it('an intentional disconnect schedules nothing', () => {
    rosConnectionManager.url = 'ws://localhost/rosbridge';
    rosConnectionManager.intentionalDisconnect = true;
    rosConnectionManager._scheduleReconnect();
    expect(delays).toEqual([]);
  });
});
