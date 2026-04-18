import React, { useCallback, useEffect, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdAdd } from 'react-icons/md';
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
import {
  Btn,
  Divider,
  Pill,
  Progress,
  StatBig,
  TopBar,
} from '../../components/EbUI';

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

  const poolAvailableTone =
    poolAvailable === null || poolAvailable === undefined
      ? undefined
      : poolAvailable > 0
      ? 'success'
      : 'danger';

  return (
    <div
      className="h-screen w-screen flex flex-col"
      style={{ background: 'var(--bg)' }}
    >
      <TopBar
        title="EduBotics"
        subtitle="Lehrer-Dashboard"
        roleBadge={
          <Pill tone="accent" dot>
            Lehrer
          </Pill>
        }
        user={fullName || username || '—'}
        userSub={username}
        userName={fullName || username}
        onLogout={onLogout}
      />

      {/* Stat rail */}
      <div className="bg-white border-b border-[var(--line)] px-8 py-5 flex items-center gap-10 overflow-x-auto">
        <StatBig label="Pool" value={poolTotal ?? '—'} sub="Credits insgesamt" />
        <Divider />
        <StatBig label="Verteilt" value={allocatedTotal ?? '—'} sub="an Schüler" />
        <Divider />
        <StatBig
          label="Verfügbar"
          value={poolAvailable ?? '—'}
          sub="im Pool"
          tone={poolAvailableTone}
        />
        <Divider />
        <StatBig
          label="Schüler"
          value={studentCount ?? '—'}
          sub={`über ${classrooms.length} ${
            classrooms.length === 1 ? 'Klasse' : 'Klassen'
          }`}
        />
        <div className="ml-auto max-w-sm p-3 rounded-[var(--radius)] bg-[var(--bg-sunk)] text-xs text-[var(--ink-3)] leading-snug shrink-0">
          Pool und Credits werden vom Admin vergeben. Du verteilst sie an
          Schüler mit den +/− Buttons.
        </div>
      </div>

      <div className="flex-1 flex min-h-0">
        {/* Classroom sidebar */}
        <aside className="w-[280px] shrink-0 bg-white border-r border-[var(--line)] flex flex-col">
          <div className="px-4 py-3 border-b border-[var(--line)] flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              Klassen
            </span>
            <Btn variant="primary" size="sm" onClick={() => setShowCreate(true)}>
              <MdAdd /> Neu
            </Btn>
          </div>
          <div className="flex-1 overflow-y-auto">
            {classroomsLoading ? (
              <div className="p-4 text-sm text-[var(--ink-3)]">Laden…</div>
            ) : classrooms.length === 0 ? (
              <div className="p-4 text-sm text-[var(--ink-3)]">
                Noch keine Klassen. Erstelle deine erste Klasse oben.
              </div>
            ) : (
              <ul>
                {classrooms.map((c) => {
                  const active = selectedClassroomId === c.id;
                  const count = c.student_count ?? 0;
                  return (
                    <li key={c.id}>
                      <button
                        onClick={() => dispatch(selectClassroom(c.id))}
                        className={clsx(
                          'w-full text-left px-4 py-3.5 border-b border-[var(--line)] transition relative',
                          active
                            ? 'bg-[var(--accent-wash)]'
                            : 'hover:bg-[var(--bg-sunk)]'
                        )}
                      >
                        {active && (
                          <span
                            className="absolute left-0 top-0 bottom-0 w-[3px]"
                            style={{ background: 'var(--accent)' }}
                          />
                        )}
                        <div className="flex items-center justify-between">
                          <span
                            className={clsx(
                              'font-medium truncate',
                              active ? 'text-[var(--accent-ink)]' : 'text-[var(--ink)]'
                            )}
                          >
                            {c.name}
                          </span>
                          <span className="font-mono text-[11px] text-[var(--ink-3)] shrink-0 ml-2">
                            {count}/30
                          </span>
                        </div>
                        <Progress pct={(count / 30) * 100} className="mt-2" />
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </aside>

        <main
          className="flex-1 flex flex-col min-w-0"
          style={{ background: 'var(--bg)' }}
        >
          {selectedClassroomId ? (
            <ClassroomDetail
              key={selectedClassroomId}
              classroomId={selectedClassroomId}
              onClassroomsChanged={fetchClassrooms}
            />
          ) : (
            <div className="flex-1 flex items-center justify-center p-10 text-center grid-dot">
              <div className="max-w-sm">
                <p className="text-[var(--ink-2)] mb-2 font-semibold">
                  Keine Klasse ausgewählt
                </p>
                <p className="text-sm text-[var(--ink-3)]">
                  Wähle eine Klasse aus der Seitenleiste oder erstelle eine neue
                  Klasse.
                </p>
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
