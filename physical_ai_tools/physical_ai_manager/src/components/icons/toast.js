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
// A toast icon is drawn in the toast's OWN text colour (`currentColor`): the
// one Toaster (src/toasterOptions.js) paints white text on a dark, green or
// red toast, and a coloured icon vanished on two of the three (a red icon on
// a red error toast, a slate one on the dark toast). `success` and `error` are
// also that Toaster's defaults, so a bare `toast.success(…)` / `toast.error(…)`
// draws the same icon as one that names it.

import React from 'react';
import Icon from './Icon';

export const TOAST_ICONS = Object.freeze({
  info: 'info',
  warning: 'warning',
  error: 'failed',
  success: 'checkCircle',
  camera: 'camera',
  save: 'save',
  record: 'record',
  stop: 'hand',
  arm: 'robotArm',
  unplugged: 'unplugged',
  timeout: 'alarm',
});

export const TOAST_ICON_KINDS = Object.freeze(Object.keys(TOAST_ICONS));

const built = new Map();

/** The react-hot-toast `icon` element for `kind` (one of TOAST_ICON_KINDS). */
export function toastIcon(kind) {
  if (built.has(kind)) return built.get(kind);
  const name = Object.prototype.hasOwnProperty.call(TOAST_ICONS, kind) ? TOAST_ICONS[kind] : null;
  if (!name) throw new Error(`Unknown toast icon kind: ${String(kind)}`);
  const element = React.createElement(Icon, { name, size: '1.15em', className: 'shrink-0' });
  built.set(kind, element);
  return element;
}
