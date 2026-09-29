// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The end of a session on the stage (spec §3.11): three steps — the dataset is
// closed, uploaded to Hugging Face, entered in the student's dataset list —
// each done, running, failed, skipped or unknown, an upload bar while the
// upload runs, where the dataset was saved, and the next actions. Which step
// is in which state is decided by the page (finishSteps); this only draws it.

import React from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';

const STEP_ICON = { done: 'check', failed: 'failed', now: 'loading' };

function StepDot({ state }) {
  const name = STEP_ICON[state];
  if (!name) return <span className="rec-sdot" />;
  return (
    <span className="rec-sdot">
      <Icon
        name={name}
        size={state === 'now' ? 14 : 12}
        strokeWidth={3}
        className={state === 'now' ? 'animate-spin' : ''}
      />
    </span>
  );
}

export default function FinishCard({
  eyebrow,
  title,
  note = '',
  steps = [],
  barPct = null,
  savedAs = null,
  hint = '',
  actions = [],
  onAction,
}) {
  return (
    <div className="rec-statecard" data-testid="rec-finish-card">
      <div className="rec-finish" role="status" aria-live="polite">
        <div>
          <div className="rec-eyebrow">{eyebrow}</div>
          <h2>{title}</h2>
          {note ? <div className="rec-finish-note" data-testid="rec-finish-note">{note}</div> : null}
        </div>
        <div className="rec-steps">
          {steps.map((s) => (
            <div key={s.key} className={clsx('rec-step', s.state)} data-state={s.state || 'upcoming'} data-step={s.key}>
              <StepDot state={s.state} />
              <span className="rec-step-label">
                {s.label}
                {s.detail ? <span className="rec-step-detail">{s.detail}</span> : null}
              </span>
              {s.pct ? <span className="rec-step-pct">{s.pct}</span> : null}
            </div>
          ))}
        </div>
        {barPct !== null && barPct !== undefined ? (
          <div className="rec-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(barPct)}>
            <i style={{ width: `${Math.max(0, Math.min(100, barPct))}%` }} />
          </div>
        ) : null}
        {savedAs ? (
          <div className="rec-savename" style={{ margin: 0 }}>
            <Icon name="lock" size={16} />
            <div>
              {savedAs.label}
              <br />
              <code>{savedAs.repoId}</code>
            </div>
          </div>
        ) : null}
        {actions.length ? (
          <div className="rec-finish-actions">
            {actions.map((a) => (
              <button
                key={a.id}
                type="button"
                className={clsx('rec-btn md', a.variant || 'ghost')}
                onClick={() => onAction && onAction(a.id)}
              >
                {a.label}
              </button>
            ))}
            {hint ? <span className="rec-finish-hint">{hint}</span> : null}
          </div>
        ) : hint ? <span className="rec-finish-hint">{hint}</span> : null}
      </div>
    </div>
  );
}
