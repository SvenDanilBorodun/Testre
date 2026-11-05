// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// „Ausgleich gutschreiben" (owner decision D4, spec §10.5): the admin credits
// hours straight into the account that PAID a charged training — preset from
// the charge row (its account and training), funded from outside the teacher
// pool — with a mandatory reason the teacher sees in the history. Naming the
// training also gives the member's group-limit headroom back [Q-D]. The
// reason can never be blank: the key stays off until it has text.

import React, { useState } from 'react';
import toast from 'react-hot-toast';
import Modal from '../teacher/Modal';
import { Btn } from '../EbUI';
import { compensate } from '../../services/adminApi';
import { hoursDe } from '../../utils/hoursDe';
import { REASON_MAX, apiErrorText, useRequestIdFor } from './TeacherCreditsModal';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

export const MIN_COMP_HOURS = 1;
export const MAX_COMP_HOURS = 100;

export const COMPENSATION_DE = {
  title: 'Ausgleich gutschreiben',
  text: 'Die Stunden werden direkt dem Guthaben gutgeschrieben, das das Training bezahlt hat – nicht aus dem Lehrer-Pool.',
  hours: 'Stunden',
  reason: 'Grund (Pflicht)',
  reasonHint: 'Die Lehrkraft sieht diesen Grund im Stundenverlauf. Ohne Namen von Schülerinnen und Schülern.',
  confirm: 'Gutschreiben',
  back: 'Zurück',
  done: (h) => `Ausgleich gutgeschrieben: ${hoursDe(h)}`,
};

const clampHours = (h) => {
  const n = Math.round(Number(h));
  if (!Number.isFinite(n)) return MIN_COMP_HOURS;
  return Math.min(MAX_COMP_HOURS, Math.max(MIN_COMP_HOURS, n));
};

/**
 * @param {object} preset  {target_kind: 'student'|'group', target_id, training_id, hours}
 *                         — the charge row's paying account, training and charged hours
 */
export default function CompensationModal({ token, preset, onClose, onDone }) {
  const [hours, setHours] = useState(String(clampHours(preset?.hours)));
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const requestIdFor = useRequestIdFor();

  const parsed = Number(hours);
  const hoursValid = hours !== '' && Number.isInteger(parsed) && parsed >= MIN_COMP_HOURS && parsed <= MAX_COMP_HOURS;
  const trimmed = reason.trim();
  const canSend = hoursValid && trimmed.length > 0 && trimmed.length <= REASON_MAX && !busy;

  const submit = async (e) => {
    e?.preventDefault();
    if (!canSend) return;
    const body = {
      target_kind: preset.target_kind,
      target_id: preset.target_id,
      hours: parsed,
      reason: trimmed,
      training_id: preset.training_id ?? null,
    };
    body.request_id = requestIdFor(JSON.stringify(body));
    setBusy(true);
    setError(null);
    try {
      const res = await compensate(token, body);
      toast.success(COMPENSATION_DE.done(parsed));
      onDone?.(res);
      onClose();
    } catch (err) {
      setError(apiErrorText(err));
      setBusy(false);
    }
  };

  return (
    <Modal
      title={COMPENSATION_DE.title}
      onClose={() => { if (!busy) onClose(); }}
      footer={(
        <>
          <Btn variant="ghost" onClick={onClose} disabled={busy}>{COMPENSATION_DE.back}</Btn>
          <Btn variant="primary" type="submit" form="compensation-form" disabled={!canSend}>
            {COMPENSATION_DE.confirm}
          </Btn>
        </>
      )}
    >
      <form id="compensation-form" onSubmit={submit} className="flex flex-col gap-4">
        <p className="text-sm text-[var(--ink-2)] leading-snug">{COMPENSATION_DE.text}</p>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">{COMPENSATION_DE.hours}</span>
          <input
            type="number"
            className={inputClass}
            value={hours}
            onChange={(e) => setHours(e.target.value.replace(/[^0-9]/g, ''))}
            min={MIN_COMP_HOURS}
            max={MAX_COMP_HOURS}
            inputMode="numeric"
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">{COMPENSATION_DE.reason}</span>
          <input
            type="text"
            className={inputClass}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={REASON_MAX}
            required
            autoFocus
          />
          <p className="text-[11px] text-[var(--ink-3)] mt-1.5 leading-snug">{COMPENSATION_DE.reasonHint}</p>
        </label>
        {error && <p role="alert" className="text-sm text-[color:var(--danger)]">{error}</p>}
      </form>
    </Modal>
  );
}
