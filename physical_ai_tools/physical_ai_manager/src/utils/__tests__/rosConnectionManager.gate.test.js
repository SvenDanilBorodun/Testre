// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The Orange Pi's robot login gates the rosbridge websocket (2026-11-10): no
// socket is opened before the gate resolves, every concurrent caller shares ONE
// gated attempt, a refusal keeps the reconnect loop alive without stacking
// timers, and off a Pi (no gate) nothing changes.

import { vi } from 'vitest';

const constructed = [];

vi.mock('roslib', () => {
  class FakeRos {
    constructor({ url }) {
      this.url = url;
      this.isConnected = false;
      this.handlers = {};
      constructed.push(this);
    }

    on(event, fn) {
      this.handlers[event] = fn;
    }

    close() {}

    emit(event, arg) {
      if (this.handlers[event]) this.handlers[event](arg);
    }
  }
  return { __esModule: true, default: { Ros: FakeRos } };
});

// eslint-disable-next-line import/first
import rosConnectionManager from '../rosConnectionManager';

const URL = 'ws://edubotics-07.local/rosbridge';

beforeEach(() => {
  constructed.length = 0;
  rosConnectionManager.disconnect();
  rosConnectionManager.setConnectGate(null);
});

afterEach(() => {
  rosConnectionManager.disconnect();
  rosConnectionManager.setConnectGate(null);
  vi.useRealTimers();
});

describe('the robot-login connect gate', () => {
  it('opens no socket while the gate refuses, and keeps one timer for the retry', async () => {
    vi.useFakeTimers();
    const gate = vi.fn(() => Promise.reject(new Error('pi_login:login_required')));
    rosConnectionManager.setConnectGate(gate);
    const a = rosConnectionManager.getConnection(URL);
    const b = rosConnectionManager.getConnection(URL);
    await expect(a).rejects.toThrow(/robot login/);
    await expect(b).rejects.toThrow(/robot login/);
    expect(gate).toHaveBeenCalledTimes(1); // ONE gated attempt for both callers
    expect(constructed).toHaveLength(0);
    expect(rosConnectionManager.reconnectTimer).not.toBeNull();
    // Another refused attempt while the timer is pending adds no timer.
    const timer = rosConnectionManager.reconnectTimer;
    await expect(rosConnectionManager.getConnection(URL)).rejects.toThrow();
    expect(rosConnectionManager.reconnectTimer).toBe(timer);
  });

  it('the retry loop connects by itself once the login exists', async () => {
    // The first attempt after a URL change carries disconnect()'s intentional
    // flag; a refused gate must still leave a live retry, or a page whose
    // student logs in afterwards would never connect.
    vi.useFakeTimers();
    let allow = false;
    rosConnectionManager.setConnectGate(() => (allow ? Promise.resolve() : Promise.reject(new Error('no'))));
    await expect(rosConnectionManager.getConnection(URL)).rejects.toThrow();
    allow = true;
    await vi.advanceTimersByTimeAsync(1000);
    expect(constructed).toHaveLength(1);
  });

  it('a disconnect while the gate waits opens nothing and revives nothing', async () => {
    let release;
    rosConnectionManager.setConnectGate(() => new Promise((resolve) => { release = resolve; }));
    const pending = rosConnectionManager.getConnection(URL);
    rosConnectionManager.disconnect();
    release();
    await expect(pending).rejects.toThrow(/closed/);
    expect(constructed).toHaveLength(0);
    expect(rosConnectionManager.reconnectTimer).toBeNull();
  });

  it('opens the socket once the gate resolves', async () => {
    let allow = false;
    rosConnectionManager.setConnectGate(() => (allow ? Promise.resolve() : Promise.reject(new Error('no'))));
    await expect(rosConnectionManager.getConnection(URL)).rejects.toThrow();
    allow = true;
    rosConnectionManager.reconnectTimer = null; // drive the next attempt directly
    const pending = rosConnectionManager.getConnection(URL);
    await Promise.resolve();
    await Promise.resolve();
    expect(constructed).toHaveLength(1);
    constructed[0].isConnected = true;
    constructed[0].emit('connection');
    await expect(pending).resolves.toBe(constructed[0]);
  });

  it('off a Pi there is no gate and the socket opens at once', async () => {
    const pending = rosConnectionManager.getConnection(URL);
    expect(constructed).toHaveLength(1);
    constructed[0].isConnected = true;
    constructed[0].emit('connection');
    await expect(pending).resolves.toBe(constructed[0]);
  });

  it('an open socket is reused without asking the gate again', async () => {
    const gate = vi.fn(() => Promise.resolve());
    rosConnectionManager.setConnectGate(gate);
    const first = rosConnectionManager.getConnection(URL);
    await Promise.resolve();
    await Promise.resolve();
    constructed[0].isConnected = true;
    constructed[0].emit('connection');
    await first;
    await rosConnectionManager.getConnection(URL);
    expect(gate).toHaveBeenCalledTimes(1);
    expect(constructed).toHaveLength(1);
  });
});
