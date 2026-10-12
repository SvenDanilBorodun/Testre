/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// THE pane divider of Roboter Studio: every resizable pane (editor ↔ dock, the
// dock's two panels, editor ↔ simulator, 3D ↔ table, the code panes) is
// resized by this one primitive.
//
//   * It writes ONE CSS variable (`cssVar`) on its own parentElement; the panes
//     size themselves from it (`md:w-[var(--dock-w,380px)]` …). It is the ONLY
//     writer of that variable: an owner never puts it into its own `style`,
//     which would shadow it.
//   * A drag changes the variable through requestAnimationFrame — one write per
//     frame, no React state, so the owner (and a live Blockly canvas next to
//     it) never re-renders while the pointer moves. BlocklyWorkspace's own
//     ResizeObserver hears the new box.
//   * The value is COMMITTED exactly once per release of a drag that changed
//     it, arrow key, Home/End or double-click (`onCommit`, which persists it
//     through layoutPrefs.js). A click commits nothing.
//   * A parent resize re-clamps the COMMITTED value into the new bounds without
//     committing: a narrower window never persists a smaller pane, and growing
//     it back restores the student's size.
//   * Keyboard: the arrow `growKey` grows, the opposite arrow shrinks (Shift: a
//     large step), Home/End jump to the bounds. Handled keys are stopped here so
//     Blockly's document-level shortcuts never see them.
//   * The display classes come from `showBelowMd`, never from `className`:
//     Tailwind orders `hidden` after `flex`, so a caller's `flex` could not
//     undo a `hidden`.

import React, { useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import Icon from '../../icons/Icon';
import { LAYOUT_DE } from './layoutCopy';

const OPPOSITE_KEY = Object.freeze({
  ArrowLeft: 'ArrowRight',
  ArrowRight: 'ArrowLeft',
  ArrowUp: 'ArrowDown',
  ArrowDown: 'ArrowUp',
});

function formatValue(v, unit) {
  return unit === 'px' ? `${Math.round(v)}px` : `${+v.toFixed(4)}`;
}

// aria-value* are integers: px as is, a fraction in percent.
function ariaNumber(v, unit) {
  return unit === 'px' ? Math.round(v) : Math.round(v * 100);
}

export default function ResizeHandle({
  orientation,
  cssVar,
  unit,
  value,
  defaultValue,
  bounds,
  fromPointer,
  growKey,
  step,
  largeStep,
  label,
  valueText,
  onCommit,
  showBelowMd = false,
  className = '',
}) {
  const ref = useRef(null);
  // The latest props, read by the pointer/key/observer handlers.
  const propsRef = useRef(null);
  propsRef.current = {
    orientation, cssVar, unit, value, defaultValue, bounds, fromPointer, valueText, onCommit,
  };
  // The value the variable shows right now (after a drag frame, a key, a
  // re-clamp) — the base of the next arrow step.
  const appliedRef = useRef(null);
  // One drag: { active, start, value }. `active` is cleared by the FIRST of
  // pointerup / pointercancel / lostpointercapture, so a drag commits once —
  // and only when it moved the divider: a click commits nothing, or a size a
  // narrow window squeezed would be persisted as the student's choice.
  const dragRef = useRef({ active: false, start: null, value: null });
  const rafRef = useRef(null);

  // The parent's box, or null when it cannot be measured (no layout, as in
  // jsdom): a bound computed from a zero-size box would squeeze the pane.
  const parentRect = useCallback(() => {
    const el = ref.current;
    const parent = el ? el.parentElement : null;
    if (!parent || typeof parent.getBoundingClientRect !== 'function') return null;
    const rect = parent.getBoundingClientRect();
    if (!rect) return null;
    const size = propsRef.current.orientation === 'vertical' ? rect.width : rect.height;
    return size > 0 ? rect : null;
  }, []);

  const boundsFor = useCallback((rect) => {
    const p = propsRef.current;
    const b = (typeof p.bounds === 'function' ? p.bounds(rect) : null) || {};
    const min = Number.isFinite(b.min) ? b.min : -Infinity;
    const max = Number.isFinite(b.max) ? Math.max(b.max, min) : Infinity;
    return { min, max };
  }, []);

  const clamp = useCallback((v, rect) => {
    const p = propsRef.current;
    const { min, max } = boundsFor(rect);
    const n = Number.isFinite(v) ? v : p.defaultValue;
    return Math.min(max, Math.max(min, n));
  }, [boundsFor]);

  // Show `v`: the variable on the parent and the separator's own ARIA values,
  // written to the DOM directly (no React state).
  const apply = useCallback((v, rect) => {
    const el = ref.current;
    if (!el) return;
    const p = propsRef.current;
    appliedRef.current = v;
    const parent = el.parentElement;
    if (parent && parent.style) parent.style.setProperty(p.cssVar, formatValue(v, p.unit));
    const { min, max } = boundsFor(rect);
    el.setAttribute('aria-valuenow', String(ariaNumber(v, p.unit)));
    if (Number.isFinite(min)) el.setAttribute('aria-valuemin', String(ariaNumber(min, p.unit)));
    if (Number.isFinite(max)) el.setAttribute('aria-valuemax', String(ariaNumber(max, p.unit)));
    if (typeof p.valueText === 'function') el.setAttribute('aria-valuetext', p.valueText(v));
  }, [boundsFor]);

  const commit = useCallback((v) => {
    const p = propsRef.current;
    if (typeof p.onCommit === 'function') p.onCommit(v);
  }, []);

  // The committed value, clamped into what the parent allows now.
  useLayoutEffect(() => {
    const rect = parentRect();
    apply(clamp(value, rect), rect);
  }, [value, cssVar, parentRect, apply, clamp]);

  // A parent resize re-clamps the COMMITTED value — never the last clamped one,
  // so growing the window back restores the student's size — and commits
  // nothing. Ignored during a drag (the drag owns the variable then).
  useEffect(() => {
    const el = ref.current;
    const parent = el ? el.parentElement : null;
    if (!parent || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => {
      if (dragRef.current.active) return;
      const rect = parentRect();
      apply(clamp(propsRef.current.value, rect), rect);
    });
    observer.observe(parent);
    return () => observer.disconnect();
  }, [parentRect, apply, clamp]);

  useEffect(() => () => {
    if (rafRef.current !== null && typeof window.cancelAnimationFrame === 'function') {
      window.cancelAnimationFrame(rafRef.current);
    }
    rafRef.current = null;
  }, []);

  const onPointerDown = (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    const start = appliedRef.current !== null ? appliedRef.current : clamp(value, parentRect());
    dragRef.current = { active: true, start, value: start };
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (_) { /* jsdom / no capture */ }
  };

  const onPointerMove = (e) => {
    const drag = dragRef.current;
    if (!drag.active) return;
    const rect = parentRect();
    const p = propsRef.current;
    if (typeof p.fromPointer !== 'function') return;
    drag.value = clamp(p.fromPointer(e, rect), rect);
    if (rafRef.current !== null) return;
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = null;
      if (dragRef.current === drag) apply(drag.value, parentRect());
    });
  };

  const endDrag = (e) => {
    const drag = dragRef.current;
    if (!drag.active) return;
    drag.active = false;
    if (rafRef.current !== null) {
      window.cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    apply(drag.value, parentRect());
    try {
      if (e && e.currentTarget && typeof e.currentTarget.hasPointerCapture === 'function'
          && e.currentTarget.hasPointerCapture(e.pointerId)) {
        e.currentTarget.releasePointerCapture(e.pointerId);
      }
    } catch (_) { /* jsdom / capture already gone */ }
    if (drag.value !== drag.start) commit(drag.value);
  };

  const onKeyDown = (e) => {
    const p = propsRef.current;
    const rect = parentRect();
    const { min, max } = boundsFor(rect);
    const base = appliedRef.current !== null ? appliedRef.current : clamp(p.value, rect);
    const delta = e.shiftKey ? largeStep : step;
    let next = null;
    if (e.key === growKey) next = base + delta;
    else if (e.key === OPPOSITE_KEY[growKey]) next = base - delta;
    else if (e.key === 'Home') next = min;
    else if (e.key === 'End') next = max;
    if (next === null) return;
    e.preventDefault();
    e.stopPropagation();
    const v = clamp(next, rect);
    apply(v, rect);
    commit(v);
  };

  const onDoubleClick = (e) => {
    e.preventDefault();
    const rect = parentRect();
    const v = clamp(propsRef.current.defaultValue, rect);
    apply(v, rect);
    commit(v);
  };

  const vertical = orientation === 'vertical';
  // What React renders before the first measurement; the layout effect above
  // replaces every value with the measured one at once.
  const initial = clamp(value, null);
  const initialBounds = boundsFor(null);
  const classes = [
    showBelowMd ? 'flex' : 'hidden md:flex',
    'shrink-0 items-center justify-center touch-none select-none',
    'text-[var(--ink-4)] hover:text-[var(--accent)]',
    'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]',
    vertical ? 'w-2 cursor-col-resize' : 'h-2 cursor-row-resize',
    className,
  ].filter(Boolean).join(' ');

  return (
    <div
      ref={ref}
      role="separator"
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
      aria-orientation={vertical ? 'vertical' : 'horizontal'}
      aria-label={label}
      aria-valuenow={ariaNumber(initial, unit)}
      aria-valuemin={Number.isFinite(initialBounds.min) ? ariaNumber(initialBounds.min, unit) : undefined}
      aria-valuemax={Number.isFinite(initialBounds.max) ? ariaNumber(initialBounds.max, unit) : undefined}
      aria-valuetext={typeof valueText === 'function' ? valueText(initial) : undefined}
      title={LAYOUT_DE.RESIZE_TITLE}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onLostPointerCapture={endDrag}
      onKeyDown={onKeyDown}
      onDoubleClick={onDoubleClick}
      className={classes}
    >
      <Icon name={vertical ? 'resizeGrip' : 'resizeGripRow'} className="pointer-events-none" />
    </div>
  );
}
