// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// A segmented control (Sucher §4.8) with the semantics of the Aufnahme page's
// StageViewSwitch: one group label, buttons with aria-pressed, `onChange`
// only when the value really changes, the focus back to the page after a
// mouse click. Tones: light (on a plate), dark (on a screen), glass (over a
// picture); `accent` (light only) fills the active segment with the signal.

import React from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';
import { releasePointerFocus } from './pointerFocus';

const TONES = new Set(['light', 'dark', 'glass']);

export default function Segmented({
  label,
  value,
  onChange,
  options = [],
  tone = 'light',
  accent = false,
  size = 'sm',
  className,
}) {
  const t = TONES.has(tone) ? tone : 'light';
  return (
    <div
      role="group"
      aria-label={label}
      className={clsx(
        'su-seg',
        `su-seg--${t}`,
        `su-seg--${size === 'md' ? 'md' : 'sm'}`,
        accent && t === 'light' && 'su-seg--accent',
        className,
      )}
    >
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          aria-pressed={o.value === value}
          disabled={o.disabled}
          title={o.title}
          onClick={(e) => {
            releasePointerFocus(e);
            if (o.value !== value && onChange) onChange(o.value);
          }}
        >
          {o.icon ? <Icon name={o.icon} size={14} /> : null}
          {o.label}
        </button>
      ))}
    </div>
  );
}
