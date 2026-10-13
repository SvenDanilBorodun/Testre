// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// „Aufgabe" (spec §3.12, replaces InfoPanel on the Aufnahme page): the task
// name and instruction, the four steppers of the timed loop with an estimate
// bar, the name the dataset will be saved under, and a collapsed „Erweitert"
// with exactly five rows — upload after finishing, visibility, Benutzer-ID,
// frames per second, keywords. Everything is locked (with the reason shown)
// whenever the page says so: not connected, a session running or finishing.
//
// Presentational: the values, the limits, the German words and the change
// handler come from the page. Its buttons give the focus back to the page
// after a mouse click, so Space still starts a recording instead of pressing
// „+" once more (keyboard focus stays where the student put it). Validation runs when Start is pressed, not here;
// a refused field is only marked.
//
// Sucher (§8): a plate with the title in the serif, well fields, and the
// four steppers as the shared Stepper primitive (the same DOM as the local
// stepper it replaced; ↑/↓ and the wheel over the number step it while the
// form is unlocked).

import React, { useState } from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';
import TagInput from '../TagInput';
import { Plate, Stepper } from '../sucher';
import { releasePointerFocus } from './ActionBar';

export default function TaskCard({
  labels,
  form,
  onChange,
  editable = false,
  lockedReason = '',
  steppers = [],
  estimate = null,
  saveName = null,
  hfUsers = { list: [], reload: null, loading: false },
  invalid = null,
  taskNameMax = 60,
}) {
  const [advOpen, setAdvOpen] = useState(false);
  const locked = !editable;
  const change = (field, value) => { if (!locked && onChange) onChange(field, value); };
  const users = Array.isArray(hfUsers.list) ? hfUsers.list : [];
  const userOptions = form.userId && !users.includes(form.userId) ? [form.userId, ...users] : users;
  const markInvalid = (field) => (invalid && invalid.field === field ? invalid.messageDe : '');

  return (
    <Plate as="section" className="rec-card rec-taskcard" aria-labelledby="rec-task-title" data-testid="rec-task-card">
      <div className="rec-card-h">
        <h3 id="rec-task-title" className="su-serif"><Icon name="task" size={17} />{labels.title}</h3>
        <span className="rec-chipwrap">
          {locked ? <Icon name="lock" size={13} /> : null}
          <span className={clsx('rec-chip', !locked && 'ok')}>{locked ? labels.chipLocked : labels.chipEditable}</span>
        </span>
      </div>
      <div className="rec-card-b">
        <div className="rec-field">
          <label htmlFor="rec-task-name">{labels.name}</label>
          <input
            id="rec-task-name"
            className="su-field"
            value={form.taskName || ''}
            maxLength={taskNameMax}
            placeholder={labels.namePlaceholder}
            disabled={locked}
            aria-invalid={markInvalid('taskName') ? 'true' : undefined}
            onChange={(e) => change('taskName', e.target.value)}
          />
          {markInvalid('taskName') ? <span className="rec-invalid">{markInvalid('taskName')}</span> : null}
        </div>
        <div className="rec-field">
          <label htmlFor="rec-task-instruction">{labels.instruction}</label>
          <textarea
            id="rec-task-instruction"
            className="su-field"
            value={form.taskInstruction || ''}
            placeholder={labels.instructionPlaceholder}
            disabled={locked}
            aria-invalid={markInvalid('taskInstruction') ? 'true' : undefined}
            onChange={(e) => change('taskInstruction', e.target.value)}
          />
          {markInvalid('taskInstruction') ? <span className="rec-invalid">{markInvalid('taskInstruction')}</span> : null}
        </div>
        <div className="rec-field">
          <span className="rec-flabel">{labels.flow}</span>
          <div className="rec-steppers">
            {steppers.map((s) => (
              <Stepper
                key={s.field}
                className="rec-stp"
                field={s.field}
                label={s.label}
                value={form[s.field]}
                min={s.min}
                max={s.max}
                unit={s.unit}
                color={s.color}
                disabled={locked}
                onChange={change}
                decLabel={labels.dec(s.label)}
                incLabel={labels.inc(s.label)}
              />
            ))}
          </div>
          {estimate ? (
            <div className="rec-estimate" data-testid="rec-estimate">
              <div className="rec-est-bar" aria-hidden="true">
                {estimate.parts.map(([seconds, color], i) => (
                  <span key={`${i}:${color}`} style={{ flex: seconds, background: color }} />
                ))}
              </div>
              <div className="rec-help">{estimate.text}</div>
            </div>
          ) : null}
        </div>
        {saveName ? (
          <div className={clsx('rec-savename', saveName.public && 'public')} data-testid="rec-savename">
            <Icon name="lock" size={16} />
            <div>
              {saveName.text}
              <br />
              <code>{saveName.repoId}</code>
            </div>
          </div>
        ) : null}
        {locked && lockedReason ? (
          <div className="rec-locked" data-testid="rec-locked">
            <Icon name="lock" size={14} />
            <span>{lockedReason}</span>
          </div>
        ) : null}
        <button
          type="button"
          className="rec-adv-t"
          aria-expanded={advOpen ? 'true' : 'false'}
          aria-controls="rec-adv"
          onClick={(e) => { releasePointerFocus(e); setAdvOpen((o) => !o); }}
        >
          <Icon name="chevronRight" size={14} />
          {labels.advanced}
        </button>
        {advOpen ? (
          <div className="rec-adv" id="rec-adv" data-testid="rec-adv">
            <div className="rec-arow">
              <span id="rec-adv-upload">{labels.upload}</span>
              <button
                type="button"
                className="rec-sw"
                role="switch"
                aria-checked={form.pushToHub ? 'true' : 'false'}
                aria-labelledby="rec-adv-upload"
                disabled={locked}
                onClick={(e) => { releasePointerFocus(e); change('pushToHub', !form.pushToHub); }}
              />
            </div>
            <div className="rec-arow">
              <span>{labels.visibility}</span>
              <div className="rec-segl" role="group" aria-label={labels.visibility}>
                <button
                  type="button"
                  aria-pressed={form.privateMode !== false}
                  disabled={locked || !form.pushToHub}
                  onClick={(e) => { releasePointerFocus(e); change('privateMode', true); }}
                >
                  {labels.private}
                </button>
                <button
                  type="button"
                  aria-pressed={form.privateMode === false}
                  disabled={locked || !form.pushToHub}
                  onClick={(e) => { releasePointerFocus(e); change('privateMode', false); }}
                >
                  {labels.public}
                </button>
              </div>
            </div>
            <div className="rec-arow">
              <label htmlFor="rec-adv-user">{labels.userId}</label>
              <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                {userOptions.length ? (
                  <select
                    id="rec-adv-user"
                    className="rec-mini su-field"
                    style={{ width: 130 }}
                    value={form.userId || ''}
                    disabled={locked}
                    onChange={(e) => change('userId', e.target.value)}
                  >
                    {!form.userId ? <option value="" disabled>{labels.noUserId}</option> : null}
                    {userOptions.map((u) => <option key={u} value={u}>{u}</option>)}
                  </select>
                ) : (
                  <span className="rec-help" id="rec-adv-user">{labels.noUserId}</span>
                )}
                <button
                  type="button"
                  className="rec-linkbtn"
                  disabled={typeof hfUsers.reload !== 'function' || hfUsers.loading}
                  onClick={(e) => { releasePointerFocus(e); if (hfUsers.reload) hfUsers.reload(); }}
                >
                  <Icon name="refresh" size={13} className={hfUsers.loading ? 'animate-spin' : ''} />
                  {labels.reload}
                </button>
              </span>
            </div>
            <div className="rec-arow">
              <label htmlFor="rec-adv-fps">{labels.fps}</label>
              <input
                id="rec-adv-fps"
                className="rec-mini su-field"
                type="number"
                min={1}
                max={60}
                step={1}
                value={form.fps ?? ''}
                disabled={locked}
                aria-invalid={markInvalid('fps') ? 'true' : undefined}
                onChange={(e) => change('fps', e.target.value === '' ? '' : Number(e.target.value))}
              />
            </div>
            <div className="rec-arow stack">
              <span>{labels.tags}</span>
              <TagInput tags={form.tags || []} onChange={(tags) => change('tags', tags)} disabled={locked} />
            </div>
          </div>
        ) : null}
      </div>
    </Plate>
  );
}
