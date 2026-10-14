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
import { fireEvent, render, screen } from '@testing-library/react';
import Segmented from '../Segmented';

const OPTS = [
  { value: 'cams', label: 'Kameras' },
  { value: '3d', label: '3D', icon: 'rotate3d' },
  { value: 'both', label: 'Beides', disabled: true, title: 'Nur mit zwei Kameras' },
];

describe('Segmented', () => {
  it('one labelled group of pressed-state buttons', () => {
    render(<Segmented label="Ansicht" value="cams" options={OPTS} onChange={() => {}} />);
    const group = screen.getByRole('group', { name: 'Ansicht' });
    expect(group).toHaveClass('su-seg', 'su-seg--light', 'su-seg--sm');
    expect(screen.getByRole('button', { name: 'Kameras' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '3D' })).toHaveAttribute('aria-pressed', 'false');
    const both = screen.getByRole('button', { name: 'Beides' });
    expect(both).toBeDisabled();
    expect(both).toHaveAttribute('title', 'Nur mit zwei Kameras');
    expect(screen.getByRole('button', { name: '3D' }).querySelector('svg[data-icon="rotate3d"]')).not.toBeNull();
  });

  it('onChange only when the value differs; pointer focus released, keyboard kept', () => {
    const onChange = vi.fn();
    render(<Segmented label="Ansicht" value="cams" options={OPTS} onChange={onChange} />);
    const cams = screen.getByRole('button', { name: 'Kameras' });
    cams.focus();
    fireEvent.click(cams, { detail: 1 });
    expect(onChange).not.toHaveBeenCalled();
    expect(cams).not.toHaveFocus();
    const d3 = screen.getByRole('button', { name: '3D' });
    d3.focus();
    fireEvent.click(d3, { detail: 0 });
    expect(onChange).toHaveBeenCalledWith('3d');
    expect(d3).toHaveFocus();
  });

  it.each([['dark', false, 'su-seg--dark'], ['glass', false, 'su-seg--glass'], ['light', true, 'su-seg--accent']])(
    'tone %s accent %s', (tone, accent, cls) => {
      render(<Segmented label="x" value="cams" options={OPTS} tone={tone} accent={accent} size="md" />);
      const g = screen.getByRole('group', { name: 'x' });
      expect(g).toHaveClass(cls, 'su-seg--md');
    },
  );

  it('accent only on the light tone', () => {
    render(<Segmented label="x" value="cams" options={OPTS} tone="dark" accent />);
    expect(screen.getByRole('group', { name: 'x' })).not.toHaveClass('su-seg--accent');
  });
});
