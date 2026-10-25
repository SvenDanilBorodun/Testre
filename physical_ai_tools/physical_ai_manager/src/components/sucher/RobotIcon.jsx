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
// lazily — the images are 82–107 KB each. `reducedMotion` (boolean) lets a
// caller that already follows the media query hand its answer in: the icon
// then opens no matchMedia subscription of its own.
//
// It draws the robot the student started with and NO OTHER: an unknown profile
// (before the first status tick, or an id this build has no sprite for) draws
// an empty box of the same size — never the OMX as a stand-in. `light` picks
// the cream variant of THAT robot's sprite for a dark screen (the Startseite's
// offline hero), one per profile (render-robot-pictures.sh).

import React from 'react';
import clsx from 'clsx';
import useReducedMotion from './useReducedMotion';

// profile id → sprite name. `omx_f` is the DATA robot type of both OMX profiles
// (taskStatus.robotType): callers that only know the type draw the follower.
const SPRITES = Object.freeze({
  omx_full: 'omx_full',
  omx_follower: 'omx_f',
  omx_f: 'omx_f',
  v1_studio: 'v1',
});

/** The sprite name for a robot profile id, or null for an unknown one; `light` is the cream variant for dark screens. */
export function robotSpriteName(profile, light = false) {
  if (typeof profile !== 'string' || !Object.prototype.hasOwnProperty.call(SPRITES, profile)) return null;
  return light ? `${SPRITES[profile]}_light` : SPRITES[profile];
}

/** The sprite's URL, or null while the profile is unknown. */
export function robotSpriteUrl(profile, light = false) {
  const name = robotSpriteName(profile, light);
  return name ? `${process.env.PUBLIC_URL || ''}/robots/${name}.webp` : null;
}

export default function RobotIcon({
  profile, size = 48, animate = false, light = false, dim = false, title, className, reducedMotion,
}) {
  const given = typeof reducedMotion === 'boolean';
  const systemReduced = useReducedMotion(!given);
  const reduced = given ? reducedMotion : systemReduced;
  const url = robotSpriteUrl(profile, light);
  const a11y = title && url ? { role: 'img', 'aria-label': title } : { role: 'presentation' };
  return (
    <span
      {...a11y}
      className={clsx('su-robot', animate && !reduced && 'su-robot--animate', dim && 'su-robot--dim', className)}
      style={{ width: size, height: size }}
    >
      {url ? (
        <img
          src={url}
          loading="lazy"
          decoding="async"
          alt=""
          draggable="false"
        />
      ) : null}
    </span>
  );
}
