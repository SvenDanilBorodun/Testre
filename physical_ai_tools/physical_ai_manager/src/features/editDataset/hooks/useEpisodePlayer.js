// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The player's ONE clock (spec §F2), measured in Chrome (P6–P8):
//
//  - the frame index comes from the DRIVER video's requestVideoFrameCallback
//    metadata (`round(mediaTime·fps)`); without rVFC a requestAnimationFrame
//    loop reads `currentTime` (`floor(t·fps + 1e-3)`);
//  - a seek pauses first and sets EVERY video to `(i + 0.5)/fps` (to `i/fps`
//    one frame in six landed one early); it resolves when each video's next
//    frame callback reports `i`, or after 1 s;
//  - a step goes by INDEX, never `currentTime ± 1/fps`;
//  - play starts all videos together; at the last frame or `ended` everything
//    pauses and seeks to `L-1` itself (after `ended` the shown frame can be
//    1–2 short); play from the last frame restarts at 0;
//  - drift: each camera's latest frame callback is paired with the driver's of
//    the same `expectedDisplayTime` (±1 ms); |Δ| ≥ 2 frames in 3 consecutive
//    pairs → that camera is hard-resynced to the driver (never playbackRate
//    nudging — it made it worse);
//  - NO React state per frame: subscribers get the index and move the DOM
//    through refs; React state changes only on play/pause, speed and episode.
//
// With no playable video (both cameras unplayable) a time-based clock keeps
// the charts and the 3D twin going.

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  clampFrame, idxFromCurrentTime, idxFromMediaTime, lastFrame, seekTime,
} from '../model/playerClock';

export const SEEK_TIMEOUT_MS = 1000;
export const DRIFT_FRAMES = 2;
export const DRIFT_PAIRS = 3;
const EDT_TOLERANCE_MS = 1;
export const SPEEDS = Object.freeze([0.25, 0.5, 1, 2]);

const hasRvfc = (el) => !!el && typeof el.requestVideoFrameCallback === 'function';

/**
 * The clock without React (tested with fake videos). `raf`/`caf`/`now` are
 * injectable for tests.
 */
export function createEpisodeEngine({
  raf = (cb) => (typeof window !== 'undefined' ? window.requestAnimationFrame(cb) : setTimeout(cb, 16)),
  caf = (id) => (typeof window !== 'undefined' ? window.cancelAnimationFrame(id) : clearTimeout(id)),
  now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
} = {}) {
  let fps = 30;
  let length = 1;
  let idx = 0;
  let playing = false;
  let rate = 1;
  let autoplay = false;
  let pendingSeek = null; // frame to seek to once the media can
  let rafId = null;
  let virtual = null; // {startIdx, startAt} while the time-based clock runs
  let disposed = false;
  const videos = new Map(); // cam → entry
  const subscribers = new Set();
  const playingListeners = new Set();
  const seekWaiters = new Set();
  const api = { resyncCount: 0 };

  const notify = () => subscribers.forEach((fn) => { try { fn(idx); } catch { /* a subscriber must not stop the clock */ } });
  const setPlayingState = (v) => {
    if (playing === v) return;
    playing = v;
    playingListeners.forEach((fn) => { try { fn(v); } catch { /* ignore */ } });
  };
  const setIdx = (i) => {
    const v = clampFrame(i, length);
    if (v === idx) return;
    idx = v;
    notify();
  };

  const playable = () => [...videos.values()].filter((e) => !e.failed);
  const driver = () => {
    const list = playable();
    return list.find((e) => e.driver) || list[0] || null;
  };

  // ---- the frame callbacks ------------------------------------------------
  function resolveSeeks(entry, i) {
    seekWaiters.forEach((w) => {
      if (w.target !== i) return;
      w.pending.delete(entry.cam);
      if (w.pending.size === 0) w.done();
    });
  }

  function pairDrift(entry, drv) {
    if (!drv || entry === drv || !entry.last || !drv.last) return;
    if (Math.abs(entry.last.edt - drv.last.edt) > EDT_TOLERANCE_MS) return;
    if (entry.pairedEdt === drv.last.edt) return;
    entry.pairedEdt = drv.last.edt;
    const delta = Math.abs(entry.last.idx - drv.last.idx);
    if (delta >= DRIFT_FRAMES) {
      entry.driftPairs += 1;
      if (entry.driftPairs >= DRIFT_PAIRS && playing) {
        entry.driftPairs = 0;
        try { entry.el.currentTime = seekTime(drv.last.idx, fps); } catch { /* the element may be gone */ }
        api.resyncCount += 1;
      }
    } else {
      entry.driftPairs = 0;
    }
  }

  function onFrame(entry, _when, meta) {
    if (disposed || videos.get(entry.cam) !== entry) return;
    entry.handle = null;
    const i = idxFromMediaTime(meta && meta.mediaTime, fps, length);
    const edt = meta && Number.isFinite(meta.expectedDisplayTime) ? meta.expectedDisplayTime : now();
    entry.last = { edt, idx: i };
    resolveSeeks(entry, i);
    const drv = driver();
    if (drv === entry) {
      if (playing || !seekWaiters.size) setIdx(i);
      videos.forEach((other) => pairDrift(other, entry));
      if (playing && i >= lastFrame(length)) end();
    } else {
      pairDrift(entry, drv);
    }
    arm(entry);
  }

  function arm(entry) {
    if (!hasRvfc(entry.el) || entry.handle !== null || disposed) return;
    entry.handle = entry.el.requestVideoFrameCallback((when, meta) => onFrame(entry, when, meta));
  }

  // ---- the rAF fallback and the virtual clock ------------------------------
  function loop() {
    rafId = null;
    if (disposed) return;
    const drv = driver();
    if (drv && !hasRvfc(drv.el)) {
      const i = idxFromCurrentTime(drv.el.currentTime || 0, fps, length);
      if (playing || !seekWaiters.size) setIdx(i);
      seekWaiters.forEach((w) => { if (i === w.target) { w.pending.clear(); w.done(); } });
      if (playing && (i >= lastFrame(length) || drv.el.ended)) { end(); return; }
    } else if (!drv && playing && virtual) {
      const i = virtual.startIdx + Math.floor(((now() - virtual.startAt) / 1000) * fps * rate);
      if (i >= lastFrame(length)) { setIdx(lastFrame(length)); setPlayingState(false); virtual = null; return; }
      setIdx(i);
    }
    if (playing || seekWaiters.size) rafId = raf(loop);
  }
  function kick() {
    if (rafId === null && !disposed) {
      const drv = driver();
      if (!drv || !hasRvfc(drv.el)) rafId = raf(loop);
    }
  }

  // ---- control ---------------------------------------------------------------
  function pauseAll() {
    videos.forEach((e) => { try { e.el.pause(); } catch { /* ignore */ } });
    virtual = null;
    setPlayingState(false);
  }

  function seekFrame(i) {
    const target = clampFrame(i, length);
    pauseAll();
    autoplay = false;
    setIdx(target);
    const list = playable();
    if (list.length === 0) return Promise.resolve(target);
    return new Promise((resolve) => {
      const w = { target, pending: new Set(list.map((e) => e.cam)), done: null, timer: null };
      w.done = () => {
        if (!seekWaiters.has(w)) return;
        seekWaiters.delete(w);
        clearTimeout(w.timer);
        resolve(target);
      };
      // a newer seek replaces an older one
      seekWaiters.forEach((old) => old.done());
      seekWaiters.add(w);
      w.timer = setTimeout(w.done, SEEK_TIMEOUT_MS);
      list.forEach((e) => {
        if (e.el.readyState === 0) {
          pendingSeek = target;
        } else {
          try { e.el.currentTime = seekTime(target, fps); } catch { pendingSeek = target; }
        }
        arm(e);
      });
      kick();
    });
  }

  function play() {
    if (playing) return;
    if (idx >= lastFrame(length)) {
      seekFrame(0).then(() => startPlaying());
      return;
    }
    startPlaying();
  }
  function startPlaying() {
    const list = playable();
    setPlayingState(true);
    if (list.length === 0) {
      virtual = { startIdx: idx, startAt: now() };
      kick();
      return;
    }
    list.forEach((e) => {
      e.driftPairs = 0;
      try { e.el.playbackRate = rate; } catch { /* ignore */ }
      try {
        const p = e.el.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch { /* jsdom has no media playback */ }
      arm(e);
    });
    kick();
  }

  function end() {
    pauseAll();
    seekFrame(lastFrame(length));
  }

  // ---- videos ------------------------------------------------------------------
  function attach(cam, el, { driver: isDriver = false } = {}) {
    if (!el) return;
    const prev = videos.get(cam);
    if (prev && prev.el === el) { prev.driver = isDriver; return; }
    if (prev) detach(cam);
    const entry = {
      cam, el, driver: isDriver, handle: null, last: null, pairedEdt: null, driftPairs: 0, failed: false,
      onEnded: null, onLoaded: null,
    };
    entry.onEnded = () => { if (playing && driver() === entry) end(); };
    entry.onLoaded = () => {
      if (pendingSeek !== null) {
        try { el.currentTime = seekTime(pendingSeek, fps); } catch { /* ignore */ }
      }
      if (autoplay && driver() === entry) {
        autoplay = false;
        pendingSeek = null;
        startPlaying();
      }
    };
    try { el.playbackRate = rate; } catch { /* ignore */ }
    if (typeof el.addEventListener === 'function') {
      el.addEventListener('ended', entry.onEnded);
      el.addEventListener('loadeddata', entry.onLoaded);
    }
    videos.set(cam, entry);
    arm(entry);
  }

  function detach(cam) {
    const e = videos.get(cam);
    if (!e) return;
    if (e.handle !== null && typeof e.el.cancelVideoFrameCallback === 'function') {
      try { e.el.cancelVideoFrameCallback(e.handle); } catch { /* ignore */ }
    }
    if (typeof e.el.removeEventListener === 'function') {
      e.el.removeEventListener('ended', e.onEnded);
      e.el.removeEventListener('loadeddata', e.onLoaded);
    }
    videos.delete(cam);
  }

  /** A camera whose clip cannot play (409 unplayable, a failed load): the clock moves on without it. */
  function setFailed(cam, failed) {
    const e = videos.get(cam);
    if (e) e.failed = !!failed;
  }

  function setRate(r) {
    rate = SPEEDS.includes(r) ? r : 1;
    videos.forEach((e) => { try { e.el.playbackRate = rate; } catch { /* ignore */ } });
    if (virtual) virtual = { startIdx: idx, startAt: now() };
  }

  /** A new episode: pause, frame 0, keep playing when it was (the media reloads). */
  function setEpisode({ fps: f, length: l, keepPlaying = false }) {
    const was = playing;
    pauseAll();
    seekWaiters.forEach((w) => w.done());
    fps = Number(f) > 0 ? Number(f) : 30;
    length = Math.max(1, Math.floor(Number(l) || 1));
    idx = 0;
    pendingSeek = 0;
    autoplay = !!(keepPlaying && was);
    videos.forEach((e) => { e.last = null; e.pairedEdt = null; e.driftPairs = 0; });
    notify();
    if (autoplay && playable().length === 0) { autoplay = false; startPlaying(); }
  }

  Object.assign(api, {
    attach,
    detach,
    setFailed,
    seekFrame,
    step: (d) => seekFrame(idx + d),
    play,
    pause: pauseAll,
    toggle: () => (playing ? pauseAll() : play()),
    setRate,
    setEpisode,
    getIdx: () => idx,
    getFps: () => fps,
    getLength: () => length,
    isPlaying: () => playing,
    getRate: () => rate,
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
    onPlayingChange(fn) { playingListeners.add(fn); return () => playingListeners.delete(fn); },
    dispose() {
      disposed = true;
      if (rafId !== null) caf(rafId);
      [...videos.keys()].forEach(detach);
      seekWaiters.forEach((w) => w.done());
      subscribers.clear();
      playingListeners.clear();
    },
    // test seam
    _onFrame: (cam, meta) => { const e = videos.get(cam); if (e) onFrame(e, 0, meta); },
  });
  return api;
}

/**
 * The player's clock for one episode at a time.
 * @param {{fps: number, length: number, episodeKey: string}} p
 * @returns {{engine: object, playing: boolean, speed: number, setSpeed: Function, videoRef: Function}}
 */
export default function useEpisodePlayer({ fps, length, episodeKey }) {
  const engineRef = useRef(null);
  if (engineRef.current === null) engineRef.current = createEpisodeEngine();
  const engine = engineRef.current;
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeedState] = useState(1);

  useEffect(() => engine.onPlayingChange(setPlaying), [engine]);
  useEffect(() => () => engine.dispose(), [engine]);

  // A new episode (or new data for it): frame 0, keep playing when it was.
  useEffect(() => {
    engine.setEpisode({ fps, length, keepPlaying: true });
  }, [engine, episodeKey, fps, length]);

  const setSpeed = useCallback((r) => {
    engine.setRate(r);
    setSpeedState(engine.getRate());
  }, [engine]);

  // One ref callback per camera, stable per (cam, driver).
  const refs = useRef(new Map());
  const videoRef = useCallback((cam, isDriver) => {
    const key = `${cam}:${isDriver ? 1 : 0}`;
    if (!refs.current.has(key)) {
      refs.current.set(key, (el) => {
        if (el) engine.attach(cam, el, { driver: isDriver });
        else engine.detach(cam);
      });
    }
    return refs.current.get(key);
  }, [engine]);

  return { engine, playing, speed, setSpeed, videoRef };
}
