// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Rolling digits („Zählwerk", M5 / M10). At rest it is the plain value, one
// bare text node (no wrapper element). When an integer changes to another integer after mount, every
// digit becomes a column of 0–9 that rolls from the old digit to the new one
// (right → left, `staggerMs` apart); the columns are aria-hidden and one
// visually hidden node carries the real value, so the text a reader or a test
// sees is the number, never „0123456789". When the roll is over it is plain
// text again. Anything that is not a plain integer (formatted strings like
// „12,4 GB", „3 / 5", „—") never rolls. Reduced motion: it jumps.
//
// Two state updates per change (start, end) — never one per animation frame.

import React, { useEffect, useState } from 'react';
import clsx from 'clsx';

const INT_RE = /^\d+$/;

/** A plain non-negative integer: a number, or a string of digits only. */
export function isPlainInteger(v) {
  if (typeof v === 'number') return Number.isInteger(v) && v >= 0;
  return typeof v === 'string' && INT_RE.test(v);
}

const STRIP = '0123456789';

export default function RollingDigits({
  value, durationMs = 520, staggerMs = 40, reduced = false, className,
}) {
  const text = String(value);
  // `shown` is the value this component last rendered; `from` the value a
  // running roll started at (null at rest). Derived during render, the React
  // way to adjust state to a new prop, so the first frame of a roll already
  // has its columns.
  const [state, setState] = useState({ shown: text, from: null, id: 0 });
  let current = state;
  if (state.shown !== text) {
    const roll = !reduced && isPlainInteger(state.shown) && isPlainInteger(text);
    current = { shown: text, from: roll ? state.shown : null, id: state.id + 1 };
    setState(current);
  }
  const rolling = current.from !== null;
  const digits = text.length;
  const totalMs = durationMs + staggerMs * Math.max(0, digits - 1) + 60;

  useEffect(() => {
    if (!rolling) return undefined;
    const id = current.id;
    const t = setTimeout(() => {
      setState((s) => (s.id === id ? { ...s, from: null } : s));
    }, totalMs);
    return () => clearTimeout(t);
  }, [rolling, current.id, totalMs]);

  // At rest: the bare text, so the DOM is exactly what a plain value renders.
  if (!rolling) return text;

  const from = current.from;
  return (
    <span className={clsx('su-roll', 'su-roll--on', className)}>
      <span className="su-sr">{text}</span>
      <span className="su-roll__cols" aria-hidden="true">
        {text.split('').map((d, idx) => {
          const fromRight = digits - 1 - idx;
          const fromIdx = from.length - 1 - fromRight;
          const isNew = fromIdx < 0;
          const fromDigit = isNew ? 0 : Number(from[fromIdx]);
          return (
            <span
              // Keyed by the place from the right, so the units column stays the units column.
              key={fromRight}
              className={clsx('su-roll__col', isNew && 'su-roll__col--new')}
              style={{
                '--from': fromDigit,
                '--to': Number(d),
                '--dur': `${durationMs}ms`,
                '--delay': `${fromRight * staggerMs}ms`,
              }}
            >
              <span className="su-roll__strip">{STRIP}</span>
            </span>
          );
        })}
      </span>
    </span>
  );
}
