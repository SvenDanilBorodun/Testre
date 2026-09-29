// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The client half of /edubotics/signal_status (spec §2.4 schema v1, §3.9).
// The server publishes FACTS (rates, ages, free bytes); the verdicts are the
// client's, because only the client knows the fps being recorded.

import {
  BOOT_GRACE_S,
  SAVING_GRACE_MS,
  SIGNAL_STALE_MS,
  SLOW_RATIO,
  STALLED_AFTER_S,
  diskVerdict,
  parseSignalStatus,
  sourceVerdicts,
} from '../signalStatus';

const WIRE = {
  v: 1,
  seq: 42,
  uptime_s: 12.0,
  recording: false,
  sources: [
    { kind: 'camera', name: 'gripper', topic: '/gripper/image_raw', hz: 29.8, age_s: 0.03 },
    { kind: 'camera', name: 'scene', topic: '/scene/image_raw', hz: 11.2, age_s: 0.08 },
    { kind: 'follower', name: 'follower', topic: '/joint_states', hz: 98.9, age_s: 0.01 },
    { kind: 'leader', name: 'leader', topic: '/leader/joint_trajectory', hz: null, age_s: null },
  ],
  disk: { free_bytes: 52345678901, start_floor_bytes: 3000000000, critical_floor_bytes: 1000000000 },
};

const raw = (patch = {}) => JSON.stringify({ ...WIRE, ...patch });

describe('constants', () => {
  it('match the spec', () => {
    expect(SLOW_RATIO).toBe(0.9);
    expect(STALLED_AFTER_S).toBe(2.0);
    expect(SIGNAL_STALE_MS).toBe(5000);
    expect(BOOT_GRACE_S).toBe(10);
    expect(SAVING_GRACE_MS).toBe(2500);
  });
});

describe('parseSignalStatus', () => {
  it('parses schema v1 from the std_msgs/String data', () => {
    const p = parseSignalStatus(raw());
    expect(p.v).toBe(1);
    expect(p.seq).toBe(42);
    expect(p.uptime_s).toBe(12);
    expect(p.recording).toBe(false);
    expect(p.sources).toHaveLength(4);
    expect(p.sources[3]).toEqual({
      kind: 'leader', name: 'leader', topic: '/leader/joint_trajectory', hz: null, age_s: null,
    });
    expect(p.disk).toEqual(WIRE.disk);
  });

  it.each([
    ['not JSON', '{'],
    ['empty', ''],
    ['not a string', 42],
    ['an array', '[]'],
    ['another schema version', raw({ v: 2 })],
    ['no sources array', raw({ sources: null })],
  ])('refuses %s', (_label, data) => {
    expect(parseSignalStatus(data)).toBeNull();
  });

  it('drops malformed source rows and keeps the rest', () => {
    const p = parseSignalStatus(raw({
      sources: [
        { kind: 'camera', name: 'gripper', topic: '/g', hz: 30, age_s: 0.1 },
        { kind: 'lidar', name: 'x', topic: '/x', hz: 1, age_s: 0 },
        { kind: 'camera', name: 7, topic: '/y', hz: 1, age_s: 0 },
        { kind: 'follower', name: 'follower', topic: '/joint_states', hz: 'fast', age_s: 0 },
      ],
    }));
    expect(p.sources.map((s) => s.name)).toEqual(['gripper']);
  });

  it('reads a missing or broken disk block as null', () => {
    expect(parseSignalStatus(raw({ disk: null })).disk).toBeNull();
    expect(parseSignalStatus(raw({ disk: { free_bytes: 'viel' } })).disk).toBeNull();
  });
});

describe('sourceVerdicts', () => {
  const payload = parseSignalStatus(raw());
  const opts = (patch = {}) => ({ expectedHz: 30, receivedAt: 1000, nowMs: 1500, suppress: false, ...patch });

  it('returns null without a payload or when the payload is stale (old image, dead link)', () => {
    expect(sourceVerdicts(null, opts())).toBeNull();
    expect(sourceVerdicts(payload, opts({ nowMs: 1000 + SIGNAL_STALE_MS + 1 }))).toBeNull();
    expect(sourceVerdicts(payload, opts({ receivedAt: null }))).toBeNull();
  });

  it('judges ok / slow / stalled against the recorded fps', () => {
    const v = sourceVerdicts(payload, opts());
    expect(v.map((s) => [s.name, s.verdict])).toEqual([
      ['gripper', 'ok'],
      ['scene', 'slow'], // 11.2 < 0.9 × 30
      ['follower', 'ok'],
      ['leader', 'stalled'], // nothing since boot, and the boot grace is over
    ]);
    expect(v[1]).toMatchObject({ kind: 'camera', topic: '/scene/image_raw', hz: 11.2 });
  });

  it('27 Hz is fine at 30 fps, 26.9 is slow', () => {
    const p = parseSignalStatus(raw({
      sources: [
        { kind: 'camera', name: 'a', topic: '/a', hz: 27, age_s: 0 },
        { kind: 'camera', name: 'b', topic: '/b', hz: 26.9, age_s: 0 },
      ],
    }));
    expect(sourceVerdicts(p, opts()).map((s) => s.verdict)).toEqual(['ok', 'slow']);
  });

  it('a source silent for 2 s is stalled, whatever its last rate', () => {
    const p = parseSignalStatus(raw({
      sources: [
        { kind: 'camera', name: 'a', topic: '/a', hz: 30, age_s: 1.99 },
        { kind: 'camera', name: 'b', topic: '/b', hz: 30, age_s: 2.0 },
      ],
    }));
    expect(sourceVerdicts(p, opts()).map((s) => s.verdict)).toEqual(['ok', 'stalled']);
  });

  it('within the boot grace, a source that never sent is unknown, not stalled', () => {
    const p = parseSignalStatus(raw({ uptime_s: BOOT_GRACE_S - 0.1 }));
    expect(sourceVerdicts(p, opts())[3].verdict).toBe('unknown');
  });

  it('a live source whose rate is not measurable yet is ok', () => {
    const p = parseSignalStatus(raw({
      sources: [{ kind: 'follower', name: 'follower', topic: '/joint_states', hz: null, age_s: 0.2 }],
    }));
    expect(sourceVerdicts(p, opts())[0].verdict).toBe('ok');
  });

  it('suppressed (SAVING / FINISHING / COLLISION) → every source unknown', () => {
    expect(sourceVerdicts(payload, opts({ suppress: true })).map((s) => s.verdict))
      .toEqual(['unknown', 'unknown', 'unknown', 'unknown']);
  });

  it('without a known fps nothing is slow', () => {
    expect(sourceVerdicts(payload, opts({ expectedHz: 0 }))[1].verdict).toBe('ok');
  });
});

describe('diskVerdict', () => {
  const withFree = (free) => parseSignalStatus(raw({ disk: { ...WIRE.disk, free_bytes: free } }));
  it.each([
    [52e9, 'ok'],
    [3e9, 'ok'],
    [2999999999, 'low'],
    [1e9, 'low'],
    [999999999, 'critical'],
  ])('%s free → %s', (free, verdict) => {
    expect(diskVerdict(withFree(free))).toBe(verdict);
  });

  it('unknown without a payload or a disk block', () => {
    expect(diskVerdict(null)).toBe('unknown');
    expect(diskVerdict(parseSignalStatus(raw({ disk: null })))).toBe('unknown');
  });
});
