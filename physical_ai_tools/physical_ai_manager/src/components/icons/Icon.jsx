/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// <Icon name="play" /> — the one way React UI draws an icon. Decorative by
// default (`aria-hidden`, never focusable): the control around it carries the
// words. A `title` makes it an image with that name instead. An unknown name is
// a programming error, so it throws in development and tests; production draws
// nothing rather than take a page down over an icon.

import React from 'react';
import { ICONS } from './registry';

export default function Icon({
  name, size = '1em', className = '', title, ...rest
}) {
  const Component = Object.prototype.hasOwnProperty.call(ICONS, name) ? ICONS[name] : null;
  if (!Component) {
    if (process.env.NODE_ENV !== 'production') throw new Error(`Unknown icon: ${String(name)}`);
    return null;
  }
  const a11y = title
    ? { role: 'img', 'aria-label': title, title }
    : { 'aria-hidden': 'true' };
  return (
    <Component
      {...a11y}
      focusable="false"
      size={size}
      className={`inline-block shrink-0 ${className}`.trim()}
      data-icon={name}
      {...rest}
    />
  );
}
