import React, { useState } from 'react';
import toast from 'react-hot-toast';
import { useDispatch, useSelector } from 'react-redux';
import {
  deleteTeacher,
  eraseReason,
  getCreditHistory,
  resetTeacherPassword,
} from '../../services/adminApi';
import { removeTeacher } from '../../features/admin/adminSlice';
import PasswordResetModal from '../teacher/PasswordResetModal';
import CreditHistoryDrawer from '../teacher/CreditHistoryDrawer';
import Modal from '../teacher/Modal';
import TeacherCreditsModal, { apiErrorText } from './TeacherCreditsModal';
import CompensationModal from './CompensationModal';
import { Avatar, Btn } from '../EbUI';
import Icon from '../icons/Icon';
import { hoursDe, hoursOf, std } from '../../utils/hoursDe';

/** The teacher's hours in one line (credit system v2, spec §10.5). */
export function teacherHoursLine(t) {
  return `${std(hoursOf(t.pool_available))} frei · ${std(hoursOf(t.allocated_total))} verteilt · `
    + `${std(hoursOf(t.hours_spent))} verbraucht`;
}

/** The delete confirmation: names the free pool hours the deletion revokes (D8). */
export function deleteTeacherConfirmText(t) {
  const head = `Lehrer ${t.full_name || t.username} wirklich löschen?`;
  const free = hoursOf(t.pool_available);
  return free > 0
    ? `${head}\n\nFrei im Lehrer-Pool: ${hoursDe(free)}. Beim Löschen der Lehrkraft wird dieses Guthaben entzogen.`
    : head;
}

export const ERASE_DE = {
  button: 'Grund löschen',
  confirm: 'Den Grund endgültig löschen? Die Buchung selbst bleibt.',
  erase: 'Löschen',
  back: 'Zurück',
  done: 'Grund gelöscht.',
};

const accountKindOf = (e) => e?.account_kind ?? e?.account ?? null;
const hasReason = (e) => !e?.reason_erased && typeof e?.reason === 'string' && e.reason !== ''
  && e?.reason_id !== null && e?.reason_id !== undefined;

export default function TeacherRow({ teacher, onChanged }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const [showPwModal, setShowPwModal] = useState(false);
  const [creditsMode, setCreditsMode] = useState(null); // 'grant' | 'revoke' | null
  const [showHistory, setShowHistory] = useState(false);
  const [compTarget, setCompTarget] = useState(null);
  const [eraseTarget, setEraseTarget] = useState(null); // { entry, patch }
  const [erasing, setErasing] = useState(false);
  const [busy, setBusy] = useState(false);

  const handleDelete = async () => {
    if (teacher.classroom_count > 0) {
      toast.error('Lehrer hat noch Klassen — zuerst löschen');
      return;
    }
    if (!window.confirm(deleteTeacherConfirmText(teacher))) return;
    setBusy(true);
    try {
      await deleteTeacher(token, teacher.id);
      dispatch(removeTeacher(teacher.id));
      toast.success('Lehrer gelöscht');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
    }
  };

  const handleResetPw = async (newPassword) => {
    await resetTeacherPassword(token, teacher.id, newPassword);
  };

  const confirmErase = async () => {
    if (!eraseTarget || erasing) return;
    setErasing(true);
    try {
      await eraseReason(token, eraseTarget.entry.reason_id);
      eraseTarget.patch({ reason: null, reason_erased: true });
      toast.success(ERASE_DE.done);
      setEraseTarget(null);
    } catch (err) {
      toast.error(apiErrorText(err));
    } finally {
      setErasing(false);
    }
  };

  // The admin's row actions in the history: a compensation for a charged
  // training (preset with the account that paid it), and the erasure of a
  // reason's text.
  const renderActions = (entry, patch) => {
    const canCompensate = entry.kind === 'charge' && typeof entry.hours === 'number' && entry.hours > 0
      && (accountKindOf(entry) === 'student' || accountKindOf(entry) === 'group') && entry.account_id;
    if (!canCompensate && !hasReason(entry)) return null;
    return (
      <div className="flex flex-wrap gap-1 mt-1">
        {canCompensate && (
          <Btn
            variant="secondary"
            size="sm"
            onClick={() => setCompTarget({
              target_kind: accountKindOf(entry),
              target_id: entry.account_id,
              training_id: entry.training_id ?? null,
              hours: entry.hours,
            })}
          >
            Ausgleich gutschreiben
          </Btn>
        )}
        {hasReason(entry) && (
          <Btn variant="ghost" size="sm" onClick={() => setEraseTarget({ entry, patch })}>
            {ERASE_DE.button}
          </Btn>
        )}
      </div>
    );
  };

  const availTone =
    hoursOf(teacher.pool_available) > 0
      ? 'text-[color:var(--success)]'
      : 'text-[color:var(--danger)]';
  const hasOld = teacher.old_credits !== null && teacher.old_credits !== undefined;

  return (
    <tr className="border-b last:border-0 border-[var(--line)] hover:bg-[var(--bg-sunk)] group transition-colors">
      <td className="py-3 px-5">
        <div className="flex items-center gap-3">
          <Avatar name={teacher.full_name || teacher.username} />
          <div className="min-w-0">
            <div className="font-medium text-[var(--ink)] truncate">
              {teacher.full_name}
            </div>
            <div className="font-mono text-[11px] text-[var(--ink-3)] truncate">
              {teacher.username}
            </div>
          </div>
        </div>
      </td>
      <td className="py-3 px-3">
        <div className={`font-mono text-[12px] font-semibold ${availTone}`} data-testid="teacher-hours">
          {teacherHoursLine(teacher)}
        </div>
        {hasOld && (
          <div className="font-mono text-[10px] text-[var(--ink-3)] mt-0.5">
            {`Vor der Umstellung: ${teacher.old_credits} (alte Einheit)`}
          </div>
        )}
      </td>
      <td className="text-center py-3 px-3 font-mono text-[var(--ink-2)]">
        {teacher.classroom_count}
      </td>
      <td className="text-center py-3 px-3 font-mono text-[var(--ink-2)]">
        {teacher.student_count}
      </td>
      <td className="py-3 px-5 text-right">
        <div className="inline-flex flex-wrap justify-end gap-1">
          <Btn variant="secondary" size="sm" onClick={() => setCreditsMode('grant')}>
            Stunden vergeben
          </Btn>
          <Btn variant="secondary" size="sm" onClick={() => setCreditsMode('revoke')}>
            Stunden entziehen
          </Btn>
          <Btn variant="secondary" size="sm" onClick={() => setShowHistory(true)}>
            <Icon name="history" /> Verlauf
          </Btn>
          <Btn
            variant="ghost"
            size="sm"
            onClick={() => setShowPwModal(true)}
            title="Passwort zurücksetzen"
          >
            <Icon name="key" size={18} />
          </Btn>
          <Btn
            variant="ghost"
            size="sm"
            onClick={handleDelete}
            disabled={busy}
            title="Lehrer löschen"
          >
            <Icon name="trash" size={18} />
          </Btn>
        </div>
      </td>
      {showPwModal && (
        <PasswordResetModal
          username={teacher.username}
          onClose={() => setShowPwModal(false)}
          onSubmit={handleResetPw}
        />
      )}
      {creditsMode && (
        <TeacherCreditsModal
          token={token}
          teacher={teacher}
          mode={creditsMode}
          onClose={() => setCreditsMode(null)}
          onDone={() => onChanged?.()}
        />
      )}
      {showHistory && (
        <CreditHistoryDrawer
          title={`Stundenverlauf · ${teacher.full_name || teacher.username}`}
          load={(page) => getCreditHistory(token, { scope: 'teacher_all', teacher_id: teacher.id, ...page })}
          renderActions={renderActions}
          onClose={() => setShowHistory(false)}
        />
      )}
      {compTarget && (
        <CompensationModal
          token={token}
          preset={compTarget}
          onClose={() => setCompTarget(null)}
          onDone={() => onChanged?.()}
        />
      )}
      {eraseTarget && (
        <Modal
          title={ERASE_DE.button}
          onClose={() => { if (!erasing) setEraseTarget(null); }}
          footer={(
            <>
              <Btn variant="ghost" onClick={() => setEraseTarget(null)} disabled={erasing}>{ERASE_DE.back}</Btn>
              <Btn variant="danger" onClick={confirmErase} disabled={erasing}>{ERASE_DE.erase}</Btn>
            </>
          )}
        >
          <p className="text-sm text-[var(--ink-2)]">{ERASE_DE.confirm}</p>
        </Modal>
      )}
    </tr>
  );
}
