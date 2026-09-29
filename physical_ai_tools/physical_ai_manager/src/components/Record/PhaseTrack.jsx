// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The Phasenleiste (spec §3.4 / §3.5): the current cycle as segments —
// Aufwärmen (once), Aufnehmen, a fixed Speichern dot, Zurücksetzen — sized by
// their seconds. Finished segments are tinted, the running one fills with a
// white playhead, and the seconds left sit on the right. Outside a session it
// is one neutral segment with the idle text.
//
// The fill moves every animation frame, but the page must not re-render at
// 60 Hz: `subscribeFrame(fn)` hands `fn({frac})` each frame and this component
// writes the width straight into the DOM through refs (no React state).

import React, { useEffect, useRef } from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';

// The four phase colours as CSS values (record.css defines the variables on
// .rec-page, spec §3.5), keyed by the kind a segment, dot or overlay shows.
export const PHASE_COLOR = Object.freeze({
  warmup: 'var(--rec-warm)',
  record: 'var(--rec-run)',
  save: 'var(--rec-save)',
  reset: 'var(--rec-reset)',
  ready: 'var(--rec-reset)',
  paused: 'var(--ink-4)',
});

/** The colour for a segment kind; an unknown kind is neutral grey. */
export function phaseColor(kind) {
  return PHASE_COLOR[kind] || '#3A4448';
}

function setFrac(fill, head, frac) {
  const f = Math.max(0, Math.min(1, Number.isFinite(frac) ? frac : 0));
  if (fill) fill.style.width = `${f * 100}%`;
  if (head) head.style.left = `calc(${f * 100}% - 1px)`;
}

export default function PhaseTrack({
  ariaLabel,
  segments = null,
  idleText = '',
  remaining = '',
  subscribeFrame = null,
  staticFrac = null,
}) {
  const fillRef = useRef(null);
  const headRef = useRef(null);
  const nowIndex = segments ? segments.findIndex((s) => s.state === 'now') : -1;
  const now = nowIndex >= 0 ? segments[nowIndex] : null;
  const nowKey = now ? now.key : null;
  const nowIsDot = !!(now && now.dot);

  useEffect(() => {
    if (!nowKey) return undefined;
    // The Speichern dot has no time of its own: it shows half full while saving.
    if (nowIsDot) {
      setFrac(fillRef.current, headRef.current, 0.5);
      return undefined;
    }
    if (staticFrac !== null && staticFrac !== undefined) {
      setFrac(fillRef.current, headRef.current, staticFrac);
      return undefined;
    }
    setFrac(fillRef.current, headRef.current, 0);
    if (typeof subscribeFrame !== 'function') return undefined;
    return subscribeFrame((f) => setFrac(fillRef.current, headRef.current, f && f.frac));
  }, [nowKey, nowIsDot, subscribeFrame, staticFrac]);

  if (!segments || segments.length === 0) {
    return (
      <div className="rec-track" role="group" aria-label={ariaLabel} data-testid="rec-track">
        <div className="rec-segs">
          <div className="rec-sg idle" style={{ flex: 1 }}><span>{idleText}</span></div>
        </div>
        <div className="rec-remain">{remaining}</div>
      </div>
    );
  }

  return (
    <div className="rec-track" role="group" aria-label={ariaLabel} data-testid="rec-track">
      <div className="rec-segs">
        {segments.map((s) => {
          const isNow = s.state === 'now';
          return (
            <div
              key={s.key}
              className={clsx('rec-sg', s.state, s.dot && 'dotseg')}
              style={{ flex: s.dot ? undefined : Math.max(0.001, Number(s.weight) || 1), '--c': phaseColor(s.kind) }}
              title={s.label}
              data-kind={s.kind}
              data-state={s.state || 'upcoming'}
            >
              {isNow ? <i className="rec-sg-fill" ref={fillRef} /> : null}
              {isNow && !s.dot ? <i className="rec-sg-head" ref={headRef} /> : null}
              <span>{s.dot ? <Icon name="save" size={14} /> : s.label}</span>
            </div>
          );
        })}
      </div>
      <div className="rec-remain">{remaining}</div>
    </div>
  );
}
