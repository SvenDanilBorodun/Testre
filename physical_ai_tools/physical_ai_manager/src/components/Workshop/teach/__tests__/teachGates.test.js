/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

import { DE } from '../../blocks/messages_de';
import {
  resolveTeachMode, teachLeaderStatus, teachLeaderStatusNoticeDe,
  teachEntryBlockReason, teachModeFor, TEACH_BLOCK_TITLES_DE, TEACH_COUNTDOWN_S, TEACH_SPACE_DEBOUNCE_MS,
  TEACH_KEEPALIVE_MS, TEACH_RECORD_MAX_S, TEACH_MIN_POINTS, TEACH_ROBOT_PREVIEW_LEAD_IN_MAX_MS,
  TEACH_REPLAY_VELOCITY_FLOOR_RAD_S, TEACH_ROBOT_PREVIEW_NO_MOTION_HINT_MS,
  TEACH_ROBOT_PREVIEW_SETTLE_DELTA_RAD, TEACH_ROBOT_PREVIEW_SETTLE_STEP_MS,
  TEACH_ROBOT_PREVIEW_SETTLE_SAMPLES, TEACH_ROBOT_PREVIEW_FEED_STALE_MS,
  TEACH_ROBOT_PREVIEW_STOP_TAIL_MAX_MS, TEACH_LEADER_COLLISION_GRACE_MS, replayDriveEstimateMs,
} from '../teachGates';

// 30 s at 25 fps, joint1 = 0.8·sin(2π·t), 7-wide. Its peak joint speed (5.03 rad/s)
// is above 0.6 × v_limit, so the server stretches it: resegment_trajectory at
// 4.72 rad/s with a 1.5 s lead-in ends at 40.87 s (measured).
function fastFixtureRows() {
  const rows = [];
  for (let i = 0; i <= 750; i += 1) {
    const t = i / 25;
    rows.push([0.8 * Math.sin(2 * Math.PI * t), 0, 0, 0, 0, 0, t]);
  }
  return rows;
}

const OPEN = {
  heartbeatStatus: 'connected', runState: 'idle', paused: false, simMode: false,
  jogHandGuideOn: false, previewActive: false,
};

describe('teachEntryBlockReason', () => {
  it('opens when nothing blocks', () => {
    expect(teachEntryBlockReason(OPEN)).toBeNull();
  });

  it('orders offline > running/preview > sim > handguide', () => {
    const all = { ...OPEN, heartbeatStatus: 'disconnected', runState: 'running', simMode: true,
      jogHandGuideOn: true };
    expect(teachEntryBlockReason(all)).toBe('offline');
    expect(teachEntryBlockReason({ ...all, heartbeatStatus: 'connected' })).toBe('running');
    expect(teachEntryBlockReason({ ...all, heartbeatStatus: 'connected', previewActive: true }))
      .toBe('preview');
    expect(teachEntryBlockReason({ ...all, heartbeatStatus: 'connected', runState: 'idle' }))
      .toBe('sim');
    expect(teachEntryBlockReason({ ...OPEN, jogHandGuideOn: true })).toBe('handguide');
  });

  it('D8: a live leader is no longer a refusal (it selects leader mode)', () => {
    expect(teachEntryBlockReason({ ...OPEN, rsLeaderOn: true })).toBeNull();
    expect(Object.keys(TEACH_BLOCK_TITLES_DE)).not.toContain('leader');
    expect(DE.TEACH_BLOCK_LEADER).toBeUndefined();
  });

  it('treats a paused run as running and an unknown heartbeat as offline', () => {
    expect(teachEntryBlockReason({ ...OPEN, paused: true })).toBe('running');
    expect(teachEntryBlockReason({ ...OPEN, heartbeatStatus: undefined })).toBe('offline');
  });

  it('has a German title for every reason', () => {
    expect(TEACH_BLOCK_TITLES_DE).toEqual({
      offline: DE.TEACH_BLOCK_OFFLINE, running: DE.TEACH_BLOCK_RUNNING,
      preview: DE.TEACH_BLOCK_PREVIEW, sim: DE.TEACH_BLOCK_SIM, handguide: DE.TEACH_BLOCK_JOG,
      glide: DE.TEACH_BLOCK_GLIDE,
    });
    expect(Object.keys(TEACH_BLOCK_TITLES_DE).sort())
      .toEqual(['glide', 'handguide', 'offline', 'preview', 'running', 'sim']);
    Object.values(TEACH_BLOCK_TITLES_DE).forEach((t) => expect(t).toMatch(/\S/));
  });
});

describe('teachModeFor', () => {
  it.each([
    [true, null, 'leader'],
    [true, undefined, 'leader'],
    [true, {}, 'leader'],
    [true, { has_leader: true }, 'leader'],
    [true, { has_leader: undefined }, 'leader'],
    [true, { has_leader: false }, 'hand'],
    [false, { has_leader: true }, 'hand'],
    [false, null, 'hand'],
    [undefined, { has_leader: true }, 'hand'],
    [false, { has_leader: false }, 'hand'],
  ])('rsLeaderOn=%s caps=%j → %s', (rsLeaderOn, caps, mode) => {
    expect(teachModeFor({ rsLeaderOn, caps })).toBe(mode);
  });

  it('defaults to hand with no arguments', () => {
    expect(teachModeFor()).toBe('hand');
  });
});

describe('teach constants', () => {
  it('hold the values the session and the server mirrors rely on', () => {
    expect(TEACH_COUNTDOWN_S).toBe(3);
    expect(TEACH_SPACE_DEBOUNCE_MS).toBe(400);
    expect(TEACH_KEEPALIVE_MS).toBe(15000);
    expect(TEACH_RECORD_MAX_S).toBe(120);
    expect(TEACH_MIN_POINTS).toBe(2);
    expect(TEACH_ROBOT_PREVIEW_LEAD_IN_MAX_MS).toBe(4500);
    expect(TEACH_REPLAY_VELOCITY_FLOOR_RAD_S).toBe(4.72);
    expect(TEACH_ROBOT_PREVIEW_NO_MOTION_HINT_MS).toBe(52000);
    expect(TEACH_ROBOT_PREVIEW_SETTLE_DELTA_RAD).toBe(0.01);
    expect(TEACH_ROBOT_PREVIEW_SETTLE_STEP_MS).toBe(150);
    expect(TEACH_ROBOT_PREVIEW_SETTLE_SAMPLES).toBe(4);
    expect(TEACH_ROBOT_PREVIEW_FEED_STALE_MS).toBe(1000);
    expect(TEACH_ROBOT_PREVIEW_STOP_TAIL_MAX_MS).toBe(2000);
    expect(TEACH_LEADER_COLLISION_GRACE_MS).toBe(1000);
  });

  it('bounds the lead-in above the worst shipped lead-in (2π full turn)', () => {
    const worstS = (2 * Math.PI * (15 / 8)) / (0.6 * TEACH_REPLAY_VELOCITY_FLOOR_RAD_S);
    expect(TEACH_ROBOT_PREVIEW_LEAD_IN_MAX_MS).toBeGreaterThan(worstS * 1000);
  });
});

describe('replayDriveEstimateMs', () => {
  const row = (q, grip, t) => [q, 0, 0, 0, 0, grip, t];

  it('is the lead-in bound for no pair', () => {
    expect(replayDriveEstimateMs([])).toBe(4500);
    expect(replayDriveEstimateMs([row(0, 0, 0)])).toBe(4500);
    expect(replayDriveEstimateMs(null)).toBe(4500);
  });

  it('adds the recorded dt when the pair is slow', () => {
    expect(replayDriveEstimateMs([row(0, 0, 0), row(0.01, 0, 0.04)])).toBe(4500 + 40);
  });

  it('counts the gripper column against the velocity floor', () => {
    expect(replayDriveEstimateMs([row(0, 0, 0), row(0, 1.0, 0.04)]))
      .toBe(4500 + Math.ceil(1000 / (0.6 * 4.72)));
    expect(replayDriveEstimateMs([row(0, 0, 0), row(0, 1.0, 0.04)])).toBe(4854);
  });

  it('floors a sub-millisecond pair at 1 ms and a zero-dt pair at 1/30 s', () => {
    expect(replayDriveEstimateMs([row(0, 0, 0), row(0, 0, 0.0005)])).toBe(4501);
    expect(replayDriveEstimateMs([row(0, 0, 0.5), row(0, 0, 0.5)]))
      .toBe(Math.ceil(4500 + 1000 / 30));
    expect(replayDriveEstimateMs([row(0, 0, 0.5), row(0, 0, 0.4)]))
      .toBe(Math.ceil(4500 + 1000 / 30));
  });

  it('halves dt at speed 2 but never the velocity floor', () => {
    expect(replayDriveEstimateMs([row(0, 0, 0), row(0.01, 0, 0.04)], 2.0)).toBe(4520);
    const fast = [row(0, 0, 0), row(1.0, 0, 0.04)];
    expect(replayDriveEstimateMs(fast, 2.0)).toBe(replayDriveEstimateMs(fast, 1.0));
    expect(replayDriveEstimateMs(fast, 2.0)).toBe(4854);
  });

  it('outlasts the server span of a fast take (the take replays longer than recorded)', () => {
    const rows = fastFixtureRows();
    const est = replayDriveEstimateMs(rows);
    expect(est).toBeGreaterThanOrEqual(40870);
    // The old fixed window (lead-in + recorded duration + 1 s) ended while the
    // arm still moved.
    expect(est).toBeGreaterThan(4500 + 30000 + 1000);
  });
});

// R7 (fixed 2026-09-15): a rig that may have a leader never teaches before the
// leader-status bridge has given a DEFINITE answer.
describe('teachLeaderStatus / resolveTeachMode', () => {
  const PENDING = { available: false, followerOnly: false, hasLeader: undefined, busy: false, leaderOn: false, probed: false };
  const DOWN = { ...PENDING, probed: true };
  const LEADER_ON = { available: true, followerOnly: false, hasLeader: true, busy: false, leaderOn: true, probed: true };
  const FOLLOWER = { available: true, followerOnly: true, hasLeader: true, busy: false, leaderOn: false, probed: true };
  it.each([
    ['pending, omx_full', PENDING, { has_leader: true }, 'pending', null],
    ['pending, caps not yet pushed', PENDING, null, 'pending', null],
    ['answered unavailable', DOWN, { has_leader: true }, 'unavailable', null],
    ['an older hook without probed, unavailable', { available: false, leaderOn: false }, { has_leader: true }, 'unavailable', null],
    ['no bridge object at all', null, { has_leader: true }, 'unavailable', null],
    ['answered leader on', LEADER_ON, { has_leader: true }, 'known', 'leader'],
    ['answered follower only', FOLLOWER, { has_leader: true }, 'known', 'hand'],
    ['answered leader on, caps unknown', LEADER_ON, null, 'known', 'leader'],
    // Proven leader-less rigs (omx_follower, edu6_studio, edu1_studio) never wait.
    ['leader-less caps, pending', PENDING, { has_leader: false }, 'known', 'hand'],
    ['leader-less caps, unavailable', DOWN, { has_leader: false }, 'known', 'hand'],
    ['leader-less caps, no bridge', null, { has_leader: false }, 'known', 'hand'],
    ['the bridge saying has_leader false', { ...FOLLOWER, hasLeader: false }, null, 'known', 'hand'],
  ])('%s', (_label, rsBridge, caps, status, mode) => {
    expect(teachLeaderStatus({ rsBridge, caps })).toBe(status);
    expect(resolveTeachMode({ rsBridge, caps })).toBe(mode);
  });

  it('the notice: pending wording, then the platform-specific unavailable wording, none when known', () => {
    expect(teachLeaderStatusNoticeDe('pending', false)).toBe('Roboterstatus wird geprüft …');
    expect(teachLeaderStatusNoticeDe('pending', true)).toBe(DE.TEACH_LEADER_STATUS_PENDING);
    expect(teachLeaderStatusNoticeDe('unavailable', false)).toBe(
      'Leader-Status unbekannt — das EduBotics-Programm auf diesem PC antwortet nicht. '
      + 'Vormachen ist gesperrt, bis es wieder antwortet.');
    expect(teachLeaderStatusNoticeDe('unavailable', true)).toBe(
      'Leader-Status unbekannt — der Roboter-Dienst antwortet nicht. Bitte die System-Seite prüfen. '
      + 'Vormachen ist gesperrt, bis er wieder antwortet.');
    expect(teachLeaderStatusNoticeDe('known', false)).toBeNull();
    expect(teachLeaderStatusNoticeDe('known', true)).toBeNull();
  });
});
