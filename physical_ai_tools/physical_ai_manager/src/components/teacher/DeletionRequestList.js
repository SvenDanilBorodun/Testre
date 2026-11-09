// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// The open GDPR Art. 17 deletion requests (launch plan step 5, owner decision
// „teacher confirms"), shared by the teacher dashboard (the students of the
// teacher's own classrooms) and the admin dashboard (every open request). It
// shows each request with its Art. 12(3) deadline and carries it out only after
// a German confirmation dialog; what the deletion did is the caller's to
// announce (utils/deletionRequest.js::announceStudentDeletion). There is no
// „decline": a mistaken request is withdrawn by the student.

import React, { useState } from 'react';
import { Btn, Pill } from '../EbUI';
import Icon from '../icons/Icon';
import Modal from './Modal';
import { DELETION_DE, deadlineState, formatDateDe } from '../../utils/deletionRequest';

function DeadlinePill({ dueBy }) {
  const state = deadlineState(dueBy);
  if (state === 'unknown') return null;
  const date = formatDateDe(dueBy);
  if (state === 'overdue') return <Pill tone="danger">{DELETION_DE.overdue(date)}</Pill>;
  return <Pill tone={state === 'soon' ? 'amber' : 'neutral'}>{DELETION_DE.dueBy(date)}</Pill>;
}

/**
 * @param {object} props
 * @param {Array<{id: string, name: string, username?: string, role?: string,
 *   classroomName?: string|null, teacherName?: string|null, requestedAt: string,
 *   dueBy?: string|null}>} props.requests
 * @param {(request: object) => Promise<void>} props.onConfirm  carries one out
 * @param {boolean} [props.showTeacher]  the admin view names the usual confirmer
 * @param {boolean} [props.loading]
 * @param {string|null} [props.error]
 */
export default function DeletionRequestList({ requests, onConfirm, showTeacher = false, loading = false, error = null }) {
  const [asking, setAsking] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const run = async () => {
    const req = asking;
    if (!req || busyId) return;
    setBusyId(req.id);
    try {
      await onConfirm(req);
      setAsking(null);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="text-sm">
      <p className="text-[var(--ink-2)] leading-snug mb-3" data-testid="deletion-deadline-note">
        {DELETION_DE.deadlineNote}
      </p>
      {error ? (
        <p role="alert" className="text-[color:var(--danger)]">{error}</p>
      ) : loading && requests.length === 0 ? (
        <p className="text-[var(--ink-3)]">Laden…</p>
      ) : requests.length === 0 ? (
        <p className="text-[var(--ink-3)]">{DELETION_DE.none}</p>
      ) : (
        <ul className="divide-y divide-[var(--line)] border border-[var(--line)] rounded-[var(--radius)]">
          {requests.map((r) => (
            <li
              key={r.id}
              data-testid={`deletion-request-${r.id}`}
              className="flex flex-wrap items-center gap-3 px-4 py-3"
            >
              <div className="min-w-0 flex-1">
                <div className="font-medium text-[var(--ink)] truncate">
                  {r.name}
                  {r.username && r.username !== r.name && (
                    <span className="ml-2 font-mono text-[11px] text-[var(--ink-3)]">{r.username}</span>
                  )}
                </div>
                <div className="text-xs text-[var(--ink-3)] flex flex-wrap gap-x-2">
                  <span>
                    {r.role === 'teacher' ? DELETION_DE.teacherAccount : r.classroomName || DELETION_DE.noClassroom}
                  </span>
                  {showTeacher && r.teacherName && <span>· {r.teacherName}</span>}
                  <span>· {DELETION_DE.requestedOn(formatDateDe(r.requestedAt))}</span>
                </div>
              </div>
              <DeadlinePill dueBy={r.dueBy} />
              <Btn
                variant="danger"
                size="sm"
                disabled={Boolean(busyId)}
                onClick={() => setAsking(r)}
              >
                <Icon name="trash" size={14} /> {DELETION_DE.confirm}
              </Btn>
            </li>
          ))}
        </ul>
      )}
      {asking && (
        <Modal
          title={DELETION_DE.confirmTitle}
          onClose={() => (busyId ? null : setAsking(null))}
          footer={
            <>
              <Btn variant="ghost" onClick={() => setAsking(null)} disabled={Boolean(busyId)}>
                {DELETION_DE.cancel}
              </Btn>
              <Btn variant="danger" onClick={run} disabled={Boolean(busyId)}>
                {busyId ? <Icon name="loading" className="animate-spin" /> : <Icon name="trash" />}
                {DELETION_DE.confirmGo}
              </Btn>
            </>
          }
        >
          <p className="leading-relaxed text-[var(--ink-2)]">
            {asking.role === 'teacher'
              ? DELETION_DE.confirmTeacherBody(asking.name)
              : DELETION_DE.confirmBody(asking.name)}
          </p>
          <p className="mt-3 text-xs text-[var(--ink-3)]">
            {DELETION_DE.requestedOn(formatDateDe(asking.requestedAt))}
          </p>
        </Modal>
      )}
    </div>
  );
}
