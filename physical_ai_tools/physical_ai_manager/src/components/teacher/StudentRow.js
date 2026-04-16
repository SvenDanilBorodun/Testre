import React, { useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdDelete, MdKey, MdHistory, MdEdit, MdMoveDown, MdAdd, MdRemove } from 'react-icons/md';
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

function RenameInline({ student, onSave, onCancel }) {
  const [value, setValue] = useState(student.full_name || '');
  return (
    <div className="flex items-center gap-2">
      <input
        type="text"
        className="px-2 py-1 border border-gray-300 rounded text-sm flex-1"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        maxLength={100}
        autoFocus
      />
      <button
        onClick={() => onSave(value)}
        className="text-xs px-2 py-1 bg-teal-600 text-white rounded hover:bg-teal-700"
        disabled={!value.trim()}
      >
        Speichern
      </button>
      <button
        onClick={onCancel}
        className="text-xs px-2 py-1 text-gray-600 hover:bg-gray-100 rounded"
      >
        Abbrechen
      </button>
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
        className="px-2 py-1 border border-gray-300 rounded text-sm"
      >
        {classrooms.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      <button
        onClick={() => onSave(target)}
        className="text-xs px-2 py-1 bg-teal-600 text-white rounded hover:bg-teal-700"
        disabled={target === student.classroom_id}
      >
        Verschieben
      </button>
      <button
        onClick={onCancel}
        className="text-xs px-2 py-1 text-gray-600 hover:bg-gray-100 rounded"
      >
        Abbrechen
      </button>
    </div>
  );
}

export default function StudentRow({ student, classrooms, onShowHistory }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const [busy, setBusy] = useState(false);
  const [showPwModal, setShowPwModal] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [moving, setMoving] = useState(false);
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
    if (!window.confirm(`Schueler ${student.full_name} wirklich loeschen?`)) return;
    setBusy(true);
    try {
      await apiDeleteStudent(token, student.id);
      dispatch(removeStudentFromSelected(student.id));
      await refreshTeacherPool();
      toast.success('Schueler geloescht');
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
      toast.success('Name geaendert');
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
      // Student moved out of current classroom view; remove from list.
      dispatch(removeStudentFromSelected(student.id));
      toast.success('Schueler verschoben');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setBusy(false);
      setMoving(false);
    }
  };

  const remaining = (student.training_credits || 0) - (student.trainings_used || 0);
  const remainingClass = clsx(
    'text-xs font-semibold px-2 py-0.5 rounded-full',
    remaining > 0 ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
  );

  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50 transition-colors">
      <td className="px-4 py-3">
        {renaming ? (
          <RenameInline
            student={student}
            onSave={handleRename}
            onCancel={() => setRenaming(false)}
          />
        ) : (
          <div className="flex items-center gap-2">
            <div>
              <div className="font-medium text-gray-800">{student.full_name}</div>
              <div className="text-xs text-gray-500 font-mono">{student.username}</div>
            </div>
            <button
              onClick={() => setRenaming(true)}
              className="text-gray-400 hover:text-gray-700"
              title="Namen aendern"
            >
              <MdEdit size={14} />
            </button>
          </div>
        )}
      </td>
      <td className="px-4 py-3 whitespace-nowrap">
        <div className="flex items-center gap-2">
          <button
            onClick={() => handleDelta(-1)}
            disabled={busy || student.training_credits <= student.trainings_used}
            className="w-7 h-7 rounded-full bg-gray-100 hover:bg-red-100 hover:text-red-700 disabled:bg-gray-50 disabled:text-gray-300 flex items-center justify-center"
            title="Credit entziehen"
          >
            <MdRemove size={16} />
          </button>
          <div className="flex flex-col items-center min-w-[72px]">
            <span className="text-sm font-semibold text-gray-800">
              {student.trainings_used || 0} / {student.training_credits || 0}
            </span>
            <span className={remainingClass}>{remaining} frei</span>
          </div>
          <button
            onClick={() => handleDelta(1)}
            disabled={busy}
            className="w-7 h-7 rounded-full bg-gray-100 hover:bg-teal-100 hover:text-teal-700 disabled:bg-gray-50 disabled:text-gray-300 flex items-center justify-center"
            title="Credit hinzufuegen"
          >
            <MdAdd size={16} />
          </button>
        </div>
      </td>
      <td className="px-4 py-3">
        {moving ? (
          otherClassrooms.length === 0 ? (
            <span className="text-xs text-gray-500">Keine andere Klasse</span>
          ) : (
            <MoveInline
              student={student}
              classrooms={classrooms}
              onSave={handleMove}
              onCancel={() => setMoving(false)}
            />
          )
        ) : (
          <div className="flex items-center gap-1">
            <button
              onClick={() => onShowHistory(student)}
              className="p-1.5 rounded hover:bg-gray-100 text-gray-500 hover:text-gray-800"
              title="Trainings-Historie"
            >
              <MdHistory size={18} />
            </button>
            <button
              onClick={() => setShowPwModal(true)}
              className="p-1.5 rounded hover:bg-gray-100 text-gray-500 hover:text-gray-800"
              title="Passwort zuruecksetzen"
            >
              <MdKey size={18} />
            </button>
            <button
              onClick={() => setMoving(true)}
              className="p-1.5 rounded hover:bg-gray-100 text-gray-500 hover:text-gray-800 disabled:opacity-40"
              disabled={otherClassrooms.length === 0}
              title={
                otherClassrooms.length === 0
                  ? 'Keine andere Klasse verfuegbar'
                  : 'In andere Klasse verschieben'
              }
            >
              <MdMoveDown size={18} />
            </button>
            <button
              onClick={handleDelete}
              disabled={busy}
              className="p-1.5 rounded hover:bg-red-50 text-gray-500 hover:text-red-700"
              title="Schueler loeschen"
            >
              <MdDelete size={18} />
            </button>
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
