// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

/* eslint-disable testing-library/no-container, testing-library/no-node-access --
   these tests pin markup: hook classes, custom properties and the DOM shape. */

// Readout + the rolling digits (M5, F16): only a plain integer on BOTH the old
// and the new value rolls, and only after mount; the digit columns are
// aria-hidden and one visually hidden node carries the real value; at rest it
// is the bare text again; reduced motion jumps.

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import Readout from '../Readout';
import RollingDigits, { isPlainInteger } from '../RollingDigits';

const cols = (container) => container.querySelectorAll('.su-roll__col');

describe('isPlainInteger', () => {
  it.each([[0, true], [12, true], ['12', true], ['007', true], [1.5, false], [-1, false], ['12,4 GB', false],
    ['3 / 5', false], ['—', false], ['…', false], ['', false], [null, false], [undefined, false], [NaN, false]])(
    '%j → %s', (v, ok) => { expect(isPlainInteger(v)).toBe(ok); },
  );
});

describe('Readout', () => {
  it('a span with size and tone classes, upper case only by CSS (the text is unchanged)', () => {
    const { container } = render(<Readout size="md" tone="sig">Verbunden</Readout>);
    const el = container.firstChild;
    expect(el.tagName).toBe('SPAN');
    expect(el).toHaveClass('su-read', 'su-read--md', 'su-read--sig');
    expect(el).toHaveTextContent('Verbunden');
  });

  it('led puts a decorative light before the text; label adds a caption after the box', () => {
    const { container } = render(<Readout led="ok" ledPulse="breathe" label="Aufnahmen" size="xl" as="div">12</Readout>);
    const root = container.firstChild;
    expect(root.tagName).toBe('DIV');
    expect(root).toHaveClass('su-readout');
    const box = root.querySelector('.su-read');
    expect(box).toHaveClass('su-read--xl');
    expect(box.firstChild).toHaveClass('su-led', 'su-led--ok', 'su-led--breathe');
    expect(root.querySelector('.su-readout__label')).toHaveTextContent('Aufnahmen');
  });

  it('without roll a change is just new text', () => {
    const { container, rerender } = render(<Readout>3</Readout>);
    rerender(<Readout>4</Readout>);
    expect(cols(container)).toHaveLength(0);
    expect(container.firstChild).toHaveTextContent(/^4$/);
  });

  describe('roll', () => {
    beforeEach(() => { vi.useFakeTimers(); });
    afterEach(() => { vi.useRealTimers(); });

    it('mount shows the bare value, no columns', () => {
      const { container } = render(<Readout roll>12</Readout>);
      expect(cols(container)).toHaveLength(0);
      expect(container.firstChild).toHaveTextContent(/^12$/);
    });

    it('integer → integer rolls: aria-hidden columns, one sr value, then the bare text again', () => {
      const { container, rerender } = render(<Readout roll>{9}</Readout>);
      rerender(<Readout roll>{10}</Readout>);
      const c = cols(container);
      expect(c).toHaveLength(2);
      expect(container.querySelector('.su-roll__cols')).toHaveAttribute('aria-hidden', 'true');
      expect(container.querySelector('.su-sr')).toHaveTextContent(/^10$/);
      // getByText finds the real value only (a strip is one text node „0123456789").
      expect(screen.getByText('10')).toHaveClass('su-sr');
      // units 9 → 0, tens new (0 → 1); right → left 40 ms apart
      expect(c[1].style.getPropertyValue('--from')).toBe('9');
      expect(c[1].style.getPropertyValue('--to')).toBe('0');
      expect(c[1].style.getPropertyValue('--delay')).toBe('0ms');
      expect(c[0].style.getPropertyValue('--to')).toBe('1');
      expect(c[0]).toHaveClass('su-roll__col--new');
      expect(c[0].style.getPropertyValue('--delay')).toBe('40ms');
      expect(c[0].style.getPropertyValue('--dur')).toBe('520ms');
      act(() => { vi.advanceTimersByTime(1000); });
      expect(cols(container)).toHaveLength(0);
      expect(container.firstChild).toHaveTextContent(/^10$/);
    });

    it('a digit string rolls too', () => {
      const { container, rerender } = render(<Readout roll>{'3'}</Readout>);
      rerender(<Readout roll>{'4'}</Readout>);
      expect(cols(container)).toHaveLength(1);
    });

    it.each([['…', '12'], ['12', '—'], ['3 / 5', '4 / 5'], ['12,4 GB', '12,5 GB']])(
      '%j → %j never rolls', (a, b) => {
        const { container, rerender } = render(<Readout roll>{a}</Readout>);
        rerender(<Readout roll>{b}</Readout>);
        expect(cols(container)).toHaveLength(0);
        expect(container.firstChild).toHaveTextContent(b);
      },
    );

    it('the same value again does not roll', () => {
      const { container, rerender } = render(<Readout roll>5</Readout>);
      rerender(<Readout roll>5</Readout>);
      expect(cols(container)).toHaveLength(0);
    });
  });
});

describe('RollingDigits', () => {
  it('reduced motion jumps', () => {
    const { container, rerender } = render(<span><RollingDigits value={1} reduced /></span>);
    rerender(<span><RollingDigits value={2} reduced /></span>);
    expect(cols(container)).toHaveLength(0);
    expect(container.firstChild).toHaveTextContent(/^2$/);
  });

  it('at rest it is a bare text node (no wrapper element)', () => {
    const { container } = render(<span><RollingDigits value={42} /></span>);
    expect(container.firstChild.childNodes).toHaveLength(1);
    expect(container.firstChild.firstChild.nodeType).toBe(3);
  });

  it('a new change while rolling restarts from the value shown last', () => {
    vi.useFakeTimers();
    try {
      const { container, rerender } = render(<span><RollingDigits value={1} durationMs={360} /></span>);
      rerender(<span><RollingDigits value={2} durationMs={360} /></span>);
      rerender(<span><RollingDigits value={3} durationMs={360} /></span>);
      const c = cols(container);
      expect(c).toHaveLength(1);
      expect(c[0].style.getPropertyValue('--from')).toBe('2');
      expect(c[0].style.getPropertyValue('--to')).toBe('3');
      expect(c[0].style.getPropertyValue('--dur')).toBe('360ms');
      act(() => { vi.advanceTimersByTime(500); });
      expect(cols(container)).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
