// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Pi-mode marker guard is load-bearing: on a NON-Pi image the SPA catch-all
// answers GET /pi-mode.json with 200 + index.html, so a bare res.ok would flip
// Pi mode on every non-Pi rig. fetchPiMarker MUST require a valid JSON parse of
// {"pi": true}. The agent-status probe likewise must not mistake a 502/HTML body
// (the gateway listener not yet bound) for a healthy agent.

import {
  fetchPiMarker,
  fetchAgentStatus,
  getPiModeSync,
  rsControlBase,
} from '../piMode';

function res({ ok = true, status = 200, json }) {
  return Promise.resolve({
    ok,
    status,
    json: json || (() => Promise.resolve({})),
  });
}

// A body that is NOT valid JSON (the index.html SPA fallthrough) → json() throws.
function htmlRes() {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.reject(new SyntaxError('Unexpected token < in JSON')),
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('fetchPiMarker', () => {
  it('returns true ONLY on a valid {"pi": true} JSON body', async () => {
    global.fetch = vi.fn(() => res({ json: () => Promise.resolve({ pi: true }) }));
    await expect(fetchPiMarker()).resolves.toBe(true);
  });

  it('returns false when the SPA catch-all serves index.html (json parse throws)', async () => {
    global.fetch = vi.fn(() => htmlRes());
    await expect(fetchPiMarker()).resolves.toBe(false);
  });

  it('returns false on {"pi": false} or a missing flag', async () => {
    global.fetch = vi.fn(() => res({ json: () => Promise.resolve({ pi: false }) }));
    await expect(fetchPiMarker()).resolves.toBe(false);
    global.fetch = vi.fn(() => res({ json: () => Promise.resolve({}) }));
    await expect(fetchPiMarker()).resolves.toBe(false);
  });

  it('returns false on a non-2xx response', async () => {
    global.fetch = vi.fn(() => res({ ok: false, status: 404 }));
    await expect(fetchPiMarker()).resolves.toBe(false);
  });

  it('returns false on a network error', async () => {
    global.fetch = vi.fn(() => Promise.reject(new Error('offline')));
    await expect(fetchPiMarker()).resolves.toBe(false);
  });
});

describe('fetchAgentStatus', () => {
  it('returns the parsed status object on a healthy 200 JSON body', async () => {
    const status = { lan_ip: '192.168.1.7', robot_tier_up: false };
    global.fetch = vi.fn(() => res({ json: () => Promise.resolve(status) }));
    await expect(fetchAgentStatus()).resolves.toEqual(status);
  });

  it('returns null on a 502 (agent gateway not yet bound)', async () => {
    global.fetch = vi.fn(() => res({ ok: false, status: 502 }));
    await expect(fetchAgentStatus()).resolves.toBeNull();
  });

  it('returns null when the proxy serves a non-JSON body', async () => {
    global.fetch = vi.fn(() => htmlRes());
    await expect(fetchAgentStatus()).resolves.toBeNull();
  });
});

describe('rsControlBase / getPiModeSync defaults', () => {
  it('defaults to non-Pi (loopback :8769) with no provider resolved', () => {
    expect(getPiModeSync()).toBe(false);
    expect(rsControlBase()).toBe('http://localhost:8769');
  });
});
