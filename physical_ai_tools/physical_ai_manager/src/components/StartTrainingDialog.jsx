// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// „Training starten?" (credit system v2, spec §10.1): the student confirms the
// reservation BEFORE the API reserves it. The panel sends one fresh
// `idempotency_key` per confirmed dialog; a retry of the same send (the dialog
// stays open after a failure) reuses it, so a lost answer never reserves twice.
//
// Also the home of the training dialog frame (`TrainingDialog`, shared with
// CancelTrainingDialog) and of the two „no hours" sentences the page and the
// panel both show.

import React, { useEffect, useId, useRef } from 'react';
import ReactDOM from 'react-dom';
import { Key } from './sucher';
import { hoursDe } from '../utils/hoursDe';

export const NO_HOURS_DE = 'Keine freien Trainingsstunden. Frag deine Lehrkraft nach weiteren Stunden.';
export const MEMBER_LIMIT_DE = 'Du hast dein Stundenlimit in der Arbeitsgruppe erreicht. Frag deine Lehrkraft.';
export const NO_HOURS_LABEL_DE = 'Keine freien Stunden';

/**
 * Why a start would reserve nothing right now, from the quota in the auth
 * slice: 'member_limit' (the group's per-member limit, while the group still
 * has free hours — the API's own rule), 'no_hours', or null when a start is
 * possible or the quota is not known yet (unknown is not „no hours").
 */
export function noHoursKind({ nextReservationHours, hoursFree, memberLimitHours, memberLeftHours }) {
  if (typeof nextReservationHours !== 'number' || nextReservationHours >= 1) return null;
  if (typeof memberLimitHours === 'number' && typeof memberLeftHours === 'number'
      && memberLeftHours < 1 && typeof hoursFree === 'number' && hoursFree >= 1) {
    return 'member_limit';
  }
  return 'no_hours';
}

export const START_DIALOG_DE = {
  title: 'Training starten?',
  reserved: (n) =>
    `Reserviert für dieses Training: ${hoursDe(n)}. Berechnet wird jede angefangene Stunde ab dem Start auf dem GPU-Server (mit dem Laden des Datensatzes) – die Wartezeit auf einen GPU-Server zählt nicht. Was übrig bleibt, geht nach dem Training automatisch zurück an dein Guthaben.`,
  onlyLeft: (n) =>
    `Du hast nur noch ${hoursDe(n)}: Das Training stoppt nach ${hoursDe(n)} Trainingszeit, und der letzte Zwischenstand wird veröffentlicht.`,
  fromGroup: (name) => `Die Stunden kommen aus dem Guthaben deiner Arbeitsgruppe „${name}“.`,
  confirm: 'Training starten',
  back: 'Zurück',
  started: (n) =>
    (typeof n === 'number' && Number.isFinite(n)
      ? `Training gestartet – ${hoursDe(n)} reserviert.`
      : 'Training gestartet.'),
  failed: (detail) => `Cloud-Training konnte nicht gestartet werden: ${detail}`,
};

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * The frame of a training dialog: `role="dialog"`, `aria-modal`, labelled by
 * its title, focus on the `[data-autofocus]` action (else the first) and back
 * to the opener on close, Tab kept inside, Esc and a backdrop click close it
 * unless `locked` (a request in flight). Rendered into <body>, so no ancestor's
 * stacking or transform can clip it.
 */
export function TrainingDialog({ title, locked = false, onClose, children, actions, testId }) {
  const id = useId();
  const titleId = `trn-dlg-${id}`;
  const boxRef = useRef(null);
  const openerRef = useRef(typeof document !== 'undefined' ? document.activeElement : null);
  const lockedRef = useRef(locked);
  lockedRef.current = locked;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const opener = openerRef.current;
    const box = boxRef.current;
    const first = box && (box.querySelector('[data-autofocus]:not([disabled])') || box.querySelector(FOCUSABLE));
    if (first && typeof first.focus === 'function') first.focus();
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
    if (e.key !== 'Tab') return;
    const items = Array.from(boxRef.current?.querySelectorAll(FOCUSABLE) || []);
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
  };

  return ReactDOM.createPortal(
    // A click that starts on the backdrop closes the dialog (not while locked).
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !lockedRef.current && onCloseRef.current) onCloseRef.current();
      }}
    >
      <div
        ref={boxRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        data-testid={testId}
        className="w-full max-w-lg max-h-[90vh] overflow-y-auto rounded-[22px] bg-white text-[var(--ink)] shadow-pop p-6 flex flex-col gap-4"
      >
        <h2 id={titleId} className="su-serif text-[28px] leading-tight text-[var(--ink)]">
          {title}
        </h2>
        <div className="flex flex-col gap-3 text-[14.5px] leading-relaxed text-[var(--ink-2)]">
          {children}
        </div>
        <div className="flex items-center justify-end gap-3 flex-wrap pt-1">{actions}</div>
      </div>
    </div>,
    document.body,
  );
}

/**
 * @param {number} hours        what this start reserves (the quota's next_reservation_hours)
 * @param {number} maxHours     the per-training maximum (max_hours_per_training)
 * @param {string|null} workgroupName  set when the hours come from a group
 */
export default function StartTrainingDialog({ hours, maxHours, workgroupName, busy = false, onConfirm, onCancel }) {
  const lessThanMax = typeof maxHours === 'number' && typeof hours === 'number' && hours < maxHours;
  return (
    <TrainingDialog
      title={START_DIALOG_DE.title}
      locked={busy}
      onClose={onCancel}
      testId="start-training-dialog"
      actions={(
        <>
          <Key variant="default" onClick={onCancel} disabled={busy}>
            {START_DIALOG_DE.back}
          </Key>
          <Key variant="signal" icon="play" onClick={onConfirm} disabled={busy} data-autofocus="">
            {START_DIALOG_DE.confirm}
          </Key>
        </>
      )}
    >
      <p>{START_DIALOG_DE.reserved(hours)}</p>
      {lessThanMax ? <p>{START_DIALOG_DE.onlyLeft(hours)}</p> : null}
      {workgroupName ? <p>{START_DIALOG_DE.fromGroup(workgroupName)}</p> : null}
    </TrainingDialog>
  );
}
