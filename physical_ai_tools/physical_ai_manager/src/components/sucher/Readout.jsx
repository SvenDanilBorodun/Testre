// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A readout (Sucher §4.6): a dark inset box with Doto digits, the device's
// display for a number or a short state word (upper case comes from CSS).
// `led` puts a light before the text (a tone name), `label` a caption after
// the box. `roll` makes a plain integer roll when it changes (M5); anything
// else switches without rolling.

import React from 'react';
import clsx from 'clsx';
import Led from './Led';
import RollingDigits, { isPlainInteger } from './RollingDigits';
import useReducedMotion from './useReducedMotion';

const SIZES = new Set(['sm', 'md', 'lg', 'xl']);
const TONES = new Set(['ink', 'sig', 'ok', 'warn', 'rec']);

export default function Readout({
  as: Tag = 'span',
  size = 'sm',
  tone = 'ink',
  led,
  ledPulse = 'none',
  label,
  roll = false,
  className,
  children,
  ...rest
}) {
  const reduced = useReducedMotion();
  const s = SIZES.has(size) ? size : 'sm';
  const t = TONES.has(tone) ? tone : 'ink';
  const content = roll && isPlainInteger(children)
    ? <RollingDigits value={children} reduced={reduced} />
    : children;
  const box = (
    <span className="su-read__text">{content}</span>
  );
  const ledTone = led === true ? 'ok' : led;
  if (!label) {
    return (
      <Tag {...rest} className={clsx('su-read', `su-read--${s}`, `su-read--${t}`, className)}>
        {ledTone ? <Led tone={ledTone} pulse={ledPulse} /> : null}
        {box}
      </Tag>
    );
  }
  return (
    <Tag {...rest} className={clsx('su-readout', className)}>
      <span className={clsx('su-read', `su-read--${s}`, `su-read--${t}`)}>
        {ledTone ? <Led tone={ledTone} pulse={ledPulse} /> : null}
        {box}
      </span>
      <span className="su-lab su-readout__label">{label}</span>
    </Tag>
  );
}
