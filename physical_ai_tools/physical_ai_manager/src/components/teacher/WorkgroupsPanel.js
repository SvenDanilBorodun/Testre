import React, { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import {
  MdAdd,
  MdDelete,
  MdEdit,
  MdEventNote,
  MdGroups,
  MdPeople,
  MdSavings,
} from 'react-icons/md';
import { useDispatch, useSelector } from 'react-redux';
import { Btn, Card, Pill, Progress } from '../EbUI';
import {
  createWorkgroup,
  deleteWorkgroup,
  getWorkgroup,
  listWorkgroups,
  renameWorkgroup,
} from '../../services/workgroupsApi';
import { getMe } from '../../services/meApi';
import { updateTeacherPool } from '../../features/auth/authSlice';
import { setSelectedClassroom } from '../../features/teacher/teacherSlice';
import CreateWorkgroupModal from './CreateWorkgroupModal';
import WorkgroupMembersModal from './WorkgroupMembersModal';
import WorkgroupCreditsModal from './WorkgroupCreditsModal';

/**
 * Sidebar+detail panel rendered inside a classroom's tab area.
 *
 * Left rail: list of workgroups in this classroom; "neu" button up top.
 * Right rail: detail view with members, credit chip, rename, delete.
 *
 * Updates trigger a parent refetch of the classroom (so StudentRow gets
 * the new workgroup_id badges) and the teacher pool summary (so the
 * dashboard's "verfügbar" number refreshes).
 */
export default function WorkgroupsPanel({
  classroomId,
  classroomStudents = [],
  onShowGroupProgress,
}) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const poolAvailable = useSelector((s) => s.auth.poolAvailable);
  const selectedClassroom = useSelector((s) => s.teacher.selectedClassroom);

  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showMembers, setShowMembers] = useState(false);
  const [showCredits, setShowCredits] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');

  const refreshTeacherPool = useCallback(async () => {
    try {
      const me = await getMe(token);
      dispatch(
        updateTeacherPool({
          pool_total: me.pool_total,
          allocated_total: me.allocated_total,
          pool_available: me.pool_available,
          student_count: me.student_count,
          group_count: me.group_count,
          group_credits_total: me.group_credits_total,
        })
      );
    } catch {}
  }, [dispatch, token]);

  // Refresh the classroom's student list so workgroup_id chips show up
  // immediately after add/remove.
  const refreshClassroomStudents = useCallback(async () => {
    if (!selectedClassroom) return;
    try {
      // Force a refetch by re-loading detail through the existing path —
      // we call /teacher/classrooms/{id} via getClassroom indirectly by
      // dispatching a setSelectedClassroom to the same id, and rely on
      // ClassroomDetail.useEffect; but the cleanest is to re-fetch here.
      const { getClassroom } = await import('../../services/teacherApi');
      const fresh = await getClassroom(token, classroomId);
      dispatch(setSelectedClassroom(fresh));
    } catch (err) {
      console.warn('classroom refetch failed:', err);
    }
  }, [classroomId, dispatch, selectedClassroom, token]);

  const fetchGroups = useCallback(async () => {
    if (!token || !classroomId) return;
    setLoading(true);
    try {
      const list = await listWorkgroups(token, classroomId);
      setGroups(list);
      if (selectedId && !list.some((g) => g.id === selectedId)) {
        setSelectedId(null);
        setDetail(null);
      } else if (!selectedId && list.length > 0) {
        setSelectedId(list[0].id);
      }
    } catch (err) {
      toast.error(err.message || 'Fehler beim Laden');
    } finally {
      setLoading(false);
    }
  }, [token, classroomId, selectedId]);

  const fetchDetail = useCallback(async () => {
    if (!token || !selectedId) {
      setDetail(null);
      return;
    }
    try {
      const d = await getWorkgroup(token, selectedId);
      setDetail(d);
    } catch (err) {
      toast.error(err.message || 'Fehler beim Laden');
      setDetail(null);
    }
  }, [token, selectedId]);

  useEffect(() => {
    fetchGroups();
  }, [fetchGroups]);

  useEffect(() => {
    fetchDetail();
  }, [fetchDetail]);

  const handleCreate = async (name) => {
    const created = await createWorkgroup(token, classroomId, name);
    setGroups((prev) => [...prev, created]);
    setSelectedId(created.id);
    await refreshTeacherPool();
  };

  const handleRename = async () => {
    if (!detail || !renameValue.trim() || renameValue === detail.name) {
      setRenaming(false);
      return;
    }
    try {
      const updated = await renameWorkgroup(token, detail.id, renameValue.trim());
      setDetail((d) => ({ ...d, name: updated.name }));
      setGroups((prev) =>
        prev.map((g) => (g.id === detail.id ? { ...g, name: updated.name } : g))
      );
      toast.success('Umbenannt');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setRenaming(false);
    }
  };

  const handleDelete = async () => {
    if (!detail) return;
    if ((detail.member_count || 0) > 0) {
      toast.error('Gruppe ist nicht leer — erst alle Mitglieder entfernen');
      return;
    }
    if (!window.confirm(`Arbeitsgruppe "${detail.name}" löschen?`)) return;
    try {
      await deleteWorkgroup(token, detail.id);
      setGroups((prev) => prev.filter((g) => g.id !== detail.id));
      setSelectedId(null);
      setDetail(null);
      toast.success('Gruppe gelöscht');
      await refreshTeacherPool();
    } catch (err) {
      toast.error(err.message || 'Fehler');
    }
  };

  const handleMembersChanged = async (updated) => {
    setDetail(updated);
    setGroups((prev) =>
      prev.map((g) =>
        g.id === updated.id
          ? {
              ...g,
              member_count: updated.member_count,
              shared_credits: updated.shared_credits,
              trainings_used: updated.trainings_used,
              remaining: updated.remaining,
            }
          : g
      )
    );
    await refreshClassroomStudents();
  };

  const handleCreditsChanged = async (updated) => {
    setDetail((d) => ({ ...d, ...updated }));
    setGroups((prev) =>
      prev.map((g) =>
        g.id === updated.id
          ? {
              ...g,
              shared_credits: updated.shared_credits,
              remaining: updated.remaining,
            }
          : g
      )
    );
    await refreshTeacherPool();
  };

  return (
    <Card padded={false} className="mb-4">
      <div className="flex flex-col md:flex-row min-h-[280px]">
        {/* Left rail: groups list */}
        <div className="md:w-64 md:border-r border-[var(--line)] bg-[var(--bg-sunk)] p-3 flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-[var(--ink)]">
              <MdGroups />
              <span className="text-sm font-semibold">Arbeitsgruppen</span>
            </div>
            <Btn
              variant="primary"
              size="sm"
              onClick={() => setShowCreate(true)}
              title="Neue Gruppe erstellen"
            >
              <MdAdd /> Neu
            </Btn>
          </div>
          {loading ? (
            <p className="text-xs text-[var(--ink-3)] py-1">Laden…</p>
          ) : groups.length === 0 ? (
            <p className="text-xs text-[var(--ink-3)] py-2 leading-snug">
              Noch keine Gruppen. Erstelle eine mit "Neu".
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {groups.map((g) => {
                const active = g.id === selectedId;
                const used = g.trainings_used || 0;
                const total = g.shared_credits || 0;
                const pct = total > 0 ? (used / total) * 100 : 0;
                return (
                  <li key={g.id}>
                    <button
                      onClick={() => setSelectedId(g.id)}
                      className={`w-full text-left px-3 py-2 rounded-[var(--radius-sm)] transition ${
                        active
                          ? 'bg-white border border-[var(--accent)] shadow-soft'
                          : 'bg-white border border-transparent hover:border-[var(--line)]'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span className="text-sm font-medium text-[var(--ink)] truncate">
                          {g.name}
                        </span>
                        <Pill tone={used >= total && total > 0 ? 'danger' : 'accent'}>
                          {used} / {total}
                        </Pill>
                      </div>
                      <Progress pct={pct} tone={used >= total && total > 0 ? 'danger' : 'accent'} />
                      <div className="font-mono text-[10px] text-[var(--ink-3)] mt-1">
                        {g.member_count} Mitglieder
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Right rail: detail */}
        <div className="flex-1 p-5">
          {!detail ? (
            <div className="flex items-center justify-center h-full text-[var(--ink-3)] text-sm">
              {groups.length === 0
                ? 'Erstelle eine Arbeitsgruppe, um Credits, Trainings und Datensätze für mehrere Schüler zu bündeln.'
                : 'Wähle links eine Gruppe.'}
            </div>
          ) : (
            <div className="flex flex-col gap-5">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                {renaming ? (
                  <div className="flex items-center gap-2">
                    <input
                      type="text"
                      className="h-9 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-base font-semibold text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)]"
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
                  <div className="flex items-center gap-2">
                    <h3 className="text-lg font-semibold text-[var(--ink)]">
                      {detail.name}
                    </h3>
                    <button
                      onClick={() => {
                        setRenameValue(detail.name);
                        setRenaming(true);
                      }}
                      className="text-[var(--ink-4)] hover:text-[var(--ink)] transition"
                      title="Umbenennen"
                    >
                      <MdEdit size={16} />
                    </button>
                  </div>
                )}
                <div className="flex items-center gap-1.5">
                  <Btn
                    variant="secondary"
                    size="sm"
                    onClick={() => setShowMembers(true)}
                    title="Mitglieder verwalten"
                  >
                    <MdPeople /> Mitglieder
                  </Btn>
                  <Btn
                    variant="secondary"
                    size="sm"
                    onClick={() => setShowCredits(true)}
                    title="Geteilte Credits anpassen"
                  >
                    <MdSavings /> Credits
                  </Btn>
                  {onShowGroupProgress && (
                    <Btn
                      variant="secondary"
                      size="sm"
                      onClick={() => onShowGroupProgress(detail)}
                      title="Tägliche Fortschrittsnotizen für diese Gruppe"
                    >
                      <MdEventNote /> Fortschritt
                    </Btn>
                  )}
                  <button
                    onClick={handleDelete}
                    className="w-9 h-9 rounded-[var(--radius-sm)] text-[var(--ink-3)] hover:bg-[var(--danger-wash)] hover:text-[color:var(--danger)] flex items-center justify-center transition"
                    title="Gruppe löschen"
                  >
                    <MdDelete size={18} />
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-3 gap-3 text-center">
                <div className="bg-[var(--bg-sunk)] rounded-[var(--radius-sm)] py-3">
                  <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--ink-3)] mb-1">
                    Mitglieder
                  </div>
                  <div className="text-2xl font-semibold text-[var(--ink)]">
                    {detail.member_count}
                  </div>
                </div>
                <div className="bg-[var(--bg-sunk)] rounded-[var(--radius-sm)] py-3">
                  <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--ink-3)] mb-1">
                    Geteilte Credits
                  </div>
                  <div className="text-2xl font-semibold text-[var(--ink)]">
                    {detail.shared_credits}
                  </div>
                </div>
                <div className="bg-[var(--accent-wash)] rounded-[var(--radius-sm)] py-3">
                  <div className="text-[10px] uppercase font-mono tracking-wider text-[var(--accent-ink)] mb-1">
                    Frei
                  </div>
                  <div className="text-2xl font-semibold text-[var(--accent-ink)]">
                    {detail.remaining}
                  </div>
                </div>
              </div>

              <div>
                <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)] mb-2">
                  Mitglieder
                </div>
                {(detail.members || []).length === 0 ? (
                  <p className="text-sm text-[var(--ink-3)]">
                    Noch keine Mitglieder. Verwende "Mitglieder" oben rechts.
                  </p>
                ) : (
                  <ul className="flex flex-wrap gap-1.5">
                    {detail.members.map((m) => (
                      <li
                        key={m.id}
                        className="px-2.5 py-1 rounded-[var(--radius-sm)] bg-white border border-[var(--line)] text-xs text-[var(--ink)]"
                      >
                        {m.full_name || m.username}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {showCreate && (
        <CreateWorkgroupModal
          onClose={() => setShowCreate(false)}
          onSubmit={handleCreate}
        />
      )}
      {showMembers && detail && (
        <WorkgroupMembersModal
          token={token}
          classroomStudents={classroomStudents}
          workgroup={detail}
          onClose={() => setShowMembers(false)}
          onChanged={handleMembersChanged}
        />
      )}
      {showCredits && detail && (
        <WorkgroupCreditsModal
          token={token}
          workgroup={detail}
          poolAvailable={poolAvailable}
          onClose={() => setShowCredits(false)}
          onChanged={handleCreditsChanged}
        />
      )}
    </Card>
  );
}
