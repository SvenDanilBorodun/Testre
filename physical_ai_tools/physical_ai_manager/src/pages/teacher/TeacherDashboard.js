import React, { useCallback, useEffect, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdAdd, MdLogout, MdSchool } from 'react-icons/md';
import { useDispatch, useSelector } from 'react-redux';
import {
  createClassroom,
  listClassrooms,
} from '../../services/teacherApi';
import {
  selectClassroom,
  setClassrooms,
  setClassroomsLoading,
} from '../../features/teacher/teacherSlice';
import CreateClassroomModal from '../../components/teacher/CreateClassroomModal';
import ClassroomDetail from '../../components/teacher/ClassroomDetail';

export default function TeacherDashboard({ onLogout }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const fullName = useSelector((s) => s.auth.fullName);
  const username = useSelector((s) => s.auth.username);
  const poolTotal = useSelector((s) => s.auth.poolTotal);
  const allocatedTotal = useSelector((s) => s.auth.allocatedTotal);
  const poolAvailable = useSelector((s) => s.auth.poolAvailable);
  const studentCount = useSelector((s) => s.auth.studentCount);

  const classrooms = useSelector((s) => s.teacher.classrooms);
  const classroomsLoading = useSelector((s) => s.teacher.classroomsLoading);
  const selectedClassroomId = useSelector((s) => s.teacher.selectedClassroomId);

  const [showCreate, setShowCreate] = useState(false);

  const fetchClassrooms = useCallback(async () => {
    if (!token) return;
    dispatch(setClassroomsLoading(true));
    try {
      const list = await listClassrooms(token);
      dispatch(setClassrooms(list));
    } catch (err) {
      toast.error(err.message || 'Fehler beim Laden der Klassen');
    } finally {
      dispatch(setClassroomsLoading(false));
    }
  }, [token, dispatch]);

  useEffect(() => {
    fetchClassrooms();
  }, [fetchClassrooms]);

  const handleCreate = async (name) => {
    const created = await createClassroom(token, name);
    dispatch(setClassrooms([...classrooms, created]));
    dispatch(selectClassroom(created.id));
  };

  return (
    <div className="h-screen w-screen flex flex-col bg-gray-50">
      {/* Top bar */}
      <header className="bg-white border-b border-gray-200 px-8 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <MdSchool size={26} className="text-teal-600" />
          <h1 className="text-lg font-bold text-gray-800">EduBotics - Lehrer-Dashboard</h1>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="text-sm font-medium text-gray-800">{fullName || username}</div>
            <div className="text-xs text-gray-500 font-mono">{username}</div>
          </div>
          <button
            onClick={onLogout}
            className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-800 px-3 py-2 rounded-lg hover:bg-gray-100"
          >
            <MdLogout size={18} />
            Abmelden
          </button>
        </div>
      </header>

      {/* Credit summary */}
      <div className="bg-white border-b border-gray-200 px-8 py-4 flex items-center gap-6 overflow-x-auto">
        <Stat label="Pool" value={poolTotal ?? '-'} />
        <Stat label="Verteilt" value={allocatedTotal ?? '-'} />
        <Stat
          label="Verfuegbar"
          value={poolAvailable ?? '-'}
          highlight={poolAvailable > 0 ? 'green' : 'red'}
        />
        <Stat label="Schueler" value={studentCount ?? '-'} />
        <div className="ml-auto text-xs text-gray-500 max-w-md">
          Pool und Gesamt-Credits werden vom Admin vergeben. Du verteilst sie an
          Schueler mit den +/- Buttons.
        </div>
      </div>

      {/* Body: sidebar + detail */}
      <div className="flex-1 flex min-h-0">
        <aside className="w-72 bg-white border-r border-gray-200 flex flex-col">
          <div className="px-4 py-3 border-b border-gray-200 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-700 uppercase tracking-wide">
              Klassen
            </h2>
            <button
              onClick={() => setShowCreate(true)}
              className="flex items-center gap-1 px-2 py-1 text-xs font-medium rounded bg-teal-600 text-white hover:bg-teal-700"
            >
              <MdAdd size={14} />
              Neu
            </button>
          </div>
          <div className="flex-1 overflow-y-auto">
            {classroomsLoading ? (
              <div className="p-4 text-sm text-gray-500">Laden...</div>
            ) : classrooms.length === 0 ? (
              <div className="p-4 text-sm text-gray-500">
                Noch keine Klassen. Erstelle deine erste Klasse oben.
              </div>
            ) : (
              <ul>
                {classrooms.map((c) => (
                  <li key={c.id}>
                    <button
                      onClick={() => dispatch(selectClassroom(c.id))}
                      className={clsx(
                        'w-full text-left px-4 py-3 border-b border-gray-100 transition-colors',
                        selectedClassroomId === c.id
                          ? 'bg-teal-50 border-l-4 border-l-teal-500'
                          : 'hover:bg-gray-50'
                      )}
                    >
                      <div className="font-medium text-gray-800 truncate">
                        {c.name}
                      </div>
                      <div className="text-xs text-gray-500 mt-0.5">
                        {c.student_count} / 30 Schueler
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>

        <main className="flex-1 flex flex-col bg-gray-50 min-w-0">
          {selectedClassroomId ? (
            <ClassroomDetail
              key={selectedClassroomId}
              classroomId={selectedClassroomId}
              onClassroomsChanged={fetchClassrooms}
            />
          ) : (
            <div className="flex-1 flex items-center justify-center text-gray-500 p-10 text-center">
              <div>
                <p className="mb-2">Waehle eine Klasse aus der Seitenleiste,</p>
                <p>oder erstelle eine neue Klasse.</p>
              </div>
            </div>
          )}
        </main>
      </div>

      {showCreate && (
        <CreateClassroomModal
          onClose={() => setShowCreate(false)}
          onSubmit={handleCreate}
        />
      )}
    </div>
  );
}

function Stat({ label, value, highlight }) {
  const colorClass =
    highlight === 'green'
      ? 'text-green-700'
      : highlight === 'red'
      ? 'text-red-700'
      : 'text-gray-800';
  return (
    <div className="flex flex-col">
      <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">
        {label}
      </span>
      <span className={`text-2xl font-bold ${colorClass}`}>{value}</span>
    </div>
  );
}
