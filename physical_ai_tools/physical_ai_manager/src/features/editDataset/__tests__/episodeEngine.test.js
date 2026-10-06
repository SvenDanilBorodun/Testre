// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The player's one clock (spec §F2) with fake videos: seeks aim at the middle
// of the frame and resolve when every camera reports it; steps go by index;
// the end pauses and seeks to L-1 itself; play from the end restarts at 0;
// the drift rule hard-resyncs a camera only after 3 consecutive pairs ≥ 2
// frames apart (never playbackRate); without rVFC a rAF loop reads currentTime.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DRIFT_PAIRS, SEEK_TIMEOUT_MS, createEpisodeEngine } from '../hooks/useEpisodePlayer';

function fakeVideo({ rvfc = true } = {}) {
  const listeners = {};
  const v = {
    currentTime: 0,
    playbackRate: 1,
    readyState: 4,
    ended: false,
    paused: true,
    cb: null,
    sets: [],
    play: vi.fn(() => { v.paused = false; return Promise.resolve(); }),
    pause: vi.fn(() => { v.paused = true; }),
    addEventListener: (k, fn) => { listeners[k] = fn; },
    removeEventListener: (k) => { delete listeners[k]; },
    fire: (k) => listeners[k] && listeners[k](),
  };
  Object.defineProperty(v, 'currentTime', {
    get() { return v._t || 0; },
    set(t) { v._t = t; v.sets.push(t); },
  });
  if (rvfc) {
    v.requestVideoFrameCallback = (cb) => { v.cb = cb; return 1; };
    v.cancelVideoFrameCallback = () => { v.cb = null; };
  }
  v.frame = (idx, fps, edt) => {
    const cb = v.cb;
    v.cb = null;
    if (cb) cb(0, { mediaTime: idx / fps, expectedDisplayTime: edt });
  };
  return v;
}

const FPS = 30;
let rafQueue;
const raf = (cb) => { rafQueue.push(cb); return rafQueue.length; };
const caf = () => {};
const runRaf = () => { const q = rafQueue; rafQueue = []; q.forEach((cb) => cb()); };

beforeEach(() => { rafQueue = []; vi.useFakeTimers(); });
afterEach(() => vi.useRealTimers());

function setup(n = 2, length = 60) {
  const engine = createEpisodeEngine({ raf, caf, now: () => 0 });
  const vids = Array.from({ length: n }, () => fakeVideo());
  vids.forEach((v, i) => engine.attach(i, v, { driver: i === 1 }));
  engine.setEpisode({ fps: FPS, length });
  return { engine, vids };
}

describe('seek, step, end', () => {
  it('a seek pauses and sets every video to (i + 0.5)/fps; it resolves when both report i', async () => {
    const { engine, vids } = setup();
    const seen = [];
    engine.subscribe((i) => seen.push(i));
    let done = false;
    engine.seekFrame(10).then(() => { done = true; });
    vids.forEach((v) => {
      expect(v.pause).toHaveBeenCalled();
      expect(v.sets[v.sets.length - 1]).toBeCloseTo(10.5 / FPS, 12);
    });
    expect(engine.getIdx()).toBe(10);
    vids[0].frame(10, FPS, 100);
    await Promise.resolve();
    expect(done).toBe(false);
    vids[1].frame(10, FPS, 100);
    await Promise.resolve();
    expect(done).toBe(true);
    expect(seen).toContain(10);
  });

  it('a seek that never reports still resolves after 1 s', async () => {
    const { engine } = setup();
    let done = false;
    engine.seekFrame(5).then(() => { done = true; });
    vi.advanceTimersByTime(SEEK_TIMEOUT_MS + 1);
    await Promise.resolve();
    expect(done).toBe(true);
  });

  it('steps go by index and clamp', () => {
    const { engine, vids } = setup();
    engine.seekFrame(59);
    engine.step(1);
    expect(engine.getIdx()).toBe(59);
    engine.step(-3);
    expect(engine.getIdx()).toBe(56);
    expect(vids[1].sets[vids[1].sets.length - 1]).toBeCloseTo(56.5 / FPS, 12);
  });

  it('reaching the last frame while playing pauses and seeks to L-1 itself', () => {
    const { engine, vids } = setup();
    engine.play();
    expect(engine.isPlaying()).toBe(true);
    vids.forEach((v) => expect(v.play).toHaveBeenCalled());
    vids[1].frame(59, FPS, 1);
    expect(engine.isPlaying()).toBe(false);
    expect(engine.getIdx()).toBe(59);
    expect(vids[0].sets[vids[0].sets.length - 1]).toBeCloseTo(59.5 / FPS, 12);
  });

  it('`ended` on the driver ends it the same way', () => {
    const { engine, vids } = setup();
    engine.play();
    vids[1].fire('ended');
    expect(engine.isPlaying()).toBe(false);
    expect(engine.getIdx()).toBe(59);
  });

  it('play from the last frame restarts at 0', async () => {
    const { engine, vids } = setup();
    engine.seekFrame(59);
    engine.play();
    expect(engine.getIdx()).toBe(0);
    vids.forEach((v) => v.frame(0, FPS, 5));
    await Promise.resolve();
    await Promise.resolve();
    expect(engine.isPlaying()).toBe(true);
  });

  it('the speed is playbackRate on every video', () => {
    const { engine, vids } = setup();
    engine.setRate(0.25);
    vids.forEach((v) => expect(v.playbackRate).toBe(0.25));
    engine.setRate(7);
    expect(engine.getRate()).toBe(1);
  });

  it('the driver\'s frames move the index; the other camera\'s do not', () => {
    const { engine, vids } = setup();
    engine.play();
    vids[1].frame(7, FPS, 10);
    expect(engine.getIdx()).toBe(7);
    vids[0].frame(30, FPS, 10);
    expect(engine.getIdx()).toBe(7);
  });

  it('a new episode starts at frame 0 and keeps playing once its media loaded', () => {
    const { engine, vids } = setup();
    engine.play();
    engine.setEpisode({ fps: FPS, length: 90, keepPlaying: true });
    expect(engine.getIdx()).toBe(0);
    expect(engine.isPlaying()).toBe(false);
    vids[1].fire('loadeddata');
    expect(engine.isPlaying()).toBe(true);
  });
});

describe('drift (P7): hard resync after 3 consecutive pairs ≥ 2 frames apart', () => {
  it('resyncs the lagging camera to the driver, never touching playbackRate', () => {
    const { engine, vids } = setup();
    engine.play();
    for (let k = 0; k < DRIFT_PAIRS; k += 1) {
      const edt = 100 + k * 16;
      vids[0].frame(10 + k, FPS, edt);
      vids[1].frame(13 + k, FPS, edt);
    }
    expect(engine.resyncCount).toBe(1);
    expect(vids[0].sets[vids[0].sets.length - 1]).toBeCloseTo((13 + DRIFT_PAIRS - 1 + 0.5) / FPS, 12);
    expect(vids[0].playbackRate).toBe(1);
  });

  it('pairs only callbacks of the same vsync (±1 ms); a Δ of 1 never counts; a good pair resets the count', () => {
    const { engine, vids } = setup();
    engine.play();
    // two bad pairs, a good one, two bad ones: never three in a row
    [[10, 13], [11, 14], [12, 12], [13, 16], [14, 17]].forEach(([a, b], k) => {
      const edt = 200 + k * 16;
      vids[0].frame(a, FPS, edt);
      vids[1].frame(b, FPS, edt);
    });
    expect(engine.resyncCount).toBe(0);
    // different vsyncs never pair
    vids[0].frame(0, FPS, 1000);
    vids[1].frame(9, FPS, 1020);
    expect(engine.resyncCount).toBe(0);
    // Δ = 1 in many pairs: no resync
    for (let k = 0; k < 6; k += 1) {
      vids[0].frame(20 + k, FPS, 2000 + k * 16);
      vids[1].frame(21 + k, FPS, 2000 + k * 16);
    }
    expect(engine.resyncCount).toBe(0);
  });

  it('the other order of callbacks in one vsync pairs too', () => {
    const { engine, vids } = setup();
    engine.play();
    for (let k = 0; k < DRIFT_PAIRS; k += 1) {
      const edt = 300 + k * 16;
      vids[1].frame(20 + k, FPS, edt);
      vids[0].frame(16 + k, FPS, edt);
    }
    expect(engine.resyncCount).toBe(1);
  });
});

describe('without requestVideoFrameCallback', () => {
  it('a rAF loop reads currentTime with floor(t·fps + 1e-3)', () => {
    const engine = createEpisodeEngine({ raf, caf, now: () => 0 });
    const v = fakeVideo({ rvfc: false });
    engine.attach(0, v, { driver: true });
    engine.setEpisode({ fps: FPS, length: 60 });
    engine.play();
    v._t = 12 / FPS;
    runRaf();
    expect(engine.getIdx()).toBe(12);
    v._t = 59.2 / FPS;
    runRaf();
    expect(engine.isPlaying()).toBe(false);
    expect(engine.getIdx()).toBe(59);
  });

  it('with no playable video a time-based clock keeps the charts going', () => {
    let t = 0;
    const engine = createEpisodeEngine({ raf, caf, now: () => t });
    const v = fakeVideo();
    engine.attach(0, v, { driver: true });
    engine.setFailed(0, true);
    engine.setEpisode({ fps: FPS, length: 60 });
    engine.play();
    t = 500;
    runRaf();
    expect(engine.getIdx()).toBe(15);
  });
});
