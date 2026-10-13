// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

/* eslint-disable testing-library/no-container, testing-library/no-node-access --
   these tests pin markup: hook classes, custom properties and the DOM shape. */

import React from 'react';
import { render, screen } from '@testing-library/react';
import Plate from '../Plate';
import Screen from '../Screen';
import Led from '../Led';
import BrandMark from '../BrandMark';

describe('Plate', () => {
  it('is a section with the tone class by default and passes props through', () => {
    render(<Plate aria-label="Platte" className="p-4" data-x="1">Inhalt</Plate>);
    const el = screen.getByRole('region', { name: 'Platte' });
    expect(el.tagName).toBe('SECTION');
    expect(el).toHaveClass('su-plate', 'su-plate--plate', 'p-4');
    expect(el).toHaveAttribute('data-x', '1');
    expect(el).toHaveTextContent('Inhalt');
  });

  it.each(['paper', 'well'])('tone %s', (tone) => {
    const { container } = render(<Plate as="div" tone={tone}>x</Plate>);
    expect(container.firstChild.tagName).toBe('DIV');
    expect(container.firstChild).toHaveClass(`su-plate--${tone}`);
  });

  it('an unknown tone falls back to plate', () => {
    const { container } = render(<Plate tone="neon">x</Plate>);
    expect(container.firstChild).toHaveClass('su-plate--plate');
  });
});

describe('Screen', () => {
  it('full bezel by default, no power', () => {
    const { container } = render(<Screen>x</Screen>);
    expect(container.firstChild).toHaveClass('su-screen', 'su-screen--full');
    expect(container.firstChild).not.toHaveClass('su-screen--power');
  });

  it('thin / none bezels', () => {
    const { container, rerender } = render(<Screen bezel="thin">x</Screen>);
    expect(container.firstChild).toHaveClass('su-screen--thin');
    rerender(<Screen bezel="none">x</Screen>);
    expect(container.firstChild).toHaveClass('su-screen--none');
  });

  it('power plays once on mount: a later change of the prop does not add or remove it', () => {
    const { container, rerender } = render(<Screen power>x</Screen>);
    expect(container.firstChild).toHaveClass('su-screen--power');
    rerender(<Screen power={false}>x</Screen>);
    expect(container.firstChild).toHaveClass('su-screen--power');
    const view = render(<Screen>y</Screen>);
    view.rerender(<Screen power>y</Screen>);
    expect(view.container.firstChild).not.toHaveClass('su-screen--power');
  });
});

describe('Led', () => {
  it('is decorative without a label', () => {
    const { container } = render(<Led tone="ok" />);
    const el = container.firstChild;
    expect(el).toHaveAttribute('aria-hidden', 'true');
    expect(el).not.toHaveAttribute('role');
    expect(el).toHaveClass('su-led', 'su-led--ok', 'su-led--none');
    expect(el.style.width).toBe('9px');
  });

  it('with a label it is an image with that name', () => {
    render(<Led tone="rec" pulse="blink" size={12} label="Aufnahme läuft" />);
    const el = screen.getByRole('img', { name: 'Aufnahme läuft' });
    expect(el).toHaveClass('su-led--rec', 'su-led--blink');
    expect(el.style.height).toBe('12px');
  });

  it('unknown tone and pulse fall back to off / none', () => {
    const { container } = render(<Led tone="pink" pulse="disco" />);
    expect(container.firstChild).toHaveClass('su-led--off', 'su-led--none');
  });

  it('breathe', () => {
    const { container } = render(<Led tone="sig" pulse="breathe" />);
    expect(container.firstChild).toHaveClass('su-led--sig', 'su-led--breathe');
  });
});

describe('BrandMark', () => {
  it('is one image named EduBotics with four bars', () => {
    render(<BrandMark size={44} />);
    const mark = screen.getByRole('img', { name: 'EduBotics' });
    expect(mark.style.width).toBe('44px');
    expect(mark.style.getPropertyValue('--s')).toBe('44px');
    const bars = mark.querySelectorAll('.su-brand__bar');
    expect(bars).toHaveLength(4);
    expect([...bars].map((b) => b.style.height)).toEqual(['30%', '45%', '62%', '80%']);
    expect(mark.textContent).toBe('');
  });
});
