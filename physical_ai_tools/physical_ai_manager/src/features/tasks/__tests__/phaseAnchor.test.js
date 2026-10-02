// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The phase anchor turns the server's floored whole-second `proceed_time`
// into a smooth clock (spec §3.2): one anchor per (phase, episode), a new
// instance whenever the time runs backwards, the same reference while nothing
// changed.

import { STALE_HOLD_MS, elapsedAt, nextPhaseAnchor } from '../phaseAnchor';

const tick = (patch = {}) => ({
  phase: 3, currentEpisodeNumber: 0, proceedTime: 0, totalTime: 20, receivedAt: 1000, ...patch,
});

describe('nextPhaseAnchor', () => {
  it('starts an instance on the first status', () => {
    const a = nextPhaseAnchor(null, tick());
    expect(a).toEqual({
      key: '3:0', instance: 1, phase: 3, episode: 0, value: 0, total: 20, at: 1000,
    });
  });

  it('keeps the SAME reference while key, proceedTime and totalTime are unchanged', () => {
    const a = nextPhaseAnchor(null, tick());
    expect(nextPhaseAnchor(a, tick({ receivedAt: 1033 }))).toBe(a);
  });

  it('moves the value (and its arrival time) within the same instance', () => {
    const a = nextPhaseAnchor(null, tick());
    const b = nextPhaseAnchor(a, tick({ proceedTime: 1, receivedAt: 2010 }));
    expect(b).not.toBe(a);
    expect(b).toMatchObject({ instance: 1, value: 1, at: 2010 });
  });

  it('a new phase, a new episode or a backwards time is a new instance', () => {
    const a = nextPhaseAnchor(null, tick({ proceedTime: 4 }));
    expect(nextPhaseAnchor(a, tick({ phase: 4, proceedTime: 0 })).instance).toBe(2);
    expect(nextPhaseAnchor(a, tick({ currentEpisodeNumber: 1, proceedTime: 4 })).instance).toBe(2);
    expect(nextPhaseAnchor(a, tick({ proceedTime: 0 })).instance).toBe(2); // redo with reset 0
  });

  it('carries a null arrival time when the status has none', () => {
    expect(nextPhaseAnchor(null, tick({ receivedAt: undefined })).at).toBeNull();
  });
});

describe('elapsedAt', () => {
  const anchor = { key: '3:0', instance: 1, phase: 3, episode: 0, value: 4, total: 20, at: 10000 };

  it('is 0 without an anchor, and the value without an arrival time', () => {
    expect(elapsedAt(null, 5000, 5000)).toBe(0);
    expect(elapsedAt({ ...anchor, at: null }, 99999, 99999)).toBe(4);
  });

  it('runs on from the value between whole seconds', () => {
    expect(elapsedAt(anchor, 10500, 10490)).toBeCloseTo(4.5, 6);
  });

  it('never passes value + 0.999 (proceed_time is floored: true elapsed is in [k, k+1))', () => {
    expect(elapsedAt(anchor, 11500, 11490)).toBeCloseTo(4.999, 6);
  });

  it('never passes the phase total', () => {
    const late = { ...anchor, value: 19.5 };
    expect(elapsedAt(late, 11000, 11000)).toBe(20);
  });

  it('holds when the feed freezes: at most STALE_HOLD_MS past the last tick', () => {
    expect(STALE_HOLD_MS).toBe(1500);
    // last tick at 10000, now 20000: time stops at 10000 + 1500
    expect(elapsedAt({ ...anchor, value: 0 }, 20000, 10000)).toBeCloseTo(0.999, 6);
    expect(elapsedAt({ ...anchor, value: 0, at: 9000 }, 20000, 9500)).toBeCloseTo(0.999, 6);
    expect(elapsedAt({ ...anchor, value: 0, at: 10000 }, 20000, 10200)).toBeCloseTo(0.999, 6);
    expect(elapsedAt({ ...anchor, value: 0, at: 10000 }, 10300, 10000)).toBeCloseTo(0.3, 6);
  });

  it('a total of 0 means no cap from the total', () => {
    expect(elapsedAt({ ...anchor, total: 0 }, 10500, 10500)).toBeCloseTo(4.5, 6);
  });

  it('never runs backwards when the clock reads before the arrival', () => {
    expect(elapsedAt(anchor, 9000, 9000)).toBe(4);
  });
});
