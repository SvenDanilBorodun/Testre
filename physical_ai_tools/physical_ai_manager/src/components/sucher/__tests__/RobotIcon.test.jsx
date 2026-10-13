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
import RobotIcon, { robotSpriteName, robotSpriteUrl } from '../RobotIcon';

describe('robotSpriteName', () => {
  it.each([
    ['omx_full', 'omx_full'], ['omx_follower', 'omx_f'], ['edu6_studio', 'edu6'], ['edu1_studio', 'edu1'],
    ['omx_f', 'omx_f'], ['', 'omx_f'], [undefined, 'omx_f'], ['toString', 'omx_f'],
  ])('%j → %s', (profile, name) => { expect(robotSpriteName(profile)).toBe(name); });

  it('light is the OMX for dark screens', () => {
    expect(robotSpriteName('edu6_studio', true)).toBe('omx_f_light');
    expect(robotSpriteUrl('omx_full')).toBe('/robots/omx_full.webp');
  });
});

describe('RobotIcon', () => {
  it('a lazy, undraggable, alt-less sprite in a square box; presentation without a title', () => {
    const { container } = render(<RobotIcon profile="edu1_studio" size={72} />);
    const box = container.firstChild;
    expect(box).toHaveAttribute('role', 'presentation');
    expect(box).toHaveClass('su-robot');
    expect(box).not.toHaveClass('su-robot--animate');
    expect(box.style.width).toBe('72px');
    expect(box.style.height).toBe('72px');
    const img = box.querySelector('img');
    expect(img).toHaveAttribute('src', '/robots/edu1.webp');
    expect(img).toHaveAttribute('loading', 'lazy');
    expect(img).toHaveAttribute('decoding', 'async');
    expect(img).toHaveAttribute('alt', '');
    expect(img).toHaveAttribute('draggable', 'false');
  });

  it('a title makes it an image with that name; animate and dim set their classes', () => {
    render(<RobotIcon profile="omx_full" title="OMX – Voll" animate dim />);
    const el = screen.getByRole('img', { name: 'OMX – Voll' });
    expect(el).toHaveClass('su-robot--animate', 'su-robot--dim');
  });

  it('reduced motion: never animated (frame 0)', () => {
    const orig = window.matchMedia;
    window.matchMedia = (q) => ({
      matches: q === '(prefers-reduced-motion: reduce)', media: q, addEventListener() {}, removeEventListener() {},
    });
    try {
      const { container } = render(<RobotIcon profile="omx_full" animate />);
      expect(container.firstChild).not.toHaveClass('su-robot--animate');
    } finally {
      window.matchMedia = orig;
    }
  });
});
