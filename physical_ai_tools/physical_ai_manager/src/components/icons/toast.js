/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The icon a toast shows (react-hot-toast's `icon` option), by what the toast
// says: `toast(text, { icon: toastIcon('camera') })`. One prebuilt element per
// kind, so a toast never builds an icon of its own and every toast of a kind
// looks the same. An unknown kind throws: it is a programming error.
//
// The one robot toast, kind `arm` („the arm stays where it is", the switch to
// the real robot), draws the arm of the robot the student started with, so it
// needs the profile at call time: `toastIcon('arm', { profile })`. It is the
// real URDF's glyph (RobotGlyph.jsx), never a stand-in: an unknown profile
// answers `undefined`, which react-hot-toast shows as a toast without an icon.
//
// A toast icon is drawn in the toast's OWN text colour (`currentColor`): the
// one Toaster (src/toasterOptions.js) paints white text on a dark, green or
// red toast, and a coloured icon vanished on two of the three (a red icon on
// a red error toast, a slate one on the dark toast). `success` and `error` are
// also that Toaster's defaults, so a bare `toast.success(…)` / `toast.error(…)`
// draws the same icon as one that names it.

import React from 'react';
import Icon from './Icon';
import RobotGlyph from './RobotGlyph';
import { robotGlyphName } from './robotAssets';

export const TOAST_ICONS = Object.freeze({
  info: 'info',
  warning: 'warning',
  error: 'failed',
  success: 'checkCircle',
  camera: 'camera',
  save: 'save',
  record: 'record',
  stop: 'hand',
  simulator: 'simulator',
  unplugged: 'unplugged',
  timeout: 'alarm',
});

/** The toast kind that draws a robot; it needs `{ profile }` (see above). */
export const ROBOT_TOAST_KIND = 'arm';

export const TOAST_ICON_KINDS = Object.freeze(Object.keys(TOAST_ICONS));

const built = new Map();

/**
 * The react-hot-toast `icon` element for `kind` (one of TOAST_ICON_KINDS, or
 * ROBOT_TOAST_KIND with the robot's `profile`; `undefined` for an unknown one).
 */
export function toastIcon(kind, { profile } = {}) {
  if (kind === ROBOT_TOAST_KIND) {
    const glyph = robotGlyphName(profile, 'arm');
    if (!glyph) return undefined;
    const key = `${ROBOT_TOAST_KIND}:${glyph}`;
    if (!built.has(key)) {
      built.set(key, React.createElement(RobotGlyph, {
        profile, part: 'arm', size: '1.15em', className: 'shrink-0',
      }));
    }
    return built.get(key);
  }
  if (built.has(kind)) return built.get(kind);
  const name = Object.prototype.hasOwnProperty.call(TOAST_ICONS, kind) ? TOAST_ICONS[kind] : null;
  if (!name) throw new Error(`Unknown toast icon kind: ${String(kind)}`);
  const element = React.createElement(Icon, { name, size: '1.15em', className: 'shrink-0' });
  built.set(kind, element);
  return element;
}
