// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// EbUI is shared with the teacher web build (spec §3.4): every component keeps
// its Tailwind classes and gains stable hook classes the student „Sucher" look
// restyles under :root[data-ui='sucher']. Btn releases the focus after a
// pointer click in both builds (D11).

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import {
  Avatar, Btn, Card, Divider, Pill, Progress, SectionHeader, Stat, StatBig,
} from '../../EbUI';

describe('EbUI hook classes', () => {
  it('Btn: hooks beside every Tailwind class it had', () => {
    render(<Btn variant="primary" size="lg">Starten</Btn>);
    const b = screen.getByRole('button', { name: 'Starten' });
    expect(b).toHaveClass('eb-btn', 'eb-btn--primary', 'eb-btn--lg');
    expect(b).toHaveClass('inline-flex', 'h-12', 'bg-[var(--accent)]', 'text-white', 'disabled:opacity-50');
  });

  it('Btn: a pointer click releases the focus and then calls the handler; keyboard keeps it', () => {
    const order = [];
    const onClick = vi.fn((e) => order.push(document.activeElement === e.currentTarget ? 'focused' : 'released'));
    render(<Btn onClick={onClick}>Weiter</Btn>);
    const b = screen.getByRole('button', { name: 'Weiter' });
    b.focus();
    fireEvent.click(b, { detail: 1 });
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(order).toEqual(['released']);
    expect(b).not.toHaveFocus();
    b.focus();
    fireEvent.click(b, { detail: 0 });
    expect(b).toHaveFocus();
    expect(onClick).toHaveBeenCalledTimes(2);
  });

  it('Btn: without a handler a click still works (submit buttons)', () => {
    const onSubmit = vi.fn((e) => e.preventDefault());
    render(<form onSubmit={onSubmit}><Btn type="submit">Anmelden</Btn></form>);
    fireEvent.click(screen.getByRole('button', { name: 'Anmelden' }), { detail: 1 });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('Card: hooks; titleStyle serif adds its modifier only', () => {
    const { rerender } = render(<Card title="Gesundheit" subtitle="Zustand">x</Card>);
    const h = screen.getByRole('heading', { name: 'Gesundheit' });
    expect(h).toHaveClass('eb-card__title', 'text-sm', 'font-semibold');
    expect(h).not.toHaveClass('eb-card__title--serif');
    // eslint-disable-next-line testing-library/no-node-access
    const card = h.closest('section');
    expect(card).toHaveClass('eb-card', 'bg-white', 'shadow-soft');
    // eslint-disable-next-line testing-library/no-node-access
    expect(card.querySelector('header')).toHaveClass('eb-card__head', 'border-b');
    expect(screen.getByText('Zustand')).toHaveClass('eb-card__sub');
    // eslint-disable-next-line testing-library/no-node-access
    expect(card.querySelector('.eb-card__body')).toHaveClass('p-5');
    rerender(<Card title={<>Deine <em>Arbeit</em></>} titleStyle="serif" padded={false}>x</Card>);
    expect(screen.getByRole('heading', { name: 'Deine Arbeit' })).toHaveClass('eb-card__title--serif');
  });

  it('Pill: tone hooks and the dot hook', () => {
    const { container, rerender } = render(<Pill tone="success" dot>Bereit</Pill>);
    expect(container.firstChild).toHaveClass('eb-pill', 'eb-pill--success', 'rounded-full');
    // eslint-disable-next-line testing-library/no-node-access
    expect(container.querySelector('.eb-pill__dot')).not.toBeNull();
    rerender(<Pill tone="nope">x</Pill>);
    expect(container.firstChild).toHaveClass('eb-pill--neutral');
  });

  it('SectionHeader: size modifier (default xl) and part hooks', () => {
    const { container, rerender } = render(<SectionHeader eyebrow="Startseite" title="Hallo" description="Text" />);
    expect(container.firstChild).toHaveClass('eb-section', 'eb-section--xl');
    expect(screen.getByText('Startseite')).toHaveClass('eb-section__eyebrow', 'font-mono');
    expect(screen.getByRole('heading', { name: 'Hallo' })).toHaveClass('eb-section__title', 'text-[28px]');
    expect(screen.getByText('Text')).toHaveClass('eb-section__desc');
    rerender(<SectionHeader title="Hallo" size="sm" />);
    expect(container.firstChild).toHaveClass('eb-section--sm');
    rerender(<SectionHeader title="Hallo" size="huge" />);
    expect(container.firstChild).toHaveClass('eb-section--xl');
  });

  it('Stat, StatBig, Progress, Divider hooks', () => {
    render(
      <div>
        <Stat label="Credits" value="3" trend="+1" />
        <StatBig label="Episoden" value="12" sub="heute" />
        <Progress pct={40} />
        <Divider />
      </div>,
    );
    expect(screen.getByText('Credits')).toHaveClass('eb-stat__label');
    expect(screen.getByText('3')).toHaveClass('eb-stat__value', 'font-mono');
    expect(screen.getByText('+1')).toHaveClass('eb-stat__trend');
    expect(screen.getByText('12')).toHaveClass('eb-stat__value');
    expect(screen.getByText('heute')).toHaveClass('eb-stat__trend');
    // eslint-disable-next-line testing-library/no-node-access
    expect(document.querySelector('.eb-progress > .eb-progress__fill').style.width).toBe('40%');
    // eslint-disable-next-line testing-library/no-node-access
    expect(document.querySelector('.eb-divider')).toHaveClass('w-px');
  });

  it('Avatar: the background reads --eb-avatar-bg with the old hue as the fallback', () => {
    const { container } = render(<Avatar name="Anna Berg" />);
    const el = container.firstChild;
    expect(el).toHaveClass('eb-avatar', 'text-white');
    expect(el).toHaveTextContent('AB');
    // The hue the web build always drew for this name (hash of the characters).
    const hues = ['#0A7F7A', '#2461C7', '#6A46C7', '#C74660', '#D08A1E', '#2F9E63'];
    const hue = hues['Anna Berg'.split('').reduce((a, c) => a + c.charCodeAt(0), 0) % hues.length];
    expect(el.getAttribute('style')).toContain(`var(--eb-avatar-bg, ${hue})`);
  });
});
