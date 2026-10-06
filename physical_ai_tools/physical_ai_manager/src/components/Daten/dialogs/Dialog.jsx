// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Every Daten dialog's frame (spec §G8): `role="dialog"`, `aria-modal`,
// labelled by its title; focus goes to the first `[data-autofocus]` (else the
// first action) and returns to the opener on close; Tab stays inside; Esc and
// a click on the backdrop close it unless it is `locked` (a running job's
// progress). The player's keys are ignored while it is open (they look for
// `[role="dialog"]`).

import React, { useEffect, useId, useRef } from 'react';
import Icon from '../../icons/Icon';

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export default function Dialog({
  title, icon = null, iconTone = null, iconSpin = false, wide = false, locked = false, onClose, children, testId,
}) {
  const id = useId();
  const titleId = `dat-dlg-${id}`;
  const boxRef = useRef(null);
  const openerRef = useRef(typeof document !== 'undefined' ? document.activeElement : null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const lockedRef = useRef(locked);
  lockedRef.current = locked;

  useEffect(() => {
    const box = boxRef.current;
    if (box) {
      const first = box.querySelector('[data-autofocus]') || box.querySelector('.dat-acts button:not([disabled])') || box;
      if (first && typeof first.focus === 'function') first.focus();
    }
    const opener = openerRef.current;
    return () => {
      if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
    };
  }, []);

  const onKeyDown = (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      e.preventDefault();
      if (!lockedRef.current && onCloseRef.current) onCloseRef.current();
      return;
    }
    if (e.key === 'Tab') {
      const box = boxRef.current;
      if (!box) return;
      const items = Array.from(box.querySelectorAll(FOCUSABLE));
      if (items.length === 0) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    }
  };

  const toneStyle = iconTone ? { color: `var(--${iconTone})` } : undefined;
  return (
    // The backdrop closes the dialog on a click that starts AND ends on it.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
    <div
      className="dat-ov"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !lockedRef.current && onCloseRef.current) onCloseRef.current();
      }}
    >
      <div
        ref={boxRef}
        className={`dat-dlg${wide ? ' dat-wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        data-testid={testId}
      >
        <h2 id={titleId}>
          {icon ? <Icon name={icon} size={20} style={toneStyle} className={iconSpin ? 'animate-spin' : undefined} /> : null}
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}
