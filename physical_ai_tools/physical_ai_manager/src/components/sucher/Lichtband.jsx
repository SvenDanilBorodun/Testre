// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The „Lichtband" (Sucher §4.9, M7): a row of lights in the phase colour,
// the newest brightest, the older ones glowing out — the owner's replacement
// for the tick ruler. ONE React render builds `count` lights; progress moves
// by writing three custom properties on the row (`--h` head index, `--full`)
// and `aria-valuenow` through a ref, and only when the head index or `full`
// changes — never React state per frame. Updates come from `subscribeFrame`
// (the Aufnahme clock's subscription, `cb({frac})`) or the static `frac`.
// Every light's brightness is one calc() over `--h` and its own `--i`.
// Reduced motion: no moving head, no afterglow (every passed light is full).

import React, { useCallback, useLayoutEffect, useRef } from 'react';
import clsx from 'clsx';
import useReducedMotion from './useReducedMotion';

/** Head index and `full` for a fraction 0..1 over `count` lights. */
export function bandHead(frac, count) {
  const n = Math.max(1, Math.floor(Number(count) || 1));
  const x = Number.isFinite(frac) ? Math.max(0, Math.min(1, frac)) : 0;
  const full = x >= 1 ? 1 : 0;
  const h = full ? n - 1 : Math.min(n - 1, Math.floor(x * n));
  return { h, full, pct: Math.round(x * 100) };
}

export default function Lichtband({
  count = 32,
  color,
  subscribeFrame,
  frac,
  label,
  remaining,
  reduced,
  className,
}) {
  const systemReduced = useReducedMotion();
  const isReduced = reduced == null ? systemReduced : !!reduced;
  const n = Math.max(1, Math.floor(Number(count) || 1));
  const rootRef = useRef(null);
  const lastRef = useRef({ h: null, full: null });

  const apply = useCallback((f) => {
    const el = rootRef.current;
    if (!el) return;
    const { h, full, pct } = bandHead(f, n);
    const last = lastRef.current;
    if (last.h === h && last.full === full) return;
    lastRef.current = { h, full };
    el.style.setProperty('--h', String(h));
    el.style.setProperty('--full', String(full));
    el.setAttribute('aria-valuenow', String(pct));
  }, [n]);

  const subscribed = typeof subscribeFrame === 'function';

  // The static fraction (no subscription).
  useLayoutEffect(() => {
    if (subscribed) return;
    lastRef.current = { h: null, full: null };
    apply(frac);
  }, [subscribed, frac, apply]);

  // The frame subscription: `cb({frac})` up to every frame, applied only on
  // a change of the head index or `full`.
  useLayoutEffect(() => {
    if (!subscribed) return undefined;
    lastRef.current = { h: null, full: null };
    apply(0);
    return subscribeFrame((frame) => apply(frame && frame.frac));
  }, [subscribed, subscribeFrame, apply]);

  const leds = [];
  for (let i = 0; i < n; i += 1) {
    leds.push(<span key={i} className="su-band__led" style={{ '--i': i }} />);
  }

  return (
    <div
      ref={rootRef}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={typeof label === 'string' ? label : undefined}
      className={clsx('su-band', isReduced && 'su-band--reduced', className)}
      style={{ '--count': n, '--fade': isReduced ? 0 : 1, '--band-c': color || 'var(--su-sig)' }}
    >
      {label ? (
        <span className="su-band__label">
          <span className="su-led su-band__dot" aria-hidden="true" />
          {label}
        </span>
      ) : null}
      <span className="su-band__track" aria-hidden="true">
        {leds}
        <i className="su-band__head" />
      </span>
      {remaining != null && remaining !== '' ? (
        <span className="su-band__remain">{remaining}</span>
      ) : null}
    </div>
  );
}
