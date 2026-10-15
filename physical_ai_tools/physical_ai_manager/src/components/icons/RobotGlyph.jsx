/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// A robot as a small icon: the real URDF of the robot the student started
// with, rendered as a tonal alpha glyph (public/robots/glyphs.png) and painted
// with CSS mask-image in `currentColor`, so it takes the colour of the text
// around it exactly like a Lucide icon. It replaces the hand-drawn stick arms
// that used to sit in the icon registry.
//
// `profile` is the robot profile id (`taskStatus.robotProfile`); an unknown or
// missing one draws NOTHING (null), never another robot. `part` picks which
// arm of that robot: 'robot' (the whole robot, both arms for omx_full), 'arm'
// (the follower, the one a program drives) or 'leader' (the arm moved by
// hand; only a profile that has one). Decorative by default (`aria-hidden`);
// a `title` makes it a named image.

import React from 'react';
import clsx from 'clsx';
import {
  GLYPH_ORDER, robotGlyphName, robotGlyphUrl,
} from './robotAssets';
import './RobotGlyph.css';

export default function RobotGlyph({
  profile, part = 'robot', size = '1em', title, className, style,
}) {
  const name = robotGlyphName(profile, part);
  if (!name) return null;
  const cell = GLYPH_ORDER.indexOf(name);
  const url = `url(${robotGlyphUrl()})`;
  const a11y = title ? { role: 'img', 'aria-label': title, title } : { 'aria-hidden': 'true' };
  return (
    <span
      {...a11y}
      data-robot-glyph={name}
      className={clsx('robot-glyph', className)}
      style={{
        width: size,
        height: size,
        WebkitMaskImage: url,
        maskImage: url,
        WebkitMaskSize: `${GLYPH_ORDER.length * 100}% 100%`,
        maskSize: `${GLYPH_ORDER.length * 100}% 100%`,
        WebkitMaskPosition: `${(cell / (GLYPH_ORDER.length - 1)) * 100}% 0`,
        maskPosition: `${(cell / (GLYPH_ORDER.length - 1)) * 100}% 0`,
        ...style,
      }}
    />
  );
}
