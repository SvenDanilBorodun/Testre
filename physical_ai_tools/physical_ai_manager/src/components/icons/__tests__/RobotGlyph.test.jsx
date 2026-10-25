/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/* eslint-disable testing-library/no-container, testing-library/no-node-access --
   these tests pin markup: the data attribute and the inline mask. */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import RobotGlyph from '../RobotGlyph';
import { GLYPH_ORDER } from '../robotAssets';

describe('RobotGlyph', () => {
  it.each([
    ['omx_full', 'robot', 'omx_full'], ['omx_full', 'arm', 'omx_f'], ['omx_full', 'leader', 'omx_l'],
    ['omx_follower', 'robot', 'omx_f'], ['v1_studio', 'robot', 'v1'], ['v1_studio', 'arm', 'v1'],
  ])('%s / %s draws the %s cell of the sheet, in the text colour', (profile, part, cell) => {
    const { container } = render(<RobotGlyph profile={profile} part={part} size={20} />);
    const el = container.firstChild;
    expect(el).toHaveAttribute('data-robot-glyph', cell);
    expect(el).toHaveClass('robot-glyph');
    expect(el).toHaveAttribute('aria-hidden', 'true');
    expect(el.style.width).toBe('20px');
    expect(el.style.height).toBe('20px');
    // One sheet of GLYPH_ORDER.length cells: four times as wide as the box, the cell's own position.
    const i = GLYPH_ORDER.indexOf(cell);
    expect(el.style.maskImage || el.style.webkitMaskImage).toContain('/robots/glyphs.png');
    expect(el.style.maskSize || el.style.webkitMaskSize).toBe(`${GLYPH_ORDER.length * 100}% 100%`);
    expect(el.style.maskPosition || el.style.webkitMaskPosition).toMatch(new RegExp(`^${(i / (GLYPH_ORDER.length - 1)) * 100}% 0(px)?$`));
  });

  it.each([null, undefined, '', 'omx_f', 'nonsense'])('an unknown profile (%j) renders NOTHING', (profile) => {
    const { container } = render(<RobotGlyph profile={profile} />);
    expect(container.firstChild).toBeNull();
  });

  it('a part the profile does not have renders nothing: no leader on a V1', () => {
    const { container } = render(<RobotGlyph profile="v1_studio" part="leader" />);
    expect(container.firstChild).toBeNull();
  });

  it('a title makes it a named image', () => {
    render(<RobotGlyph profile="v1_studio" title="V1 – Roboter Studio" />);
    const img = screen.getByRole('img', { name: 'V1 – Roboter Studio' });
    expect(img).toHaveAttribute('data-robot-glyph', 'v1');
    expect(img).not.toHaveAttribute('aria-hidden');
  });

  it('takes a class and extra style without losing its mask', () => {
    const { container } = render(<RobotGlyph profile="omx_full" className="text-white" style={{ opacity: 0.5 }} />);
    expect(container.firstChild).toHaveClass('robot-glyph', 'text-white');
    expect(container.firstChild.style.opacity).toBe('0.5');
    expect(container.firstChild.style.maskImage || container.firstChild.style.webkitMaskImage).toContain('glyphs.png');
  });
});
