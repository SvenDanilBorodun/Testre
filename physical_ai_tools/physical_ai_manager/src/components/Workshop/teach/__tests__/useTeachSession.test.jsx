/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

// useTeachSession — the Vormachen state machine against the REAL keyboard path
// (document keydown in the capture phase) and deferred service mocks the tests
// resolve explicitly, so every ordering claim is observed, not assumed.

import { renderHook, act } from '@testing-library/react';
import useTeachSession from '../useTeachSession';
import { DE } from '../../blocks/messages_de';
import { replayDriveEstimateMs } from '../teachGates';
import { compactTrajectoryPoints } from '../../../../utils/trajectoryCompact';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeServices() {
  const calls = [];
  const mk = (name) => vi.fn((...args) => {
    const d = deferred();
    calls.push({ name, args, d, settled: false });
    return d.promise;
  });
  return {
    calls,
    services: {
      handGuide: mk('handGuide'), recordControl: mk('recordControl'),
      capturePose: mk('capturePose'), replayMotion: mk('replayMotion'),
    },
  };
}

const flush = async () => {
  for (let i = 0; i < 25; i += 1) await Promise.resolve(); // eslint-disable-line no-await-in-loop
};

const points = (n) => JSON.stringify({
  fps: 25, points: Array.from({ length: n }, (_, i) => [0.1 * i, 0, 0, 0, 0, 0.8, 0.04 * i]),
});

// 30 s at 25 fps, joint1 = 0.8·sin(2π·t) — replays LONGER than recorded.
const FAST_ROWS = Array.from({ length: 751 }, (_, i) => {
  const t = i / 25;
  return [0.8 * Math.sin(2 * Math.PI * t), 0, 0, 0, 0, 0, t];
});

function setup(overrides = {}) {
  const svc = makeServices();
  const feed = { cb: null, subs: 0, unsubs: 0 };
  const subscribeFollowerJoints = vi.fn((cb) => {
    feed.cb = cb;
    feed.subs += 1;
    return () => { feed.unsubs += 1; feed.cb = null; };
  });
  const sounds = { tick: vi.fn(), start: vi.fn(), stop: vi.fn(), capture: vi.fn(), dispose: vi.fn() };
  const cbs = {
    onCapture: vi.fn(), onTake: vi.fn(), onKeep: vi.fn(), onError: vi.fn(), onFinished: vi.fn(),
  };
  let counter = 0;
  const namer = vi.fn((kind) => { counter += 1; return `${kind}-${counter}`; });
  const initialProps = {
    enabled: true, mode: 'hand', heartbeatOk: true, collisionActive: false, leaderLive: false,
    roundItemCount: 0, services: svc.services, sounds, subscribeFollowerJoints, ...cbs, ...overrides,
  };
  const view = renderHook((props) => useTeachSession(props), { initialProps });
  const listener = (e) => view.result.current.onKeyDown(e);
  document.addEventListener('keydown', listener, true);
  view.result.current.setCaptureNamer(namer);
  const target = document.createElement('div');
  document.body.appendChild(target);
  let props = initialProps;

  const h = {
    ...svc, feed, sounds, cbs, namer, view, target, subscribeFollowerJoints,
    get cur() { return view.result.current; },
    get state() { return view.result.current.state; },
    rerender: (patch) => { props = { ...props, ...patch }; view.rerender(props); },
    unmount: () => {
      document.removeEventListener('keydown', listener, true);
      view.unmount();
      target.remove();
    },
    async press(key, { on = target, repeat = false } = {}) {
      const e = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, repeat });
      await act(async () => { on.dispatchEvent(e); await flush(); });
      return e;
    },
    async advance(ms) {
      await act(async () => { vi.advanceTimersByTime(ms); await flush(); });
    },
    last(name) {
      const list = svc.calls.filter((c) => c.name === name);
      return list[list.length - 1];
    },
    async resolve(call, value) {
      expect(call).toBeTruthy();
      call.settled = true;
      await act(async () => { call.d.resolve(value); await flush(); });
    },
    async reject(call, err = new Error('boom')) {
      call.settled = true;
      await act(async () => { call.d.reject(err); await flush(); });
    },
    count(name, ...args) {
      return svc.calls.filter((c) => c.name === name
        && args.every((a, i) => c.args[i] === a)).length;
    },
  };
  return h;
}

// ---- reaching each state ----------------------------------------------

async function toFrei(h) {
  await h.press('f');
  await h.advance(3000);
  await h.resolve(h.last('handGuide'), { success: true, message: 'Handbetrieb aktiv.' });
  expect(h.state).toBe('frei');
}

async function toAufnahme(h) {
  await h.press(' ');
  await h.advance(3000);
  await h.resolve(h.last('recordControl'), { success: true, message: 'Aufnahme läuft.' });
  expect(h.state).toBe('aufnahme');
}

async function toPruefen(h, { n = 3, stopRes = {}, close = { success: true } } = {}) {
  await toAufnahme(h);
  await h.advance(500);
  await h.press(' ');
  await h.resolve(h.last('recordControl'), {
    success: true, points_json: points(n), sample_count: n, duration_s: 0.04 * (n - 1), ...stopRes,
  });
  await h.resolve(h.last('handGuide'), close);
}

describe('useTeachSession', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  it('starts locked with nothing sent', () => {
    const h = setup();
    expect(h.state).toBe('fest');
    expect(h.cur.relock).toBe('none');
    expect(h.cur.busy).toBe(false);
    expect(h.calls).toHaveLength(0);
    h.unmount();
  });

  describe('key table — fest', () => {
    it('Space counts down (tick per second) and then starts a recording', async () => {
      const h = setup();
      const e = await h.press(' ');
      expect(e.defaultPrevented).toBe(true);
      expect(h.state).toBe('countdown');
      expect(h.cur.countdownLeft).toBe(3);
      expect(h.calls).toHaveLength(0);
      await h.advance(2000);
      expect(h.cur.countdownLeft).toBe(1);
      expect(h.sounds.tick).toHaveBeenCalledTimes(3);
      expect(h.calls).toHaveLength(0);
      await h.advance(1000);
      expect(h.count('recordControl', 'start')).toBe(1);
      await h.resolve(h.last('recordControl'), { success: true });
      expect(h.state).toBe('aufnahme');
      expect(h.cur.releasedOnce).toBe(true);
      expect(h.sounds.start).toHaveBeenCalledTimes(1);
      await h.advance(1000);
      expect(h.cur.elapsedS).toBeCloseTo(1, 1);
      h.unmount();
    });

    it('F counts down and then frees the arm', async () => {
      const h = setup();
      await h.press('f');
      expect(h.state).toBe('countdown');
      await h.advance(3000);
      expect(h.count('handGuide', true)).toBe(1);
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('frei');
      expect(h.cur.relock).toBe('none');
      h.unmount();
    });

    it('a failed start returns to the state before the countdown', async () => {
      const h = setup();
      await h.press(' ');
      await h.advance(3000);
      await h.resolve(h.last('recordControl'), { success: false, message: 'Nicht möglich.' });
      expect(h.state).toBe('fest');
      expect(h.cbs.onError).toHaveBeenCalledWith('Nicht möglich.');
      await h.advance(500);
      await h.press('f');
      await h.advance(3000);
      await h.resolve(h.last('handGuide'), { success: false, message: 'Leader aktiv.' });
      expect(h.state).toBe('fest');
      expect(h.cbs.onError).toHaveBeenLastCalledWith('Leader aktiv.');
      h.unmount();
    });

    it('P and Z capture with a name chosen on the key press', async () => {
      const h = setup();
      await h.press('p');
      expect(h.namer).toHaveBeenCalledWith('pose');
      expect(h.last('capturePose').args).toEqual(['pose-1']);
      await h.resolve(h.last('capturePose'), { success: true, world_x: 0.1 });
      expect(h.sounds.capture).toHaveBeenCalledTimes(1);
      expect(h.cbs.onCapture).toHaveBeenCalledWith({
        kind: 'pose', name: 'pose-1', response: { success: true, world_x: 0.1 },
      });
      await h.press('z');
      expect(h.last('capturePose').args).toEqual(['ziel-2']);
      await h.resolve(h.last('capturePose'), { success: true });
      expect(h.cbs.onCapture).toHaveBeenLastCalledWith(expect.objectContaining({ kind: 'ziel', name: 'ziel-2' }));
      expect(h.state).toBe('fest');
      h.unmount();
    });

    it('a refused capture is reported, never stored', async () => {
      const h = setup();
      await h.press('p');
      await h.resolve(h.last('capturePose'), { success: false, message: 'Armstellung unbekannt.' });
      expect(h.cbs.onCapture).not.toHaveBeenCalled();
      expect(h.cbs.onError).toHaveBeenCalledWith('Armstellung unbekannt.');
      h.unmount();
    });

    it('Esc finishes', async () => {
      const h = setup();
      await h.press('Escape');
      expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: false, relockOk: true, offline: false });
      expect(h.calls).toHaveLength(0);
      h.unmount();
    });
  });

  describe('key table — countdown', () => {
    it.each([[' '], ['f'], ['Escape']])('%j cancels without a call', async (key) => {
      const h = setup();
      await h.press('f');
      await h.advance(1000);
      await h.press(key);
      expect(h.state).toBe('fest');
      await h.advance(5000);
      expect(h.calls).toHaveLength(0);
      h.unmount();
    });
  });

  describe('key table — frei', () => {
    it('Space records at once, without a countdown', async () => {
      const h = setup();
      await toFrei(h);
      await h.press(' ');
      expect(h.count('recordControl', 'start')).toBe(1);
      await h.resolve(h.last('recordControl'), { success: true });
      expect(h.state).toBe('aufnahme');
      h.unmount();
    });

    it('F locks (confirmed hand_guide(false))', async () => {
      const h = setup();
      await toFrei(h);
      await h.press('f');
      expect(h.count('handGuide', false)).toBe(1);
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('fest');
      expect(h.cur.relock).toBe('ok');
      h.unmount();
    });

    it.each([['p', 'pose'], ['z', 'ziel']])('%s captures a %s', async (key, kind) => {
      const h = setup();
      await toFrei(h);
      await h.press(key);
      expect(h.last('capturePose').args).toEqual([`${kind}-1`]);
      await h.resolve(h.last('capturePose'), { success: true });
      expect(h.cbs.onCapture).toHaveBeenCalledWith(expect.objectContaining({ kind }));
      expect(h.state).toBe('frei');
      h.unmount();
    });

    it('Esc locks first, then finishes', async () => {
      const h = setup();
      await toFrei(h);
      await h.press('Escape');
      expect(h.cbs.onFinished).not.toHaveBeenCalled();
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: true, relockOk: true, offline: false });
      h.unmount();
    });
  });

  describe('key table — aufnahme', () => {
    it.each([[' '], ['f']])('%j stops, then closes the session (stop BEFORE close)', async (key) => {
      const h = setup();
      await toAufnahme(h);
      await h.advance(500);
      await h.press(key);
      expect(h.count('recordControl', 'stop')).toBe(1);
      expect(h.count('handGuide', false)).toBe(0);
      await h.resolve(h.last('recordControl'), { success: true, points_json: points(3), sample_count: 3 });
      expect(h.count('handGuide', false)).toBe(1);
      const stopCall = h.services.recordControl.mock.invocationCallOrder[1];
      const closeCall = h.services.handGuide.mock.invocationCallOrder[0];
      expect(stopCall).toBeLessThan(closeCall);
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('pruefen');
      expect(h.sounds.stop).toHaveBeenCalledTimes(1);
      h.unmount();
    });

    it('P captures a waypoint during the take', async () => {
      const h = setup();
      await toAufnahme(h);
      await h.press('p');
      expect(h.last('capturePose').args).toEqual(['pose-1']);
      await h.resolve(h.last('capturePose'), { success: true });
      expect(h.state).toBe('aufnahme');
      expect(h.cbs.onCapture).toHaveBeenCalledTimes(1);
      h.unmount();
    });

    it('Z is refused with a German hint and no call', async () => {
      const h = setup();
      await toAufnahme(h);
      const before = h.calls.length;
      const e = await h.press('z');
      expect(e.defaultPrevented).toBe(true);
      expect(h.calls).toHaveLength(before);
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_ZIEL_BLOCKED_REC);
      expect(h.state).toBe('aufnahme');
      h.unmount();
    });

    it('Esc stops and closes after the review is kept', async () => {
      const h = setup({ roundItemCount: 0 });
      await toAufnahme(h);
      await h.press('Escape');
      await h.resolve(h.last('recordControl'), { success: true, points_json: points(3) });
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('pruefen');
      expect(h.cbs.onFinished).not.toHaveBeenCalled();
      await h.press('Enter');
      expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
      // The kept take is one item the overlay's prop does not show yet.
      expect(h.state).toBe('abschluss');
      h.unmount();
    });
  });

  describe('key table — pruefen', () => {
    it('Enter keeps the take and returns to fest', async () => {
      const h = setup();
      await toPruefen(h);
      const take = h.cur.take;
      expect(take).toEqual(expect.objectContaining({ fps: 25, sampleCount: 3, relockOk: true }));
      expect(h.cbs.onTake).toHaveBeenCalledWith(take);
      await h.press('Enter');
      expect(h.cbs.onKeep).toHaveBeenCalledWith(take);
      expect(h.state).toBe('fest');
      expect(h.cur.take).toBeNull();
      expect(h.cbs.onFinished).not.toHaveBeenCalled();
      h.unmount();
    });

    it('Enter on a focused review-strip handle (role slider) still keeps', async () => {
      const h = setup();
      await toPruefen(h);
      const handle = document.createElement('div');
      handle.setAttribute('role', 'slider');
      handle.tabIndex = 0;
      h.target.appendChild(handle);
      await h.press('ArrowRight', { on: handle });
      expect(h.state).toBe('pruefen');
      await h.press('Enter', { on: handle });
      expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
      expect(h.state).toBe('fest');
      h.unmount();
    });

    it('R discards and counts down to a new take', async () => {
      const h = setup();
      await toPruefen(h);
      await h.press('r');
      expect(h.state).toBe('countdown');
      expect(h.cur.take).toBeNull();
      expect(h.cbs.onKeep).not.toHaveBeenCalled();
      await h.advance(3000);
      expect(h.count('recordControl', 'start')).toBe(2);
      h.unmount();
    });

    it('R then Esc during the countdown lands in fest with no take (never a take-less pruefen)', async () => {
      const h = setup();
      await toPruefen(h);
      await h.press('r');
      await h.press('Escape');
      expect(h.state).toBe('fest');
      expect(h.cur.take).toBeNull();
      await h.press('Enter');
      expect(h.cbs.onKeep).not.toHaveBeenCalled();
      h.unmount();
    });

    it('Entf discards', async () => {
      const h = setup();
      await toPruefen(h);
      await h.press('Delete');
      expect(h.state).toBe('fest');
      expect(h.cbs.onKeep).not.toHaveBeenCalled();
      h.unmount();
    });

    it('Esc keeps, then finishes (summary when the take is the only item)', async () => {
      const h = setup();
      await toPruefen(h);
      await h.press('Escape');
      expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
      expect(h.state).toBe('abschluss');
      await h.press('Escape');
      expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: true, relockOk: true, offline: false });
      h.unmount();
    });

    it('F locks only while the re-lock failed', async () => {
      const h = setup();
      await toPruefen(h, { close: { success: false, message: 'Arm nicht fest.' } });
      expect(h.cur.relock).toBe('failed');
      expect(h.cur.take.relockOk).toBe(false);
      await h.press('f');
      expect(h.count('handGuide', false)).toBe(2);
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.cur.relock).toBe('ok');
      expect(h.state).toBe('pruefen');
      await h.press('f');
      expect(h.count('handGuide', false)).toBe(2);
      h.unmount();
    });
  });

  describe('vorschau — the real-arm preview ends on observed stillness', () => {
    const ESTIMATE = replayDriveEstimateMs(compactTrajectoryPoints(FAST_ROWS), 1.0);
    const MOVING = (t) => [t * 0.001, 0, 0, 0, 0, 0.8];
    const STILL = () => [0.5, 0, 0, 0, 0, 0.8];

    // Emits a pose every `step` ms for `ms` ms (the rosbridge throttle is 30 ms).
    function feedFor(h, ms, poseAt, step = 30) {
      act(() => {
        let t = 0;
        while (t + step <= ms) {
          vi.advanceTimersByTime(step);
          t += step;
          if (h.feed.cb) h.feed.cb(poseAt(t));
        }
        if (ms > t) vi.advanceTimersByTime(ms - t);
      });
    }

    async function toVorschau(h) {
      await toPruefen(h, { stopRes: { points_json: JSON.stringify({ fps: 25, points: FAST_ROWS }) } });
      await act(async () => { h.cur.actions.previewOnRobot(); await flush(); });
      const replay = h.last('replayMotion');
      expect(replay.args[0].speed).toBe(1.0);
      expect(JSON.parse(replay.args[0].points_json)).toEqual({
        fps: 25, points: compactTrajectoryPoints(FAST_ROWS),
      });
      await h.resolve(replay, { success: true, message: 'Wiedergabe gestartet.' });
      expect(h.state).toBe('vorschau');
    }

    it('the estimate is the stretched drive, not the recorded duration', () => {
      expect(ESTIMATE).toBeGreaterThan(4500 + 30000 + 1000);
    });

    it('opens exactly one subscription; teaching keys send nothing', async () => {
      const h = setup();
      await toVorschau(h);
      expect(h.feed.subs).toBe(1);
      const before = h.calls.length;
      for (const key of [' ', 'f', 'r', 'Enter', 'p', 'z']) {
        await h.press(key); // eslint-disable-line no-await-in-loop
        await h.advance(500); // eslint-disable-line no-await-in-loop
      }
      expect(h.calls).toHaveLength(before);
      expect(h.state).toBe('vorschau');
      h.unmount();
    });

    it('never exits on time alone while the arm keeps moving', async () => {
      const h = setup();
      await toVorschau(h);
      feedFor(h, 4500 + 30000 + 1000, MOVING);
      expect(h.state).toBe('vorschau');
      h.unmount();
    });

    it('a still pause inside the take (before the estimate) does not end it', async () => {
      const h = setup();
      await toVorschau(h);
      feedFor(h, 1000, MOVING);
      feedFor(h, 3000, STILL);
      expect(3000).toBeLessThan(ESTIMATE);
      expect(h.state).toBe('vorschau');
      h.unmount();
    });

    it('exits once the estimate passed since the first motion AND the arm is still', async () => {
      const h = setup();
      await toVorschau(h);
      // The first judged sample lands on tick 1 (150 ms); motion is seen on tick 2.
      feedFor(h, ESTIMATE - 1000, MOVING);
      feedFor(h, 1000 + 299 - 30, STILL); // up to 300 + ESTIMATE - 1 since the answer
      expect(h.state).toBe('vorschau');
      expect(h.feed.unsubs).toBe(0);
      feedFor(h, 30 + 150, STILL);
      expect(h.state).toBe('pruefen');
      expect(h.feed.unsubs).toBe(1);
      expect(h.cur.take).not.toBeNull();
      h.unmount();
    });

    it('cleaned rows from the overlay are what it sends AND what the exit estimate is computed from', async () => {
      const cleaned = FAST_ROWS.slice(0, 26); // one second of the take
      const short = replayDriveEstimateMs(compactTrajectoryPoints(cleaned), 1.0);
      expect(short).toBeLessThan(ESTIMATE - 5000);
      const h = setup();
      await toPruefen(h, { stopRes: { points_json: JSON.stringify({ fps: 25, points: FAST_ROWS }) } });
      await act(async () => { h.cur.actions.previewOnRobot(cleaned); await flush(); });
      const replay = h.last('replayMotion');
      expect(JSON.parse(replay.args[0].points_json)).toEqual({ fps: 25, points: compactTrajectoryPoints(cleaned) });
      await h.resolve(replay, { success: true });
      feedFor(h, short - 1000, MOVING);
      feedFor(h, 1000 + 299 - 30, STILL);
      expect(h.state).toBe('vorschau');
      feedFor(h, 30 + 150, STILL);
      expect(h.state).toBe('pruefen');
      h.unmount();
    });

    it('fewer than 2 cleaned rows fall back to the take as recorded', async () => {
      const h = setup();
      await toPruefen(h, { stopRes: { points_json: JSON.stringify({ fps: 25, points: FAST_ROWS }) } });
      await act(async () => { h.cur.actions.previewOnRobot([FAST_ROWS[0]]); await flush(); });
      expect(JSON.parse(h.last('replayMotion').args[0].points_json).points).toEqual(compactTrajectoryPoints(FAST_ROWS));
      h.unmount();
    });

    it('a dead feed never reads as still', async () => {
      const h = setup();
      await toVorschau(h);
      feedFor(h, 2000, MOVING);
      await h.advance(ESTIMATE + 600000);
      expect(h.state).toBe('vorschau');
      h.unmount();
    });

    it('a feed that stalls mid-settle restarts the still count', async () => {
      const h = setup();
      await toVorschau(h);
      // One emit right after each 150 ms tick, so the next tick judges it.
      const tickThenEmit = (n, poseAt) => act(() => {
        for (let i = 0; i < n; i += 1) {
          vi.advanceTimersByTime(150);
          h.feed.cb(poseAt(i));
        }
      });
      tickThenEmit(Math.ceil((ESTIMATE + 1000) / 150), (i) => MOVING(i * 150));
      tickThenEmit(4, STILL); // judged: motion, 1, 2 …
      act(() => { vi.advanceTimersByTime(150); }); // … 3 still ticks
      expect(h.state).toBe('vorschau');
      act(() => { vi.advanceTimersByTime(1200); }); // no sample for > 1 s
      tickThenEmit(1, STILL);
      act(() => { vi.advanceTimersByTime(150); });
      expect(h.state).toBe('vorschau'); // 1 fresh still tick, not 4
      tickThenEmit(3, STILL);
      act(() => { vi.advanceTimersByTime(150); });
      expect(h.state).toBe('pruefen');
      h.unmount();
    });

    it('never moved: no timed exit, only the no-motion hint at 52 s', async () => {
      const h = setup();
      await toVorschau(h);
      feedFor(h, 51990, STILL);
      await h.advance(9);
      expect(h.cur.previewNoMotionHint).toBe(false);
      await h.advance(1);
      expect(h.cur.previewNoMotionHint).toBe(true);
      feedFor(h, 600000 - 52000, STILL);
      expect(h.state).toBe('vorschau');
      feedFor(h, 1000, MOVING);
      expect(h.cur.previewNoMotionHint).toBe(false);
      feedFor(h, ESTIMATE + 1000, STILL);
      expect(h.state).toBe('pruefen');
      h.unmount();
    });

    it('Stopp: one hand_guide(false); confirmed waits for stillness', async () => {
      const h = setup();
      await toVorschau(h);
      feedFor(h, 1000, MOVING);
      await h.press('Escape');
      expect(h.count('handGuide', false)).toBe(2);
      await h.resolve(h.last('handGuide'), { success: true });
      feedFor(h, 1500, MOVING);
      expect(h.state).toBe('vorschau');
      feedFor(h, 5 * 150, STILL);
      expect(h.state).toBe('pruefen');
      expect(h.count('handGuide', false)).toBe(2);
      h.unmount();
    });

    it('Stopp confirmed with a dead feed exits at the 2 s tail cap', async () => {
      const h = setup();
      await toVorschau(h);
      await h.press('Escape');
      await h.resolve(h.last('handGuide'), { success: true });
      await h.advance(1950);
      expect(h.state).toBe('vorschau');
      await h.advance(200);
      expect(h.state).toBe('pruefen');
      h.unmount();
    });

    it('Stopp refused: German reason, still vorschau, Stopp can be pressed again', async () => {
      const h = setup();
      await toVorschau(h);
      await h.press('Escape');
      await h.resolve(h.last('handGuide'), { success: false, message: 'Der Arm ist noch in Bewegung.' });
      expect(h.cbs.onError).toHaveBeenCalledWith('Der Arm ist noch in Bewegung.');
      expect(h.state).toBe('vorschau');
      await h.press('Escape');
      expect(h.count('handGuide', false)).toBe(3);
      h.unmount();
    });

    it('is not offered while the re-lock failed, while busy, or offline', async () => {
      const failed = setup();
      await toPruefen(failed, { close: { success: false } });
      await act(async () => { failed.cur.actions.previewOnRobot(); await flush(); });
      expect(failed.count('replayMotion')).toBe(0);
      failed.unmount();

      const busy = setup();
      await toPruefen(busy);
      // The first request is queued/in flight, so the second finds the queue busy.
      await act(async () => {
        busy.cur.actions.previewOnRobot();
        busy.cur.actions.previewOnRobot();
        await flush();
      });
      expect(busy.count('replayMotion')).toBe(1);
      busy.unmount();

      const offline = setup();
      await toPruefen(offline);
      offline.rerender({ heartbeatOk: false });
      await act(async () => { offline.cur.actions.previewOnRobot(); await flush(); });
      expect(offline.count('replayMotion')).toBe(0);
      offline.unmount();
    });

    it('a replay call that throws enters vorschau (the drive may have started) and reports offline', async () => {
      const h = setup();
      await toPruefen(h);
      await act(async () => { h.cur.actions.previewOnRobot(); await flush(); });
      await h.reject(h.last('replayMotion'));
      expect(h.state).toBe('vorschau');
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_OFFLINE);
      h.unmount();
    });

    it('a free refusal with no message names the arm, not the connection', async () => {
      const h = setup();
      await h.press('f');
      await h.advance(3000);
      await h.resolve(h.last('handGuide'), { success: false, message: '' });
      expect(h.state).toBe('fest');
      expect(h.cbs.onError).toHaveBeenLastCalledWith(DE.TEACH_FREE_FAILED);
      h.unmount();
    });

    it('a refused replay stays in pruefen with the reason', async () => {
      const h = setup();
      await toPruefen(h);
      await act(async () => { h.cur.actions.previewOnRobot(); await flush(); });
      await h.resolve(h.last('replayMotion'), { success: false, message: 'Die Aufnahme enthält keine Bewegung.' });
      expect(h.state).toBe('pruefen');
      expect(h.cbs.onError).toHaveBeenCalledWith('Die Aufnahme enthält keine Bewegung.');
      h.unmount();
    });
  });

  describe('key table — abschluss', () => {
    it('Esc closes; continueTeaching returns to fest', async () => {
      const h = setup({ roundItemCount: 2 });
      await h.press('Escape');
      expect(h.state).toBe('abschluss');
      expect(h.cbs.onFinished).not.toHaveBeenCalled();
      act(() => { h.cur.actions.continueTeaching(); });
      expect(h.state).toBe('fest');
      await h.press('Escape');
      await h.press('Escape');
      expect(h.cbs.onFinished).toHaveBeenCalledTimes(1);
      expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: false, relockOk: true, offline: false });
      expect(h.calls).toHaveLength(0);
      h.unmount();
    });
  });

  describe('key table — every „—" cell', () => {
    const toVorschauSimple = async (h) => {
      await toPruefen(h);
      await act(async () => { h.cur.actions.previewOnRobot(); await flush(); });
      await h.resolve(h.last('replayMotion'), { success: true });
    };
    const REACH = {
      fest: async () => {},
      countdown: async (h) => { await h.press(' '); },
      frei: toFrei,
      aufnahme: toAufnahme,
      pruefen: (h) => toPruefen(h),
      vorschau: toVorschauSimple,
      abschluss: async (h) => { h.rerender({ roundItemCount: 1 }); await h.press('Escape'); },
    };
    const DASH = [
      ['fest', ['Enter', 'r', 'Delete']],
      ['countdown', ['p', 'z', 'Enter', 'r', 'Delete']],
      ['frei', ['Enter', 'r', 'Delete']],
      ['aufnahme', ['Enter', 'r', 'Delete']],
      ['pruefen', [' ', 'p', 'z']],
      ['vorschau', [' ', 'f', 'p', 'z', 'Enter', 'r', 'Delete']],
      ['abschluss', [' ', 'f', 'p', 'z', 'Enter', 'r', 'Delete']],
    ].flatMap(([state, keys]) => keys.map((key) => [state, key]));

    it.each(DASH)('%s + %j: prevented, no call, state unchanged', async (state, key) => {
      const h = setup();
      await REACH[state](h);
      expect(h.state).toBe(state);
      await h.advance(450); // clear the Space debounce of the reaching press
      if (state === 'countdown') {
        act(() => { h.cur.actions.continueTeaching(); }); // no-op outside abschluss
      }
      const stateNow = h.state;
      const before = h.calls.length;
      const e = await h.press(key);
      expect(e.defaultPrevented).toBe(true);
      expect(h.calls).toHaveLength(before);
      expect(h.state).toBe(stateNow);
      expect(h.cbs.onKeep).not.toHaveBeenCalled();
      h.unmount();
    });
  });

  describe('the service queue', () => {
    it('a lock waits for the in-flight keepalive, then sends exactly one hand_guide(false)', async () => {
      const h = setup();
      await toFrei(h);
      await h.advance(15000);
      expect(h.count('handGuide', true)).toBe(2);
      await h.press('f');
      expect(h.count('handGuide', false)).toBe(0);
      expect(h.cur.busy).toBe(true);
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.count('handGuide', false)).toBe(1);
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('fest');
      expect(h.cur.busy).toBe(false);
      h.unmount();
    });

    it('a record start waits for the pending capture', async () => {
      const h = setup();
      await toFrei(h);
      await h.press('p');
      await h.advance(450);
      await h.press(' ');
      expect(h.count('recordControl', 'start')).toBe(0);
      await h.resolve(h.last('capturePose'), { success: true });
      expect(h.count('recordControl', 'start')).toBe(1);
      h.unmount();
    });

    it('a queued stop whose state changed meanwhile sends nothing', async () => {
      const h = setup();
      await toAufnahme(h);
      await h.advance(500);
      await h.press('p'); // capture in flight
      await h.press('f'); // stop queued behind it
      await h.advance(450);
      await h.press(' '); // second stop queued
      expect(h.count('recordControl', 'stop')).toBe(0);
      await h.resolve(h.last('capturePose'), { success: true });
      expect(h.count('recordControl', 'stop')).toBe(1);
      await h.resolve(h.last('recordControl'), { success: true, points_json: points(3) });
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('pruefen');
      expect(h.count('recordControl', 'stop')).toBe(1);
      expect(h.count('handGuide', false)).toBe(1);
      h.unmount();
    });
  });

  describe('keepalive', () => {
    it('runs only in frei, every 15 s', async () => {
      const h = setup();
      await h.advance(60000);
      expect(h.count('handGuide', true)).toBe(0);
      await toFrei(h);
      await h.advance(14999);
      expect(h.count('handGuide', true)).toBe(1);
      await h.advance(1);
      expect(h.count('handGuide', true)).toBe(2);
      await h.resolve(h.last('handGuide'), { success: true });
      await h.press(' ');
      await h.resolve(h.last('recordControl'), { success: true });
      expect(h.state).toBe('aufnahme');
      await h.advance(60000);
      expect(h.count('handGuide', true)).toBe(2);
      h.unmount();
    });

    it('is queued behind a capture spanning the tick, never skipped', async () => {
      const h = setup();
      await toFrei(h);
      await h.advance(14000);
      await h.press('p');
      await h.advance(1500); // tick at 15 s while the capture is in flight
      expect(h.count('handGuide', true)).toBe(1);
      await h.resolve(h.last('capturePose'), { success: true }); // answer at 15.5 s
      expect(h.count('handGuide', true)).toBe(2);
      h.unmount();
    });

    it('never queues two keepalives', async () => {
      const h = setup();
      await toFrei(h);
      await h.press('p');
      await h.advance(45000); // three ticks while the capture hangs
      await h.resolve(h.last('capturePose'), { success: true });
      expect(h.count('handGuide', true)).toBe(2);
      h.unmount();
    });

    it('a refused keepalive reports, re-locks once, and stops the keepalive', async () => {
      const h = setup();
      await toFrei(h);
      await h.advance(15000);
      const msg = 'Handbetrieb nicht möglich, solange der Leader-Arm aktiv ist.';
      await h.resolve(h.last('handGuide'), { success: false, message: msg });
      expect(h.cbs.onError).toHaveBeenCalledWith(msg);
      expect(h.count('handGuide', false)).toBe(1);
      await h.resolve(h.last('handGuide'), { success: false, message: 'Arm nicht fest.' });
      expect(h.state).toBe('frei');
      await h.advance(30000);
      expect(h.count('handGuide', true)).toBe(2);
      h.unmount();
    });
  });

  describe('stop', () => {
    it('≥ 2 points + confirmed close → pruefen, relockOk true', async () => {
      const h = setup();
      await toPruefen(h, { n: 2 });
      expect(h.state).toBe('pruefen');
      expect(h.cur.relock).toBe('ok');
      expect(h.cur.take).toEqual(expect.objectContaining({ relockOk: true, sampleCount: 2 }));
      h.unmount();
    });

    it.each([[1], [0]])('%i point(s) → „no motion", no take, fest', async (n) => {
      const h = setup();
      await toPruefen(h, { n });
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_NO_MOTION);
      expect(h.cbs.onTake).not.toHaveBeenCalled();
      expect(h.state).toBe('fest');
      expect(h.cur.take).toBeNull();
      h.unmount();
    });

    it('record stop success:false WITH 3 points + confirmed close → pruefen, relock ok', async () => {
      const h = setup();
      await toPruefen(h, { stopRes: { success: false, message: 'Arm konnte nicht wieder verriegelt werden.' } });
      expect(h.state).toBe('pruefen');
      expect(h.cur.relock).toBe('ok');
      expect(h.cbs.onError).not.toHaveBeenCalled();
      h.unmount();
    });

    it('1 point + record stop success:false + confirmed close → „no motion", not the stale re-lock text', async () => {
      const h = setup();
      await toPruefen(h, {
        n: 1,
        stopRes: { success: false, message: 'Arm konnte nicht wieder verriegelt werden — der Greifer ist noch frei beweglich.' },
      });
      expect(h.cbs.onError).toHaveBeenCalledTimes(1);
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_NO_MOTION);
      expect(h.state).toBe('fest');
      expect(h.cur.relock).toBe('ok');
      h.unmount();
    });

    it('a stop that did not execute (no points_json) shows the server reason', async () => {
      const h = setup();
      await toPruefen(h, { stopRes: { success: false, points_json: '', message: 'Aufnahme ist beschäftigt.' } });
      expect(h.cbs.onError).toHaveBeenCalledWith('Aufnahme ist beschäftigt.');
      expect(h.state).toBe('fest');
      h.unmount();
    });

    it('success + REFUSED close → pruefen with relock failed; F then re-locks', async () => {
      const h = setup();
      await toPruefen(h, { close: { success: false, message: 'Arm nicht fest.' } });
      expect(h.state).toBe('pruefen');
      expect(h.cur.relock).toBe('failed');
      await h.press('f');
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.cur.relock).toBe('ok');
      h.unmount();
    });

    it('a thrown record stop keeps recording (the server records until its cap)', async () => {
      const h = setup();
      await toAufnahme(h);
      await h.advance(500);
      await h.press(' ');
      await h.reject(h.last('recordControl'));
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_OFFLINE);
      expect(h.state).toBe('aufnahme');
      expect(h.count('handGuide', false)).toBe(0);
      h.unmount();
    });

    it('an Esc-stop with no take clears close-after-review: a later keep does not finish', async () => {
      const h = setup();
      await toAufnahme(h);
      await h.press('Escape');
      await h.resolve(h.last('recordControl'), { success: true, points_json: points(1) });
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('fest');
      await toPruefen(h);
      await h.press('Enter');
      expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
      expect(h.state).toBe('fest');
      expect(h.cbs.onFinished).not.toHaveBeenCalled();
      h.unmount();
    });

    it('stops once at the 120 s cap', async () => {
      const h = setup();
      await toAufnahme(h);
      await h.advance(120000);
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_CAP_REACHED);
      expect(h.count('recordControl', 'stop')).toBe(1);
      await h.advance(5000);
      expect(h.count('recordControl', 'stop')).toBe(1);
      expect(h.cbs.onError.mock.calls.filter(([m]) => m === DE.TEACH_CAP_REACHED)).toHaveLength(1);
      h.unmount();
    });
  });

  describe('relock recovery and the lock precondition', () => {
    it('frei + F refused → relock failed, still frei, no keepalive; F again confirmed → fest', async () => {
      const h = setup();
      await toFrei(h);
      await h.press('f');
      await h.resolve(h.last('handGuide'), { success: false, message: 'Arm nicht fest.' });
      expect(h.cur.relock).toBe('failed');
      expect(h.cbs.onError).toHaveBeenCalledWith('Arm nicht fest.');
      expect(h.state).toBe('frei');
      await h.advance(30000);
      expect(h.count('handGuide', true)).toBe(1);
      await h.press('f');
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('fest');
      h.unmount();
    });

    it('a refused lock without a reason uses the German default', async () => {
      const h = setup();
      await toFrei(h);
      await h.press('f');
      await h.resolve(h.last('handGuide'), { success: false });
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_RELOCK_FAILED);
      h.unmount();
    });

    it('pruefen (failed) + R records at once and resets relock; lock is then a no-op in aufnahme', async () => {
      const h = setup();
      await toPruefen(h, { close: { success: false } });
      await h.press('r');
      expect(h.count('recordControl', 'start')).toBe(2);
      await h.resolve(h.last('recordControl'), { success: true });
      expect(h.state).toBe('aufnahme');
      expect(h.cur.relock).toBe('none');
      const before = h.count('handGuide', false);
      await act(async () => { h.cur.actions.lock(); await flush(); });
      expect(h.count('handGuide', false)).toBe(before);
      expect(h.state).toBe('aufnahme');
      h.unmount();
    });

    it('actions.lock sends nothing in fest, countdown and vorschau', async () => {
      const h = setup();
      await act(async () => { h.cur.actions.lock(); await flush(); });
      await h.press(' ');
      await act(async () => { h.cur.actions.lock(); await flush(); });
      expect(h.calls).toHaveLength(0);
      await h.press('Escape');
      expect(h.state).toBe('fest');
      await h.advance(450); // the Space debounce
      await toPruefen(h);
      await act(async () => { h.cur.actions.previewOnRobot(); await flush(); });
      await h.resolve(h.last('replayMotion'), { success: true });
      expect(h.state).toBe('vorschau');
      const before = h.calls.length;
      await act(async () => { h.cur.actions.lock(); await flush(); });
      expect(h.calls).toHaveLength(before);
      h.unmount();
    });
  });

  describe('finish and the summary', () => {
    it('fest + Esc: at once with 0 items, abschluss with 2', async () => {
      const zero = setup({ roundItemCount: 0 });
      await zero.press('Escape');
      expect(zero.cbs.onFinished).toHaveBeenCalledTimes(1);
      zero.unmount();

      const two = setup({ roundItemCount: 2 });
      await two.press('Escape');
      expect(two.state).toBe('abschluss');
      await two.press('Escape');
      expect(two.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: false, relockOk: true, offline: false });
      two.unmount();
    });

    it('finish from frei: lock confirmed → abschluss', async () => {
      const h = setup({ roundItemCount: 1 });
      await toFrei(h);
      await act(async () => { h.cur.actions.finish(); await flush(); });
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('abschluss');
      act(() => { h.cur.actions.finish(); });
      expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: true, relockOk: true, offline: false });
      h.unmount();
    });

    it('finish from frei: lock refused → stays frei, never finished', async () => {
      const h = setup({ roundItemCount: 1 });
      await toFrei(h);
      await h.press('Escape');
      await h.resolve(h.last('handGuide'), { success: false, message: 'Arm nicht fest.' });
      expect(h.state).toBe('frei');
      expect(h.cur.relock).toBe('failed');
      expect(h.cbs.onFinished).not.toHaveBeenCalled();
      h.unmount();
    });

    it('„Fertig" does what Esc does: countdown cancels, aufnahme stops, vorschau stops the preview', async () => {
      const h = setup();
      await h.press(' ');
      act(() => { h.cur.actions.finish(); });
      expect(h.state).toBe('fest');
      await h.advance(450);
      await toAufnahme(h);
      await act(async () => { h.cur.actions.finish(); await flush(); });
      expect(h.count('recordControl', 'stop')).toBe(1);
      await h.resolve(h.last('recordControl'), { success: true, points_json: points(3) });
      await h.resolve(h.last('handGuide'), { success: true });
      expect(h.state).toBe('pruefen');
      await act(async () => { h.cur.actions.previewOnRobot(); await flush(); });
      await h.resolve(h.last('replayMotion'), { success: true });
      await act(async () => { h.cur.actions.finish(); await flush(); });
      expect(h.count('handGuide', false)).toBe(2);
      expect(h.state).toBe('vorschau');
      h.unmount();
    });
  });

  describe('offline', () => {
    it('ignores teaching keys (still prevented)', async () => {
      const h = setup({ heartbeatOk: false });
      const e = await h.press(' ');
      expect(e.defaultPrevented).toBe(true);
      await h.advance(5000);
      expect(h.calls).toHaveLength(0);
      expect(h.state).toBe('fest');
      h.unmount();
    });

    it('Esc in frei closes at once: hand_guide(false) fired, offline onFinished', async () => {
      const h = setup();
      await toFrei(h);
      h.rerender({ heartbeatOk: false });
      await h.press('Escape');
      expect(h.count('handGuide', false)).toBe(1);
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_CLOSE_OFFLINE);
      expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: true, relockOk: false, offline: true });
      h.unmount();
      expect(h.count('handGuide', false)).toBe(1); // unmount does not tear down twice
    });

    it('Esc offline with a keepalive in flight closes only after the keepalive answered', async () => {
      const h = setup();
      await toFrei(h);
      await h.advance(15000);
      expect(h.count('handGuide', true)).toBe(2);
      const keepalive = h.last('handGuide');
      h.rerender({ heartbeatOk: false });
      await h.press('Escape');
      expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: true, relockOk: false, offline: true });
      expect(h.count('handGuide', false)).toBe(0);
      await h.resolve(keepalive, { success: true });
      expect(h.count('handGuide', false)).toBe(1);
      h.unmount();
      expect(h.count('handGuide', false)).toBe(1);
    });

    it('a thrown hand_guide(false) during finish closes offline', async () => {
      const h = setup();
      await toFrei(h);
      await h.press('Escape');
      await h.reject(h.last('handGuide'));
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_CLOSE_OFFLINE);
      expect(h.cbs.onFinished).toHaveBeenCalledWith(expect.objectContaining({ offline: true, relockOk: false }));
      h.unmount();
    });
  });

  describe('leader turned on while teaching by hand', () => {
    it('frei: reports, locks, then only Esc works', async () => {
      const h = setup();
      await toFrei(h);
      h.rerender({ leaderLive: true });
      await act(async () => { await flush(); });
      expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_LEADER_TURNED_ON);
      expect(h.count('handGuide', false)).toBe(1);
      await h.resolve(h.last('handGuide'), { success: true });
      const before = h.calls.length;
      await h.press(' ');
      await h.press('f');
      await h.press('p');
      await h.advance(5000);
      expect(h.calls).toHaveLength(before);
      await h.press('Escape');
      expect(h.cbs.onFinished).toHaveBeenCalledTimes(1);
      h.unmount();
    });

    it('countdown is cancelled and aufnahme is stopped', async () => {
      const c = setup();
      await c.press('f');
      c.rerender({ leaderLive: true });
      expect(c.state).toBe('fest');
      await c.advance(5000);
      expect(c.calls).toHaveLength(0);
      c.unmount();

      const a = setup();
      await toAufnahme(a);
      a.rerender({ leaderLive: true });
      await act(async () => { await flush(); });
      expect(a.count('recordControl', 'stop')).toBe(1);
      a.unmount();
    });
  });

  describe('keyboard rules', () => {
    it('collisionActive: nothing handled and nothing prevented', async () => {
      const h = setup({ collisionActive: true });
      const e = await h.press(' ');
      expect(e.defaultPrevented).toBe(false);
      await h.advance(5000);
      expect(h.calls).toHaveLength(0);
      expect(h.state).toBe('fest');
      h.unmount();
    });

    it('target rule: interactive targets keep their native keys, Esc on a button still finishes', async () => {
      const h = setup();
      const button = document.createElement('button');
      const input = document.createElement('input');
      document.body.append(button, input);
      const space = await h.press(' ', { on: button });
      expect(space.defaultPrevented).toBe(false);
      expect(h.state).toBe('fest');
      const escInput = await h.press('Escape', { on: input });
      expect(escInput.defaultPrevented).toBe(false);
      expect(h.cbs.onFinished).not.toHaveBeenCalled();
      const escButton = await h.press('Escape', { on: button });
      expect(escButton.defaultPrevented).toBe(true);
      expect(h.cbs.onFinished).toHaveBeenCalledTimes(1);
      h.unmount();
    });

    it('Space on a plain div in pruefen is prevented and sends nothing', async () => {
      const h = setup();
      await toPruefen(h);
      await h.advance(450);
      const before = h.calls.length;
      const e = await h.press(' ');
      expect(e.defaultPrevented).toBe(true);
      expect(h.calls).toHaveLength(before);
      h.unmount();
    });

    it('a repeated key is ignored (prevented) and Space is debounced', async () => {
      const h = setup();
      const rep = await h.press(' ', { repeat: true });
      expect(rep.defaultPrevented).toBe(true);
      expect(h.state).toBe('fest');
      await h.press(' ');
      expect(h.state).toBe('countdown');
      await h.advance(399);
      const bounced = await h.press(' ');
      expect(bounced.defaultPrevented).toBe(true);
      expect(h.state).toBe('countdown');
      await h.advance(1);
      await h.press(' ');
      expect(h.state).toBe('fest');
      h.unmount();
    });

    it('a capture name is chosen before the service call', async () => {
      const h = setup();
      await h.press('p');
      expect(h.namer.mock.invocationCallOrder[0])
        .toBeLessThan(h.services.capturePose.mock.invocationCallOrder[0]);
      h.unmount();
    });
  });

  describe('teardown', () => {
    it('unmount during a pending record start cancels once, after it answered', async () => {
      const h = setup();
      await h.press(' ');
      await h.advance(3000);
      const start = h.last('recordControl');
      h.unmount();
      expect(h.count('recordControl', 'cancel')).toBe(0);
      await h.resolve(start, { success: true });
      expect(h.count('recordControl', 'cancel')).toBe(1);
      expect(h.count('handGuide', false)).toBe(0);
    });

    it('unmount in frei closes the session once; in fest sends nothing', async () => {
      const f = setup();
      await toFrei(f);
      f.unmount();
      expect(f.count('handGuide', false)).toBe(1);

      const g = setup();
      g.unmount();
      expect(g.calls).toHaveLength(0);
    });

    it('pagehide behaves like unmount', async () => {
      const h = setup();
      await toFrei(h);
      act(() => { window.dispatchEvent(new Event('pagehide')); });
      expect(h.count('handGuide', false)).toBe(1);
      h.unmount();
      expect(h.count('handGuide', false)).toBe(1);

      const p = setup();
      await p.press(' ');
      await p.advance(3000);
      const start = p.last('recordControl');
      act(() => { window.dispatchEvent(new Event('pagehide')); });
      await p.resolve(start, { success: true });
      expect(p.count('recordControl', 'cancel')).toBe(1);
      p.unmount();
    });

    // A keepalive IS a hand_guide(true): a teardown false completing inside
    // its server claim-to-lock window leaves the arm limp with on_manual False.
    it('unmount with a keepalive in flight waits for it, then closes once', async () => {
      const h = setup();
      await toFrei(h);
      await h.advance(15000);
      expect(h.count('handGuide', true)).toBe(2);
      const keepalive = h.last('handGuide');
      h.unmount();
      expect(h.count('handGuide', false)).toBe(0);
      await h.resolve(keepalive, { success: true });
      expect(h.count('handGuide', false)).toBe(1);
      expect(h.count('handGuide', true)).toBe(2);
    });

    it('pagehide with a keepalive in flight waits for it, then closes once', async () => {
      const h = setup();
      await toFrei(h);
      await h.advance(15000);
      const keepalive = h.last('handGuide');
      act(() => { window.dispatchEvent(new Event('pagehide')); });
      expect(h.count('handGuide', false)).toBe(0);
      await h.resolve(keepalive, { success: true });
      expect(h.count('handGuide', false)).toBe(1);
      h.unmount();
      expect(h.count('handGuide', false)).toBe(1);
    });

    it('a keepalive still QUEUED at teardown is never sent, and the close waits', async () => {
      const h = setup();
      await toFrei(h);
      await h.advance(14800);
      await h.press('p'); // capture in flight across the 15 s tick
      await h.advance(400); // keepalive queued behind it
      expect(h.count('handGuide', true)).toBe(1);
      h.unmount();
      expect(h.count('handGuide', false)).toBe(0);
      await h.resolve(h.last('capturePose'), { success: true });
      expect(h.count('handGuide', true)).toBe(1);
      expect(h.count('handGuide', false)).toBe(1);
    });

    it('a start still QUEUED at teardown is never sent', async () => {
      const h = setup();
      await toFrei(h);
      await h.press('p'); // capture in flight
      await h.press(' '); // start queued behind it
      h.unmount();
      await h.resolve(h.last('capturePose'), { success: true });
      expect(h.count('recordControl', 'start')).toBe(0);
      expect(h.count('handGuide', false)).toBe(1);
    });

    it('a replay answered after unmount is aborted and arms no feed or timer', async () => {
      const h = setup();
      await toPruefen(h);
      await act(async () => { h.cur.actions.previewOnRobot(); await flush(); });
      const replay = h.last('replayMotion');
      h.unmount();
      expect(h.count('handGuide', false)).toBe(1);
      await h.resolve(replay, { success: true });
      expect(h.count('handGuide', false)).toBe(2);
      expect(h.feed.subs).toBe(0);
      expect(vi.getTimerCount()).toBe(0);
    });

    it('a start answered after unmount arms no timer', async () => {
      const h = setup();
      await h.press('f');
      await h.advance(3000);
      const start = h.last('handGuide');
      h.unmount();
      await h.resolve(start, { success: true });
      expect(h.count('handGuide', false)).toBe(1);
      expect(vi.getTimerCount()).toBe(0);
    });

    it('unmount in vorschau aborts the drive (the Stopp nobody can press any more)', async () => {
      const h = setup();
      await toPruefen(h);
      await act(async () => { h.cur.actions.previewOnRobot(); await flush(); });
      await h.resolve(h.last('replayMotion'), { success: true });
      h.unmount();
      expect(h.count('handGuide', false)).toBe(2);
      expect(h.feed.unsubs).toBe(1);
    });
  });
});

// ---- leader mode (D8) ------------------------------------------------------

async function leaderToAufnahme(h) {
  await h.press(' ');
  await h.resolve(h.last('recordControl'), { success: true, message: 'Aufnahme gestartet — führe den Leader-Arm.' });
  expect(h.state).toBe('aufnahme');
}

// The stop answer lands at the current fake time; the grace window runs from it.
async function leaderToPruefen(h, { n = 3 } = {}) {
  await leaderToAufnahme(h);
  await h.advance(500);
  await h.press(' ');
  await h.resolve(h.last('recordControl'), {
    success: true, points_json: points(n), sample_count: n, duration_s: 0.04 * (n - 1),
  });
  expect(h.state).toBe('pruefen');
}

const leaderSetup = (over = {}) => setup({ mode: 'leader', leaderLive: true, ...over });

describe('useTeachSession — leader mode key table (D8)', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  it('starts in bereit with nothing sent', () => {
    const h = leaderSetup();
    expect(h.state).toBe('bereit');
    expect(h.calls).toHaveLength(0);
    h.unmount();
  });

  it('bereit + Space: start_leader at once (no countdown) → aufnahme + start sound', async () => {
    const h = leaderSetup();
    await h.press(' ');
    expect(h.count('recordControl', 'start_leader')).toBe(1);
    expect(h.sounds.tick).not.toHaveBeenCalled();
    await h.resolve(h.last('recordControl'), { success: true });
    expect(h.state).toBe('aufnahme');
    expect(h.sounds.start).toHaveBeenCalledTimes(1);
    await h.advance(2000);
    expect(h.cur.elapsedS).toBeGreaterThanOrEqual(1.75);
    h.unmount();
  });

  it('bereit + Space refused: the server sentence, still bereit', async () => {
    const h = leaderSetup();
    await h.press(' ');
    await h.resolve(h.last('recordControl'), { success: false, message: 'Dieser Roboter hat keinen Leader-Arm.' });
    expect(h.state).toBe('bereit');
    expect(h.cbs.onError).toHaveBeenCalledWith('Dieser Roboter hat keinen Leader-Arm.');
    expect(h.cur.staleLeaderTake).toBe(false);
    h.unmount();
  });

  it('a take already running: staleLeaderTake, and „Alte Aufnahme verwerfen" cancels it', async () => {
    const h = leaderSetup();
    await h.press(' ');
    await h.resolve(h.last('recordControl'), {
      success: false, message: 'Eine Leader-Aufnahme läuft gerade — bitte zuerst beenden.',
    });
    expect(h.cur.staleLeaderTake).toBe(true);
    expect(h.cbs.onError).toHaveBeenCalledWith('Eine Leader-Aufnahme läuft gerade — bitte zuerst beenden.');
    await act(async () => { h.cur.actions.discardStaleLeaderTake(); await flush(); });
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
    expect(h.cur.staleLeaderTake).toBe(true);
    await h.resolve(h.last('recordControl'), { success: true, message: 'Aufnahme verworfen.' });
    expect(h.cur.staleLeaderTake).toBe(false);
    expect(h.state).toBe('bereit');
    h.unmount();
  });

  it('bereit + P and + Z capture (named on the key press)', async () => {
    const h = leaderSetup();
    await h.press('p');
    await h.resolve(h.last('capturePose'), { success: true, world_z: 0.1 });
    await h.press('z');
    await h.resolve(h.last('capturePose'), { success: true, world_z: 0.04 });
    expect(h.count('capturePose')).toBe(2);
    expect(h.cbs.onCapture.mock.calls.map((c) => c[0].kind)).toEqual(['pose', 'ziel']);
    h.unmount();
  });

  it('bereit + Esc: finished at once with 0 items (never a glide), abschluss with items', async () => {
    const zero = leaderSetup({ roundItemCount: 0 });
    await zero.press('Escape');
    expect(zero.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: false, relockOk: true, offline: false });
    expect(zero.calls).toHaveLength(0);
    zero.unmount();

    const two = leaderSetup({ roundItemCount: 2 });
    await two.press('Escape');
    expect(two.state).toBe('abschluss');
    await two.press('Escape');
    expect(two.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: false, relockOk: true, offline: false });
    two.unmount();
  });

  it('aufnahme + Space: stop_leader → pruefen with the take (relockOk), stop sound, onTake', async () => {
    const h = leaderSetup();
    await leaderToPruefen(h);
    expect(h.cur.take).toMatchObject({ fps: 25, sampleCount: 3, relockOk: true });
    expect(h.cur.take.points).toHaveLength(3);
    expect(h.sounds.stop).toHaveBeenCalledTimes(1);
    expect(h.cbs.onTake).toHaveBeenCalledTimes(1);
    expect(h.count('recordControl', 'stop_leader')).toBe(1);
    h.unmount();
  });

  it('aufnahme + P and + Z both capture (the follower stays torqued)', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    await h.press('p');
    await h.resolve(h.last('capturePose'), { success: true });
    await h.press('z');
    await h.resolve(h.last('capturePose'), { success: true });
    expect(h.count('capturePose')).toBe(2);
    expect(h.cbs.onError).not.toHaveBeenCalledWith(DE.TEACH_ZIEL_BLOCKED_REC);
    expect(h.state).toBe('aufnahme');
    h.unmount();
  });

  it('aufnahme + Esc: stop first, review, keep after the grace window, then finish', async () => {
    const h = leaderSetup({ roundItemCount: 0 });
    await leaderToAufnahme(h);
    await h.advance(500);
    await h.press('Escape');
    expect(h.count('recordControl', 'stop_leader')).toBe(1);
    await h.resolve(h.last('recordControl'), { success: true, points_json: points(4) });
    expect(h.state).toBe('pruefen');
    await h.advance(1100);
    await h.press('Enter');
    expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
    // The kept take counts as an item: the summary, never an immediate close.
    expect(h.state).toBe('abschluss');
    expect(h.cbs.onFinished).not.toHaveBeenCalled();
    h.unmount();
  });

  it('pruefen + Enter: ignored 200 ms after the stop, keeps at 1100 ms', async () => {
    const h = leaderSetup();
    await leaderToPruefen(h);
    expect(h.cur.keepHeld).toBe(true);
    await h.advance(200);
    await h.press('Enter');
    expect(h.cbs.onKeep).not.toHaveBeenCalled();
    expect(h.state).toBe('pruefen');
    await h.advance(900);
    expect(h.cur.keepHeld).toBe(false);
    await h.press('Enter');
    expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
    expect(h.state).toBe('bereit');
    h.unmount();
  });

  it('pruefen + Esc: ignored inside the grace window, keeps and finishes after it', async () => {
    const h = leaderSetup({ roundItemCount: 0 });
    await leaderToPruefen(h);
    await h.press('Escape');
    expect(h.cbs.onKeep).not.toHaveBeenCalled();
    expect(h.state).toBe('pruefen');
    await h.advance(1100);
    await h.press('Escape');
    expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
    expect(h.state).toBe('abschluss');
    h.unmount();
  });

  it('pruefen + R: discards and starts a new take (start_leader, no countdown)', async () => {
    const h = leaderSetup();
    await leaderToPruefen(h);
    await h.press('r');
    expect(h.cur.take).toBeNull();
    expect(h.count('recordControl', 'start_leader')).toBe(2);
    await h.resolve(h.last('recordControl'), { success: true });
    expect(h.state).toBe('aufnahme');
    expect(h.cbs.onKeep).not.toHaveBeenCalled();
    h.unmount();
  });

  it('pruefen + Entf: discards with no service call', async () => {
    const h = leaderSetup();
    await leaderToPruefen(h);
    const before = h.calls.length;
    await h.press('Delete');
    expect(h.state).toBe('bereit');
    expect(h.cur.take).toBeNull();
    expect(h.calls).toHaveLength(before);
    expect(h.cbs.onKeep).not.toHaveBeenCalled();
    h.unmount();
  });

  it('abschluss + Esc closes', async () => {
    const h = leaderSetup({ roundItemCount: 1 });
    await h.press('Escape');
    expect(h.state).toBe('abschluss');
    await h.press('Escape');
    expect(h.cbs.onFinished).toHaveBeenCalledTimes(1);
    h.unmount();
  });

  it('every „—" cell sends nothing and changes nothing', async () => {
    const b = leaderSetup();
    for (const k of ['f', 'Enter', 'r', 'Delete']) {
      await b.press(k); // eslint-disable-line no-await-in-loop
    }
    expect(b.calls).toHaveLength(0);
    expect(b.state).toBe('bereit');
    b.unmount();

    const a = leaderSetup();
    await leaderToAufnahme(a);
    const beforeA = a.calls.length;
    for (const k of ['f', 'Enter', 'r', 'Delete']) {
      await a.press(k); // eslint-disable-line no-await-in-loop
    }
    expect(a.calls).toHaveLength(beforeA);
    expect(a.state).toBe('aufnahme');
    a.unmount();

    const p = leaderSetup();
    await leaderToPruefen(p);
    const beforeP = p.calls.length;
    await p.advance(500);
    for (const k of [' ', 'f', 'p', 'z']) {
      await p.press(k); // eslint-disable-line no-await-in-loop
    }
    expect(p.calls).toHaveLength(beforeP);
    expect(p.state).toBe('pruefen');
    p.unmount();

    const s = leaderSetup({ roundItemCount: 1 });
    await s.press('Escape');
    for (const k of [' ', 'f', 'p', 'z', 'Enter', 'r', 'Delete']) {
      await s.press(k); // eslint-disable-line no-await-in-loop
    }
    expect(s.calls).toHaveLength(0);
    expect(s.state).toBe('abschluss');
    s.unmount();
  });

  it('handGuide is never called, and no preview, over a whole leader session', async () => {
    const h = leaderSetup({ roundItemCount: 0 });
    await leaderToPruefen(h);
    await act(async () => { h.cur.actions.previewOnRobot(); h.cur.actions.lock(); h.cur.actions.toggleFree(); await flush(); });
    await h.advance(20000);
    await h.press('Enter');
    await h.press('Escape');
    h.unmount();
    expect(h.count('handGuide')).toBe(0);
    expect(h.count('replayMotion')).toBe(0);
    expect(h.sounds.tick).not.toHaveBeenCalled();
  });
});

describe('useTeachSession — leader mode stops and collisions (D8)', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  const collide = async (h, active = true) => {
    h.rerender({ collisionActive: active });
    await act(async () => { await flush(); });
  };

  it('a 1-point take (a still follower, an un-activated rig) is „no motion" → bereit', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    await h.advance(500);
    await h.press(' ');
    await h.resolve(h.last('recordControl'), { success: true, points_json: points(1), sample_count: 1 });
    expect(h.state).toBe('bereit');
    expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_NO_MOTION);
    expect(h.cbs.onTake).not.toHaveBeenCalled();
    h.unmount();
  });

  it('a refused stop shows the server sentence verbatim → bereit', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    await h.advance(500);
    await h.press(' ');
    const msg = 'Der Leader-Arm sendet keine Daten mehr — die Aufnahme wurde verworfen.';
    await h.resolve(h.last('recordControl'), { success: false, message: msg, points_json: '' });
    expect(h.state).toBe('bereit');
    expect(h.cbs.onError).toHaveBeenCalledWith(msg);
    expect(h.cbs.onTake).not.toHaveBeenCalled();
    h.unmount();
  });

  it('a thrown stop keeps aufnahme (the student can retry)', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    await h.advance(500);
    await h.press(' ');
    await h.reject(h.last('recordControl'));
    expect(h.state).toBe('aufnahme');
    expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_OFFLINE);
    h.unmount();
  });

  it('the 120 s cap stops once with TEACH_CAP_REACHED', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    await h.advance(120250);
    expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_CAP_REACHED);
    expect(h.count('recordControl', 'stop_leader')).toBe(1);
    await h.advance(5000);
    expect(h.count('recordControl', 'stop_leader')).toBe(1);
    h.unmount();
  });

  it('collision in aufnahme: cancel_leader, bereit, TEACH_COLLISION_DISCARDED; keys ignored, not prevented', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    await collide(h);
    expect(h.state).toBe('bereit');
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
    expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_COLLISION_DISCARDED);
    await h.resolve(h.last('recordControl'), { success: true, message: 'Aufnahme verworfen.' });
    const before = h.calls.length;
    await h.advance(500);
    for (const k of [' ', 'p', 'z', 'Escape']) {
      const e = await h.press(k); // eslint-disable-line no-await-in-loop
      expect(e.defaultPrevented).toBe(false);
    }
    expect(h.calls).toHaveLength(before);
    expect(h.cbs.onFinished).not.toHaveBeenCalled();
    h.unmount();
  });

  it('collision 600 ms after the stop discards the returned take', async () => {
    const h = leaderSetup();
    await leaderToPruefen(h);
    await h.advance(600);
    await collide(h);
    expect(h.state).toBe('bereit');
    expect(h.cur.take).toBeNull();
    expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_COLLISION_DISCARDED);
    await collide(h, false);
    await h.advance(2000);
    await h.press('Enter');
    expect(h.cbs.onKeep).not.toHaveBeenCalled();
    h.unmount();
  });

  it('collision 1500 ms after the stop keeps the take', async () => {
    const h = leaderSetup();
    await leaderToPruefen(h);
    await h.advance(1500);
    await collide(h);
    expect(h.state).toBe('pruefen');
    expect(h.cur.take).not.toBeNull();
    expect(h.cbs.onError).not.toHaveBeenCalledWith(DE.TEACH_COLLISION_DISCARDED);
    await collide(h, false);
    await h.press('Enter');
    expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
    h.unmount();
  });

  it('a collision that reaches the client while stop_leader is in flight drops the take it returns', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    await h.advance(500);
    await h.press(' ');
    const stopCall = h.last('recordControl');
    await collide(h);
    expect(h.state).toBe('bereit');
    // The server handled the stop before its detector tripped: it returns the press.
    await h.resolve(stopCall, { success: true, points_json: points(10), sample_count: 10 });
    expect(h.state).toBe('bereit');
    expect(h.cur.take).toBeNull();
    expect(h.cbs.onTake).not.toHaveBeenCalled();
    expect(h.cbs.onError).toHaveBeenCalledTimes(1);
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
    await collide(h, false);
    await h.advance(2000);
    await h.press('Enter');
    expect(h.cbs.onKeep).not.toHaveBeenCalled();
    h.unmount();
  });

  it('a collision while start_leader is in flight cancels the take it arms', async () => {
    const h = leaderSetup();
    await h.press(' ');
    const start = h.last('recordControl');
    await collide(h);
    await h.resolve(start, { success: true });
    expect(h.state).toBe('bereit');
    expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_COLLISION_DISCARDED);
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
    h.unmount();
  });

  it('keepHeld clears on its own once the window has passed', async () => {
    const h = leaderSetup();
    await leaderToPruefen(h);
    expect(h.cur.keepHeld).toBe(true);
    await h.advance(1001);
    expect(h.cur.keepHeld).toBe(false);
    h.unmount();
  });
});

describe('useTeachSession — leader mode gates and teardown (D8)', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  it('a failed bridge probe mid-take is NOT leader-gone: Space still stops', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    // What the overlay derives from {available:false, followerOnly:false}.
    h.rerender({ leaderLive: false, leaderGone: false });
    await h.advance(500);
    await h.press(' ');
    expect(h.count('recordControl', 'stop_leader')).toBe(1);
    h.unmount();
  });

  it('leaderGone: every key but Esc waits (buttons too); Esc still finishes', async () => {
    const h = leaderSetup({ leaderGone: true, roundItemCount: 0 });
    for (const k of [' ', 'p', 'z']) {
      await h.press(k); // eslint-disable-line no-await-in-loop
    }
    act(() => { h.cur.actions.space(); h.cur.actions.capturePose(); h.cur.actions.captureZiel(); });
    await act(async () => { await flush(); });
    expect(h.calls).toHaveLength(0);
    await h.press('Escape');
    expect(h.cbs.onFinished).toHaveBeenCalledTimes(1);
    h.unmount();
  });

  it('leaderGone in aufnahme: Space waits, Esc stops the take', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    h.rerender({ leaderGone: true });
    await h.advance(500);
    await h.press(' ');
    expect(h.count('recordControl', 'stop_leader')).toBe(0);
    await h.press('Escape');
    expect(h.count('recordControl', 'stop_leader')).toBe(1);
    h.unmount();
  });

  it('activationBlocked: Space/P/Z send nothing, Esc finishes', async () => {
    const h = leaderSetup({ activationBlocked: true, roundItemCount: 0 });
    for (const k of [' ', 'p', 'z']) {
      const e = await h.press(k); // eslint-disable-line no-await-in-loop
      expect(e.defaultPrevented).toBe(true);
    }
    act(() => { h.cur.actions.space(); });
    await act(async () => { await flush(); });
    expect(h.calls).toHaveLength(0);
    h.rerender({ activationBlocked: false });
    await h.advance(500);
    await h.press(' ');
    expect(h.count('recordControl', 'start_leader')).toBe(1);
    h.rerender({ activationBlocked: true });
    await h.press('Escape');
    expect(h.cbs.onFinished).toHaveBeenCalledTimes(1);
    h.unmount();
  });

  it('a live leader is expected: no TEACH_LEADER_TURNED_ON lock-out', async () => {
    const h = leaderSetup({ leaderLive: false });
    h.rerender({ leaderLive: true });
    await act(async () => { await flush(); });
    expect(h.cbs.onError).not.toHaveBeenCalledWith(DE.TEACH_LEADER_TURNED_ON);
    await h.press(' ');
    expect(h.count('recordControl', 'start_leader')).toBe(1);
    h.unmount();
  });

  it('unmount in aufnahme cancels the take once', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    h.unmount();
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
    expect(h.count('recordControl', 'cancel')).toBe(0);
    expect(h.count('handGuide')).toBe(0);
  });

  it('unmount during a pending start_leader cancels once, after it answered', async () => {
    const h = leaderSetup();
    await h.press(' ');
    const start = h.last('recordControl');
    h.unmount();
    expect(h.count('recordControl', 'cancel_leader')).toBe(0);
    await h.resolve(start, { success: true });
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('pagehide during a pending start_leader behaves like unmount', async () => {
    const h = leaderSetup();
    await h.press(' ');
    const start = h.last('recordControl');
    act(() => { window.dispatchEvent(new Event('pagehide')); });
    await h.resolve(start, { success: true });
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
    h.unmount();
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
  });

  it('a start_leader refused before teardown sends no cancel (it could be another tab\'s take)', async () => {
    const h = leaderSetup();
    await h.press(' ');
    const start = h.last('recordControl');
    h.unmount();
    await h.resolve(start, { success: false, message: 'Eine Leader-Aufnahme läuft gerade — bitte zuerst beenden.' });
    expect(h.count('recordControl', 'cancel_leader')).toBe(0);
  });

  it('offline close in aufnahme: cancel_leader as the teardown, no limp-arm advice', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    h.rerender({ heartbeatOk: false });
    await h.press('Escape');
    expect(h.count('recordControl', 'cancel_leader')).toBe(1);
    expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_OFFLINE);
    expect(h.cbs.onError).not.toHaveBeenCalledWith(DE.TEACH_CLOSE_OFFLINE);
    expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: false, relockOk: false, offline: true });
    h.unmount();
  });
});

// R7 (fixed 2026-09-15): an unknown leader status blocks NEW teaching actions
// and never an exit; `mode: 'pending'` (TeachHost has not picked a mode yet)
// behaves as blocked throughout.
describe('useTeachSession — leader status unknown (R7)', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  it('hand mode, fest: Space, F, P, Z and the buttons send nothing and start no countdown', async () => {
    const h = setup({ leaderStatusUnknown: true });
    for (const k of [' ', 'f', 'p', 'z']) {
      const e = await h.press(k); // eslint-disable-line no-await-in-loop
      expect(e.defaultPrevented).toBe(true);
      expect(h.state).toBe('fest');
    }
    act(() => {
      h.cur.actions.space(); h.cur.actions.toggleFree(); h.cur.actions.capturePose(); h.cur.actions.captureZiel();
    });
    await h.advance(4000);
    expect(h.state).toBe('fest');
    expect(h.calls).toHaveLength(0);
    // The answer arrives: teaching works again.
    h.rerender({ leaderStatusUnknown: false });
    await h.press('p');
    expect(h.count('capturePose')).toBe(1);
    h.unmount();
  });

  it('a take in progress is NOT interrupted: P waits, Space still stops it (hand mode)', async () => {
    const h = setup();
    await toAufnahme(h);
    h.rerender({ leaderStatusUnknown: true });
    await h.advance(1000);
    expect(h.state).toBe('aufnahme');
    expect(h.count('recordControl', 'cancel')).toBe(0);
    expect(h.count('handGuide', false)).toBe(0);
    await h.press('p');
    expect(h.count('capturePose')).toBe(0);
    await h.press(' ');
    expect(h.count('recordControl', 'stop')).toBe(1);
    await h.resolve(h.last('recordControl'), {
      success: true, points_json: points(3), sample_count: 3, duration_s: 0.08,
    });
    await h.resolve(h.last('handGuide'), { success: true });
    expect(h.state).toBe('pruefen');
    // Review: R (a new take) and the real-arm replay wait; Enter still keeps.
    await h.press('r');
    expect(h.state).toBe('pruefen');
    act(() => { h.cur.actions.previewOnRobot(); });
    await act(async () => { await flush(); });
    expect(h.count('replayMotion')).toBe(0);
    await h.press('Enter');
    expect(h.cbs.onKeep).toHaveBeenCalledTimes(1);
    expect(h.state).toBe('fest');
    h.unmount();
  });

  it('frei: F still re-locks, Space and P wait (hand mode)', async () => {
    const h = setup();
    await toFrei(h);
    h.rerender({ leaderStatusUnknown: true });
    await h.press(' ');
    await h.press('p');
    expect(h.count('recordControl', 'start')).toBe(0);
    expect(h.count('capturePose')).toBe(0);
    await h.press('f');
    expect(h.count('handGuide', false)).toBe(1);
    await h.resolve(h.last('handGuide'), { success: true });
    expect(h.state).toBe('fest');
    h.unmount();
  });

  it('a countdown still running when the status turns unknown ends where it began, and F/Space cancel it', async () => {
    const h = setup();
    await h.press(' ');
    expect(h.state).toBe('countdown');
    h.rerender({ leaderStatusUnknown: true });
    await h.advance(3000);
    expect(h.state).toBe('fest');
    expect(h.calls).toHaveLength(0);
    h.rerender({ leaderStatusUnknown: false });
    await h.press('f');
    expect(h.state).toBe('countdown');
    h.rerender({ leaderStatusUnknown: true });
    await h.press('f');
    expect(h.state).toBe('fest');
    expect(h.calls).toHaveLength(0);
    h.unmount();
  });

  it('leader mode: a leader take keeps running and Space stops it; a new take, P and Z wait', async () => {
    const h = leaderSetup();
    await leaderToAufnahme(h);
    h.rerender({ leaderStatusUnknown: true });
    await h.advance(1000);
    expect(h.state).toBe('aufnahme');
    expect(h.count('recordControl', 'cancel_leader')).toBe(0);
    await h.press('p');
    await h.press('z');
    expect(h.count('capturePose')).toBe(0);
    await h.press(' ');
    expect(h.count('recordControl', 'stop_leader')).toBe(1);
    await h.resolve(h.last('recordControl'), { success: false, message: 'Aufnahme verworfen.' });
    expect(h.state).toBe('bereit');
    await h.advance(500);
    await h.press(' ');
    expect(h.count('recordControl', 'start_leader')).toBe(1);
    h.unmount();
  });

  it('pending mode: nothing teaches, no lock-out toast when the leader answers „on", Esc closes', async () => {
    const h = setup({ mode: 'pending', roundItemCount: 0 });
    expect(h.state).toBe('fest');
    for (const k of [' ', 'f', 'p', 'z']) {
      await h.press(k); // eslint-disable-line no-await-in-loop
    }
    await h.advance(4000);
    expect(h.calls).toHaveLength(0);
    h.rerender({ leaderLive: true });
    await act(async () => { await flush(); });
    expect(h.cbs.onError).not.toHaveBeenCalledWith(DE.TEACH_LEADER_TURNED_ON);
    await h.press('Escape');
    expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: false, relockOk: true, offline: false });
    h.unmount();
    expect(h.calls).toHaveLength(0);
  });

  it('pending mode offline: closes with „Keine Verbindung", never the hold-the-limp-arm sentence', async () => {
    const h = setup({ mode: 'pending', heartbeatOk: false });
    await h.press('Escape');
    expect(h.cbs.onError).toHaveBeenCalledWith(DE.TEACH_OFFLINE);
    expect(h.cbs.onError).not.toHaveBeenCalledWith(DE.TEACH_CLOSE_OFFLINE);
    expect(h.cbs.onFinished).toHaveBeenCalledWith({ releasedOnce: false, relockOk: false, offline: true });
    h.unmount();
  });
});
