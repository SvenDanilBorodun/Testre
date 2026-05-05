import React, { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import { MdAdd, MdDelete, MdEdit, MdEventNote, MdConstruction } from 'react-icons/md';
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
import useRefetchOnFocus from '../../hooks/useRefetchOnFocus';
import CreateStudentModal from './CreateStudentModal';
import StudentRow from './StudentRow';
import StudentTrainingHistoryDrawer from './StudentTrainingHistoryDrawer';
import DailyProgressDrawer from './DailyProgressDrawer';
import WorkflowTemplatesPage from '../../pages/teacher/WorkflowTemplatesPage';
import { Btn, Card } from '../EbUI';

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
  const [showClassProgress, setShowClassProgress] = useState(false);
  const [progressStudent, setProgressStudent] = useState(null);
  const [showWorkflowTemplates, setShowWorkflowTemplates] = useState(false);

  const fetchClassroom = useCallback(() => {
    if (!token || !classroomId) return;
    dispatch(setClassroomLoading(true));
    getClassroom(token, classroomId)
      .then((c) => dispatch(setSelectedClassroom(c)))
      .catch((err) => toast.error(err.message || 'Fehler beim Laden'))
      .finally(() => dispatch(setClassroomLoading(false)));
  }, [classroomId, token, dispatch]);

  useEffect(() => {
    fetchClassroom();
  }, [fetchClassroom]);

  useRefetchOnFocus(fetchClassroom);

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
      toast.error('Klasse ist nicht leer — erst alle Schüler entfernen');
      return;
    }
    if (!window.confirm(`Klasse "${selected.name}" wirklich löschen?`)) return;
    try {
      await apiDeleteClassroom(token, classroomId);
      dispatch(selectClassroom(null));
      toast.success('Klasse gelöscht');
      onClassroomsChanged?.();
    } catch (err) {
      toast.error(err.message || 'Fehler');
    }
  };

  if (loading || !selected) {
    return (
      <div className="flex items-center justify-center h-full text-[var(--ink-3)]">
        Laden…
      </div>
    );
  }

  const students = selected.students || [];
  const full = students.length >= 30;

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="eb-rail border-b border-[var(--line)] bg-white">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-3 min-w-0">
            {renaming ? (
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  className="h-9 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-lg font-semibold text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)]"
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleRename();
                    if (e.key === 'Escape') setRenaming(false);
                  }}
                  autoFocus
                />
                <Btn variant="primary" size="sm" onClick={handleRename}>
                  Speichern
                </Btn>
                <Btn variant="ghost" size="sm" onClick={() => setRenaming(false)}>
                  Abbrechen
                </Btn>
              </div>
            ) : (
              <>
                <h2 className="text-xl font-semibold tracking-tight text-[var(--ink)] truncate">
                  {selected.name}
                </h2>
                <button
                  onClick={() => {
                    setRenameValue(selected.name);
                    setRenaming(true);
                  }}
                  className="text-[var(--ink-4)] hover:text-[var(--ink)] transition"
                  title="Klasse umbenennen"
                >
                  <MdEdit size={16} />
                </button>
                <span className="text-sm text-[var(--ink-3)] font-mono">
                  {students.length} / 30 Schüler
                </span>
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Btn
              variant="secondary"
              onClick={() => setShowClassProgress(true)}
              title="Klassen-Fortschritt · tägliche Notizen für die ganze Klasse"
            >
              <MdEventNote /> Klassen-Fortschritt
            </Btn>
            <Btn
              variant="secondary"
              onClick={() => setShowWorkflowTemplates((v) => !v)}
              title="Roboter-Studio-Vorlagen für diese Klasse veröffentlichen"
            >
              <MdConstruction /> {showWorkflowTemplates ? 'Vorlagen ausblenden' : 'Workflow-Vorlagen'}
            </Btn>
            <Btn
              variant="primary"
              onClick={() => setShowCreateStudent(true)}
              disabled={full}
            >
              <MdAdd /> Schüler hinzufügen
            </Btn>
            <button
              onClick={handleDeleteClassroom}
              className="w-9 h-9 rounded-[var(--radius-sm)] text-[var(--ink-3)] hover:bg-[var(--danger-wash)] hover:text-[color:var(--danger)] flex items-center justify-center transition"
              title="Klasse löschen"
            >
              <MdDelete size={20} />
            </button>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="eb-shell">
          {showWorkflowTemplates && (
            <Card padded={true} className="mb-4">
              <WorkflowTemplatesPage classroomId={classroomId} />
            </Card>
          )}
          {students.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-[var(--ink-3)] p-8 md:p-10 text-center">
              <p className="mb-4">Noch keine Schüler in dieser Klasse.</p>
              <Btn variant="primary" onClick={() => setShowCreateStudent(true)}>
                <MdAdd /> Ersten Schüler hinzufügen
              </Btn>
            </div>
          ) : (
            <Card padded={false}>
              <div className="overflow-x-auto">
                <table className="w-full text-sm min-w-[560px]">
                  <thead className="bg-[var(--bg-sunk)] border-b border-[var(--line)]">
                    <tr className="text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
                      <th className="text-left py-3 px-5">Schüler</th>
                      <th className="text-left py-3 px-3">Credits</th>
                      <th className="text-right py-3 px-5">Aktionen</th>
                    </tr>
                  </thead>
                  <tbody>
                    {students.map((s) => (
                      <StudentRow
                        key={s.id}
                        student={s}
                        classrooms={classrooms}
                        onShowHistory={setHistoryStudent}
                        onShowProgress={setProgressStudent}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
        </div>
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
      {showClassProgress && (
        <DailyProgressDrawer
          classroomId={classroomId}
          onClose={() => setShowClassProgress(false)}
        />
      )}
      {progressStudent && (
        <DailyProgressDrawer
          classroomId={classroomId}
          student={progressStudent}
          onClose={() => setProgressStudent(null)}
        />
      )}
    </div>
  );
}
