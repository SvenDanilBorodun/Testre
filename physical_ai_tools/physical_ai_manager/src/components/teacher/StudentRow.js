import React, { useEffect, useRef, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  MdDelete,
  MdKey,
  MdHistory,
  MdEdit,
  MdMoveDown,
  MdAdd,
  MdRemove,
  MdTune,
  MdEventNote,
  MdGroups,
} from 'react-icons/md';
import { useDispatch, useSelector } from 'react-redux';
import {
  adjustStudentCredits,
  deleteStudent as apiDeleteStudent,
  patchStudent,
  resetStudentPassword,
} from '../../services/teacherApi';
import { getMe } from '../../services/meApi';
import {
  removeStudentFromSelected,
  upsertStudentInSelected,
} from '../../features/teacher/teacherSlice';
import { updateTeacherPool } from '../../features/auth/authSlice';
import PasswordResetModal from './PasswordResetModal';
import { Avatar, Btn, Pill, Progress } from '../EbUI';

function RenameInline({ student, onSave, onCancel }) {
  const [value, setValue] = useState(student.full_name || '');
  return (
    <div className="flex items-center gap-2">
      <input
        type="text"
        className="h-8 px-2 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm flex-1 focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)]"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        maxLength={100}
        autoFocus
      />
      <Btn variant="primary" size="sm" onClick={() => onSave(value)} disabled={!value.trim()}>
        Speichern
      </Btn>
      <Btn variant="ghost" size="sm" onClick={onCancel}>
        Abbrechen
      </Btn>
    </div>
  );
}

function CreditDeltaPopover({ student, onApply, onClose, busy }) {
  const [amount, setAmount] = useState('');
  const ref = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const onDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) onClose();
    };
    const onEsc = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onEsc);
    };
  }, [onClose]);

  const used = student.trainings_used || 0;
  const total = student.training_credits || 0;
  const remaining = Math.max(total - used, 0);
  const parsed = Number(amount);
  const canAdd = Number.isFinite(parsed) && parsed > 0 && parsed <= 1000;
  const canSubtract = Number.isFinite(parsed) && parsed > 0 && parsed <= remaining;

  const submit = (delta) => {
    if (!Number.isFinite(delta) || delta === 0) return;
    onApply(delta);
  };

  return (
    <div
      ref={ref}
      className="absolute z-30 top-full mt-2 right-0 w-[240px] bg-white border border-[var(--line)] rounded-[var(--radius-lg)] shadow-pop p-3"
    >
      <div className="text-[10px] font-mono uppercase tracking-wider text-[var(--ink-3)] mb-2">
        Credits anpassen
      </div>
      <div className="flex items-center gap-1.5">
        <button
          onClick={() => submit(-Math.abs(parsed))}
          disabled={busy || !canSubtract}
          className="w-8 h-9 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] hover:bg-[var(--danger-wash)] hover:text-[color:var(--danger)] text-[var(--ink-2)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition"
          title={
            canSubtract
              ? 'Abziehen'
              : remaining === 0
              ? 'Keine freien Credits zum Abziehen'
              : `Max. ${remaining} abziehbar`
          }
        >
          <MdRemove size={16} />
        </button>
        <input
          ref={inputRef}
          type="number"
          min={1}
          max={1000}
          inputMode="numeric"
          placeholder="Betrag"
          value={amount}
          onChange={(e) => setAmount(e.target.value.replace(/[^0-9]/g, ''))}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && canAdd && !busy) {
              e.preventDefault();
              submit(Math.abs(parsed));
            }
          }}
          className="flex-1 h-9 px-2 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm font-mono text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition"
        />
        <button
          onClick={() => submit(Math.abs(parsed))}
          disabled={busy || !canAdd}
          className="w-8 h-9 rounded-[var(--radius-sm)] bg-[var(--accent-wash)] hover:brightness-95 text-[var(--accent-ink)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition"
          title="Hinzufügen"
        >
          <MdAdd size={16} />
        </button>
      </div>
      <div className="flex items-center gap-1 mt-2">
        {[5, 10, 25].map((v) => (
          <button
            key={v}
            type="button"
            disabled={busy}
            onClick={() => submit(v)}
            className="h-7 flex-1 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] hover:bg-[var(--accent-wash)] hover:text-[var(--accent-ink)] text-[11px] font-mono text-[var(--ink-2)] disabled:opacity-40 transition"
            title={`+${v} Credits sofort hinzufügen`}
          >
            +{v}
          </button>
        ))}
      </div>
      <div className="text-[10px] text-[var(--ink-3)] mt-2 leading-snug font-mono">
        <span className="text-[var(--ink-2)]">↩ Enter</span> = hinzufügen · Aktuell <span className="text-[var(--ink)]">{total}</span> · verbraucht {used} · max. ±1000
      </div>
    </div>
  );
}

function MoveInline({ student, classrooms, onSave, onCancel }) {
  const [target, setTarget] = useState(student.classroom_id);
  return (
    <div className="flex items-center gap-2">
      <select
        value={target}
        onChange={(e) => setTarget(e.target.value)}
        className="eb h-8 pl-2 pr-8 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm focus:outline-none focus:border-[var(--accent)]"
      >
        {classrooms.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      <Btn variant="primary" size="sm" onClick={() => onSave(target)} disabled={target === student.classroom_id}>
        Verschieben
      </Btn>
      <Btn variant="ghost" size="sm" onClick={onCancel}>
        Abbrechen
      </Btn>
    </div>
  );
}

export default function StudentRow({ student, classrooms, onShowHistory, onShowProgress }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const [busy, setBusy] = useState(false);
  const [showPwModal, setShowPwModal] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [moving, setMoving] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const otherClassrooms = classrooms.filter((c) => c.id !== student.classroom_id);

  const refreshTeacherPool = async () => {
    try {
      const me = await getMe(token);
      dispatch(
        updateTeacherPool({
          pool_total: me.pool_total,
          allocated_total: me.allocated_total,
          pool_available: me.pool_available,
          student_count: me.student_count,
        })
      );
    } catch {}
  };

  const handleDelta = async (delta) => {
    if (busy) return;
    setBusy(true);
    try {
      const res = await adjustStudentCredits(token, student.id, delta);
      dispatch(
        upsertStudentInSelected({
          ...student,
          training_credits: res.new_amount,
          remaining: res.new_amount - (student.trainings_used || 0),
        })
      );
      await refreshTeacherPool();
      toast.success(`Credits: ${res.new_amount}`);
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!window.confirm(`Schüler ${student.full_name} wirklich löschen?`)) return;
    setBusy(true);
    try {
      await apiDeleteStudent(token, student.id);
      dispatch(removeStudentFromSelected(student.id));
      await refreshTeacherPool();
      toast.success('Schüler gelöscht');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
    }
  };

  const handleResetPw = async (newPassword) => {
    await resetStudentPassword(token, student.id, newPassword);
  };

  const handleRename = async (newName) => {
    setBusy(true);
    try {
      const updated = await patchStudent(token, student.id, { full_name: newName });
      dispatch(
        upsertStudentInSelected({
          ...student,
          full_name: updated.full_name,
        })
      );
      setRenaming(false);
      toast.success('Name geändert');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
    }
  };

  const handleMove = async (classroomId) => {
    setBusy(true);
    try {
      await patchStudent(token, student.id, { classroom_id: classroomId });
      dispatch(removeStudentFromSelected(student.id));
      toast.success('Schüler verschoben');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
      setMoving(false);
    }
  };

  const used = student.trainings_used || 0;
  const total = student.training_credits || 0;
  const remaining = total - used;
  const pct = total > 0 ? (used / total) * 100 : 0;
  const progressTone = remaining === 0 ? 'danger' : 'accent';

  return (
    <tr className="border-b last:border-0 border-[var(--line)] hover:bg-[var(--bg-sunk)] group transition-colors">
      <td className="py-3 px-5">
        {renaming ? (
          <RenameInline
            student={student}
            onSave={handleRename}
            onCancel={() => setRenaming(false)}
          />
        ) : (
          <div className="flex items-center gap-3">
            <Avatar name={student.full_name || student.username} />
            <div className="min-w-0">
              <div className="flex items-center gap-1.5 flex-wrap">
                <div className="font-medium text-[var(--ink)] truncate">
                  {student.full_name || student.username}
                </div>
                <button
                  onClick={() => setRenaming(true)}
                  className="text-[var(--ink-4)] hover:text-[var(--ink)] transition"
                  title="Namen ändern"
                >
                  <MdEdit size={14} />
                </button>
                {student.workgroup_id && student.workgroup_name && (
                  <Pill tone="success" title="In Arbeitsgruppe">
                    <span className="inline-flex items-center gap-1">
                      <MdGroups size={12} />
                      {student.workgroup_name}
                    </span>
                  </Pill>
                )}
              </div>
              <div className="font-mono text-[11px] text-[var(--ink-3)] truncate">
                {student.username}
              </div>
            </div>
          </div>
        )}
      </td>
      <td className="py-3 px-3 whitespace-nowrap">
        {student.workgroup_id ? (
          <div className="text-xs text-[var(--ink-3)] leading-snug max-w-[220px]">
            <div className="font-mono text-[11px] mb-0.5">
              In Gruppe <span className="text-[var(--ink)]">{student.workgroup_name || '—'}</span>
            </div>
            <div className="text-[10px]">
              Credits werden über die Gruppe geteilt
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <div className="w-28 shrink-0">
              <div className="flex justify-between font-mono text-[11px] text-[var(--ink-3)]">
                <span className="text-[var(--ink)] font-semibold">{used}</span>
                <span>/ {total}</span>
              </div>
              <Progress pct={pct} tone={progressTone} />
              <div
                className={clsx(
                  'mt-1 text-[10px] font-mono font-semibold',
                  remaining === 0
                    ? 'text-[color:var(--danger)]'
                    : 'text-[color:var(--success)]'
                )}
              >
                {remaining} frei
              </div>
            </div>
            <div className="flex gap-1 relative">
              <button
                onClick={() => handleDelta(-1)}
                disabled={busy || student.training_credits <= student.trainings_used}
                className="w-7 h-7 rounded-[var(--radius-sm)] bg-[var(--bg-sunk)] hover:bg-[var(--danger-wash)] hover:text-[color:var(--danger)] text-[var(--ink-2)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition"
                title="Credit entziehen"
              >
                <MdRemove size={16} />
              </button>
              <button
                onClick={() => handleDelta(1)}
                disabled={busy}
                className="w-7 h-7 rounded-[var(--radius-sm)] bg-[var(--accent-wash)] hover:brightness-95 text-[var(--accent-ink)] disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition"
                title="Credit hinzufügen"
              >
                <MdAdd size={16} />
              </button>
              <button
                onClick={() => setCustomOpen((v) => !v)}
                disabled={busy}
                className={clsx(
                  'w-7 h-7 rounded-[var(--radius-sm)] flex items-center justify-center transition',
                  customOpen
                    ? 'bg-[var(--accent-wash)] text-[var(--accent-ink)]'
                    : 'bg-[var(--bg-sunk)] hover:bg-[var(--line)] text-[var(--ink-2)]'
                )}
                title="Beliebigen Betrag anpassen"
              >
                <MdTune size={14} />
              </button>
              {customOpen && (
                <CreditDeltaPopover
                  student={student}
                  busy={busy}
                  onClose={() => setCustomOpen(false)}
                  onApply={async (delta) => {
                    await handleDelta(delta);
                    setCustomOpen(false);
                  }}
                />
              )}
            </div>
          </div>
        )}
      </td>
      <td className="py-3 px-5 text-right">
        {moving ? (
          otherClassrooms.length === 0 ? (
            <span className="text-xs text-[var(--ink-3)]">Keine andere Klasse</span>
          ) : (
            <MoveInline
              student={student}
              classrooms={classrooms}
              onSave={handleMove}
              onCancel={() => setMoving(false)}
            />
          )
        ) : (
          <div className="inline-flex gap-0.5 opacity-60 group-hover:opacity-100 transition">
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => onShowProgress?.(student)}
              title="Fortschritt · tägliche Notizen"
            >
              <MdEventNote size={18} />
            </Btn>
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => onShowHistory(student)}
              title="Trainings-Historie"
            >
              <MdHistory size={18} />
            </Btn>
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => setShowPwModal(true)}
              title="Passwort zurücksetzen"
            >
              <MdKey size={18} />
            </Btn>
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => setMoving(true)}
              disabled={otherClassrooms.length === 0}
              title={
                otherClassrooms.length === 0
                  ? 'Keine andere Klasse verfügbar'
                  : 'In andere Klasse verschieben'
              }
            >
              <MdMoveDown size={18} />
            </Btn>
            <Btn
              variant="ghost"
              size="sm"
              onClick={handleDelete}
              disabled={busy}
              title="Schüler löschen"
            >
              <MdDelete size={18} />
            </Btn>
          </div>
        )}
      </td>
      {showPwModal && (
        <PasswordResetModal
          username={student.username}
          onClose={() => setShowPwModal(false)}
          onSubmit={handleResetPw}
        />
      )}
    </tr>
  );
}
