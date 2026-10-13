// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

/* eslint-disable testing-library/no-container, testing-library/no-node-access --
   these tests pin markup: hook classes, custom properties and the DOM shape. */

// The Lichtband (M7, F15): ONE render builds the lights; progress only writes
// the row's custom properties + aria-valuenow, and only when the head index or
// `full` changes — never React state per frame.

import React, { Profiler } from 'react';
import { act, render, screen } from '@testing-library/react';
import Lichtband, { bandHead } from '../Lichtband';

function makeClock() {
  const subs = new Set();
  return {
    subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); },
    emit: (frac) => subs.forEach((fn) => fn({ frac })),
    size: () => subs.size,
  };
}

describe('bandHead', () => {
  it.each([
    [0, 32, 0, 0, 0], [0.5, 32, 16, 0, 50], [0.999, 32, 31, 0, 100], [1, 32, 31, 1, 100],
    [-1, 10, 0, 0, 0], [NaN, 10, 0, 0, 0], [0.31, 10, 3, 0, 31],
  ])('frac %s over %s → h %s full %s pct %s', (frac, count, h, full, pct) => {
    expect(bandHead(frac, count)).toEqual({ h, full, pct });
  });
});

describe('Lichtband', () => {
  it('a labelled progressbar with `count` lights, each carrying its index', () => {
    const { container } = render(<Lichtband count={30} frac={0.5} color="#FF5B1F" label="Aufnehmen · Ep. 2" remaining="noch 9 s" />);
    const bar = screen.getByRole('progressbar', { name: 'Aufnehmen · Ep. 2' });
    expect(bar).toHaveAttribute('aria-valuemin', '0');
    expect(bar).toHaveAttribute('aria-valuemax', '100');
    expect(bar).toHaveAttribute('aria-valuenow', '50');
    expect(bar.style.getPropertyValue('--count')).toBe('30');
    expect(bar.style.getPropertyValue('--h')).toBe('15');
    expect(bar.style.getPropertyValue('--full')).toBe('0');
    expect(bar.style.getPropertyValue('--fade')).toBe('1');
    expect(bar.style.getPropertyValue('--band-c')).toBe('#FF5B1F');
    const leds = container.querySelectorAll('.su-band__led');
    expect(leds).toHaveLength(30);
    expect(leds[7].style.getPropertyValue('--i')).toBe('7');
    expect(container.querySelectorAll('.su-band__head')).toHaveLength(1);
    expect(screen.getByText('noch 9 s')).toHaveClass('su-band__remain');
  });

  it('static frac 1 = full', () => {
    render(<Lichtband count={8} frac={1} label="x" />);
    const bar = screen.getByRole('progressbar');
    expect(bar.style.getPropertyValue('--full')).toBe('1');
    expect(bar.style.getPropertyValue('--h')).toBe('7');
  });

  it('a subscription moves it without a single React render, writing only on a head change', () => {
    const clock = makeClock();
    const renders = vi.fn();
    render(
      <Profiler id="band" onRender={renders}>
        <Lichtband count={10} subscribeFrame={clock.subscribe} label="Aufwärmen" />
      </Profiler>,
    );
    const bar = screen.getByRole('progressbar');
    const rendersAfterMount = renders.mock.calls.length;
    const setProp = vi.spyOn(bar.style, 'setProperty');
    act(() => { for (let i = 0; i <= 100; i += 1) clock.emit(i / 100 * 0.999); });
    expect(renders.mock.calls.length).toBe(rendersAfterMount);
    // 10 head positions (0 … 9): the first emit (h 0) is no change after mount.
    const hWrites = setProp.mock.calls.filter(([k]) => k === '--h').map(([, v]) => v);
    expect(hWrites).toEqual(['1', '2', '3', '4', '5', '6', '7', '8', '9']);
    expect(bar).toHaveAttribute('aria-valuenow', '91'); // written at the first frac of head 9 (0.909…)
    act(() => { clock.emit(1); });
    expect(bar.style.getPropertyValue('--full')).toBe('1');
    expect(bar).toHaveAttribute('aria-valuenow', '100');
    setProp.mockRestore();
  });

  it('unsubscribes on unmount', () => {
    const clock = makeClock();
    const { unmount } = render(<Lichtband subscribeFrame={clock.subscribe} label="x" />);
    expect(clock.size()).toBe(1);
    unmount();
    expect(clock.size()).toBe(0);
  });

  it('reduced: no afterglow and no head', () => {
    render(<Lichtband frac={0.4} reduced label="x" />);
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveClass('su-band--reduced');
    expect(bar.style.getPropertyValue('--fade')).toBe('0');
  });

  it('no label and no remaining: neither is drawn', () => {
    const { container } = render(<Lichtband frac={0.2} />);
    expect(container.querySelector('.su-band__label')).toBeNull();
    expect(container.querySelector('.su-band__remain')).toBeNull();
  });
});
