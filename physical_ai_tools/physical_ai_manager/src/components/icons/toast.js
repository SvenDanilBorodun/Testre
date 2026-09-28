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

import React from 'react';
import Icon from './Icon';

const TOAST_ICONS = Object.freeze({
  info: ['info', 'text-blue-600'],
  warning: ['warning', 'text-amber-600'],
  error: ['errorCircle', 'text-red-600'],
  success: ['checkCircle', 'text-green-600'],
  camera: ['camera', 'text-slate-600'],
  save: ['save', 'text-slate-600'],
  record: ['record', 'text-red-600'],
  unplugged: ['unplugged', 'text-red-600'],
  timeout: ['alarm', 'text-amber-600'],
});

export const TOAST_ICON_KINDS = Object.freeze(Object.keys(TOAST_ICONS));

const built = new Map();

/** The react-hot-toast `icon` element for `kind` (one of TOAST_ICON_KINDS). */
export function toastIcon(kind) {
  if (built.has(kind)) return built.get(kind);
  const entry = Object.prototype.hasOwnProperty.call(TOAST_ICONS, kind) ? TOAST_ICONS[kind] : null;
  if (!entry) throw new Error(`Unknown toast icon kind: ${String(kind)}`);
  const [name, className] = entry;
  const element = React.createElement(Icon, { name, size: '1.15em', className });
  built.set(kind, element);
  return element;
}
