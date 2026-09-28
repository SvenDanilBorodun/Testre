/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// „Abgeben": hand the teacher THIS stand of the program (A3b). The cloud
// snapshots the row server-side (`POST /workflows/{id}/submit`), so the
// document is saved FIRST through the page's one save path — a submission of
// an unsaved edit would hand in yesterday's file. Confirmed like the page's
// other irreversible actions (window.confirm, German), refused while a
// program runs.

import React, { useCallback, useState } from 'react';
import toast from 'react-hot-toast';
import { submitWorkflow } from '../../../services/workflowApi';
import { CODE_DE, formatCode } from './codeMessagesDe';
import Icon from '../../icons/Icon';

function SubmitButton({ accessToken, saveWorkflowNow, running = false }) {
  const [busy, setBusy] = useState(false);

  const handleSubmit = useCallback(async () => {
    if (running) { toast.error(CODE_DE.SUBMIT_BUSY); return; }
    if (typeof window !== 'undefined' && !window.confirm(CODE_DE.SUBMIT_CONFIRM)) return;
    setBusy(true);
    try {
      const saved = await saveWorkflowNow({ toastOnSuccess: false });
      if (!saved || !saved.ok || !saved.workflowId) {
        toast.error(CODE_DE.SUBMIT_SAVE_FAILED);
        return;
      }
      await submitWorkflow(accessToken, saved.workflowId, { note: '' });
      toast.success(CODE_DE.SUBMIT_OK);
    } catch (e) {
      toast.error(formatCode(CODE_DE.SUBMIT_FAILED, e.message || e));
    } finally {
      setBusy(false);
    }
  }, [running, saveWorkflowNow, accessToken]);

  return (
    <button
      type="button"
      onClick={handleSubmit}
      disabled={busy || running || !accessToken}
      title={CODE_DE.SUBMIT_TITLE}
      className={
        'inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border disabled:opacity-50 disabled:cursor-not-allowed '
        + 'bg-white text-[var(--ink-3)] border-[var(--line)] hover:bg-[var(--bg-sunk)]'
      }
    >
      <Icon name="send" />
      {CODE_DE.SUBMIT}
    </button>
  );
}

export default SubmitButton;
