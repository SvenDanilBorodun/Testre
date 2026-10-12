// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The waiting card (CloudWait.jsx): what a student reads while the cloud GPU
// starts (kind 'gpu') and while the model loads (kind 'model'). Pinned: the
// German texts per kind; the elapsed time as mm:ss, ticking ONCE per second
// (one timer, no animation frame) and counted from the given start; the retry
// line only from attempt 2 on; the long-wait line only after its threshold;
// reduced motion; one polite live region that never holds the clock; the
// timer gone after unmount; no percentage anywhere.

import React from 'react';
import fs from 'fs';
import path from 'path';
import { act, render, screen } from '@testing-library/react';
import CloudWait, {
  GPU_LONG_WAIT_S,
  MODEL_LONG_WAIT_S,
  formatElapsed,
  waitTexts,
} from '../CloudWait';
import { cloudCopy, fill } from '../cloudCopy';

const T0 = new Date('2026-10-12T10:00:00Z').getTime();

function mockMatchMedia(matches) {
  const listeners = new Set();
  const mq = {
    matches,
    media: '(prefers-reduced-motion: reduce)',
    addEventListener: (_type, fn) => listeners.add(fn),
    removeEventListener: (_type, fn) => listeners.delete(fn),
  };
  window.matchMedia = vi.fn(() => mq);
  return {
    mq,
    listeners,
    change(next) {
      mq.matches = next;
      listeners.forEach((fn) => fn({ matches: next }));
    },
  };
}

const elapsed = () => screen.getByTestId('cloud-wait-elapsed');

let savedMatchMedia;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  savedMatchMedia = window.matchMedia;
  delete window.matchMedia;
});

afterEach(() => {
  vi.useRealTimers();
  if (savedMatchMedia) window.matchMedia = savedMatchMedia;
  else delete window.matchMedia;
});

describe('formatElapsed', () => {
  it('is mm:ss, never negative, never NaN', () => {
    expect(formatElapsed(0)).toBe('00:00');
    expect(formatElapsed(9)).toBe('00:09');
    expect(formatElapsed(42)).toBe('00:42');
    expect(formatElapsed(114)).toBe('01:54');
    expect(formatElapsed(461)).toBe('07:41');
    expect(formatElapsed(3725)).toBe('62:05');
    expect(formatElapsed(-3)).toBe('00:00');
    expect(formatElapsed(Number.NaN)).toBe('00:00');
    expect(formatElapsed(undefined)).toBe('00:00');
  });
});

describe('the texts per waiting state', () => {
  it('the GPU start says what happens, that it is normal, that it can take minutes, and to stay', () => {
    render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    const region = screen.getByRole('status');
    expect(region).toHaveTextContent('Cloud-GPU wird gestartet …');
    expect(region).toHaveTextContent('Beim ersten Start kann das einige Minuten dauern.');
    expect(region).toHaveTextContent('Ein Rechner in einem EU-Rechenzentrum wird für dein Modell eingeschaltet.');
    expect(region).toHaveTextContent('Das ist normal. Bleib auf dieser Seite – wenn du sie verlässt, wird der Start abgebrochen.');
    expect(screen.getByText(cloudCopy.panel.preparing)).toBeInTheDocument();
    expect(screen.getByText(cloudCopy.panel.preparingSub)).toBeInTheDocument();
  });

  it('the model load says the model is loading and that the arm then moves by itself', () => {
    render(<CloudWait kind="model" tone="dark" />);
    const region = screen.getByRole('status');
    expect(region).toHaveTextContent(cloudCopy.status.loading);
    expect(region).toHaveTextContent(cloudCopy.status.loadingSub);
    expect(region).toHaveTextContent('Das dauert meist nur einige Sekunden.');
    expect(region).toHaveTextContent('Sobald das Modell bereit ist, bewegt sich der Arm von selbst.');
    expect(region).not.toHaveTextContent('Bleib auf dieser Seite');
  });

  it('every sentence is German with real umlauts, and none claims a percentage', () => {
    const all = [...Object.values(waitTexts('gpu')), ...Object.values(waitTexts('model')), ...Object.values(cloudCopy.waiting)]
      .filter((v) => typeof v === 'string');
    expect(all.length).toBeGreaterThan(8);
    all.forEach((text) => {
      expect(text).not.toMatch(/%|Prozent/);
      expect(text).not.toMatch(/\b(fuer|laenger|verlaesst|waehrend|moeglich|ueber|Schueler)\b/i);
    });
  });

  it('an unknown kind falls back to the GPU start, never to an empty card', () => {
    render(<CloudWait kind="nonsense" />);
    expect(screen.getByRole('status')).toHaveTextContent(cloudCopy.panel.preparing);
  });
});

describe('the elapsed time', () => {
  it('starts at the given start and ticks exactly once per second', () => {
    render(<CloudWait kind="gpu" startedAt={T0 - 5000} attempt={1} attempts={3} />);
    expect(elapsed()).toHaveTextContent('00:05');
    act(() => { vi.advanceTimersByTime(999); });
    expect(elapsed()).toHaveTextContent('00:05');
    act(() => { vi.advanceTimersByTime(1); });
    expect(elapsed()).toHaveTextContent('00:06');
    act(() => { vi.advanceTimersByTime(60 * 1000); });
    expect(elapsed()).toHaveTextContent('01:06');
  });

  it('a start in the middle of a second ticks on the second boundary, not a second late', () => {
    render(<CloudWait kind="gpu" startedAt={T0 - 1700} attempt={1} attempts={3} />);
    expect(elapsed()).toHaveTextContent('00:01');
    act(() => { vi.advanceTimersByTime(299); });
    expect(elapsed()).toHaveTextContent('00:01');
    act(() => { vi.advanceTimersByTime(1); });
    expect(elapsed()).toHaveTextContent('00:02');
  });

  it('without a start it counts from its own mount', () => {
    render(<CloudWait kind="model" tone="dark" />);
    expect(elapsed()).toHaveTextContent('00:00');
    act(() => { vi.advanceTimersByTime(3000); });
    expect(elapsed()).toHaveTextContent('00:03');
  });

  it('keeps ONE timer and never asks for an animation frame', () => {
    const raf = vi.fn();
    const savedRaf = window.requestAnimationFrame;
    window.requestAnimationFrame = raf;
    try {
      render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
      expect(vi.getTimerCount()).toBe(1);
      act(() => { vi.advanceTimersByTime(10 * 1000); });
      expect(vi.getTimerCount()).toBe(1);
      expect(raf).not.toHaveBeenCalled();
    } finally {
      window.requestAnimationFrame = savedRaf;
    }
  });

  it('wakes up once per second, not more often', () => {
    render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    const spy = vi.spyOn(globalThis, 'setTimeout');
    try {
      act(() => { vi.advanceTimersByTime(10 * 1000); });
      expect(spy).toHaveBeenCalledTimes(10);
    } finally {
      spy.mockRestore();
    }
  });

  it('the clock re-renders only the card, never the page around it', () => {
    const renders = vi.fn();
    function Parent(props) {
      renders();
      return <CloudWait {...props} />;
    }
    render(<Parent kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    expect(elapsed()).toHaveTextContent('00:00');
    act(() => { vi.advanceTimersByTime(5000); });
    expect(elapsed()).toHaveTextContent('00:05');
    expect(renders).toHaveBeenCalledTimes(1);
  });

  it('the timer is gone after unmount', () => {
    const { unmount } = render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('a new start time restarts the count', () => {
    const { rerender } = render(<CloudWait kind="gpu" startedAt={T0 - 30000} attempt={1} attempts={3} />);
    expect(elapsed()).toHaveTextContent('00:30');
    rerender(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    expect(elapsed()).toHaveTextContent('00:00');
    expect(vi.getTimerCount()).toBe(1);
  });
});

describe('the retry line', () => {
  it('is absent on the first attempt', () => {
    render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    expect(screen.queryByTestId('cloud-wait-retry')).toBeNull();
    expect(screen.queryByText(/Versuch/)).toBeNull();
  });

  it('names the attempt from the second one on', () => {
    const { rerender } = render(<CloudWait kind="gpu" startedAt={T0} attempt={2} attempts={3} />);
    const expected2 = fill(cloudCopy.waiting.retry, { n: 2, total: 3 });
    expect(expected2).toBe('Noch nicht geklappt – die Seite versucht es von selbst noch einmal (Versuch 2 von 3).');
    expect(screen.getByTestId('cloud-wait-retry')).toHaveTextContent(expected2);
    rerender(<CloudWait kind="gpu" startedAt={T0} attempt={3} attempts={3} />);
    expect(screen.getByTestId('cloud-wait-retry')).toHaveTextContent('Versuch 3 von 3');
  });

  it('is never shown without a known total, or for the model load', () => {
    const { rerender } = render(<CloudWait kind="gpu" startedAt={T0} attempt={2} />);
    expect(screen.queryByTestId('cloud-wait-retry')).toBeNull();
    rerender(<CloudWait kind="model" attempt={2} attempts={3} />);
    expect(screen.queryByTestId('cloud-wait-retry')).toBeNull();
  });
});

describe('a long wait', () => {
  it('the GPU start: a calm line once the wait passes its threshold', () => {
    render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    expect(screen.queryByTestId('cloud-wait-long')).toBeNull();
    act(() => { vi.advanceTimersByTime(GPU_LONG_WAIT_S * 1000 - 1000); });
    expect(screen.queryByTestId('cloud-wait-long')).toBeNull();
    act(() => { vi.advanceTimersByTime(1000); });
    expect(screen.getByTestId('cloud-wait-long')).toHaveTextContent(
      'Gerade dauert es länger als sonst. Das liegt an der Cloud, nicht an dir.',
    );
  });

  it('the model load: says the robot ends the run itself and tells why', () => {
    render(<CloudWait kind="model" tone="dark" />);
    act(() => { vi.advanceTimersByTime(MODEL_LONG_WAIT_S * 1000); });
    expect(screen.getByTestId('cloud-wait-long')).toHaveTextContent(
      'Gerade dauert es länger als sonst. Klappt es nicht, beendet der Roboter den Lauf von selbst und sagt dir, warum.',
    );
  });
});

describe('accessibility', () => {
  it('one polite live region, without the clock in it', () => {
    render(<CloudWait kind="gpu" startedAt={T0} attempt={2} attempts={3} />);
    const regions = screen.getAllByRole('status');
    expect(regions).toHaveLength(1);
    const [region] = regions;
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toHaveAttribute('aria-atomic', 'false');
    expect(region).toContainElement(screen.getByTestId('cloud-wait-retry'));
    expect(region).not.toContainElement(elapsed());
    // eslint-disable-next-line testing-library/no-node-access -- an ancestor walk is the point
    const liveAncestor = elapsed().closest('[aria-live], [role="status"]');
    expect(liveAncestor).toBeNull();
  });

  it('the picture is decorative', () => {
    render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    expect(screen.getByTestId('cloud-wait-picture')).toHaveAttribute('aria-hidden', 'true');
  });

  it('draws no progress bar and no percentage', () => {
    render(<CloudWait kind="gpu" startedAt={T0} attempt={2} attempts={3} />);
    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(screen.getByTestId('cloud-wait')).not.toHaveTextContent(/%/);
  });
});

describe('reduced motion', () => {
  it('moves by default', () => {
    mockMatchMedia(false);
    render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    const card = screen.getByTestId('cloud-wait');
    expect(card).toHaveAttribute('data-reduced-motion', 'false');
    expect(card).not.toHaveClass('ciw-reduced');
  });

  it('stands still when the system asks for less motion, and follows a change', () => {
    const media = mockMatchMedia(true);
    render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    const card = screen.getByTestId('cloud-wait');
    expect(window.matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)');
    expect(card).toHaveAttribute('data-reduced-motion', 'true');
    expect(card).toHaveClass('ciw-reduced');
    act(() => { media.change(false); });
    expect(card).toHaveAttribute('data-reduced-motion', 'false');
  });

  it('stops listening after unmount', () => {
    const media = mockMatchMedia(false);
    const { unmount } = render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    expect(media.listeners.size).toBe(1);
    unmount();
    expect(media.listeners.size).toBe(0);
  });

  it('a browser without matchMedia still renders (and moves)', () => {
    render(<CloudWait kind="gpu" startedAt={T0} attempt={1} attempts={3} />);
    expect(screen.getByTestId('cloud-wait')).toHaveAttribute('data-reduced-motion', 'false');
  });

  it('the stylesheet stops every animation under prefers-reduced-motion, too', () => {
    const css = fs.readFileSync(path.join(__dirname, '..', 'cloudWait.css'), 'utf8');
    const animated = [...css.matchAll(/\.(ciw-[a-z-]+)\s*\{[^}]*animation:\s*ciw-/g)].map((m) => m[1]);
    expect(animated.length).toBeGreaterThan(0);
    const media = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
    expect(css.indexOf('@media (prefers-reduced-motion: reduce)')).toBeGreaterThan(-1);
    animated.forEach((cls) => {
      expect(media).toContain(`.${cls}`);
      expect(css).toContain(`.ciw-reduced .${cls}`);
    });
    expect(media).toMatch(/animation:\s*none/);
  });
});
