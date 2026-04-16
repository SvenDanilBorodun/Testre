import React, { useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import { MdAdd, MdDelete, MdEdit } from 'react-icons/md';
import { useDispatch, useSelector } from 'react-redux';
import {
  createStudent,
  deleteClassroom as apiDeleteClassroom,
  getClassroom,
  renameClassroom as apiRenameClassroom,
} from '../../services/teacherApi';
import { getMe } from '../../services/meApi';
import {
  setClassroomLoading,
  setSelectedClassroom,
  upsertStudentInSelected,
  selectClassroom,
} from '../../features/teacher/teacherSlice';
import { updateTeacherPool } from '../../features/auth/authSlice';
import CreateStudentModal from './CreateStudentModal';
import StudentRow from './StudentRow';
import StudentTrainingHistoryDrawer from './StudentTrainingHistoryDrawer';

export default function ClassroomDetail({ classroomId, onClassroomsChanged }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const poolAvailable = useSelector((s) => s.auth.poolAvailable);
  const selected = useSelector((s) => s.teacher.selectedClassroom);
  const classrooms = useSelector((s) => s.teacher.classrooms);
  const loading = useSelector((s) => s.teacher.classroomLoading);

  const [showCreateStudent, setShowCreateStudent] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [historyStudent, setHistoryStudent] = useState(null);

  useEffect(() => {
    if (!token || !classroomId) return;
    dispatch(setClassroomLoading(true));
    getClassroom(token, classroomId)
      .then((c) => dispatch(setSelectedClassroom(c)))
      .catch((err) => toast.error(err.message || 'Fehler beim Laden'))
      .finally(() => dispatch(setClassroomLoading(false)));
  }, [classroomId, token, dispatch]);

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

  const handleCreateStudent = async (data) => {
    const newStudent = await createStudent(token, classroomId, data);
    dispatch(upsertStudentInSelected(newStudent));
    await refreshTeacherPool();
    onClassroomsChanged?.();
  };

  const handleRename = async () => {
    if (!renameValue.trim() || renameValue === selected.name) {
      setRenaming(false);
      return;
    }
    try {
      await apiRenameClassroom(token, classroomId, renameValue);
      dispatch(setSelectedClassroom({ ...selected, name: renameValue }));
      toast.success('Klasse umbenannt');
      onClassroomsChanged?.();
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setRenaming(false);
    }
  };

  const handleDeleteClassroom = async () => {
    if ((selected?.students || []).length > 0) {
      toast.error('Klasse ist nicht leer - erst alle Schueler entfernen');
      return;
    }
    if (!window.confirm(`Klasse "${selected.name}" wirklich loeschen?`)) return;
    try {
      await apiDeleteClassroom(token, classroomId);
      dispatch(selectClassroom(null));
      toast.success('Klasse geloescht');
      onClassroomsChanged?.();
    } catch (err) {
      toast.error(err.message || 'Fehler');
    }
  };

  if (loading || !selected) {
    return (
      <div className="flex items-center justify-center h-full text-gray-500">
        Laden...
      </div>
    );
  }

  const students = selected.students || [];
  const full = students.length >= 30;

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="px-8 py-6 border-b border-gray-200 bg-white">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            {renaming ? (
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  className="px-3 py-1 border border-gray-300 rounded text-lg font-semibold"
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleRename();
                    if (e.key === 'Escape') setRenaming(false);
                  }}
                  autoFocus
                />
                <button
                  onClick={handleRename}
                  className="px-3 py-1 bg-teal-600 text-white rounded hover:bg-teal-700 text-sm"
                >
                  Speichern
                </button>
                <button
                  onClick={() => setRenaming(false)}
                  className="px-3 py-1 text-gray-600 hover:bg-gray-100 rounded text-sm"
                >
                  Abbrechen
                </button>
              </div>
            ) : (
              <>
                <h2 className="text-xl font-bold text-gray-800 truncate">
                  {selected.name}
                </h2>
                <button
                  onClick={() => {
                    setRenameValue(selected.name);
                    setRenaming(true);
                  }}
                  className="text-gray-400 hover:text-gray-700"
                  title="Klasse umbenennen"
                >
                  <MdEdit size={16} />
                </button>
                <span className="text-sm text-gray-500">
                  {students.length} / 30 Schueler
                </span>
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowCreateStudent(true)}
              disabled={full}
              className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:bg-gray-300 text-sm font-medium"
            >
              <MdAdd size={18} />
              Schueler hinzufuegen
            </button>
            <button
              onClick={handleDeleteClassroom}
              className="p-2 rounded-lg text-gray-500 hover:bg-red-50 hover:text-red-700"
              title="Klasse loeschen"
            >
              <MdDelete size={20} />
            </button>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {students.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-gray-500 p-10 text-center">
            <p className="mb-4">Noch keine Schueler in dieser Klasse.</p>
            <button
              onClick={() => setShowCreateStudent(true)}
              className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-teal-600 text-white hover:bg-teal-700 text-sm font-medium"
            >
              <MdAdd size={18} />
              Ersten Schueler hinzufuegen
            </button>
          </div>
        ) : (
          <table className="w-full">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase">
                  Schueler
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase">
                  Credits
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase">
                  Aktionen
                </th>
              </tr>
            </thead>
            <tbody>
              {students.map((s) => (
                <StudentRow
                  key={s.id}
                  student={s}
                  classrooms={classrooms}
                  onShowHistory={setHistoryStudent}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showCreateStudent && (
        <CreateStudentModal
          onClose={() => setShowCreateStudent(false)}
          onSubmit={handleCreateStudent}
          poolAvailable={poolAvailable}
        />
      )}
      {historyStudent && (
        <StudentTrainingHistoryDrawer
          student={historyStudent}
          onClose={() => setHistoryStudent(null)}
        />
      )}
    </div>
  );
}
