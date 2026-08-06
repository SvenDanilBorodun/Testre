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
  localRosbridgeUrl,
  videoStreamBase,
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

// BOTH robot transports must ride the same-origin nginx :80 proxies
// (ws /rosbridge + /video/) on EVERY local rig — Orange Pi AND the Windows
// student PC. Two independent reasons converge on the same answer:
//
//   * Pi: school networks filter non-standard ports.
//   * Windows: SECURITY. rosbridge is unauthenticated, so a page served from
//     :80 connecting to :9090 was a CROSS-ORIGIN WebSocket handshake — and a
//     WS handshake has no CORS preflight, so any site open in the student's
//     browser could complete it and publish to /leader/joint_trajectory.
//     docker-compose.yml no longer publishes :9090/:8080 at all, so a URL
//     naming either port does not merely bypass the nginx Origin allowlist —
//     it points at nothing and bricks the rig.
//
// The `piMode` argument is retained at the call sites but no longer selects
// behaviour; these tests pin that BOTH values give the same same-origin answer.
describe('localRosbridgeUrl — same-origin proxy on every local rig', () => {
  it('builds the same-origin /rosbridge proxy URL on a Pi', () => {
    expect(localRosbridgeUrl('edubotics-42.local', true)).toBe(
      `ws://${window.location.host}/rosbridge`
    );
  });

  it('builds the SAME same-origin URL on a Windows student rig', () => {
    expect(localRosbridgeUrl('student-pc', false)).toBe(
      `ws://${window.location.host}/rosbridge`
    );
  });

  it('builds the same-origin URL with no piMode argument at all', () => {
    expect(localRosbridgeUrl('student-pc')).toBe(
      `ws://${window.location.host}/rosbridge`
    );
  });

  it('never names the direct rosbridge port — it is no longer published', () => {
    for (const piMode of [true, false, undefined]) {
      expect(localRosbridgeUrl('student-pc', piMode)).not.toContain('9090');
    }
  });
});

describe('videoStreamBase — same-origin proxy on every local rig', () => {
  it('builds the same-origin /video base on a Pi', () => {
    expect(videoStreamBase('192.168.0.5', true)).toBe('/video');
  });

  it('builds the SAME /video base on a Windows student rig', () => {
    expect(videoStreamBase('192.168.0.5', false)).toBe('/video');
  });

  it('builds the /video base with no piMode argument at all', () => {
    expect(videoStreamBase('192.168.0.5')).toBe('/video');
  });

  it('stays RELATIVE, which is what keeps <img> requests Origin-less', () => {
    // nginx.conf deliberately does NOT Origin-gate /video/ because an <img>
    // load sends no Origin. An absolute URL here would still work, but the
    // relative form is what makes "same-origin" true by construction.
    expect(videoStreamBase('192.168.0.5', false).startsWith('/')).toBe(true);
    expect(videoStreamBase('192.168.0.5', false)).not.toContain('8080');
  });
});
