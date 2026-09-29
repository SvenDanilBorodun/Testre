// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Aufnahme phase clock: one rAF loop while a timed phase runs, per-frame
// values to subscribers, React state only on whole-second changes, monotonic
// within an anchor instance, whole-second steps with reduced motion.

import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import useSmoothPhaseClock, { clockFrame } from '../useSmoothPhaseClock';
import tasksReducer, { setTaskStatus } from '../../features/tasks/taskSlice';

let now = 0;
let queue = [];
let rafSpy;
let cafSpy;
let nowSpy;

function runFrame(ms) {
  now = ms;
  const due = queue;
  queue = [];
  act(() => { due.forEach((fn) => fn(ms)); });
}

beforeEach(() => {
  now = 0;
  queue = [];
  nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => now);
  rafSpy = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((fn) => {
    queue.push(fn);
    return queue.length;
  });
  cafSpy = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => { queue = []; });
});

afterEach(() => {
  nowSpy.mockRestore();
  rafSpy.mockRestore();
  cafSpy.mockRestore();
});

const anchor = (patch = {}) => ({
  key: '1:0', instance: 1, phase: 1, episode: 0, value: 0, total: 5, at: 1000, ...patch,
});

describe('clockFrame', () => {
  it('reports elapsed, remaining and the fraction', () => {
    expect(clockFrame(anchor(), 1500, 1500)).toEqual({
      instance: 1, total: 5, elapsed: 0.5, remaining: 4.5, frac: 0.1,
    });
  });

  it('reduced motion moves in whole seconds', () => {
    expect(clockFrame(anchor({ value: 2 }), 1500, 1500, { reducedMotion: true }).elapsed).toBe(2);
  });

  it('a total of 0 has no remaining time and no fraction', () => {
    expect(clockFrame(anchor({ total: 0 }), 1500, 1500)).toMatchObject({ remaining: 0, frac: 0 });
    expect(clockFrame(null, 1500, 1500)).toMatchObject({ instance: 0, remaining: 0, frac: 0, elapsed: 0 });
  });
});

describe('useSmoothPhaseClock', () => {
  it('runs a frame loop, feeds subscribers, and changes state only per whole second', () => {
    now = 1000;
    const getLastTickAt = () => now;
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useSmoothPhaseClock(anchor(), { getLastTickAt });
    });
    expect(result.current.secondsLeft).toBe(5);
    const frames = [];
    let unsubscribe;
    act(() => { unsubscribe = result.current.subscribe((f) => frames.push(f)); });
    expect(frames).toHaveLength(1); // the latest frame, at once
    const before = renders;
    runFrame(1200);
    runFrame(1400);
    expect(frames.map((f) => f.elapsed)).toEqual([0, 0.2, 0.4]);
    expect(frames[2].frac).toBeCloseTo(0.08, 6);
    expect(renders).toBe(before); // still 5 s left: no re-render
    unsubscribe();
    runFrame(1600);
    expect(frames).toHaveLength(3);
  });

  it('secondsLeft counts down as the anchor moves', () => {
    now = 1000;
    const getLastTickAt = () => now;
    const { result, rerender } = renderHook(({ a }) => useSmoothPhaseClock(a, { getLastTickAt }), {
      initialProps: { a: anchor() },
    });
    runFrame(1900);
    expect(result.current.secondsLeft).toBe(5); // 4.1 s left
    rerender({ a: anchor({ value: 1, at: 2000 }) });
    runFrame(2100);
    expect(result.current.secondsLeft).toBe(4);
    expect(result.current.elapsed).toBe(1);
  });

  it('never runs backwards within an instance, restarts with a new one', () => {
    now = 1900;
    const getLastTickAt = () => now;
    const frames = [];
    const { result, rerender } = renderHook(({ a }) => useSmoothPhaseClock(a, { getLastTickAt }), {
      initialProps: { a: anchor({ value: 2, at: 1000 }) },
    });
    act(() => { result.current.subscribe((f) => frames.push(f)); });
    expect(frames[frames.length - 1].elapsed).toBeCloseTo(2.9, 6);
    // same instance, re-anchored later (a changed total) — would read 2.01
    rerender({ a: anchor({ value: 2, at: 1950, total: 6 }) });
    runFrame(1960);
    expect(frames[frames.length - 1].elapsed).toBeCloseTo(2.9, 6);
    // a new instance starts from its own value
    rerender({ a: anchor({ instance: 2, key: '3:0', value: 0, at: 1960, total: 10 }) });
    runFrame(1970);
    expect(frames[frames.length - 1].elapsed).toBeCloseTo(0.01, 6);
  });

  it('frozen: no loop, one static frame', () => {
    now = 1500;
    const getLastTickAt = () => now;
    const { result } = renderHook(() => useSmoothPhaseClock(anchor({ value: 3 }), { frozen: true, getLastTickAt }));
    expect(queue).toHaveLength(0);
    expect(result.current.secondsLeft).toBe(2);
  });

  it('without a timed phase there is no loop', () => {
    renderHook(() => useSmoothPhaseClock(anchor({ total: 0 })));
    expect(queue).toHaveLength(0);
  });

  it('reads the last tick from the store by default (a frozen feed stops the clock)', () => {
    const store = configureStore({ reducer: { tasks: tasksReducer } });
    store.dispatch(setTaskStatus({ receivedAt: 1000 }));
    const wrapper = ({ children }) => React.createElement(Provider, { store }, children);
    now = 1000;
    const frames = [];
    const { result } = renderHook(() => useSmoothPhaseClock(anchor({ value: 0, total: 60 })), { wrapper });
    act(() => { result.current.subscribe((f) => frames.push(f)); });
    runFrame(5000); // feed silent since 1000: held at 1000 + 1500
    expect(frames[frames.length - 1].elapsed).toBeCloseTo(0.999, 6);
  });
});
