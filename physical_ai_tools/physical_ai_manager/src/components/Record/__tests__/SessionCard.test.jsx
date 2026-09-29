// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// „Diese Sitzung" (spec §3.10): newest first as given, saved rows numbered,
// discarded rows „–" and greyed, every row with a duration, the chip and the
// sum line; the empty state.

import React from 'react';
import { render, screen } from '@testing-library/react';
import SessionCard from '../SessionCard';

describe('SessionCard', () => {
  it('shows the empty sentence before the first episode', () => {
    render(<SessionCard title="Diese Sitzung" chip={{ text: '0 gespeichert' }} emptyText="Noch keine Episode in dieser Sitzung." sum={{ left: '0 von 5 Episoden', right: 'Gesamt 0:00 min' }} />);
    expect(screen.getByRole('heading', { name: 'Diese Sitzung' })).toBeInTheDocument();
    expect(screen.getByText('Noch keine Episode in dieser Sitzung.')).toBeInTheDocument();
    expect(screen.getByText('0 gespeichert')).toBeInTheDocument();
  });

  it('rows in the given order: saved numbered, discarded „–", each with its duration', () => {
    const rows = [
      { key: 'r3', num: '2', title: 'Episode 2', sub: 'Gespeichert (vorzeitig) · 10:14', duration: '0:08', outcome: 'saved', isNew: true },
      { key: 'r2', num: '–', title: 'Wiederholt, verworfen', sub: 'Episode 2 · 10:13', duration: '0:04', outcome: 'redo', discarded: true },
      { key: 'r1', num: '1', title: 'Episode 1', sub: 'Gespeichert · 10:12', duration: '0:20', outcome: 'saved' },
    ];
    render(<SessionCard title="Diese Sitzung" chip={{ text: '2 gespeichert', ok: true }} rows={rows} sum={{ left: '2 von 5 Episoden', right: 'Gesamt 0:28 min' }} />);
    const items = screen.getAllByRole('listitem');
    expect(items.map((li) => li.getAttribute('data-outcome'))).toEqual(['saved', 'redo', 'saved']);
    expect(items[0]).toHaveTextContent('Episode 2');
    expect(items[0]).toHaveTextContent('0:08');
    expect(items[0]).toHaveClass('newrow');
    expect(items[1]).toHaveClass('x');
    expect(items[1]).toHaveTextContent('–');
    expect(items[1]).toHaveTextContent('0:04');
    expect(screen.getByText('2 gespeichert')).toHaveClass('ok');
    expect(screen.getByText('2 von 5 Episoden')).toBeInTheDocument();
    expect(screen.getByText('Gesamt 0:28 min')).toBeInTheDocument();
  });

  it('shows the note about episodes saved before a reload', () => {
    render(<SessionCard title="Diese Sitzung" note="3 Episoden wurden vor dem Neuladen gespeichert." emptyText="—" />);
    expect(screen.getByText('3 Episoden wurden vor dem Neuladen gespeichert.')).toBeInTheDocument();
  });
});
