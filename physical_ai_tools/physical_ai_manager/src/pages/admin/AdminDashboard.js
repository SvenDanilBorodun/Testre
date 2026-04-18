import React, { useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import { MdAdd } from 'react-icons/md';
import { useDispatch, useSelector } from 'react-redux';
import { createTeacher, listTeachers } from '../../services/adminApi';
import {
  setLoading,
  setTeachers,
  upsertTeacher,
} from '../../features/admin/adminSlice';
import CreateTeacherModal from '../../components/admin/CreateTeacherModal';
import TeacherRow from '../../components/admin/TeacherRow';
import {
  Btn,
  Card,
  Divider,
  Pill,
  Progress,
  StatBig,
  TopBar,
} from '../../components/EbUI';

export default function AdminDashboard({ onLogout }) {
  const dispatch = useDispatch();
  const token = useSelector((s) => s.auth.session?.access_token);
  const fullName = useSelector((s) => s.auth.fullName);
  const username = useSelector((s) => s.auth.username);
  const teachers = useSelector((s) => s.admin.teachers);
  const loading = useSelector((s) => s.admin.loading);

  const [showCreate, setShowCreate] = useState(false);

  const fetchTeachers = useCallback(async () => {
    if (!token) return;
    dispatch(setLoading(true));
    try {
      const list = await listTeachers(token);
      dispatch(setTeachers(list));
    } catch (err) {
      toast.error(err.message || 'Fehler beim Laden');
    } finally {
      dispatch(setLoading(false));
    }
  }, [token, dispatch]);

  useEffect(() => {
    fetchTeachers();
  }, [fetchTeachers]);

  const handleCreate = async (data) => {
    const created = await createTeacher(token, data);
    dispatch(upsertTeacher(created));
  };

  const totals = useMemo(() => {
    const pool = teachers.reduce((s, t) => s + (t.pool_total || 0), 0);
    const alloc = teachers.reduce((s, t) => s + (t.allocated_total || 0), 0);
    const avail = teachers.reduce(
      (s, t) => s + (t.pool_available ?? (t.pool_total || 0) - (t.allocated_total || 0)),
      0
    );
    const students = teachers.reduce((s, t) => s + (t.student_count || 0), 0);
    const classrooms = teachers.reduce((s, t) => s + (t.classroom_count || 0), 0);
    return { pool, alloc, avail, students, classrooms };
  }, [teachers]);

  return (
    <div
      className="h-screen w-screen flex flex-col"
      style={{ background: 'var(--bg)' }}
    >
      <TopBar
        title="EduBotics"
        subtitle="Admin-Dashboard"
        roleBadge={
          <Pill tone="amber" dot>
            Admin
          </Pill>
        }
        user={fullName || username || '—'}
        userSub={username}
        userName={fullName || username}
        onLogout={onLogout}
      />

      {/* Stat rail */}
      <div className="bg-white border-b border-[var(--line)] eb-rail flex items-center gap-4 md:gap-6 lg:gap-10 flex-wrap">
        <StatBig label="Lehrer" value={teachers.length} sub="insgesamt" />
        <Divider className="hidden md:block" />
        <StatBig
          label="Pool Gesamt"
          value={totals.pool}
          sub="Credits zugewiesen"
        />
        <Divider className="hidden md:block" />
        <StatBig
          label="Verteilt"
          value={totals.alloc}
          sub="an Schüler weitergegeben"
        />
        <Divider className="hidden md:block" />
        <StatBig
          label="Verfügbar"
          value={totals.avail}
          sub="im Lehrer-Pool"
          tone={totals.avail > 0 ? 'success' : undefined}
        />
        <div className="ml-auto shrink-0">
          <Btn variant="primary" onClick={() => setShowCreate(true)}>
            <MdAdd /> Neuer Lehrer
          </Btn>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="eb-shell space-y-5 md:space-y-6">
        {teachers.length > 0 && (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-5 lg:gap-6">
            <Card
              title="Credit-Verteilung"
              subtitle="Pool vs. Verteilt je Lehrer"
              className="lg:col-span-2"
            >
              <StackedBars teachers={teachers} />
            </Card>
            <Card title="Übersicht" subtitle="Aktueller Stand">
              <div className="space-y-3 text-sm">
                <Row k="Lehrer" v={teachers.length} />
                <Row k="Klassen" v={totals.classrooms} />
                <Row k="Schüler" v={totals.students} />
                <Row
                  k="Credits verbraucht"
                  v={
                    <span className="font-mono">
                      {totals.alloc} / {totals.pool}
                    </span>
                  }
                />
              </div>
              <div className="mt-5">
                <Progress
                  pct={totals.pool > 0 ? (totals.alloc / totals.pool) * 100 : 0}
                  tone="accent"
                />
                <div className="text-[11px] text-[var(--ink-3)] mt-2 font-mono">
                  {totals.pool > 0
                    ? `${Math.round((totals.alloc / totals.pool) * 100)}% ausgelastet`
                    : 'Keine Credits zugewiesen'}
                </div>
              </div>
            </Card>
          </div>
        )}

        <Card
          title="Lehrer"
          subtitle={`${teachers.length} ${teachers.length === 1 ? 'Eintrag' : 'Einträge'}`}
          padded={false}
        >
          {loading ? (
            <div className="p-12 text-center text-[var(--ink-3)]">Laden…</div>
          ) : teachers.length === 0 ? (
            <div className="p-12 text-center text-[var(--ink-3)]">
              <p className="mb-4">Noch keine Lehrer.</p>
              <Btn
                variant="primary"
                onClick={() => setShowCreate(true)}
                className="mx-auto"
              >
                <MdAdd /> Ersten Lehrer erstellen
              </Btn>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm min-w-[720px]">
                <thead className="bg-[var(--bg-sunk)] border-b border-[var(--line)]">
                  <tr className="text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
                    <th className="text-left py-3 px-5">Lehrer</th>
                    <th className="text-center py-3 px-3">Pool</th>
                    <th className="text-center py-3 px-3">Verteilt</th>
                    <th className="text-center py-3 px-3">Verfügbar</th>
                    <th className="text-center py-3 px-3">Klassen</th>
                    <th className="text-center py-3 px-3">Schüler</th>
                    <th className="text-right py-3 px-5">Aktionen</th>
                  </tr>
                </thead>
                <tbody>
                  {teachers.map((t) => (
                    <TeacherRow key={t.id} teacher={t} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        </div>
      </div>

      {showCreate && (
        <CreateTeacherModal
          onClose={() => setShowCreate(false)}
          onSubmit={handleCreate}
        />
      )}
    </div>
  );
}

function Row({ k, v }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[var(--ink-3)]">{k}</span>
      <span className="font-mono text-[var(--ink)] font-semibold">{v}</span>
    </div>
  );
}

function StackedBars({ teachers }) {
  const data = teachers.map((t) => {
    const pool = t.pool_total || 0;
    const alloc = t.allocated_total || 0;
    const avail = t.pool_available ?? Math.max(0, pool - alloc);
    const shortName = shortenName(t.full_name || t.username || '—');
    return { name: shortName, alloc, avail };
  });
  const max = Math.max(80, ...data.map((d) => d.alloc + d.avail));
  return (
    <div>
      <div className="flex items-end gap-3 h-[180px]">
        {data.map((d, i) => {
          const allocPct = (d.alloc / max) * 100;
          const availPct = (d.avail / max) * 100;
          return (
            <div key={i} className="flex-1 flex flex-col items-center gap-2 min-w-0">
              <div className="w-full flex flex-col justify-end gap-[2px] flex-1">
                <div
                  className="w-full rounded-t-[4px] bg-[var(--bg-sunk)]"
                  style={{
                    height: `${availPct}%`,
                    minHeight: d.avail ? 4 : 0,
                  }}
                />
                <div
                  className="w-full rounded-b-[4px]"
                  style={{
                    background: 'var(--accent)',
                    height: `${allocPct}%`,
                    minHeight: d.alloc ? 4 : 0,
                  }}
                />
              </div>
              <span
                className="text-[10px] font-mono text-[var(--ink-3)] truncate max-w-full"
                title={d.name}
              >
                {d.name}
              </span>
            </div>
          );
        })}
        <div className="flex flex-col items-start gap-2 text-[11px] text-[var(--ink-3)] pl-3 border-l border-[var(--line)] shrink-0">
          <span className="flex items-center gap-1.5">
            <span
              className="w-2.5 h-2.5 rounded-sm"
              style={{ background: 'var(--accent)' }}
            />{' '}
            verteilt
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-sm bg-[var(--bg-sunk)] border border-[var(--line)]" />{' '}
            verfügbar
          </span>
        </div>
      </div>
    </div>
  );
}

function shortenName(name) {
  const parts = name.trim().split(/\s+/);
  if (parts.length < 2) return name.slice(0, 10);
  return `${parts[0][0]}. ${parts[parts.length - 1]}`;
}
