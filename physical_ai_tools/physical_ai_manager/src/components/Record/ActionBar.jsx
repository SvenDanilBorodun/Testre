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
// It is a flow sibling after the stage, never absolute or fixed; on a narrow
// window record.css makes it sticky at the bottom of its column. Which buttons
// exist, and whether they may be pressed, is the page's decision.

import React from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';

function Kbd({ children }) {
  return children ? <kbd className="rec-kbd">{children}</kbd> : null;
}

function BarButton({ b, onAction }) {
  return (
    <button
      type="button"
      className={clsx('rec-btn', b.size || 'md', b.variant || 'ghost')}
      disabled={!!b.disabled}
      title={b.title || undefined}
      aria-keyshortcuts={b.shortcut || undefined}
      onClick={() => onAction && onAction(b.id)}
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
  buttons = [],
  question = null,
  onAction,
  onAnswer,
  mute = null,
}) {
  const muteButton = mute ? (
    <button
      type="button"
      className="rec-mute"
      onClick={mute.onToggle}
      aria-label={mute.muted ? mute.labelOn : mute.labelOff}
      title={mute.muted ? mute.labelOn : mute.labelOff}
      aria-pressed={mute.muted ? 'true' : 'false'}
    >
      <Icon name={mute.muted ? 'volumeOff' : 'volumeOn'} size={18} />
    </button>
  ) : null;

  if (question) {
    return (
      <div className="rec-actionbar" role="region" aria-label={ariaLabel} data-testid="rec-actionbar">
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
              onClick={() => onAnswer && onAnswer(a.id)}
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
    <div className="rec-actionbar" role="region" aria-label={ariaLabel} data-testid="rec-actionbar">
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
      <div className="rec-spacer" />
      {buttons.length ? (
        <div className="rec-btns">
          {buttons.map((b) => <BarButton key={b.id} b={b} onAction={onAction} />)}
        </div>
      ) : null}
      {muteButton}
    </div>
  );
}
