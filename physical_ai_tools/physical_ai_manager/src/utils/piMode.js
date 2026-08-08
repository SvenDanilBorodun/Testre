// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// Orange Pi „Pi mode" detection + live agent health.
//
// Two independent signals live here:
//
//   1. `piMode` — resolved ONCE at boot from the STATIC `/pi-mode.json` marker
//      baked into the `-opi` manager image (nginx.opi.conf serves
//      `{"pi":true}`). It gates the System tab and the StartupGate carve-out.
//      Deterministic and independent of agent health — the image IS the Pi
//      flavor. On a NON-Pi image the SPA catch-all answers `/pi-mode.json` with
//      200 + index.html, so we require a VALID JSON parse of `{pi:true}` (the
//      same defensive guard `useVersionCheck` uses against the `/version.json`
//      fallthrough) — a 200-HTML body must NOT flip Pi mode.
//
//   2. `agentStatus` — the live `/api/system/status` snapshot, probed with
//      BACKOFF (never one-shot): the pi-agent binds its gateway listener only
//      AFTER its boot-time manager `up`, so an early probe 502s through the
//      proxy. This feeds the System window (and the network-aware disconnect
//      wording), NOT the tab gate.
//
// `piMode` MUST resolve before StartupGate decides whether to block (deploy
// plan §6 ordering constraint) — the static marker makes that cheap and
// race-free, and the provider exposes `piModeResolved` so StartupGate can wait
// the few ms it takes.

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

// ── module-level cache ───────────────────────────────────────────────────────
//
// Written once the marker resolves (by PiModeProvider), BEFORE `piModeResolved`
// flips true. Retained as the default for a no-arg `rsControlBase()` and read by
// `getPiModeSync()`. The Workshop control components (LeaderToggle / RunControls)
// no longer read it synchronously — they consume `piMode`/`piModeResolved` from
// the context and gate their FIRST Roboter-Studio probe on resolution, so the
// control base is never read before the marker resolves (it defaulted to the
// loopback :8769 during the boot window, i.e. the student's own PC from a remote
// Pi browser). Defaults to false so a non-Pi build AND every unit test with no
// provider behaves exactly as before.
let _piModeCache = false;

export function getPiModeSync() {
  return _piModeCache;
}

// Windows GUI control bridge (roboter_studio_control.py, loopback :8769). On the
// Pi the exact same JSON contract is served SAME-ORIGIN through the manager's
// `/api/system` reverse proxy — from a remote browser `localhost:8769` resolves
// to the student's own PC, so the Roboter-Studio toggle must route host-relative
// in Pi mode.
const RS_LOCAL_BASE = 'http://localhost:8769';
const RS_PI_BASE = '/api/system';

// Resolve the Roboter-Studio control base. Callers holding a live context value
// pass `piMode` explicitly (the components do, gated on `piModeResolved`) so the
// base can never be resolved before the marker does; a no-arg call falls back to
// the module cache.
export function rsControlBase(piMode) {
  const isPi = piMode === undefined ? _piModeCache : piMode;
  return isPi ? RS_PI_BASE : RS_LOCAL_BASE;
}

// ── same-origin robot transports (ALL local rigs) ────────────────────────────
//
// These two helpers are the single source of the LOCAL robot-transport URLs,
// and since 2026-08-06 they are same-origin on EVERY platform, for two
// independent reasons that happen to want the same answer:
//
//   * Orange Pi — school networks routinely filter non-standard ports, and
//     port 80 is the one port guaranteed reachable if the SPA loaded at all.
//   * Windows student rig — SECURITY. rosbridge is unauthenticated, so anyone
//     who can open the socket can publish to /leader/joint_trajectory and drive
//     a physical arm. Serving the page from :80 while connecting to :9090 made
//     that a CROSS-ORIGIN connection, and a WebSocket handshake has no CORS
//     preflight to stop one — so any site open in the student's browser could
//     reach it. The loopback bind was never a boundary against a page running
//     ON that PC. Both host port-publishes are now REMOVED from
//     docker-compose.yml and nginx.conf carries an Origin allowlist on
//     /rosbridge, so the only route in is same-origin from our own page.
//
// The `-opi` compose deliberately KEEPS its :9090/:8080 publishes as a
// debug/rollback path; the student compose does not.
//
// Both functions keep their `piMode` parameter purely so the four call sites
// and their tests need no churn — it no longer selects behaviour. The Jetson
// path is untouched by either: `useJetsonConnection` dispatches an explicit
// `ws://<ip>:9091` through `setRosbridgeUrl` (a passthrough reducer) and
// ImageGridCell renders Jetson frames from a rosbridge topic subscription
// rather than from `videoStreamBase`.

/**
 * Local rosbridge WebSocket URL — always the same-origin nginx proxy
 * (`ws(s)://<origin-host>/rosbridge`). `hostname` is used only as the
 * fallback when there is no DOM (jsdom without a location, SSR); it keeps
 * the same-origin SHAPE rather than naming a port, because :9090 is no
 * longer published on any student rig. `piMode` is vestigial (see above).
 */
export function localRosbridgeUrl(hostname, piMode) {  // eslint-disable-line no-unused-vars
  if (typeof window !== 'undefined' && window.location) {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${window.location.host}/rosbridge`;
  }
  return `ws://${hostname}/rosbridge`;
}

/**
 * Base URL for web_video_server MJPEG streams (`<base>/stream?topic=…`).
 * Always the same-origin `/video` proxy — a relative URL, which is valid in
 * `<img src>` and is what keeps those requests Origin-less (and therefore
 * deliberately ungated in nginx.conf). `rosHost`/`piMode` are vestigial.
 */
export function videoStreamBase(rosHost, piMode) {  // eslint-disable-line no-unused-vars
  return '/video';
}

// Shared German network-blocked wording (Pi mode). Shown by StartupGate's
// timeout screen and the heartbeat „Getrennt" state instead of the Windows-era
// „Docker prüfen" text, which is actively misleading on a Pi. Since rosbridge
// now rides the same-origin :80 proxy, the old „Port 9090 blockiert" wording
// is itself misleading — the realistic causes are the robot service still
// starting, or a middlebox breaking WebSocket upgrades.
export const PI_PORT_BLOCKED_HINT =
  'Verbindung zum Roboter blockiert? Netzwerk-Anleitung prüfen';

// ── the static Pi-mode marker ────────────────────────────────────────────────

const PI_MARKER_URL = '/pi-mode.json';
const MARKER_TIMEOUT_MS = 4000;

/**
 * Fetch the baked `/pi-mode.json` marker and return true ONLY on a valid JSON
 * parse of `{ "pi": true }`. Any non-JSON body (the SPA catch-all's index.html),
 * a non-2xx, a timeout or a network error resolves to false (fail to non-Pi).
 */
export async function fetchPiMarker() {
  if (typeof fetch !== 'function') return false;
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), MARKER_TIMEOUT_MS) : null;
  try {
    const res = await fetch(`${PI_MARKER_URL}?_=${Date.now()}`, {
      cache: 'no-store',
      ...(ctrl ? { signal: ctrl.signal } : {}),
    });
    if (!res.ok) return false;
    // `res.json()` throws on an HTML body — that throw is the guard.
    const json = await res.json();
    return !!(json && json.pi === true);
  } catch {
    // Dev server / SPA fallthrough / offline / non-Pi image — not a Pi.
    return false;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// ── the live agent-status probe ──────────────────────────────────────────────

const AGENT_STATUS_URL = '/api/system/status';
const AGENT_STATUS_TIMEOUT_MS = 6000;
// Backoff ladder while the agent gateway listener is not yet bound (or the proxy
// 502s). Caps at 15 s so a persistently-down agent doesn't hammer the proxy.
const AGENT_BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];
// Steady refresh cadence once the agent answers.
const AGENT_REFRESH_MS = 15000;

/**
 * GET `/api/system/status`. Returns the parsed status object, or null when the
 * proxy 502s (agent gateway not yet bound) / the body isn't valid JSON / the
 * request errors. Like the marker, a non-JSON body must NOT be mistaken for a
 * healthy agent.
 */
export async function fetchAgentStatus() {
  if (typeof fetch !== 'function') return null;
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), AGENT_STATUS_TIMEOUT_MS) : null;
  try {
    const res = await fetch(`${AGENT_STATUS_URL}?_=${Date.now()}`, {
      cache: 'no-store',
      ...(ctrl ? { signal: ctrl.signal } : {}),
    });
    if (!res.ok) return null;
    const json = await res.json();
    return json && typeof json === 'object' ? json : null;
  } catch {
    return null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// ── React context / provider ─────────────────────────────────────────────────

const DEFAULT_CONTEXT = {
  // `piMode` false + `piModeResolved` true so a MISSING provider (unit tests,
  // the teacher WebApp) is treated as a resolved non-Pi rig — StartupGate never
  // waits and never blocks on Pi grounds.
  piMode: false,
  piModeResolved: true,
  agentStatus: null,
  agentReachable: false,
  refreshAgentStatus: async () => null,
};

const PiModeContext = createContext(DEFAULT_CONTEXT);

export function usePiMode() {
  return useContext(PiModeContext);
}

export function PiModeProvider({ children }) {
  const [piMode, setPiMode] = useState(_piModeCache);
  const [piModeResolved, setPiModeResolved] = useState(false);
  const [agentStatus, setAgentStatus] = useState(null);
  const [agentReachable, setAgentReachable] = useState(false);

  // 1) Resolve the static marker exactly once at boot.
  useEffect(() => {
    let cancelled = false;
    fetchPiMarker().then((isPi) => {
      if (cancelled) return;
      _piModeCache = isPi;
      setPiMode(isPi);
      setPiModeResolved(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // 2) Once we know it's a Pi, probe the agent with backoff, then refresh
  //    steadily. A single self-rescheduling timer (no overlapping intervals).
  useEffect(() => {
    if (!piMode) return undefined;
    let cancelled = false;
    let timer = null;
    let attempt = 0;

    const tick = async () => {
      const status = await fetchAgentStatus();
      if (cancelled) return;
      if (status) {
        setAgentStatus(status);
        setAgentReachable(true);
        attempt = 0;
        timer = setTimeout(tick, AGENT_REFRESH_MS);
      } else {
        setAgentReachable(false);
        const delay = AGENT_BACKOFF_MS[Math.min(attempt, AGENT_BACKOFF_MS.length - 1)];
        attempt += 1;
        timer = setTimeout(tick, delay);
      }
    };
    tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [piMode]);

  // Imperative refresh for the System window to call after an action (scan,
  // start, stop, …) so the snapshot reflects the change without waiting for the
  // steady poll. Runs alongside the backoff loop; both only setState, so a race
  // is idempotent.
  const refreshAgentStatus = useCallback(async () => {
    const status = await fetchAgentStatus();
    if (status) {
      setAgentStatus(status);
      setAgentReachable(true);
    } else {
      setAgentReachable(false);
    }
    return status;
  }, []);

  const value = useMemo(
    () => ({ piMode, piModeResolved, agentStatus, agentReachable, refreshAgentStatus }),
    [piMode, piModeResolved, agentStatus, agentReachable, refreshAgentStatus]
  );

  return <PiModeContext.Provider value={value}>{children}</PiModeContext.Provider>;
}
