/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Vormachen review strip: how much the arm moved over time (bars), with
// two handles choosing the first and last kept sample. Handles are sliders in
// SAMPLE INDICES (ArrowLeft/ArrowRight = one sample) but are drawn at the
// sample's TIME, because the recorder drops samples of a still arm and the
// bars are binned by time. The handles never cross and always keep ≥ 2 samples.

import React, { useRef } from 'react';
import { DE } from '../blocks/messages_de';

const CHART_W = 1000;
const CHART_H = 60;

function clamp(v, lo, hi) {
  return Math.min(Math.max(v, lo), hi);
}

export function clampRange(which, value, { startIndex, endIndex, count }) {
  const last = Math.max(1, count - 1);
  if (which === 'start') return { startIndex: clamp(value, 0, endIndex - 1), endIndex };
  return { startIndex, endIndex: clamp(value, startIndex + 1, last) };
}

function ReviewStrip({
  activity = [], timesMs = [], startIndex, endIndex, onChange, onHandleRelease, disabled = false,
}) {
  const trackRef = useRef(null);
  const dragRef = useRef(null);
  const count = timesMs.length;
  const durationMs = count > 1 ? timesMs[count - 1] : 0;
  const fraction = (i) => {
    if (count < 2) return 0;
    if (durationMs > 0) return clamp(timesMs[i] / durationMs, 0, 1);
    return i / (count - 1);
  };
  const peak = activity.reduce((m, v) => (v > m ? v : m), 0);
  const barW = activity.length ? CHART_W / activity.length : CHART_W;

  const move = (which, value) => {
    if (disabled || count < 2 || typeof onChange !== 'function') return;
    const next = clampRange(which, value, { startIndex, endIndex, count });
    if (next.startIndex !== startIndex || next.endIndex !== endIndex) onChange(next);
  };

  // Nearest sample (by time) to a pointer x.
  const indexAt = (clientX) => {
    const el = trackRef.current;
    const rect = el ? el.getBoundingClientRect() : null;
    if (!rect || !(rect.width > 0) || count < 2) return null;
    const f = clamp((clientX - rect.left) / rect.width, 0, 1);
    let best = 0;
    for (let i = 1; i < count; i += 1) {
      if (Math.abs(fraction(i) - f) < Math.abs(fraction(best) - f)) best = i;
    }
    return best;
  };

  const handleProps = (which) => {
    const value = which === 'start' ? startIndex : endIndex;
    return {
      role: 'slider',
      tabIndex: disabled ? -1 : 0,
      'aria-label': which === 'start' ? DE.TEACH_HANDLE_START : DE.TEACH_HANDLE_END,
      'aria-valuemin': which === 'start' ? 0 : startIndex + 1,
      'aria-valuemax': which === 'start' ? endIndex - 1 : Math.max(1, count - 1),
      'aria-valuenow': value,
      'aria-disabled': disabled || undefined,
      onKeyDown: (e) => {
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
          e.preventDefault();
          move(which, value + (e.key === 'ArrowLeft' ? -1 : 1));
        }
      },
      onPointerDown: (e) => {
        if (disabled) return;
        dragRef.current = which;
        try { e.currentTarget.setPointerCapture(e.pointerId); } catch (_) { /* jsdom */ }
      },
      onPointerMove: (e) => {
        if (dragRef.current !== which) return;
        const idx = indexAt(e.clientX);
        if (idx !== null) move(which, idx);
      },
      onPointerUp: (e) => {
        dragRef.current = null;
        try { e.currentTarget.releasePointerCapture(e.pointerId); } catch (_) { /* jsdom */ }
        // The overlay hands focus back to its container, so Enter keeps the take.
        if (typeof onHandleRelease === 'function') onHandleRelease();
      },
      style: { left: `${fraction(value) * 100}%` },
      className: 'absolute top-0 flex h-full w-4 -translate-x-1/2 cursor-ew-resize items-stretch justify-center '
        + 'outline-none focus-visible:ring-4 focus-visible:ring-[var(--accent)]/40',
    };
  };

  const startPct = fraction(startIndex) * 100;
  const endPct = fraction(endIndex) * 100;

  return (
    <div
      role="group"
      aria-label={DE.TEACH_STRIP_ARIA}
      ref={trackRef}
      data-testid="teach-review-strip"
      className={`relative h-16 w-full select-none rounded-lg bg-[var(--bg-sunk)] ${disabled ? 'opacity-60' : ''}`}
    >
      <svg viewBox={`0 0 ${CHART_W} ${CHART_H}`} preserveAspectRatio="none" className="absolute inset-0 h-full w-full" aria-hidden="true">
        {activity.map((v, b) => {
          const h = peak > 0 ? Math.max(1, (v / peak) * (CHART_H - 4)) : 1;
          // Bins are positional (bin b = time b × 200 ms), so the index IS the identity.
          return <rect key={b} x={b * barW + 1} y={CHART_H - h} width={Math.max(1, barW - 2)} height={h} fill="var(--accent)" />;
        })}
      </svg>
      <div className="pointer-events-none absolute inset-y-0 left-0 bg-slate-400/50" style={{ width: `${startPct}%` }} />
      <div className="pointer-events-none absolute inset-y-0 right-0 bg-slate-400/50" style={{ width: `${100 - endPct}%` }} />
      <div {...handleProps('start')}><span className="w-1 rounded bg-[var(--ink)]" /></div>
      <div {...handleProps('end')}><span className="w-1 rounded bg-[var(--ink)]" /></div>
    </div>
  );
}

export default ReviewStrip;
