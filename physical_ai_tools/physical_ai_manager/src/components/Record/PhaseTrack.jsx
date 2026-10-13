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
// Sucher (§8): a second row under the segments — the „Lichtband" in the
// running segment's colour, its label (the running segment's name, which is
// why that segment itself shows no text: it is named once, in row 2) and the
// seconds left (`.rec-remain`).
//
// The fill moves every animation frame, but the page must not re-render at
// 60 Hz: `subscribeFrame(fn)` hands `fn({frac})` each frame and this component
// writes the width straight into the DOM through refs (no React state). The
// Lichtband gets the same frames through ONE subscription of this component
// (a local fan-out), so the clock still has one subscriber per Phasenleiste.

import React, { useCallback, useEffect, useRef } from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';
import { Lichtband } from '../sucher';

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
  return PHASE_COLOR[kind] || '#4A4944';
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
  // The Lichtband's frames: one local set, fed by this component's one clock
  // subscription (never a second subscription to the clock).
  const bandSubsRef = useRef(new Set());
  const bandSubscribe = useCallback((fn) => {
    const subs = bandSubsRef.current;
    subs.add(fn);
    return () => { subs.delete(fn); };
  }, []);
  const nowIndex = segments ? segments.findIndex((s) => s.state === 'now') : -1;
  const now = nowIndex >= 0 ? segments[nowIndex] : null;
  const nowKey = now ? now.key : null;
  const nowIsDot = !!(now && now.dot);

  useEffect(() => {
    if (!nowKey) return undefined;
    const paint = (frac) => {
      setFrac(fillRef.current, headRef.current, frac);
      bandSubsRef.current.forEach((fn) => fn({ frac }));
    };
    // The Speichern dot has no time of its own: it shows half full while saving.
    if (nowIsDot) {
      paint(0.5);
      return undefined;
    }
    if (staticFrac !== null && staticFrac !== undefined) {
      paint(staticFrac);
      return undefined;
    }
    paint(0);
    if (typeof subscribeFrame !== 'function') return undefined;
    return subscribeFrame((f) => paint(f && f.frac));
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
    <div className="rec-track has-band" role="group" aria-label={ariaLabel} data-testid="rec-track">
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
              {s.dot ? <span><Icon name="save" size={14} /></span> : null}
              {!s.dot && !isNow ? <span>{s.label}</span> : null}
            </div>
          );
        })}
      </div>
      <div className="rec-bandrow">
        {now ? (
          <Lichtband
            key={now.key}
            className="rec-band"
            count={32}
            color={phaseColor(now.kind)}
            label={now.label}
            subscribeFrame={bandSubscribe}
          />
        ) : <div className="rec-band rec-band-idle" />}
        <div className="rec-remain">{remaining}</div>
      </div>
    </div>
  );
}
