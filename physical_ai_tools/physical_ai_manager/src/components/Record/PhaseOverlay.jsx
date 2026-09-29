// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The Aufwärmen / Zurücksetzen overlay over the cameras (spec §3.5, the owner's
// timed-only phase design): a countdown ring with the seconds left, the phase
// name, one line of what to do, what comes next, and a small moving picture —
// the hand on the Leader-Arm while warming up, the arm and the cube going back
// to the start mark while resetting. The last three seconds pop with each new
// number; the first frame of a recording shows a green „Los!".
//
// The ring and the pictograms are drawn inline, as illustrations
// (noIconGlyphs.test.js::INLINE_SVG_ALLOWED names this file). The ring moves
// every animation frame through `subscribeFrame` and refs, never React state.
// Under reduced motion the ring steps once per second from `secondsLeft`, the
// number does not pop and „Los!" only fades (record.css does the rest).
//
// A phase whose time is 0 s gets no overlay, and an overlay that ends fades
// out showing the phase it was showing (the text is kept while it fades).

import React, { useEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import { PHASE_COLOR } from './PhaseTrack';

export const RING_CIRCUMFERENCE = 552.92; // 2π · 88
export const LOS_MS = 700;

/**
 * The F6d wording rule, pure: which episode numbers the overlay names.
 * Warm-up is always before episode 1. After a save the kicker names the
 * episode just saved („Nach Episode n"; „Vor Episode 1" when none is saved
 * yet) and the next line names the NEXT one („Episode n+1 von N").
 * `copy` supplies the German sentences: {warmKicker, warmTitle, warmLine,
 * resetKicker(n), resetTitle, resetLine, next(n, N, E) → {pre, bold, post},
 * pictoLeader, pictoBack}.
 */
export function overlayText(phase, { saved = 0, total = 0, episodeTime = 0 } = {}, copy) {
  if (phase !== 'warmup' && phase !== 'reset') return null;
  const next = copy.next(Math.min(saved + 1, Math.max(total, 1)), total, episodeTime);
  if (phase === 'warmup') {
    return {
      kicker: copy.warmKicker,
      title: copy.warmTitle,
      line: copy.warmLine,
      next,
      picto: copy.pictoLeader,
    };
  }
  return {
    kicker: saved >= 1 ? copy.resetKicker(saved) : copy.warmKicker,
    title: copy.resetTitle,
    line: copy.resetLine,
    next,
    picto: copy.pictoBack,
  };
}

function WarmupPicture({ label }) {
  return (
    <svg className="rec-picto" viewBox="0 0 190 96" aria-hidden="true" data-picto="warmup">
      <rect x="10" y="80" width="170" height="4" rx="2" fill="#3A4448" />
      <g stroke="#E8EBEC" strokeWidth="6" strokeLinecap="round" fill="none"><path d="M40 78 V40 L80 30" /></g>
      <circle cx="40" cy="40" r="5" fill="var(--c)" />
      <circle cx="80" cy="30" r="5" fill="var(--c)" />
      <g className="rec-p-hand">
        <path d="M86 44 q10 -22 26 -10 l8 6 q6 5 0 10 l-16 8 q-12 4 -18 -14z" fill="var(--c)" opacity=".95" />
      </g>
      <text x="120" y="76" fill="#B4B9BC" fontSize="12" fontFamily="Inter Tight, sans-serif">{label}</text>
    </svg>
  );
}

function ResetPicture({ label }) {
  return (
    <svg className="rec-picto" viewBox="0 0 190 96" aria-hidden="true" data-picto="reset">
      <rect x="10" y="80" width="170" height="4" rx="2" fill="#3A4448" />
      <rect x="52" y="64" width="16" height="16" rx="2" fill="none" stroke="#B4B9BC" strokeWidth="1.5" strokeDasharray="3 3" />
      <g className="rec-p-cube"><rect x="53" y="65" width="14" height="14" rx="2" fill="#E0574A" /></g>
      <g className="rec-p-arm">
        <path d="M40 78 V38" stroke="#E8EBEC" strokeWidth="6" strokeLinecap="round" />
        <path d="M40 38 L70 28" stroke="#E8EBEC" strokeWidth="6" strokeLinecap="round" />
        <circle cx="40" cy="38" r="5" fill="var(--c)" />
      </g>
      <text x="96" y="30" fill="#B4B9BC" fontSize="12" fontFamily="Inter Tight, sans-serif">{label}</text>
    </svg>
  );
}

function NextLine({ next }) {
  if (!next) return null;
  if (typeof next === 'string') return <div className="rec-ov-next">{next}</div>;
  return (
    <div className="rec-ov-next">
      {next.pre}
      <b>{next.bold}</b>
      {next.post}
    </div>
  );
}

export default function PhaseOverlay({
  phase = null,
  text = null,
  totalS = 0,
  secondsLeft = 0,
  subscribeFrame = null,
  recording = false,
  losText = '',
  reducedMotion = false,
}) {
  const shown = (phase === 'warmup' || phase === 'reset') && totalS > 0 && !!text;
  // Keep what was shown while the overlay fades out (.3 s).
  const lastRef = useRef({ phase: null, text: null });
  if (shown) lastRef.current = { phase, text };
  const view = shown ? { phase, text } : lastRef.current;
  const ringRef = useRef(null);

  // Ring: every frame from the clock, or once per second under reduced motion.
  useEffect(() => {
    const ring = ringRef.current;
    if (!shown || !ring) return undefined;
    const paint = (frac) => {
      const f = Math.max(0, Math.min(1, Number.isFinite(frac) ? frac : 0));
      ring.style.strokeDashoffset = (RING_CIRCUMFERENCE * f).toFixed(1);
    };
    if (reducedMotion || typeof subscribeFrame !== 'function') {
      paint(totalS > 0 ? 1 - (Number(secondsLeft) || 0) / totalS : 0);
      return undefined;
    }
    return subscribeFrame((f) => paint(f && f.frac));
  }, [shown, reducedMotion, subscribeFrame, secondsLeft, totalS]);

  // „Los!": only on a RECORDING entry observed while mounted (not on a page
  // opened in the middle of a recording).
  const [losKey, setLosKey] = useState(0);
  const prevRecordingRef = useRef(recording);
  useEffect(() => {
    const was = prevRecordingRef.current;
    prevRecordingRef.current = recording;
    if (recording && !was) setLosKey((k) => k + 1);
  }, [recording]);
  useEffect(() => {
    if (!losKey) return undefined;
    const t = setTimeout(() => setLosKey(0), LOS_MS);
    return () => clearTimeout(t);
  }, [losKey]);

  const color = PHASE_COLOR[view.phase] || PHASE_COLOR.reset;
  const pops = shown && !reducedMotion && secondsLeft > 0 && secondsLeft <= 3;
  const t = view.text;

  return (
    <>
      <div
        className={clsx('rec-ov', shown && 'show')}
        style={{ '--c': color }}
        aria-hidden={shown ? undefined : 'true'}
        data-testid="rec-phase-overlay"
        data-phase={shown ? phase : 'none'}
      >
        {t ? (
          <div className="rec-ov-inner">
            <div className="rec-ringbox">
              {view.phase === 'warmup' ? (
                <>
                  <span className="rec-halo" data-testid="rec-halo" />
                  <span className="rec-halo h2" />
                </>
              ) : null}
              {view.phase === 'reset' ? (
                <svg className="rec-orbit" viewBox="-14 -14 238 238" aria-hidden="true" data-testid="rec-orbit">
                  <path d="M105 -6 a111 111 0 0 0 -62 19" fill="none" stroke="var(--c)" strokeWidth="5" strokeLinecap="round" />
                  <path d="M36 6 l6 8 -10 3" fill="none" stroke="var(--c)" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : null}
              <svg viewBox="0 0 210 210" aria-hidden="true">
                <circle className="rec-ring-track" cx="105" cy="105" r="88" />
                <circle
                  ref={ringRef}
                  className="rec-ring-prog"
                  cx="105"
                  cy="105"
                  r="88"
                  strokeDasharray={RING_CIRCUMFERENCE}
                  strokeDashoffset="0"
                  data-testid="rec-ring"
                />
              </svg>
              <div className={clsx('rec-num', pops && 'pop')} data-testid="rec-num">
                <span key={shown ? secondsLeft : 'idle'}>
                  {shown ? secondsLeft : ''}
                  <small>s</small>
                </span>
              </div>
            </div>
            <div className="rec-ov-text">
              <span className="rec-ov-kicker">{t.kicker}</span>
              <div className="rec-ov-title">{t.title}</div>
              <div className="rec-ov-line">{t.line}</div>
              {view.phase === 'warmup' ? <WarmupPicture label={t.picto} /> : <ResetPicture label={t.picto} />}
              <NextLine next={t.next} />
            </div>
          </div>
        ) : null}
      </div>
      {losKey ? (
        <div className="rec-los" key={losKey} data-testid="rec-los">
          <b>{losText}</b>
        </div>
      ) : null}
    </>
  );
}
