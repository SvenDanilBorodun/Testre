import React, { useState } from 'react';
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
      toast.error('Ungueltige Zahl');
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
      toast.error('Lehrer hat noch Klassen - zuerst loeschen');
      return;
    }
    if (!window.confirm(`Lehrer ${teacher.full_name} wirklich loeschen?`)) return;
    setBusy(true);
    try {
      await deleteTeacher(token, teacher.id);
      dispatch(removeTeacher(teacher.id));
      toast.success('Lehrer geloescht');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
    }
  };

  const handleResetPw = async (newPassword) => {
    await resetTeacherPassword(token, teacher.id, newPassword);
  };

  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50">
      <td className="px-4 py-3">
        <div className="font-medium text-gray-800">{teacher.full_name}</div>
        <div className="text-xs text-gray-500 font-mono">{teacher.username}</div>
      </td>
      <td className="px-4 py-3 text-center">
        {editingCredits ? (
          <div className="flex items-center gap-1 justify-center">
            <input
              type="number"
              min={0}
              max={10000}
              className="w-24 px-2 py-1 border border-gray-300 rounded text-sm"
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
              className="p-1 text-teal-700 hover:bg-teal-50 rounded"
              title="Speichern"
            >
              <MdCheck size={16} />
            </button>
            <button
              onClick={() => {
                setCreditsValue(String(teacher.pool_total));
                setEditingCredits(false);
              }}
              className="p-1 text-gray-500 hover:bg-gray-100 rounded"
              title="Abbrechen"
            >
              <MdClose size={16} />
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2 justify-center">
            <span className="font-semibold text-gray-800">{teacher.pool_total}</span>
            <button
              onClick={() => setEditingCredits(true)}
              className="text-gray-400 hover:text-gray-700"
              title="Credits anpassen"
            >
              <MdEdit size={14} />
            </button>
          </div>
        )}
      </td>
      <td className="px-4 py-3 text-center text-sm text-gray-700">
        {teacher.allocated_total}
      </td>
      <td className="px-4 py-3 text-center">
        <span
          className={`text-sm font-semibold ${
            teacher.pool_available > 0 ? 'text-green-700' : 'text-red-700'
          }`}
        >
          {teacher.pool_available}
        </span>
      </td>
      <td className="px-4 py-3 text-center text-sm text-gray-700">
        {teacher.classroom_count}
      </td>
      <td className="px-4 py-3 text-center text-sm text-gray-700">
        {teacher.student_count}
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center justify-center gap-1">
          <button
            onClick={() => setShowPwModal(true)}
            className="p-1.5 rounded hover:bg-gray-100 text-gray-500 hover:text-gray-800"
            title="Passwort zuruecksetzen"
          >
            <MdKey size={18} />
          </button>
          <button
            onClick={handleDelete}
            disabled={busy}
            className="p-1.5 rounded hover:bg-red-50 text-gray-500 hover:text-red-700"
            title="Lehrer loeschen"
          >
            <MdDelete size={18} />
          </button>
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
