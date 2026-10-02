// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The Phasenleiste (spec §3.4 / §3.5): segments by position (done / now /
// upcoming), sized by seconds, the Speichern dot fixed-width, the running one
// filled from the frame clock (not React state), one neutral segment when idle.

import React from 'react';
import { render, screen, act } from '@testing-library/react';
import PhaseTrack, { phaseColor } from '../PhaseTrack';

function frameClock() {
  const subs = new Set();
  return {
    subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); },
    emit: (frac) => subs.forEach((fn) => fn({ frac })),
    count: () => subs.size,
  };
}

const SEGMENTS = [
  { key: 'warmup', kind: 'warmup', label: 'Aufwärmen', weight: 5, state: 'done' },
  { key: 'record', kind: 'record', label: 'Aufnehmen · Ep. 1', weight: 20, state: 'now' },
  { key: 'save', kind: 'save', label: 'Speichern', weight: 0, state: '', dot: true },
  { key: 'reset', kind: 'reset', label: 'Zurücksetzen', weight: 5, state: '' },
];

const segs = () => screen.getByTestId('rec-track').querySelectorAll('.rec-sg'); // eslint-disable-line testing-library/no-node-access

describe('PhaseTrack', () => {
  it('outside a session: one neutral segment with the idle text', () => {
    render(<PhaseTrack ariaLabel="Phasenleiste" segments={[]} idleText="Bereit · Aufwärmen → Aufnehmen → Speichern → Zurücksetzen" />);
    expect(screen.getByRole('group', { name: 'Phasenleiste' })).toBeInTheDocument();
    expect(screen.getByText('Bereit · Aufwärmen → Aufnehmen → Speichern → Zurücksetzen')).toBeInTheDocument();
    expect(segs()).toHaveLength(1);
  });

  it('draws done / now / upcoming by position, sized by seconds, in the phase colours', () => {
    render(<PhaseTrack ariaLabel="Phasenleiste" segments={SEGMENTS} remaining="noch 12 s" />);
    const all = segs();
    expect([...all].map((s) => s.getAttribute('data-state'))).toEqual(['done', 'now', 'upcoming', 'upcoming']);
    expect(all[0].style.flex).toMatch(/^5/);
    expect(all[1].style.flex).toMatch(/^20/);
    expect(all[2]).toHaveClass('dotseg');
    expect(all[1].style.getPropertyValue('--c')).toBe(phaseColor('record'));
    expect(screen.getByText('Aufnehmen · Ep. 1')).toBeInTheDocument();
    expect(screen.getByText('noch 12 s')).toBeInTheDocument();
  });

  it('the running segment fills from the frame clock', () => {
    const clock = frameClock();
    render(<PhaseTrack ariaLabel="Phasenleiste" segments={SEGMENTS} subscribeFrame={clock.subscribe} />);
    expect(clock.count()).toBe(1);
    act(() => clock.emit(0.4));
    const fill = segs()[1].querySelector('.rec-sg-fill'); // eslint-disable-line testing-library/no-node-access
    const head = segs()[1].querySelector('.rec-sg-head'); // eslint-disable-line testing-library/no-node-access
    expect(fill.style.width).toBe('40%');
    expect(head.style.left).toBe('calc(40% - 1px)');
  });

  it('while saving the Speichern dot is the running one, half full, with no clock', () => {
    const clock = frameClock();
    const saving = SEGMENTS.map((s) => ({ ...s, state: s.key === 'save' ? 'now' : s.key === 'reset' ? '' : 'done' }));
    render(<PhaseTrack ariaLabel="Phasenleiste" segments={saving} subscribeFrame={clock.subscribe} remaining="speichert" />);
    expect(clock.count()).toBe(0);
    const fill = segs()[2].querySelector('.rec-sg-fill'); // eslint-disable-line testing-library/no-node-access
    expect(fill.style.width).toBe('50%');
    expect(screen.getByText('speichert')).toBeInTheDocument();
  });

  it('a 0-s phase the page left out is simply not there', () => {
    render(<PhaseTrack ariaLabel="Phasenleiste" segments={SEGMENTS.filter((s) => s.kind !== 'warmup')} />);
    expect([...segs()].map((s) => s.getAttribute('data-kind'))).toEqual(['record', 'save', 'reset']);
  });
});
