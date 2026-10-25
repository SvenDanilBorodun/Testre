// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Aufwärmen / Zurücksetzen overlay (spec §3.5, F6d): which episode it names,
// that the ring follows the frame clock, that the last three seconds pop, that
// reduced motion steps the ring per second without the pop, that a 0-s phase
// draws nothing, and that „Los!" appears only on a RECORDING entry seen while
// mounted.

import React from 'react';
import { render, screen, act } from '@testing-library/react';
import PhaseOverlay, { overlayText, RING_CIRCUMFERENCE, LOS_MS } from '../PhaseOverlay';
import { PHASE_PICTURES } from '../../icons/robotAssets';

const COPY = {
  warmKicker: 'Vor Episode 1',
  warmTitle: 'Aufwärmen',
  warmLine: 'Nimm den Leader-Arm in die Hand. Gleich startet Episode 1.',
  resetKicker: (n) => `Nach Episode ${n}`,
  resetTitle: 'Zurücksetzen',
  resetLine: 'Stell alles wieder so hin wie am Anfang: Gegenstände an den Start, Leader-Arm in die Ausgangslage.',
  next: (n, N, E) => ({ pre: 'Als Nächstes: ', bold: `Episode ${n} von ${N}`, post: ` · ${E} s Aufnahme` }),
  pictoLeader: 'Leader-Arm',
  pictoFollower: 'Follower-Arm',
  pictoBack: 'Ausgangslage',
};

function frameClock() {
  const subs = new Set();
  return {
    subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); },
    emit: (frac) => subs.forEach((fn) => fn({ frac })),
    count: () => subs.size,
  };
}

const ring = () => screen.getByTestId('rec-ring');
const overlay = () => screen.getByTestId('rec-phase-overlay');

describe('overlayText — the F6d wording rule', () => {
  it('warm-up is always before episode 1 and names episode 1 next', () => {
    const t = overlayText('warmup', { saved: 0, total: 5, episodeTime: 20 }, COPY);
    expect(t.kicker).toBe('Vor Episode 1');
    expect(t.title).toBe('Aufwärmen');
    expect(t.next).toEqual({ pre: 'Als Nächstes: ', bold: 'Episode 1 von 5', post: ' · 20 s Aufnahme' });
    expect(t.picto).toBe('Leader-Arm');
    expect(t.pictoFollower).toBe('Follower-Arm');
  });

  it('reset names the episode just saved and the NEXT one', () => {
    const t = overlayText('reset', { saved: 2, total: 5, episodeTime: 12 }, COPY);
    expect(t.kicker).toBe('Nach Episode 2');
    expect(t.next.bold).toBe('Episode 3 von 5');
    expect(t.picto).toBe('Ausgangslage');
    expect(t.pictoFollower).toBeUndefined();
  });

  it('a reset with nothing saved yet (a redo in episode 1) says „Vor Episode 1"', () => {
    const t = overlayText('reset', { saved: 0, total: 3, episodeTime: 10 }, COPY);
    expect(t.kicker).toBe('Vor Episode 1');
    expect(t.next.bold).toBe('Episode 1 von 3');
  });

  it('no text for any other phase', () => {
    expect(overlayText('record', { saved: 1, total: 3 }, COPY)).toBeNull();
    expect(overlayText(null, {}, COPY)).toBeNull();
  });
});

describe('PhaseOverlay', () => {
  const warm = overlayText('warmup', { saved: 0, total: 5, episodeTime: 20 }, COPY);
  const reset = overlayText('reset', { saved: 1, total: 5, episodeTime: 20 }, COPY);

  it('warm-up: ring, halos, the real-robot picture, the kicker and the next line', () => {
    const clock = frameClock();
    render(<PhaseOverlay phase="warmup" text={warm} totalS={5} secondsLeft={5} subscribeFrame={clock.subscribe} profile="omx_full" />);
    expect(overlay()).toHaveClass('show');
    expect(overlay()).toHaveAttribute('data-phase', 'warmup');
    expect(screen.getByText('Vor Episode 1')).toBeInTheDocument();
    expect(screen.getByText('Aufwärmen')).toBeInTheDocument();
    expect(screen.getByText('Episode 1 von 5')).toBeInTheDocument();
    expect(screen.getByText('Leader-Arm')).toBeInTheDocument();
    expect(screen.getByText('Follower-Arm')).toBeInTheDocument();
    expect(screen.getByTestId('rec-halo')).toBeInTheDocument();
    expect(screen.queryByTestId('rec-orbit')).toBeNull();
    expect(screen.getByTestId('rec-num')).toHaveTextContent('5s');
  });

  it('reset: orbit instead of halos, the way-home picture, „Nach Episode n"', () => {
    render(<PhaseOverlay phase="reset" text={reset} totalS={6} secondsLeft={6} profile="omx_full" />);
    expect(screen.getByText('Nach Episode 1')).toBeInTheDocument();
    expect(screen.getByText('Episode 2 von 5')).toBeInTheDocument();
    expect(screen.getByText('Ausgangslage')).toBeInTheDocument();
    expect(screen.getByTestId('rec-orbit')).toBeInTheDocument();
    expect(screen.queryByTestId('rec-halo')).toBeNull();
  });

  describe('the picture is the REAL robot the student started with', () => {
    const imgOf = () => document.querySelector('[data-picto] img'); // eslint-disable-line testing-library/no-node-access

    it('warm-up shows the shipped strip of the omx_full profile, pinned to its arms', () => {
      render(<PhaseOverlay phase="warmup" text={warm} totalS={5} secondsLeft={5} profile="omx_full" />);
      const box = document.querySelector('[data-picto="warmup"]'); // eslint-disable-line testing-library/no-node-access
      expect(box).toHaveAttribute('data-robot-picture', PHASE_PICTURES.omx_full.warmup.file);
      expect(imgOf().getAttribute('src')).toBe(`/robots/${PHASE_PICTURES.omx_full.warmup.file}`);
      const leader = screen.getByText('Leader-Arm');
      expect(leader.style.left).toBe(`${PHASE_PICTURES.omx_full.warmup.anchors.leader[0] * 100}%`);
    });

    it('warm-up is one still (nothing moves) with a hand that takes the Leader\'s handle', () => {
      render(<PhaseOverlay phase="warmup" text={warm} totalS={5} secondsLeft={5} profile="omx_full" />);
      expect(PHASE_PICTURES.omx_full.warmup.frames).toBe(1);
      const handle = screen.getByTestId('rec-pic-handle');
      const { handle: anchor } = PHASE_PICTURES.omx_full.warmup.anchors;
      expect(handle.style.left).toBe(`${anchor[0] * 100}%`);
      expect(handle.style.top).toBe(`${anchor[1] * 100}%`);
      // the open hand, the closed hand and the ring
      expect(handle.querySelectorAll('svg')).toHaveLength(2); // eslint-disable-line testing-library/no-node-access
      expect(handle.querySelector('.rec-pic-ring')).not.toBeNull(); // eslint-disable-line testing-library/no-node-access
    });

    it('reset shows its own strip, with the „Ausgangslage" chip where the arms end and no object, no hand', () => {
      render(<PhaseOverlay phase="reset" text={reset} totalS={6} secondsLeft={6} profile="omx_full" />);
      expect(imgOf().getAttribute('src')).toBe(`/robots/${PHASE_PICTURES.omx_full.reset.file}`);
      const chip = screen.getByText('Ausgangslage');
      expect(chip.style.top).toBe(`${PHASE_PICTURES.omx_full.reset.anchors.home[1] * 100}%`);
      expect(chip).toHaveClass('rec-pic-chip--arrive'); // it appears when the arms have arrived
      expect(screen.queryByText('zurück zum Start')).toBeNull();
      expect(screen.queryByTestId('rec-pic-handle')).toBeNull();
      expect(PHASE_PICTURES.omx_full.reset.anchors.mark).toBeUndefined(); // the cube and its mark are gone
    });

    it.each([null, undefined, '', 'omx_f', 'v1_studio', 'omx_follower'])(
      'a profile with no picture (%j) draws NO robot — no OMX stand-in, no chips',
      (profile) => {
        render(<PhaseOverlay phase="warmup" text={warm} totalS={5} secondsLeft={5} profile={profile} />);
        expect(document.querySelector('[data-picto]')).toBeNull(); // eslint-disable-line testing-library/no-node-access
        expect(screen.queryByText('Leader-Arm')).toBeNull();
        // The overlay's own words stay.
        expect(screen.getByText('Aufwärmen')).toBeInTheDocument();
      }
    );

    it('the strip is not requested before the overlay is first shown, then stays while it fades', () => {
      const { rerender } = render(<PhaseOverlay phase={null} text={null} totalS={0} secondsLeft={0} profile="omx_full" />);
      expect(imgOf()).toBeNull();
      rerender(<PhaseOverlay phase="reset" text={reset} totalS={6} secondsLeft={3} profile="omx_full" />);
      expect(imgOf()).not.toBeNull();
      rerender(<PhaseOverlay phase={null} text={null} totalS={0} secondsLeft={0} profile="omx_full" />);
      expect(imgOf()).not.toBeNull();
    });

    it('is decorative: hidden from assistive technology, never draggable', () => {
      render(<PhaseOverlay phase="warmup" text={warm} totalS={5} secondsLeft={5} profile="omx_full" />);
      expect(document.querySelector('[data-picto]')).toHaveAttribute('aria-hidden', 'true'); // eslint-disable-line testing-library/no-node-access
      expect(imgOf()).toHaveAttribute('alt', '');
      expect(imgOf()).toHaveAttribute('draggable', 'false');
    });
  });

  it('the ring follows the frame clock, without React re-rendering', () => {
    const clock = frameClock();
    render(<PhaseOverlay phase="warmup" text={warm} totalS={5} secondsLeft={4} subscribeFrame={clock.subscribe} />);
    expect(clock.count()).toBe(1);
    act(() => clock.emit(0.25));
    expect(Number(ring().style.strokeDashoffset)).toBeCloseTo(RING_CIRCUMFERENCE * 0.25, 0);
    act(() => clock.emit(1));
    expect(Number(ring().style.strokeDashoffset)).toBeCloseTo(RING_CIRCUMFERENCE, 0);
  });

  it('the last three seconds pop with each new number; earlier ones do not', () => {
    const { rerender } = render(<PhaseOverlay phase="reset" text={reset} totalS={6} secondsLeft={4} />);
    expect(screen.getByTestId('rec-num')).not.toHaveClass('pop');
    rerender(<PhaseOverlay phase="reset" text={reset} totalS={6} secondsLeft={3} />);
    expect(screen.getByTestId('rec-num')).toHaveClass('pop');
    expect(screen.getByTestId('rec-num')).toHaveTextContent('3s');
    rerender(<PhaseOverlay phase="reset" text={reset} totalS={6} secondsLeft={1} />);
    expect(screen.getByTestId('rec-num')).toHaveClass('pop');
  });

  it('reduced motion: no pop, and the ring steps once per second (no frame subscription)', () => {
    const clock = frameClock();
    const { rerender } = render(
      <PhaseOverlay phase="warmup" text={warm} totalS={4} secondsLeft={3} subscribeFrame={clock.subscribe} reducedMotion />
    );
    expect(clock.count()).toBe(0);
    expect(screen.getByTestId('rec-num')).not.toHaveClass('pop');
    expect(Number(ring().style.strokeDashoffset)).toBeCloseTo(RING_CIRCUMFERENCE * 0.25, 0);
    rerender(<PhaseOverlay phase="warmup" text={warm} totalS={4} secondsLeft={2} subscribeFrame={clock.subscribe} reducedMotion />);
    expect(Number(ring().style.strokeDashoffset)).toBeCloseTo(RING_CIRCUMFERENCE * 0.5, 0);
  });

  it('a phase of 0 s gets no overlay', () => {
    render(<PhaseOverlay phase="reset" text={reset} totalS={0} secondsLeft={0} />);
    expect(overlay()).not.toHaveClass('show');
    expect(overlay()).toHaveAttribute('data-phase', 'none');
  });

  it('an ending overlay fades out still showing what it showed', () => {
    const { rerender } = render(<PhaseOverlay phase="reset" text={reset} totalS={6} secondsLeft={1} />);
    rerender(<PhaseOverlay phase={null} text={null} totalS={0} secondsLeft={0} recording />);
    expect(overlay()).not.toHaveClass('show');
    expect(screen.getByText('Zurücksetzen')).toBeInTheDocument();
  });

  describe('„Los!"', () => {
    beforeEach(() => { vi.useFakeTimers(); });
    afterEach(() => { vi.useRealTimers(); });

    it('shows on a RECORDING entry observed while mounted, for LOS_MS', () => {
      const { rerender } = render(<PhaseOverlay phase="warmup" text={warm} totalS={5} secondsLeft={1} losText="Los!" />);
      expect(screen.queryByTestId('rec-los')).toBeNull();
      rerender(<PhaseOverlay phase={null} text={null} recording losText="Los!" />);
      expect(screen.getByTestId('rec-los')).toHaveTextContent('Los!');
      act(() => { vi.advanceTimersByTime(LOS_MS + 10); });
      expect(screen.queryByTestId('rec-los')).toBeNull();
    });

    it('never on a page opened in the middle of a recording', () => {
      render(<PhaseOverlay phase={null} text={null} recording losText="Los!" />);
      expect(screen.queryByTestId('rec-los')).toBeNull();
    });
  });
});
