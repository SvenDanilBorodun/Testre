// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A robot icon (Sucher §4.10, D17): a 24-frame sprite rendered from the real
// URDF (public/robots/*.webp, 3456 × 144). Frame 0 is the still icon;
// `animate` sways it (steps(24), 2.4 s), never under reduced motion. Loaded
// lazily — the images are 88–107 KB each.

import React from 'react';
import clsx from 'clsx';
import useReducedMotion from './useReducedMotion';

const SPRITES = Object.freeze({
  omx_full: 'omx_full',
  omx_follower: 'omx_f',
  edu6_studio: 'edu6',
  edu1_studio: 'edu1',
});

/** The sprite name for a robot profile id; `light` is the OMX for dark screens. */
export function robotSpriteName(profile, light = false) {
  if (light) return 'omx_f_light';
  return (typeof profile === 'string' && Object.prototype.hasOwnProperty.call(SPRITES, profile))
    ? SPRITES[profile]
    : 'omx_f';
}

export function robotSpriteUrl(profile, light = false) {
  return `${process.env.PUBLIC_URL || ''}/robots/${robotSpriteName(profile, light)}.webp`;
}

export default function RobotIcon({
  profile, size = 48, animate = false, light = false, dim = false, title, className,
}) {
  const reduced = useReducedMotion();
  const a11y = title ? { role: 'img', 'aria-label': title } : { role: 'presentation' };
  return (
    <span
      {...a11y}
      className={clsx('su-robot', animate && !reduced && 'su-robot--animate', dim && 'su-robot--dim', className)}
      style={{ width: size, height: size }}
    >
      <img
        src={robotSpriteUrl(profile, light)}
        loading="lazy"
        decoding="async"
        alt=""
        draggable="false"
      />
    </span>
  );
}
