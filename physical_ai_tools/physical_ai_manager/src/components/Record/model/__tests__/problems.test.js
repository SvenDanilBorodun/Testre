// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Aufnahme problem banner (spec §3.9): one banner, the most important
// problem first, only for a real problem.

import RECORD_COPY from '../recordCopy';
import { EMPTY_FINISH, EMPTY_RECORD_SESSION } from '../../../../features/tasks/recordSession';
import {
  WARN_NOTICE_MS,
  deriveProblems,
  diskProblem,
  firstProblem,
  slowSourceProblem,
  stalledSourceProblem,
} from '../problems';

const NOW = 1_700_000_000_000;
const P = RECORD_COPY.problem;
const v = (kind, name, verdict, hz = 30) => ({ kind, name, topic: '', hz, ageS: 0, verdict });

describe('diskProblem', () => {
  const disk = (verdict, free) => ({ verdict, free, startFloor: 3e9, criticalFloor: 1e9 });

  it('refuses Start below the start floor', () => {
    expect(diskProblem(disk('low', 2.1e9))).toEqual({ kind: 'bad', textDe: P.diskLow(2.1e9, 3e9) });
    expect(diskProblem(disk('critical', 0.4e9))).toEqual({ kind: 'bad', textDe: P.diskLow(0.4e9, 3e9) });
  });

  it('while recording: a warning between the floors, bad below the critical one', () => {
    expect(diskProblem(disk('low', 2.1e9), { running: true }))
      .toEqual({ kind: 'warn', textDe: P.diskLowRecording(2.1e9, 1e9) });
    expect(diskProblem(disk('critical', 0.4e9), { running: true }))
      .toEqual({ kind: 'bad', textDe: P.diskCritical(0.4e9) });
    expect(P.diskCritical(0.4e9)).toBe('Der Speicher ist fast voll (nur noch 0,4 GB frei). Lösche alte Datensätze im Tab Daten.');
  });

  it('nothing for ok / unknown', () => {
    expect(diskProblem(disk('ok', 5e10))).toBeNull();
    expect(diskProblem({ verdict: 'unknown' })).toBeNull();
    expect(diskProblem(null)).toBeNull();
  });
});

describe('source sentences', () => {
  it('stalled camera / follower / leader variants', () => {
    expect(stalledSourceProblem(v('camera', 'scene', 'stalled')).textDe)
      .toBe('Die Szenen-Kamera sendet keine Bilder. Prüfe das Kabel. Hilft das nicht, starte die Umgebung neu.');
    expect(stalledSourceProblem(v('follower', 'follower', 'stalled')).textDe).toBe(P.followerStalled);
    const leader = v('leader', 'leader', 'stalled');
    expect(stalledSourceProblem(leader, { bridge: { available: true, followerOnly: true }, activation: { state: 'idle' } }).textDe)
      .toBe(P.leaderOff);
    expect(stalledSourceProblem(leader, { bridge: { available: false, followerOnly: false }, activation: { state: 'failed' } }))
      .toEqual({ kind: 'bad', textDe: P.leaderNotActivated, linkToHome: true });
    expect(stalledSourceProblem(leader, { activation: null }).textDe).toBe(P.leaderStalled);
  });

  it('slow camera and arm', () => {
    expect(slowSourceProblem(v('camera', 'gripper', 'slow', 11.2), 30))
      .toEqual({ kind: 'warn', textDe: P.cameraSlow('gripper', 11.2, 30) });
    expect(slowSourceProblem(v('follower', 'follower', 'slow', 20), 30).textDe)
      .toBe('Der Follower-Arm meldet nur 20 statt mindestens 30 Messungen pro Sekunde. Die Aufnahme kann ruckeln.');
  });
});

describe('deriveProblems — the order', () => {
  const everything = {
    view: 'RECORDING',
    running: true,
    heartbeat: 'timeout',
    notice: { kind: 'error', text: 'Harter Fehler.', at: NOW },
    transient: { kind: 'bad', textDe: 'Befehl abgelehnt.', until: NOW + 1000 },
    session: EMPTY_RECORD_SESSION,
    disk: { verdict: 'critical', free: 0.5e9, startFloor: 3e9, criticalFloor: 1e9 },
    verdicts: [v('camera', 'scene', 'slow', 10), v('leader', 'leader', 'stalled')],
    fps: 30,
    nowWallMs: NOW,
  };

  it('lists them most important first', () => {
    expect(deriveProblems(everything).map((p) => p.textDe)).toEqual([
      'Harter Fehler.',
      'Befehl abgelehnt.',
      P.linkLost,
      P.diskCritical(0.5e9),
      P.leaderStalled,
      P.cameraSlow('scene', 10, 30),
    ]);
  });

  it('a record warning sits between stalled and slow sources, for 12 s', () => {
    const input = {
      ...everything, notice: { kind: 'warn', text: 'Kamera hängt.', at: NOW - 1000 }, transient: null,
      heartbeat: 'connected', disk: null,
    };
    expect(deriveProblems(input).map((p) => p.kind + ':' + p.textDe)).toEqual([
      `bad:${P.leaderStalled}`,
      'warn:Kamera hängt.',
      `warn:${P.cameraSlow('scene', 10, 30)}`,
    ]);
    const old = { ...input, notice: { ...input.notice, at: NOW - WARN_NOTICE_MS - 1 } };
    expect(deriveProblems(old).map((p) => p.textDe)).not.toContain('Kamera hängt.');
  });

  it('an info note comes last; an expired transient is gone', () => {
    const info = { view: 'READY', transient: { kind: 'info', textDe: 'Episode 2 wird wiederholt.', until: NOW + 1 }, nowWallMs: NOW,
      verdicts: [v('camera', 'scene', 'slow', 10)], fps: 30 };
    expect(deriveProblems(info).map((p) => p.kind)).toEqual(['warn', 'info']);
    expect(deriveProblems({ ...info, nowWallMs: NOW + 1 }).map((p) => p.kind)).toEqual(['warn']);
  });

  it('link lost matters only while running', () => {
    expect(deriveProblems({ running: false, heartbeat: 'timeout', nowWallMs: NOW })).toEqual([]);
  });

  it('a notice the finish card already prints is skipped', () => {
    const session = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'upload_failed', message: 'Namespace.' } };
    expect(deriveProblems({ view: 'FINISHING', session, notice: { kind: 'warn', text: 'Namespace.', at: NOW }, nowWallMs: NOW }))
      .toEqual([]);
    const stopped = { ...EMPTY_RECORD_SESSION, errorText: 'Kameras weg.', finish: { ...EMPTY_FINISH, state: 'stopped_error' } };
    expect(deriveProblems({ view: 'FINISHING', session: stopped, notice: { kind: 'error', text: 'Kameras weg.', at: NOW }, nowWallMs: NOW }))
      .toEqual([]);
    const endNote = { ...EMPTY_RECORD_SESSION, finish: { ...EMPTY_FINISH, state: 'uploading', endNote: 'Speicher knapp.' } };
    expect(deriveProblems({ view: 'FINISHING', session: endNote, notice: { kind: 'warn', text: 'Speicher knapp.', at: NOW }, nowWallMs: NOW }))
      .toEqual([]);
  });

  it('a start block the other rules do not cover is shown too — the reason Start is off (V2-R2-2)', () => {
    const startBlock = { kind: 'uploading', problem: { kind: 'bad', textDe: P.startUploading } };
    expect(deriveProblems({ view: 'READY', startBlock, nowWallMs: NOW })).toEqual([
      { kind: 'bad', textDe: P.startUploading },
    ]);
    // only where Start is offered
    expect(deriveProblems({ view: 'RECORDING', running: true, startBlock, nowWallMs: NOW })).toEqual([]);
    // disk and source blocks already have their own rows — never twice
    const disk = { verdict: 'low', free: 2e9, startFloor: 3e9, criticalFloor: 1e9 };
    const diskBlock = { kind: 'disk', problem: diskProblem(disk) };
    expect(deriveProblems({ view: 'READY', startBlock: diskBlock, disk, nowWallMs: NOW })).toHaveLength(1);
  });

  it('without facts there is no banner', () => {
    expect(firstProblem({ view: 'READY', nowWallMs: NOW })).toBeNull();
    expect(firstProblem(everything).textDe).toBe('Harter Fehler.');
  });
});
