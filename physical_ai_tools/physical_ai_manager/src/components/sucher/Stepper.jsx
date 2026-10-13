// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// A −/+ stepper (Sucher §4.7, D16), the DOM of the Aufnahme page's Ablauf
// steppers (components/Record/TaskCard.jsx) plus the PC inputs the owner
// asked for: ↑/↓ while the focus is inside the control step by one, and the
// mouse wheel over the NUMBER (only there) steps by one per event. Both are
// ignored when the stepper is disabled; ←/→ are not handled. A mouse click on
// −/+ gives the focus back to the page. The number rolls when it changes (M10).

import React, { useEffect, useRef } from 'react';
import Icon from '../icons/Icon';
import RollingDigits from './RollingDigits';
import useReducedMotion from './useReducedMotion';
import { releasePointerFocus } from './pointerFocus';

export default function Stepper({
  field,
  label,
  value,
  min,
  max,
  unit,
  color,
  disabled = false,
  onChange,
  decLabel,
  incLabel,
  className = '',
}) {
  const reduced = useReducedMotion();
  const v = Number.isFinite(Number(value)) ? Number(value) : min;
  const set = (next) => {
    if (onChange) onChange(field, Math.max(min, Math.min(max, next)));
  };
  // One step, only when it changes the value (the limits are no-ops).
  const step = (delta) => {
    const next = Math.max(min, Math.min(max, v + delta));
    if (next !== v) set(next);
  };

  // The wheel listener is added once and reads the latest values through a
  // ref: it must be NON-passive to keep the page from scrolling under it.
  const latest = useRef({ step, disabled });
  latest.current = { step, disabled };
  const outRef = useRef(null);
  useEffect(() => {
    const el = outRef.current;
    if (!el) return undefined;
    const onWheel = (e) => {
      if (latest.current.disabled) return;
      if (!(Math.abs(e.deltaY) >= 1)) return;
      e.preventDefault();
      latest.current.step(e.deltaY < 0 ? 1 : -1);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  const onKeyDown = (e) => {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    if (disabled) return;
    e.preventDefault();
    step(e.key === 'ArrowUp' ? 1 : -1);
  };

  return (
    <div className={`su-stp ${className}`.trim()} style={{ '--c': color }} data-field={field}>
      <span className="sl"><i />{label}</span>
      {/* The arrow keys act while the focus is on one of the two buttons inside. */}
      <div className="ctl" onKeyDown={onKeyDown}>
        <button
          type="button"
          disabled={disabled || v <= min}
          aria-label={decLabel}
          onClick={(e) => { releasePointerFocus(e); set(v - 1); }}
        >
          <Icon name="minus" size={16} />
        </button>
        <output ref={outRef} aria-label={label}>
          <RollingDigits value={v} durationMs={360} reduced={reduced} />
          {unit ? <small>{unit}</small> : null}
        </output>
        <button
          type="button"
          disabled={disabled || v >= max}
          aria-label={incLabel}
          onClick={(e) => { releasePointerFocus(e); set(v + 1); }}
        >
          <Icon name="plus" size={16} />
        </button>
      </div>
    </div>
  );
}
