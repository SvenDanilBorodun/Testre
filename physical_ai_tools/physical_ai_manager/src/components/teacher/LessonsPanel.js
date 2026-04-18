import React, { useEffect, useState, useCallback, useRef } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useSelector } from 'react-redux';
import {
  MdAdd,
  MdCheckCircle,
  MdDelete,
  MdEdit,
  MdExpandLess,
  MdExpandMore,
  MdPlayCircle,
  MdRadioButtonUnchecked,
  MdSave,
  MdClose,
} from 'react-icons/md';
import {
  createLesson,
  deleteLesson,
  listLessonProgress,
  listLessons,
  patchLesson,
  upsertLessonProgress,
} from '../../services/teacherApi';
import { Avatar, Btn, Card, Pill } from '../EbUI';

const STATUS_ORDER = ['not_started', 'in_progress', 'completed'];
const STATUS_LABEL = {
  not_started: 'Offen',
  in_progress: 'In Arbeit',
  completed: 'Fertig',
};
const STATUS_ICON = {
  not_started: MdRadioButtonUnchecked,
  in_progress: MdPlayCircle,
  completed: MdCheckCircle,
};

function nextStatus(status) {
  const i = STATUS_ORDER.indexOf(status);
  return STATUS_ORDER[(i + 1) % STATUS_ORDER.length];
}

export default function LessonsPanel({ classroomId, students }) {
  const token = useSelector((s) => s.auth.session?.access_token);
  const [lessons, setLessons] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);

  const fetchLessons = useCallback(async () => {
    if (!token || !classroomId) return;
    setLoading(true);
    try {
      const list = await listLessons(token, classroomId);
      setLessons(list);
    } catch (err) {
      toast.error(err.message || 'Fehler beim Laden der Lektionen');
    } finally {
      setLoading(false);
    }
  }, [token, classroomId]);

  useEffect(() => {
    fetchLessons();
  }, [fetchLessons]);

  const handleCreate = async ({ title, description }) => {
    try {
      const created = await createLesson(token, classroomId, {
        title,
        description,
        order_index: lessons.length,
      });
      setLessons((l) => [...l, created]);
      toast.success('Lektion erstellt');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    }
  };

  const handleDelete = async (lessonId) => {
    if (
      !window.confirm(
        'Lektion wirklich löschen? Auch alle Schüler-Fortschritte werden entfernt.'
      )
    ) {
      return;
    }
    try {
      await deleteLesson(token, lessonId);
      setLessons((l) => l.filter((x) => x.id !== lessonId));
      toast.success('Lektion gelöscht');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    }
  };

  const handlePatch = async (lessonId, body) => {
    try {
      const updated = await patchLesson(token, lessonId, body);
      setLessons((l) =>
        l.map((x) => (x.id === lessonId ? { ...x, ...updated } : x))
      );
      toast.success('Lektion aktualisiert');
      return updated;
    } catch (err) {
      toast.error(err.message || 'Fehler');
      throw err;
    }
  };

  const handleProgressChange = (lessonId, newCounts) => {
    setLessons((l) =>
      l.map((x) =>
        x.id === lessonId ? { ...x, progress_counts: newCounts } : x
      )
    );
  };

  if (loading) {
    return (
      <div className="p-12 text-center text-[var(--ink-3)]">Lektionen werden geladen…</div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {lessons.length === 0 ? (
        <Card>
          <div className="text-center py-10">
            <div className="text-[var(--ink-2)] font-semibold mb-2">
              Noch keine Lektionen
            </div>
            <p className="text-sm text-[var(--ink-3)] mb-4 max-w-sm mx-auto">
              Erstelle Lektionen für diese Klasse, um den Fortschritt jedes
              Schülers einzeln zu verfolgen.
            </p>
            <Btn
              variant="primary"
              onClick={() => setShowCreate(true)}
              className="mx-auto"
            >
              <MdAdd /> Erste Lektion erstellen
            </Btn>
          </div>
        </Card>
      ) : (
        <>
          {lessons.map((lesson) => (
            <LessonCard
              key={lesson.id}
              lesson={lesson}
              students={students}
              token={token}
              onDelete={() => handleDelete(lesson.id)}
              onPatch={(body) => handlePatch(lesson.id, body)}
              onProgressCountsChange={(counts) =>
                handleProgressChange(lesson.id, counts)
              }
            />
          ))}
          <div>
            <Btn variant="secondary" onClick={() => setShowCreate(true)}>
              <MdAdd /> Lektion hinzufügen
            </Btn>
          </div>
        </>
      )}

      {showCreate && (
        <CreateLessonForm
          onSubmit={async (data) => {
            await handleCreate(data);
            setShowCreate(false);
          }}
          onClose={() => setShowCreate(false)}
        />
      )}
    </div>
  );
}

function CreateLessonForm({ onSubmit, onClose }) {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    try {
      await onSubmit({ title: title.trim(), description: description.trim() });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Neue Lektion" subtitle="Titel + optionale Beschreibung">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Titel
          </span>
          <input
            type="text"
            className="w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="z. B. Objekt greifen – Grundlagen"
            maxLength={200}
            autoFocus
            required
          />
        </label>
        <label className="block">
          <span className="text-xs font-medium text-[var(--ink-2)] mb-1.5 block">
            Beschreibung (optional)
          </span>
          <textarea
            className="w-full min-h-[80px] p-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition resize-y"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Ziele der Lektion, benötigte Materialien, Hinweise…"
            maxLength={4000}
          />
        </label>
        <div className="flex items-center justify-end gap-2">
          <Btn variant="ghost" type="button" onClick={onClose} disabled={busy}>
            Abbrechen
          </Btn>
          <Btn variant="primary" type="submit" disabled={busy || !title.trim()}>
            {busy ? 'Erstellen…' : 'Erstellen'}
          </Btn>
        </div>
      </form>
    </Card>
  );
}

function LessonCard({
  lesson,
  students,
  token,
  onDelete,
  onPatch,
  onProgressCountsChange,
}) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(lesson.title);
  const [editDesc, setEditDesc] = useState(lesson.description || '');
  const [progressRows, setProgressRows] = useState(null);
  const [loadingProgress, setLoadingProgress] = useState(false);
  const loadedForRef = useRef(null);

  useEffect(() => {
    if (!expanded) return;
    if (loadedForRef.current === lesson.id && progressRows) return;
    let alive = true;
    setLoadingProgress(true);
    listLessonProgress(token, lesson.id)
      .then((rows) => {
        if (!alive) return;
        const map = {};
        for (const r of rows) map[r.student_id] = r;
        setProgressRows(map);
        loadedForRef.current = lesson.id;
      })
      .catch((err) => toast.error(err.message || 'Fehler beim Laden'))
      .finally(() => alive && setLoadingProgress(false));
    return () => {
      alive = false;
    };
  }, [expanded, token, lesson.id, progressRows]);

  const counts = lesson.progress_counts || {
    not_started: 0,
    in_progress: 0,
    completed: 0,
  };
  const totalStudents = students.length;
  const completedPct =
    totalStudents > 0 ? Math.round((counts.completed / totalStudents) * 100) : 0;

  const saveEdit = async () => {
    if (!editTitle.trim()) return;
    await onPatch({
      title: editTitle.trim(),
      description: editDesc.trim(),
    });
    setEditing(false);
  };

  const updateProgress = async (studentId, patch) => {
    const current = (progressRows && progressRows[studentId]) || {
      status: 'not_started',
      note: '',
    };
    const next = { ...current, ...patch };
    // Optimistic
    setProgressRows((prev) => ({ ...(prev || {}), [studentId]: { ...current, ...patch } }));
    try {
      const saved = await upsertLessonProgress(token, lesson.id, studentId, {
        status: next.status,
        note: next.note ?? null,
      });
      setProgressRows((prev) => ({ ...(prev || {}), [studentId]: saved }));
      // Recompute counts locally to update the header immediately.
      const newCounts = { not_started: 0, in_progress: 0, completed: 0 };
      for (const s of students) {
        const st =
          ((progressRows || {})[s.id]?.status) ||
          (s.id === studentId ? saved.status : 'not_started');
        const effective = s.id === studentId ? saved.status : st;
        if (newCounts[effective] !== undefined) newCounts[effective] += 1;
      }
      onProgressCountsChange(newCounts);
    } catch (err) {
      toast.error(err.message || 'Fehler');
      // Rollback
      setProgressRows((prev) => ({ ...(prev || {}), [studentId]: current }));
    }
  };

  return (
    <Card padded={false}>
      <div className="p-5">
        <div className="flex items-start gap-3">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="w-8 h-8 shrink-0 rounded-[var(--radius-sm)] text-[var(--ink-2)] hover:bg-[var(--bg-sunk)] flex items-center justify-center transition"
            aria-label={expanded ? 'Einklappen' : 'Ausklappen'}
          >
            {expanded ? <MdExpandLess size={22} /> : <MdExpandMore size={22} />}
          </button>
          <div className="flex-1 min-w-0">
            {editing ? (
              <div className="flex flex-col gap-2">
                <input
                  type="text"
                  className="w-full h-10 px-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-base font-semibold text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition"
                  value={editTitle}
                  onChange={(e) => setEditTitle(e.target.value)}
                  maxLength={200}
                />
                <textarea
                  className="w-full min-h-[70px] p-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition resize-y"
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  maxLength={4000}
                  placeholder="Beschreibung…"
                />
                <div className="flex items-center justify-end gap-2">
                  <Btn
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setEditing(false);
                      setEditTitle(lesson.title);
                      setEditDesc(lesson.description || '');
                    }}
                  >
                    Abbrechen
                  </Btn>
                  <Btn variant="primary" size="sm" onClick={saveEdit}>
                    <MdSave size={16} /> Speichern
                  </Btn>
                </div>
              </div>
            ) : (
              <>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="font-semibold text-[var(--ink)] truncate">
                      {lesson.title}
                    </h3>
                    {lesson.description && (
                      <p className="text-sm text-[var(--ink-3)] mt-0.5 whitespace-pre-line">
                        {lesson.description}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <Btn
                      variant="ghost"
                      size="sm"
                      onClick={() => setEditing(true)}
                      title="Bearbeiten"
                    >
                      <MdEdit size={16} />
                    </Btn>
                    <Btn
                      variant="ghost"
                      size="sm"
                      onClick={onDelete}
                      title="Lektion löschen"
                    >
                      <MdDelete size={16} />
                    </Btn>
                  </div>
                </div>
                <div className="flex items-center gap-4 mt-3 flex-wrap">
                  <Pill tone="neutral">
                    {counts.not_started} offen
                  </Pill>
                  <Pill tone="accent">
                    {counts.in_progress} in Arbeit
                  </Pill>
                  <Pill tone="success">
                    {counts.completed} fertig
                  </Pill>
                  <div className="flex items-center gap-2 font-mono text-[11px] text-[var(--ink-3)]">
                    <span className="text-[var(--ink)] font-semibold">
                      {completedPct}%
                    </span>{' '}
                    der Klasse abgeschlossen
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {expanded && !editing && (
        <div className="border-t border-[var(--line)] bg-[var(--bg-sunk)] p-5">
          {loadingProgress ? (
            <div className="text-sm text-[var(--ink-3)]">Lade Fortschritt…</div>
          ) : students.length === 0 ? (
            <div className="text-sm text-[var(--ink-3)]">
              Keine Schüler in dieser Klasse.
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {students.map((s) => (
                <LessonStudentRow
                  key={s.id}
                  student={s}
                  row={progressRows?.[s.id]}
                  onChange={(patch) => updateProgress(s.id, patch)}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function LessonStudentRow({ student, row, onChange }) {
  const status = row?.status || 'not_started';
  const noteValue = row?.note || '';
  const [noteOpen, setNoteOpen] = useState(false);
  const [noteDraft, setNoteDraft] = useState(noteValue);
  const [noteBusy, setNoteBusy] = useState(false);

  useEffect(() => {
    setNoteDraft(noteValue);
  }, [noteValue]);

  const Icon = STATUS_ICON[status];

  const cycleStatus = () => onChange({ status: nextStatus(status) });

  const saveNote = async () => {
    setNoteBusy(true);
    try {
      await onChange({ note: noteDraft });
      setNoteOpen(false);
    } finally {
      setNoteBusy(false);
    }
  };

  return (
    <div className="bg-white border border-[var(--line)] rounded-[var(--radius)]">
      <div className="flex items-center gap-3 px-4 py-2.5">
        <Avatar name={student.full_name || student.username} size={32} />
        <div className="min-w-0 flex-1">
          <div className="font-medium text-[var(--ink)] text-sm truncate">
            {student.full_name || student.username}
          </div>
          <div className="font-mono text-[11px] text-[var(--ink-3)] truncate">
            {student.username}
          </div>
        </div>
        <button
          onClick={cycleStatus}
          className={clsx(
            'inline-flex items-center gap-1.5 h-8 px-3 rounded-full border text-xs font-medium transition',
            status === 'completed' &&
              'bg-[var(--success-wash)] text-[color:var(--success)] border-transparent',
            status === 'in_progress' &&
              'bg-[var(--accent-wash)] text-[var(--accent-ink)] border-transparent',
            status === 'not_started' &&
              'bg-[var(--bg-sunk)] text-[var(--ink-2)] border-[var(--line)]'
          )}
          title="Status ändern"
        >
          <Icon size={16} />
          {STATUS_LABEL[status]}
        </button>
        <button
          onClick={() => {
            setNoteDraft(noteValue);
            setNoteOpen((v) => !v);
          }}
          className={clsx(
            'w-8 h-8 rounded-[var(--radius-sm)] flex items-center justify-center text-[var(--ink-2)] hover:bg-[var(--bg-sunk)] transition',
            noteValue && 'text-[var(--accent-ink)] bg-[var(--accent-wash)]'
          )}
          title={noteValue ? 'Notiz bearbeiten' : 'Notiz hinzufügen'}
        >
          {noteValue ? <MdEdit size={16} /> : <MdAdd size={16} />}
        </button>
      </div>
      {noteOpen && (
        <div className="border-t border-[var(--line)] px-4 py-3 bg-[var(--bg-sunk)]">
          <div className="flex items-center justify-between gap-2 mb-2">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              Lektionsnotiz
            </span>
            <button
              onClick={() => setNoteOpen(false)}
              className="text-[var(--ink-3)] hover:text-[var(--ink)]"
              title="Schließen"
            >
              <MdClose size={14} />
            </button>
          </div>
          <textarea
            className="w-full min-h-[70px] p-2.5 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition resize-y"
            value={noteDraft}
            onChange={(e) => setNoteDraft(e.target.value)}
            placeholder="Beobachtung, was gut / schlecht lief…"
            maxLength={4000}
            disabled={noteBusy}
          />
          <div className="mt-2 flex items-center justify-end gap-2">
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => {
                setNoteDraft(noteValue);
                setNoteOpen(false);
              }}
              disabled={noteBusy}
            >
              Abbrechen
            </Btn>
            <Btn
              variant="primary"
              size="sm"
              onClick={saveNote}
              disabled={noteBusy || noteDraft === noteValue}
            >
              {noteBusy ? 'Speichern…' : 'Speichern'}
            </Btn>
          </div>
        </div>
      )}
    </div>
  );
}
