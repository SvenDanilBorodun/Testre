// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// „Stunden vergeben" / „Stunden entziehen" (credit system v2, spec §10.5): the
// admin moves a teacher's FREE pool hours by a delta, never sets an absolute
// amount (decision 8), with an optional reason. One request_id per change: a
// retry of the same change (after a failure) reuses it, so it cannot count
// twice; editing the change mints a new one.

import React, { useRef, useState } from 'react';
import toast from 'react-hot-toast';
import Modal from '../teacher/Modal';
import { Btn } from '../EbUI';
import { changeTeacherCredits } from '../../services/adminApi';
import { newRequestId } from '../../services/cloudTrainingApi';
import { hoursDe } from '../../utils/hoursDe';

const inputClass =
  'w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition';

export const MAX_ADMIN_HOURS = 10000;
export const REASON_MAX = 200;

export const TEACHER_CREDITS_DE = {
  grantTitle: 'Stunden vergeben',
  revokeTitle: 'Stunden entziehen',
  hours: 'Stunden',
  reason: 'Grund (optional)',
  reasonHint: 'Ohne Namen von Schülerinnen und Schülern.',
  grant: 'Vergeben',
  revoke: 'Entziehen',
  back: 'Zurück',
  granted: (h) => `Stunden vergeben: ${hoursDe(h)}`,
  revoked: (h) => `Stunden entzogen: ${hoursDe(h)}`,
};

/** The API's German `detail`, shown verbatim. */
export function apiErrorText(err) {
  if (err && typeof err.detail === 'string' && err.detail) return err.detail;
  return (err && err.message) || 'Fehler';
}

/**
 * The request id of one change: the same id while the change is the same, a
 * new one once it differs (a reused id for another change is a 409).
 */
export function useRequestIdFor() {
  const ref = useRef({ id: null, sig: null });
  return (sig) => {
    if (ref.current.sig !== sig || !ref.current.id) ref.current = { id: newRequestId(), sig };
    return ref.current.id;
  };
}

export default function TeacherCreditsModal({ token, teacher, mode, onClose, onDone }) {
  const revoke = mode === 'revoke';
  const [hours, setHours] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const requestIdFor = useRequestIdFor();

  const parsed = Number(hours);
  const valid = hours !== '' && Number.isInteger(parsed) && parsed >= 1 && parsed <= MAX_ADMIN_HOURS;

  const submit = async (e) => {
    e?.preventDefault();
    if (busy || !valid) return;
    const delta = revoke ? -parsed : parsed;
    const trimmed = reason.trim();
    const request_id = requestIdFor(`${delta}|${trimmed}`);
    setBusy(true);
    setError(null);
    try {
      const res = await changeTeacherCredits(token, teacher.id, { delta, reason: trimmed, request_id });
      toast.success(revoke ? TEACHER_CREDITS_DE.revoked(parsed) : TEACHER_CREDITS_DE.granted(parsed));
      onDone?.(res);
      onClose();
    } catch (err) {
      setError(apiErrorText(err));
      setBusy(false);
    }
  };

  const formId = `teacher-credits-${mode}`;
  return (
    <Modal
      title={revoke ? TEACHER_CREDITS_DE.revokeTitle : TEACHER_CREDITS_DE.grantTitle}
      onClose={() => { if (!busy) onClose(); }}
      footer={(
        <>
          <Btn variant="ghost" onClick={onClose} disabled={busy}>{TEACHER_CREDITS_DE.back}</Btn>
          <Btn variant={revoke ? 'danger' : 'primary'} type="submit" form={formId} disabled={busy || !valid}>
            {revoke ? TEACHER_CREDITS_DE.revoke : TEACHER_CREDITS_DE.grant}
          </Btn>
        </>
      )}
    >
      <form id={formId} onSubmit={submit} className="flex flex-col gap-4">
        <p className="text-sm text-[var(--ink-2)]">{teacher.full_name || teacher.username}</p>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">{TEACHER_CREDITS_DE.hours}</span>
          <input
            type="number"
            className={inputClass}
            value={hours}
            onChange={(e) => setHours(e.target.value.replace(/[^0-9]/g, ''))}
            min={1}
            max={MAX_ADMIN_HOURS}
            inputMode="numeric"
            autoFocus
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">{TEACHER_CREDITS_DE.reason}</span>
          <input
            type="text"
            className={inputClass}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={REASON_MAX}
          />
          <p className="text-[11px] text-[var(--ink-3)] mt-1.5 leading-snug">{TEACHER_CREDITS_DE.reasonHint}</p>
        </label>
        {error && (
          <p role="alert" className="text-sm text-[color:var(--danger)]">{error}</p>
        )}
      </form>
    </Modal>
  );
}
