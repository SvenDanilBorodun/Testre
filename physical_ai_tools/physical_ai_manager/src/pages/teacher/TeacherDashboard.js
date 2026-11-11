import React, { useCallback, useEffect, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useDispatch, useSelector } from 'react-redux';
import {
  confirmDeletionRequest,
  createClassroom,
  getTeacherCreditHistory,
  listClassrooms,
  listDeletionRequests,
} from '../../services/teacherApi';
import { getMe } from '../../services/meApi';
import {
  removeStudentFromSelected,
  selectClassroom,
  setClassrooms,
  setClassroomsLoading,
} from '../../features/teacher/teacherSlice';
import { updateTeacherPool } from '../../features/auth/authSlice';
import CreateClassroomModal from '../../components/teacher/CreateClassroomModal';
import ClassroomDetail from '../../components/teacher/ClassroomDetail';
import CreditHistoryDrawer from '../../components/teacher/CreditHistoryDrawer';
import DeletionRequestList from '../../components/teacher/DeletionRequestList';
import Modal from '../../components/teacher/Modal';
import { hoursDe, numberDe } from '../../utils/hoursDe';
import { DELETION_DE, announceStudentDeletion } from '../../utils/deletionRequest';
import useRefetchOnFocus from '../../hooks/useRefetchOnFocus';
import {
  Btn,
  Divider,
  Progress,
  StatBig,
  TopBar,
} from '../../components/EbUI';
import Icon from '../../components/icons/Icon';
import LegalLinks from '../../components/LegalLinks';

export default function TeacherDashboard({ onLogout }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const fullName = useSelector((s) => s.auth.fullName);
  const username = useSelector((s) => s.auth.username);
  const poolTotal = useSelector((s) => s.auth.poolTotal);
  const allocatedTotal = useSelector((s) => s.auth.allocatedTotal);
  const poolAvailable = useSelector((s) => s.auth.poolAvailable);
  const studentCount = useSelector((s) => s.auth.studentCount);
  const groupCount = useSelector((s) => s.auth.groupCount);
  const groupCreditsTotal = useSelector((s) => s.auth.groupCreditsTotal);
  const compensated = useSelector((s) => s.auth.compensated);

  const classrooms = useSelector((s) => s.teacher.classrooms);
  const classroomsLoading = useSelector((s) => s.teacher.classroomsLoading);
  const selectedClassroomId = useSelector((s) => s.teacher.selectedClassroomId);

  const [showCreate, setShowCreate] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  // GDPR Art. 17 (launch plan step 5): the open deletion requests of this
  // teacher's students; the teacher confirms each (owner decision).
  const [deletionRequests, setDeletionRequests] = useState([]);
  const [deletionLoading, setDeletionLoading] = useState(false);
  const [deletionError, setDeletionError] = useState(null);
  const [showDeletions, setShowDeletions] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(
    typeof window !== 'undefined' ? window.innerWidth >= 1024 : true
  );

  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth < 1024) setSidebarOpen(false);
      else setSidebarOpen(true);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    if (selectedClassroomId && typeof window !== 'undefined' && window.innerWidth < 1024) {
      setSidebarOpen(false);
    }
  }, [selectedClassroomId]);

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

  useRefetchOnFocus(fetchClassrooms);

  const fetchDeletionRequests = useCallback(async () => {
    if (!token) return;
    setDeletionLoading(true);
    try {
      const list = await listDeletionRequests(token);
      setDeletionRequests(Array.isArray(list) ? list : []);
      setDeletionError(null);
    } catch (err) {
      // An older Cloud API has no such route (404): no queue, not an error.
      if (err?.status === 404) {
        setDeletionRequests([]);
        setDeletionError(null);
      } else {
        setDeletionError(err?.message || DELETION_DE.loadFailed);
      }
    } finally {
      setDeletionLoading(false);
    }
  }, [token]);

  useEffect(() => {
    fetchDeletionRequests();
  }, [fetchDeletionRequests]);

  useRefetchOnFocus(fetchDeletionRequests);

  const handleConfirmDeletion = async (req) => {
    try {
      const res = await confirmDeletionRequest(token, req.id);
      setDeletionRequests((list) => list.filter((r) => r.student_id !== req.id));
      dispatch(removeStudentFromSelected(req.id));
      announceStudentDeletion(res, toast);
      fetchClassrooms();
      try {
        const me = await getMe(token);
        dispatch(
          updateTeacherPool({
            pool_total: me.pool_total,
            allocated_total: me.allocated_total,
            pool_available: me.pool_available,
            student_count: me.student_count,
            compensated: me.compensated,
          })
        );
      } catch {}
    } catch (err) {
      toast.error(err?.message || 'Fehler');
      // 409: withdrawn meanwhile; 404: no longer this teacher's student.
      if (err?.status === 409 || err?.status === 404) fetchDeletionRequests();
    }
  };

  const openDeletions = deletionRequests.length;

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
        user={fullName || username || '—'}
        userSub={username}
        userName={fullName || username}
        onLogout={onLogout}
      />

      {/* Stat rail */}
      <div className="bg-white border-b border-[var(--line)] eb-rail flex items-center gap-4 md:gap-6 lg:gap-10 flex-wrap">
        <button
          onClick={() => setSidebarOpen((v) => !v)}
          className="lg:hidden w-9 h-9 rounded-[var(--radius-sm)] text-[var(--ink-2)] hover:bg-[var(--bg-sunk)] flex items-center justify-center transition"
          title={sidebarOpen ? 'Klassenliste schließen' : 'Klassenliste öffnen'}
        >
          {sidebarOpen ? <Icon name="close" size={20} /> : <Icon name="menu" size={20} />}
        </button>
        <StatBig label="Pool" value={poolTotal ?? '—'} sub="Stunden insgesamt" />
        <Divider className="hidden md:block" />
        <StatBig label="Verteilt" value={allocatedTotal ?? '—'} sub="an Schüler" />
        <Divider className="hidden md:block" />
        <StatBig
          label="Verfügbar"
          value={poolAvailable ?? '—'}
          sub="im Pool"
          tone={poolAvailableTone}
        />
        <Divider className="hidden md:block" />
        <StatBig
          label="Schüler"
          value={studentCount ?? '—'}
          sub={`über ${classrooms.length} ${
            classrooms.length === 1 ? 'Klasse' : 'Klassen'
          }`}
        />
        {(groupCount ?? 0) > 0 && (
          <>
            <Divider className="hidden md:block" />
            <StatBig
              label="Gruppen"
              value={groupCount}
              sub={`${hoursDe(groupCreditsTotal ?? 0)} bei Gruppen`}
            />
          </>
        )}
        <div className="w-full xl:w-auto xl:ml-auto xl:max-w-sm p-3 rounded-[var(--radius)] bg-[var(--bg-sunk)] text-xs text-[var(--ink-3)] leading-snug flex flex-col gap-1.5">
          {poolAvailable !== null && poolAvailable !== undefined && (
            <span className="text-sm font-semibold text-[var(--ink)]" data-testid="pool-readout">
              {`Lehrer-Pool: ${numberDe(poolAvailable)} Std. frei`}
            </span>
          )}
          {typeof compensated === 'number' && compensated > 0 && (
            <span>{`Ausgleich vom Admin: ${hoursDe(compensated)}`}</span>
          )}
          <span>Stunden vergibt der Admin. Du verteilst sie an Schüler oder an Arbeitsgruppen.</span>
          <span className="flex flex-wrap gap-2">
            <Btn variant="secondary" size="sm" onClick={() => setShowHistory(true)} title="Verlauf">
              <Icon name="history" /> Verlauf
            </Btn>
            <Btn
              variant={openDeletions > 0 ? 'danger' : 'secondary'}
              size="sm"
              onClick={() => setShowDeletions(true)}
              title={DELETION_DE.title}
              data-testid="deletion-requests-button"
            >
              <Icon name="trash" />
              {openDeletions > 0 ? DELETION_DE.openCount(openDeletions) : DELETION_DE.title}
            </Btn>
          </span>
        </div>
      </div>

      <div className="flex-1 flex min-h-0 relative">
        {/* Mobile backdrop */}
        {sidebarOpen && (
          <div
            className="lg:hidden absolute inset-0 z-10 bg-black/30"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Classroom sidebar */}
        <aside
          className={clsx(
            'bg-white border-r border-[var(--line)] flex flex-col transition-transform duration-200 ease-out',
            'z-20',
            'absolute inset-y-0 left-0 lg:static',
            'w-[280px] max-w-[85vw] shrink-0 lg:translate-x-0',
            sidebarOpen
              ? 'translate-x-0 shadow-pop lg:shadow-none'
              : '-translate-x-full'
          )}
        >
          <div className="px-4 py-3 border-b border-[var(--line)] flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              Klassen
            </span>
            <Btn variant="primary" size="sm" onClick={() => setShowCreate(true)}>
              <Icon name="plus" /> Neu
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

      {/* Impressum · Datenschutz · Nutzungsbedingungen (launch plan step 4). */}
      <LegalLinks variant="footer" />

      {showDeletions && (
        <Modal
          title={DELETION_DE.title}
          onClose={() => setShowDeletions(false)}
          widthClass="max-w-2xl"
        >
          <p className="text-xs text-[var(--ink-3)] mb-2">{DELETION_DE.teacherOnlyOwn}</p>
          <DeletionRequestList
            requests={deletionRequests.map((r) => ({
              id: r.student_id,
              name: r.full_name || r.username || '—',
              username: r.username,
              role: 'student',
              classroomName: r.classroom_name,
              requestedAt: r.requested_at,
              dueBy: r.due_by,
            }))}
            loading={deletionLoading}
            error={deletionError}
            onConfirm={handleConfirmDeletion}
          />
        </Modal>
      )}
      {showHistory && (
        <CreditHistoryDrawer
          title="Stundenverlauf · Lehrer-Pool"
          load={(page) => getTeacherCreditHistory(token, page)}
          onClose={() => setShowHistory(false)}
        />
      )}
      {showCreate && (
        <CreateClassroomModal
          onClose={() => setShowCreate(false)}
          onSubmit={handleCreate}
        />
      )}
    </div>
  );
}
