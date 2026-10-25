// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

/* eslint-disable testing-library/no-container, testing-library/no-node-access --
   these tests pin markup: hook classes, custom properties and the DOM shape. */

import fs from 'fs';
import path from 'path';
import React from 'react';
import { render, screen } from '@testing-library/react';
import RobotIcon, { robotSpriteName, robotSpriteUrl } from '../RobotIcon';

describe('robotSpriteName', () => {
  it.each([
    ['omx_full', 'omx_full'], ['omx_follower', 'omx_f'], ['v1_studio', 'v1'],
    ['omx_f', 'omx_f'],
  ])('%j → %s', (profile, name) => { expect(robotSpriteName(profile)).toBe(name); });

  // An unknown profile draws NO robot: never the OMX as a stand-in (a V1
  // student must not see an OMX before the first status tick). A sprite NAME
  // ('v1') is not a profile id either.
  it.each(['', undefined, null, 'toString', '__proto__', 'v1', 'nonsense', 42])('%j → no sprite', (profile) => {
    expect(robotSpriteName(profile)).toBeNull();
    expect(robotSpriteName(profile, true)).toBeNull();
    expect(robotSpriteUrl(profile)).toBeNull();
  });

  it('light is the cream variant of the SAME robot, for dark screens', () => {
    expect(robotSpriteName('omx_full', true)).toBe('omx_full_light');
    expect(robotSpriteName('omx_follower', true)).toBe('omx_f_light');
    expect(robotSpriteName('omx_f', true)).toBe('omx_f_light');
    expect(robotSpriteName('v1_studio', true)).toBe('v1_light');
    expect(robotSpriteUrl('omx_full')).toBe('/robots/omx_full.webp');
    expect(robotSpriteUrl('v1_studio', true)).toBe('/robots/v1_light.webp');
  });

  it('every sprite it names is a shipped file', () => {
    const dir = path.resolve(__dirname, '../../../../public/robots');
    for (const profile of ['omx_full', 'omx_follower', 'omx_f', 'v1_studio']) {
      for (const light of [false, true]) {
        expect(fs.existsSync(path.join(dir, `${robotSpriteName(profile, light)}.webp`))).toBe(true);
      }
    }
  });
});

describe('RobotIcon', () => {
  it('a lazy, undraggable, alt-less sprite in a square box; presentation without a title', () => {
    const { container } = render(<RobotIcon profile="v1_studio" size={72} />);
    const box = container.firstChild;
    expect(box).toHaveAttribute('role', 'presentation');
    expect(box).toHaveClass('su-robot');
    expect(box).not.toHaveClass('su-robot--animate');
    expect(box.style.width).toBe('72px');
    expect(box.style.height).toBe('72px');
    const img = box.querySelector('img');
    expect(img).toHaveAttribute('src', '/robots/v1.webp');
    expect(img).toHaveAttribute('loading', 'lazy');
    expect(img).toHaveAttribute('decoding', 'async');
    expect(img).toHaveAttribute('alt', '');
    expect(img).toHaveAttribute('draggable', 'false');
  });

  it('an unknown profile draws an empty box of the same size, never another robot', () => {
    const { container } = render(<RobotIcon profile="" size={72} title="Roboter" animate />);
    const box = container.firstChild;
    expect(box).toHaveClass('su-robot');
    expect(box.style.width).toBe('72px');
    expect(box.querySelector('img')).toBeNull();
    expect(box).toHaveAttribute('role', 'presentation'); // no picture, so no image to name
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

  it('reducedMotion given: it decides, and the icon neither reads nor subscribes to matchMedia', () => {
    const orig = window.matchMedia;
    const listeners = new Set();
    window.matchMedia = vi.fn((q) => ({
      matches: true, media: q, addEventListener: (_t, fn) => listeners.add(fn), removeEventListener: (_t, fn) => listeners.delete(fn),
    }));
    try {
      const { container, rerender } = render(<RobotIcon profile="omx_full" animate reducedMotion={false} />);
      expect(container.firstChild).toHaveClass('su-robot--animate');
      rerender(<RobotIcon profile="omx_full" animate reducedMotion />);
      expect(container.firstChild).not.toHaveClass('su-robot--animate');
      expect(window.matchMedia).not.toHaveBeenCalled();
      expect(listeners.size).toBe(0);
    } finally {
      window.matchMedia = orig;
    }
  });

  it('reducedMotion omitted: it follows the media query (one subscription)', () => {
    const orig = window.matchMedia;
    const listeners = new Set();
    window.matchMedia = vi.fn((q) => ({
      matches: false, media: q, addEventListener: (_t, fn) => listeners.add(fn), removeEventListener: (_t, fn) => listeners.delete(fn),
    }));
    try {
      const { container, unmount } = render(<RobotIcon profile="omx_full" animate />);
      expect(container.firstChild).toHaveClass('su-robot--animate');
      expect(window.matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)');
      expect(listeners.size).toBe(1);
      unmount();
      expect(listeners.size).toBe(0);
    } finally {
      window.matchMedia = orig;
    }
  });
});
