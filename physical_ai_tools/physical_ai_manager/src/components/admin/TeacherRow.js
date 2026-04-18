import React, { useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdCheck, MdClose, MdDelete, MdEdit, MdKey } from 'react-icons/md';
import { useDispatch, useSelector } from 'react-redux';
import {
  deleteTeacher,
  resetTeacherPassword,
  setTeacherCredits,
} from '../../services/adminApi';
import { removeTeacher, upsertTeacher } from '../../features/admin/adminSlice';
import PasswordResetModal from '../teacher/PasswordResetModal';
import { Avatar, Btn } from '../EbUI';

export default function TeacherRow({ teacher }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const [editingCredits, setEditingCredits] = useState(false);
  const [creditsValue, setCreditsValue] = useState(String(teacher.pool_total));
  const [showPwModal, setShowPwModal] = useState(false);
  const [busy, setBusy] = useState(false);

  const handleSaveCredits = async () => {
    const parsed = Number(creditsValue);
    if (!Number.isFinite(parsed) || parsed < 0) {
      toast.error('Ungültige Zahl');
      return;
    }
    setBusy(true);
    try {
      const updated = await setTeacherCredits(token, teacher.id, parsed);
      dispatch(upsertTeacher(updated));
      toast.success('Credits aktualisiert');
      setEditingCredits(false);
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (teacher.classroom_count > 0) {
      toast.error('Lehrer hat noch Klassen — zuerst löschen');
      return;
    }
    if (!window.confirm(`Lehrer ${teacher.full_name} wirklich löschen?`)) return;
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

  const availTone =
    teacher.pool_available > 0
      ? 'text-[color:var(--success)]'
      : 'text-[color:var(--danger)]';

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
      <td className="text-center py-3 px-3">
        {editingCredits ? (
          <div className="flex items-center gap-1 justify-center">
            <input
              type="number"
              min={0}
              max={10000}
              className="w-24 h-8 px-2 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition font-mono"
              value={creditsValue}
              onChange={(e) => setCreditsValue(e.target.value)}
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSaveCredits();
                if (e.key === 'Escape') setEditingCredits(false);
              }}
            />
            <button
              onClick={handleSaveCredits}
              disabled={busy}
              className="w-7 h-7 rounded-[var(--radius-sm)] text-[var(--accent-ink)] hover:bg-[var(--accent-wash)] flex items-center justify-center transition"
              title="Speichern"
            >
              <MdCheck size={16} />
            </button>
            <button
              onClick={() => {
                setCreditsValue(String(teacher.pool_total));
                setEditingCredits(false);
              }}
              className="w-7 h-7 rounded-[var(--radius-sm)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] flex items-center justify-center transition"
              title="Abbrechen"
            >
              <MdClose size={16} />
            </button>
          </div>
        ) : (
          <div className="inline-flex items-center gap-1.5">
            <span className="font-mono font-semibold text-[var(--ink)]">
              {teacher.pool_total}
            </span>
            <button
              onClick={() => setEditingCredits(true)}
              className="text-[var(--ink-4)] hover:text-[var(--ink)] transition"
              title="Credits anpassen"
            >
              <MdEdit size={14} />
            </button>
          </div>
        )}
      </td>
      <td className="text-center py-3 px-3 font-mono text-[var(--ink-2)]">
        {teacher.allocated_total}
      </td>
      <td className="text-center py-3 px-3">
        <span className={clsx('font-mono font-semibold', availTone)}>
          {teacher.pool_available}
        </span>
      </td>
      <td className="text-center py-3 px-3 font-mono text-[var(--ink-2)]">
        {teacher.classroom_count}
      </td>
      <td className="text-center py-3 px-3 font-mono text-[var(--ink-2)]">
        {teacher.student_count}
      </td>
      <td className="py-3 px-5 text-right">
        <div className="inline-flex gap-0.5 opacity-60 group-hover:opacity-100 transition">
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
            onClick={handleDelete}
            disabled={busy}
            title="Lehrer löschen"
          >
            <MdDelete size={18} />
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
    </tr>
  );
}
