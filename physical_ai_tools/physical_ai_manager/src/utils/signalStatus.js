// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The client half of `/edubotics/signal_status` (schema v1, spec §2.4).
//
// The server publishes FACTS — per-source callback rates and ages, the free
// bytes of the dataset filesystem and both floors — and never a verdict: only
// the page knows the fps it is about to record. Everything here is pure; both
// clocks (`receivedAt`, `nowMs`) are `performance.now()` readings.
//
// No payload, or a payload older than SIGNAL_STALE_MS, means "no facts": an
// older server image has no such topic, and a dead link publishes nothing. The
// page then shows no badge, no banner and refuses nothing on these grounds.

export const SIGNAL_STATUS_TOPIC = '/edubotics/signal_status';
export const SCHEMA_VERSION = 1;
export const SLOW_RATIO = 0.9;
export const STALLED_AFTER_S = 2.0;
export const SIGNAL_STALE_MS = 5000;
export const BOOT_GRACE_S = 10;
export const SAVING_GRACE_MS = 2500;

const KINDS = new Set(['camera', 'follower', 'leader']);

const isFiniteNumber = (v) => typeof v === 'number' && Number.isFinite(v);
const numberOrNull = (v) => (v === null || v === undefined ? null : isFiniteNumber(v) ? v : undefined);

function parseSource(row) {
  if (!row || typeof row !== 'object') return null;
  if (!KINDS.has(row.kind) || typeof row.name !== 'string' || !row.name) return null;
  const hz = numberOrNull(row.hz);
  const ageS = numberOrNull(row.age_s);
  if (hz === undefined || ageS === undefined) return null;
  return {
    kind: row.kind,
    name: row.name,
    topic: typeof row.topic === 'string' ? row.topic : '',
    hz,
    age_s: ageS,
  };
}

function parseDisk(disk) {
  if (!disk || typeof disk !== 'object') return null;
  const { free_bytes: free, start_floor_bytes: start, critical_floor_bytes: critical } = disk;
  if (![free, start, critical].every(isFiniteNumber)) return null;
  return { free_bytes: free, start_floor_bytes: start, critical_floor_bytes: critical };
}

/**
 * Parse one std_msgs/String payload. Returns the schema-v1 object (wire field
 * names, malformed source rows dropped) or null for anything untrustworthy.
 */
export function parseSignalStatus(data) {
  if (typeof data !== 'string' || !data) return null;
  let raw;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (raw.v !== SCHEMA_VERSION || !Array.isArray(raw.sources)) return null;
  return {
    v: SCHEMA_VERSION,
    seq: isFiniteNumber(raw.seq) ? raw.seq : 0,
    uptime_s: isFiniteNumber(raw.uptime_s) ? raw.uptime_s : 0,
    recording: raw.recording === true,
    sources: raw.sources.map(parseSource).filter(Boolean),
    disk: parseDisk(raw.disk),
  };
}

/** True when the payload is present and younger than SIGNAL_STALE_MS. */
export function isSignalFresh(payload, receivedAt, nowMs) {
  if (!payload || !isFiniteNumber(receivedAt) || !isFiniteNumber(nowMs)) return false;
  return nowMs - receivedAt <= SIGNAL_STALE_MS;
}

function verdictFor(source, { uptimeS, expectedHz }) {
  if (source.age_s === null) {
    // Nothing since boot: a fact only once the boot grace is over.
    return uptimeS >= BOOT_GRACE_S ? 'stalled' : 'unknown';
  }
  if (source.age_s >= STALLED_AFTER_S) return 'stalled';
  if (source.hz !== null && expectedHz > 0 && source.hz < SLOW_RATIO * expectedHz) return 'slow';
  return 'ok';
}

/**
 * One verdict per source: 'ok' | 'slow' | 'stalled' | 'unknown'.
 * `suppress` (SAVING / FINISHING / COLLISION and SAVING_GRACE_MS after SAVING)
 * turns every verdict into 'unknown'. Null when there are no fresh facts.
 * @returns {null | Array<{kind, name, topic, hz, ageS, verdict}>}
 */
export function sourceVerdicts(payload, { expectedHz, receivedAt, nowMs, suppress } = {}) {
  if (!isSignalFresh(payload, receivedAt, nowMs)) return null;
  const hzTarget = isFiniteNumber(expectedHz) ? expectedHz : 0;
  return payload.sources.map((s) => ({
    kind: s.kind,
    name: s.name,
    topic: s.topic,
    hz: s.hz,
    ageS: s.age_s,
    verdict: suppress ? 'unknown' : verdictFor(s, { uptimeS: payload.uptime_s, expectedHz: hzTarget }),
  }));
}

/** 'ok' | 'low' (below the start floor) | 'critical' | 'unknown'. */
export function diskVerdict(payload) {
  const disk = payload?.disk;
  if (!disk) return 'unknown';
  if (disk.free_bytes < disk.critical_floor_bytes) return 'critical';
  if (disk.free_bytes < disk.start_floor_bytes) return 'low';
  return 'ok';
}
