// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A small light (Sucher §4.5). Decorative unless it carries a `label`, then
// it is an image with that name. `pulse`: breathe (M4) or blink.

import React from 'react';
import clsx from 'clsx';

const TONES = new Set(['off', 'ok', 'warn', 'rec', 'sig', 'info']);
const PULSES = new Set(['none', 'breathe', 'blink']);

export default function Led({ tone = 'off', pulse = 'none', size = 9, label, className, style }) {
  const t = TONES.has(tone) ? tone : 'off';
  const p = PULSES.has(pulse) ? pulse : 'none';
  const a11y = label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': 'true' };
  return (
    <span
      {...a11y}
      className={clsx('su-led', `su-led--${t}`, `su-led--${p}`, className)}
      style={{ width: size, height: size, ...style }}
    />
  );
}
