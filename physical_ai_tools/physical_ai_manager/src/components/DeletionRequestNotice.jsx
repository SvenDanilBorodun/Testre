// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// One line above every page while the signed-in student's GDPR Art. 17
// deletion request is open (launch plan step 5): when it was filed, that the
// teacher carries it out and by when (Art. 12(3): one month), and a way to
// withdraw it. The account stays fully usable until the teacher confirms; a
// confirmed deletion ends the account, and the next request answers 401.
// Renders nothing while no request is open (or for an older Cloud API, whose
// /me carries no deletion_requested_at).

import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { useDispatch, useSelector } from 'react-redux';
import { withdrawDeletionRequest } from '../services/meApi';
import { clearDeletionRequest } from '../features/auth/authSlice';
import { DELETION_DE, formatDateDe } from '../utils/deletionRequest';
import Icon from './icons/Icon';
import { releasePointerFocus } from './sucher/pointerFocus';

// A pointer click gives the focus back (Space is „Aufnahme starten" on the
// Aufnahme page, and this notice sits above every page).
const clicked = (fn) => (e) => {
  releasePointerFocus(e);
  fn();
};

export default function DeletionRequestNotice() {
  const dispatch = useDispatch();
  const requestedAt = useSelector((s) => s.auth.deletionRequestedAt);
  const dueBy = useSelector((s) => s.auth.deletionDueBy);
  const token = useSelector((s) => s.auth.session?.access_token);
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!requestedAt) return null;

  const withdraw = async () => {
    if (busy || !token) return;
    setBusy(true);
    try {
      const res = await withdrawDeletionRequest(token);
      dispatch(clearDeletionRequest());
      toast.success(res?.message || DELETION_DE.withdraw, { duration: 10000 });
    } catch (err) {
      toast.error(err?.message || DELETION_DE.withdrawFailed);
    } finally {
      setBusy(false);
      setAsking(false);
    }
  };

  return (
    <div
      role="status"
      data-testid="deletion-request-notice"
      className="mx-4 mt-3 rounded-[12px] bg-[var(--amber-wash)] px-3 py-2 text-sm text-[var(--ink)] flex flex-wrap items-start gap-2"
    >
      <Icon name="warning" size={16} className="mt-0.5 shrink-0" />
      <span className="flex-1 min-w-[200px]">
        {DELETION_DE.studentNotice(formatDateDe(requestedAt), formatDateDe(dueBy))}
      </span>
      {asking ? (
        <span className="flex items-center gap-2">
          <span>{DELETION_DE.withdrawAsk}</span>
          <button type="button" className="su-key su-key--sm" onClick={clicked(withdraw)} disabled={busy}>
            {busy && <Icon name="loading" size={14} className="animate-spin" />}
            {DELETION_DE.withdrawYes}
          </button>
          <button type="button" className="su-key su-key--sm" onClick={clicked(() => setAsking(false))} disabled={busy}>
            {DELETION_DE.cancel}
          </button>
        </span>
      ) : (
        <button type="button" className="su-key su-key--sm" onClick={clicked(() => setAsking(true))}>
          {DELETION_DE.withdraw}
        </button>
      )}
    </div>
  );
}
