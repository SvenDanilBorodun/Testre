// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Aufnahme page's phase clock (spec §3.3).
//
// One requestAnimationFrame loop while a timed phase runs; every frame it asks
// `elapsedAt` (features/tasks/phaseAnchor) where the phase stands and hands
// that to the SUBSCRIBERS, which write styles straight into refs (the ring,
// the Phasenleiste, the red bottom line). React state changes only when the
// whole seconds do — `secondsLeft` and the floored `elapsed` — so the page
// renders about once a second, never per frame.
//
// Within one anchor instance the clock never runs backwards; a new instance
// (a new phase, episode, or a redo) starts from its own value. With reduced
// motion the frames move in whole-second steps.

import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { ReactReduxContext } from 'react-redux';

import { elapsedAt } from '../features/tasks/phaseAnchor';

const raf = (fn) => (typeof window !== 'undefined' && window.requestAnimationFrame
  ? window.requestAnimationFrame(fn)
  : setTimeout(() => fn(performance.now()), 16));
const caf = (id) => (typeof window !== 'undefined' && window.cancelAnimationFrame
  ? window.cancelAnimationFrame(id)
  : clearTimeout(id));

/** One frame of the clock for `anchor` at `nowMs`. Pure. */
export function clockFrame(anchor, nowMs, lastTickAt, { reducedMotion = false, floorAt = 0 } = {}) {
  const total = anchor?.total > 0 ? anchor.total : 0;
  let elapsed = Math.max(elapsedAt(anchor, nowMs, lastTickAt), floorAt);
  if (reducedMotion) elapsed = Math.floor(elapsed);
  if (total > 0) elapsed = Math.min(elapsed, total);
  const remaining = total > 0 ? Math.max(0, total - elapsed) : 0;
  return {
    instance: anchor?.instance ?? 0,
    total,
    elapsed,
    remaining,
    frac: total > 0 ? elapsed / total : 0,
  };
}

/**
 * @param anchor  the phase anchor (or null)
 * @param opts.frozen         no loop (SAVING, COLLISION, offline …): one static frame
 * @param opts.reducedMotion  whole-second steps
 * @param opts.getLastTickAt  arrival (performance.now) of the latest status;
 *                            defaults to the store's taskStatus.receivedAt
 * @returns {{subscribe: function(function): function, secondsLeft: number, elapsed: number, instance: ?number}}
 */
export default function useSmoothPhaseClock(anchor, { frozen = false, reducedMotion = false, getLastTickAt } = {}) {
  // The context, not useStore(): a caller that passes getLastTickAt needs no
  // Provider (useStore throws without one).
  const store = useContext(ReactReduxContext)?.store ?? null;
  const subscribersRef = useRef(new Set());
  const frameRef = useRef(null);
  const monoRef = useRef({ instance: null, elapsed: 0 });
  const readLastTick = useCallback(
    () => (getLastTickAt
      ? getLastTickAt()
      : store?.getState().tasks?.taskStatus?.receivedAt ?? null),
    [getLastTickAt, store],
  );
  const [shown, setShown] = useState({ secondsLeft: 0, elapsed: 0, instance: null });

  const emit = useCallback((frame) => {
    frameRef.current = frame;
    subscribersRef.current.forEach((fn) => {
      try { fn(frame); } catch { /* a subscriber must never stop the clock */ }
    });
    const secondsLeft = Math.ceil(frame.remaining - 1e-9);
    const elapsed = Math.floor(frame.elapsed + 1e-9);
    const instance = frame.instance || null;
    setShown((prev) => (prev.secondsLeft === secondsLeft && prev.elapsed === elapsed && prev.instance === instance
      ? prev
      : { secondsLeft, elapsed, instance }));
  }, []);

  useEffect(() => {
    const compute = (nowMs) => {
      const mono = monoRef.current;
      const sameInstance = anchor && mono.instance === anchor.instance;
      const frame = clockFrame(anchor, nowMs, readLastTick(), {
        reducedMotion,
        floorAt: sameInstance ? mono.elapsed : 0,
      });
      monoRef.current = { instance: anchor?.instance ?? null, elapsed: frame.elapsed };
      return frame;
    };

    if (!anchor || !(anchor.total > 0) || frozen) {
      emit(compute(performance.now()));
      return undefined;
    }
    let id = null;
    let stopped = false;
    const loop = () => {
      if (stopped) return;
      emit(compute(performance.now()));
      id = raf(loop);
    };
    loop();
    return () => {
      stopped = true;
      if (id !== null) caf(id);
    };
  }, [anchor, frozen, reducedMotion, readLastTick, emit]);

  const subscribe = useCallback((fn) => {
    subscribersRef.current.add(fn);
    if (frameRef.current) {
      try { fn(frameRef.current); } catch { /* ignore */ }
    }
    return () => { subscribersRef.current.delete(fn); };
  }, []);

  // `instance` names the anchor instance the whole seconds belong to: for one
  // render after a phase change they still describe the previous instance.
  return { subscribe, secondsLeft: shown.secondsLeft, elapsed: shown.elapsed, instance: shown.instance };
}
