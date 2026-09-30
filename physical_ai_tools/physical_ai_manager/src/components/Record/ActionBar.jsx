// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The action bar under the stage (spec §3.4 / §3.5 / §3.6): the phase pill
// (what is happening now, and how long it still runs), the Episodenleiste,
// the buttons of this phase with their keys, and the sound switch. When the
// page asks a question — „Beenden" during a running episode — the question and
// its answers take the bar's place until they are answered or Esc closes it.
//
// It is a flow sibling after the stage, never absolute or fixed (the page
// puts it with the problem banner into one footer, which record.css makes
// sticky on a narrow window). Which buttons exist, and whether they may be
// pressed, is the page's decision.
//
// ONE ROW, MEASURED. The mockup's row does not always fit: while recording,
// three buttons with the spelled-out „Strg+Umschalt+X" (H9), the pill, the
// dots and the sound switch are wider than the stage column at 1440 and
// 1366 px. So after every layout the bar checks whether it fits in one row of
// its normal height and, only if it does not, sheds one thing at a time, in
// this order: the key label of Beenden, the other key labels, the dots'
// label, the dots' size. Only a bar with THREE command buttons sheds at all;
// with fewer, the dots may wrap onto a second line, like the mockup. The
// shortcuts keep working, stay in aria-keyshortcuts and are named in the
// button title. When everything fits, nothing is shed.
//
// FOCUS. A mouse click must not leave the focus on a button: Space means
// „Aufnahme starten" on this page, and the browser would press the focused
// button again instead (a click on „Beenden", then Space). Keyboard users
// keep their focus (a key-initiated click has `detail === 0`).

import React, { useCallback, useLayoutEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';

/**
 * onClick helper for the page's buttons: after a POINTER click, give the focus
 * back to the page, so Space is „Aufnahme starten" again and not „press this
 * button once more". A keyboard click (Enter/Space, `detail === 0`) keeps the
 * focus where the student put it.
 */
export function releasePointerFocus(event) {
  if (!event || !(event.detail > 0)) return;
  const el = event.currentTarget;
  if (el && typeof el.blur === 'function') el.blur();
}

// The shedding steps, cumulative, for a bar with three command buttons
// (recording / saving). A bar with fewer buttons never sheds.
const SHED_KEYS = ['endKey', 'keys', 'label', 'dots'];
const SHED_CLASS = {
  endKey: 'rec-shed-endkey',
  keys: 'rec-shed-keys',
  label: 'rec-shed-label',
  dots: 'rec-shed-dots',
};
// The bar's own height in one row (record.css `min-height: 80px`) + rounding.
const ONE_ROW_MAX_PX = 82;

/** Does the bar fit in one row of its normal height? Real layout only. */
export function barFitsOneRow(bar) {
  if (!bar) return true;
  if (bar.scrollWidth > bar.clientWidth + 1) return false;
  return !(bar.offsetHeight > ONE_ROW_MAX_PX);
}

function Kbd({ children }) {
  return children ? <kbd className="rec-kbd">{children}</kbd> : null;
}

function BarButton({ b, onAction }) {
  return (
    <button
      type="button"
      className={clsx('rec-btn', b.size || 'md', b.variant || 'ghost')}
      disabled={!!b.disabled}
      title={b.title || b.kbd || undefined}
      aria-keyshortcuts={b.shortcut || undefined}
      onClick={(e) => {
        releasePointerFocus(e);
        if (onAction) onAction(b.id);
      }}
      data-action={b.id}
    >
      {b.spinning ? (
        <Icon name="loading" className="animate-spin" size={18} />
      ) : b.icon ? (
        <Icon name={b.icon} size={18} />
      ) : null}
      {b.label}
      {!b.disabled ? <Kbd>{b.kbd}</Kbd> : null}
    </button>
  );
}

export default function ActionBar({
  ariaLabel,
  pill,
  dots = null,
  dotCount = 0,
  buttons = [],
  question = null,
  onAction,
  onAnswer,
  mute = null,
  fitsOneRow = barFitsOneRow,
}) {
  const barRef = useRef(null);
  const threeButtons = buttons.length >= 3;
  const ladder = threeButtons ? SHED_KEYS : [];
  const [step, setStep] = useState(0);
  const [width, setWidth] = useState(0);

  // What the row holds: a change starts the measuring from „nothing shed".
  const contentKey = [
    buttons.map((b) => `${b.id}:${b.label}:${b.kbd || ''}:${b.disabled ? 1 : 0}`).join('|'),
    dotCount,
    pill ? `${pill.title}:${pill.sub}` : '',
    question ? 'q' : '',
    width,
  ].join('#');
  const measuredKeyRef = useRef(null);

  useLayoutEffect(() => {
    if (measuredKeyRef.current !== contentKey) {
      measuredKeyRef.current = contentKey;
      if (step !== 0) {
        setStep(0);
        return;
      }
    }
    if (question || step >= ladder.length) return;
    if (!fitsOneRow(barRef.current, step)) setStep((s) => s + 1);
    // Bounded: each pass either stops or moves one step up the finite ladder.
  }, [contentKey, question, step, ladder.length, fitsOneRow]);

  // Re-measure when the column changes width (window resize, scaling).
  const onBar = useCallback((node) => {
    barRef.current = node;
  }, []);
  useLayoutEffect(() => {
    const node = barRef.current;
    if (!node || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0]?.contentRect?.width || 0);
      setWidth((prev) => (prev === w ? prev : w));
    });
    ro.observe(node);
    return () => ro.disconnect();
  }, [question]);

  const shed = ladder.slice(0, step);
  const shedClasses = shed.map((k) => SHED_CLASS[k]);

  const muteButton = mute ? (
    <button
      type="button"
      className="rec-mute"
      onClick={(e) => {
        releasePointerFocus(e);
        mute.onToggle();
      }}
      aria-label={mute.muted ? mute.labelOn : mute.labelOff}
      title={mute.muted ? mute.labelOn : mute.labelOff}
      aria-pressed={mute.muted ? 'true' : 'false'}
    >
      <Icon name={mute.muted ? 'volumeOff' : 'volumeOn'} size={18} />
    </button>
  ) : null;

  if (question) {
    return (
      <div className="rec-actionbar" role="region" aria-label={ariaLabel} data-testid="rec-actionbar" ref={onBar}>
        <div className="rec-confirm" role="group" aria-label={question.title} data-testid="rec-question">
          <div className="q">
            <b>{question.title}</b>
            <span>{question.sub}</span>
          </div>
          {question.answers.map((a) => (
            <button
              key={a.id}
              type="button"
              className={clsx('rec-btn md', a.variant || 'ghost')}
              onClick={(e) => {
                releasePointerFocus(e);
                if (onAnswer) onAnswer(a.id);
              }}
              data-answer={a.id}
            >
              {a.icon ? <Icon name={a.icon} size={18} /> : null}
              {a.label}
              <Kbd>{a.kbd}</Kbd>
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div
      className={clsx('rec-actionbar', shedClasses)}
      role="region"
      aria-label={ariaLabel}
      data-testid="rec-actionbar"
      data-shed={shed.join(' ') || undefined}
      ref={onBar}
    >
      {pill ? (
        <div className="rec-phasepill" style={{ '--c': pill.color }} data-testid="rec-pill">
          <span className="rec-pp-ico">
            {pill.icon ? (
              <Icon
                name={pill.icon}
                size={22}
                className={pill.icon === 'loading' ? 'animate-spin' : ''}
              />
            ) : null}
          </span>
          <span className="rec-pp-t">
            <b>{pill.title}</b>
            {pill.sub ? <span>{pill.sub}</span> : null}
          </span>
        </div>
      ) : null}
      {dots}
      {buttons.length ? (
        <div className="rec-btns">
          {buttons.map((b) => <BarButton key={b.id} b={b} onAction={onAction} />)}
        </div>
      ) : null}
      {muteButton}
    </div>
  );
}
