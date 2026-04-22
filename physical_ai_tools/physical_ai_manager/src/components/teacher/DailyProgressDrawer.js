import React, { useCallback, useEffect, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdAdd, MdClose, MdDelete, MdEdit, MdSave } from 'react-icons/md';
import { useSelector } from 'react-redux';
import {
  createProgressEntry,
  deleteProgressEntry,
  listProgressEntries,
  patchProgressEntry,
} from '../../services/teacherApi';
import useRefetchOnFocus from '../../hooks/useRefetchOnFocus';
import { Avatar, Btn, Pill } from '../EbUI';

function formatDateLong(iso) {
  if (!iso) return '';
  try {
    return new Date(`${iso}T00:00:00`).toLocaleDateString('de-DE', {
      weekday: 'long',
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
    });
  } catch {
    return iso;
  }
}

function todayISO() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/**
 * Drawer used for both scopes:
 *  - student: pass { classroomId, student } to log daily notes for one student
 *  - class:   pass { classroomId } (no student) to log daily notes for the whole class
 */
export default function DailyProgressDrawer({ classroomId, student, onClose }) {
  const token = useSelector((s) => s.auth.session?.access_token);
  const isClassScope = !student;

  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);

  // Composer state
  const [draftDate, setDraftDate] = useState(todayISO());
  const [draftNote, setDraftNote] = useState('');
  const [saving, setSaving] = useState(false);

  // Per-row edit state
  const [editingId, setEditingId] = useState(null);
  const [editDraft, setEditDraft] = useState('');
  const [editBusy, setEditBusy] = useState(false);

  const fetchEntries = useCallback(async () => {
    if (!token || !classroomId) return;
    setLoading(true);
    try {
      const list = await listProgressEntries(token, classroomId, {
        studentId: student?.id,
        scope: isClassScope ? 'classroom' : undefined,
      });
      setEntries(list);
    } catch (err) {
      toast.error(err.message || 'Fehler beim Laden');
    } finally {
      setLoading(false);
    }
  }, [token, classroomId, student?.id, isClassScope]);

  useEffect(() => {
    fetchEntries();
  }, [fetchEntries]);

  useRefetchOnFocus(fetchEntries);

  const handleCreate = async () => {
    if (!draftNote.trim()) {
      toast.error('Bitte einen Text eingeben');
      return;
    }
    setSaving(true);
    try {
      const created = await createProgressEntry(token, classroomId, {
        note: draftNote.trim(),
        entry_date: draftDate,
        student_id: student?.id || null,
      });
      setEntries((prev) =>
        [created, ...prev].sort((a, b) => b.entry_date.localeCompare(a.entry_date))
      );
      setDraftNote('');
      toast.success('Eintrag gespeichert');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setSaving(false);
    }
  };

  const handleStartEdit = (entry) => {
    setEditingId(entry.id);
    setEditDraft(entry.note);
  };

  const handleSaveEdit = async () => {
    if (!editDraft.trim()) {
      toast.error('Text darf nicht leer sein');
      return;
    }
    setEditBusy(true);
    try {
      const updated = await patchProgressEntry(token, editingId, editDraft.trim());
      setEntries((prev) =>
        prev.map((e) => (e.id === editingId ? updated : e))
      );
      setEditingId(null);
      toast.success('Gespeichert');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    } finally {
      setEditBusy(false);
    }
  };

  const handleDelete = async (entry) => {
    if (!window.confirm(`Eintrag vom ${formatDateLong(entry.entry_date)} wirklich löschen?`)) {
      return;
    }
    try {
      await deleteProgressEntry(token, entry.id);
      setEntries((prev) => prev.filter((e) => e.id !== entry.id));
      toast.success('Gelöscht');
    } catch (err) {
      toast.error(err.message || 'Fehler');
    }
  };

  const headerTitle = isClassScope
    ? 'Klassen-Fortschritt'
    : 'Fortschritt · Täglich';
  const headerSubtitle = isClassScope
    ? 'Notizen für die gesamte Klasse'
    : `${student.full_name || student.username}`;

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end"
      onClick={onClose}
    >
      <div className="absolute inset-0 bg-black/30 backdrop-blur-sm" />
      <aside
        className="relative w-[540px] max-w-full h-full bg-[var(--bg)] border-l border-[var(--line)] shadow-pop flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-6 py-5 border-b border-[var(--line)] bg-white flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            {isClassScope ? (
              <span
                className="inline-flex items-center justify-center rounded-full text-white font-semibold shrink-0"
                style={{
                  width: 36,
                  height: 36,
                  background: 'var(--accent)',
                }}
              >
                ★
              </span>
            ) : (
              <Avatar name={student.full_name || student.username} />
            )}
            <div className="min-w-0">
              <h3 className="font-semibold text-[var(--ink)] truncate">
                {headerTitle}
              </h3>
              <div className="text-[11px] text-[var(--ink-3)] font-mono truncate">
                {headerSubtitle}
              </div>
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-9 h-9 rounded-[var(--radius-sm)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] hover:text-[var(--ink)] flex items-center justify-center transition shrink-0"
            aria-label="Schließen"
          >
            <MdClose size={20} />
          </button>
        </div>

        {/* Composer */}
        <div className="px-6 py-5 border-b border-[var(--line)] bg-white">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)] mb-3">
            Neuer Eintrag
          </div>
          <div className="flex items-center gap-2 mb-3">
            <label className="text-xs text-[var(--ink-2)] font-medium">
              Datum
            </label>
            <input
              type="date"
              value={draftDate}
              onChange={(e) => setDraftDate(e.target.value)}
              className="h-9 px-2.5 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm font-mono text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition"
            />
            <Btn
              variant="ghost"
              size="sm"
              onClick={() => setDraftDate(todayISO())}
            >
              Heute
            </Btn>
          </div>
          <textarea
            value={draftNote}
            onChange={(e) => setDraftNote(e.target.value)}
            placeholder={
              isClassScope
                ? 'Was wurde heute in der Klasse gemacht? Thema, Beobachtungen…'
                : `Wie ist es heute für ${student.full_name || student.username} gelaufen?`
            }
            className="w-full min-h-[90px] p-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] placeholder:text-[var(--ink-4)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition resize-y"
            maxLength={4000}
            disabled={saving}
          />
          <div className="mt-2 flex items-center justify-between gap-2">
            <span className="text-[11px] text-[var(--ink-3)] font-mono">
              {draftNote.length} / 4000
            </span>
            <Btn
              variant="primary"
              size="sm"
              onClick={handleCreate}
              disabled={saving || !draftNote.trim()}
            >
              <MdAdd size={16} />{' '}
              {saving ? 'Speichern…' : 'Eintrag speichern'}
            </Btn>
          </div>
        </div>

        {/* Timeline */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-3">
          {loading ? (
            <div className="text-sm text-[var(--ink-3)]">Laden…</div>
          ) : entries.length === 0 ? (
            <div className="text-sm text-[var(--ink-3)] leading-relaxed">
              Noch keine Einträge. Nutze den Editor oben, um den ersten
              Fortschritt festzuhalten.
            </div>
          ) : (
            entries.map((entry) => {
              const isEditing = editingId === entry.id;
              return (
                <div
                  key={entry.id}
                  className="bg-white border border-[var(--line)] rounded-[var(--radius)] p-4 shadow-soft"
                >
                  <div className="flex items-start justify-between gap-3 mb-2">
                    <div className="flex items-center gap-2">
                      <Pill tone="accent">
                        {formatDateLong(entry.entry_date)}
                      </Pill>
                      {isClassScope && entry.student_id && (
                        <Pill tone="neutral">Schüler-Eintrag</Pill>
                      )}
                      {!isClassScope && !entry.student_id && (
                        <Pill tone="amber">Klassen-Eintrag</Pill>
                      )}
                    </div>
                    {!isEditing && (
                      <div className="flex items-center gap-0.5">
                        <Btn
                          variant="ghost"
                          size="sm"
                          onClick={() => handleStartEdit(entry)}
                          title="Bearbeiten"
                        >
                          <MdEdit size={16} />
                        </Btn>
                        <Btn
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDelete(entry)}
                          title="Löschen"
                        >
                          <MdDelete size={16} />
                        </Btn>
                      </div>
                    )}
                  </div>
                  {isEditing ? (
                    <>
                      <textarea
                        value={editDraft}
                        onChange={(e) => setEditDraft(e.target.value)}
                        className="w-full min-h-[90px] p-3 bg-white border border-[var(--line)] rounded-[var(--radius-sm)] text-sm text-[var(--ink)] focus:outline-none focus:border-[var(--accent)] focus:ring-2 focus:ring-[color:var(--accent-wash)] transition resize-y"
                        maxLength={4000}
                        disabled={editBusy}
                      />
                      <div className="mt-2 flex items-center justify-end gap-2">
                        <Btn
                          variant="ghost"
                          size="sm"
                          onClick={() => setEditingId(null)}
                          disabled={editBusy}
                        >
                          Abbrechen
                        </Btn>
                        <Btn
                          variant="primary"
                          size="sm"
                          onClick={handleSaveEdit}
                          disabled={
                            editBusy || !editDraft.trim() || editDraft === entry.note
                          }
                        >
                          <MdSave size={16} />{' '}
                          {editBusy ? 'Speichern…' : 'Speichern'}
                        </Btn>
                      </div>
                    </>
                  ) : (
                    <div
                      className={clsx(
                        'text-sm text-[var(--ink)] whitespace-pre-wrap leading-snug'
                      )}
                    >
                      {entry.note}
                    </div>
                  )}
                  <div className="mt-2 text-[10px] font-mono text-[var(--ink-3)]">
                    zuletzt {new Date(entry.updated_at).toLocaleString('de-DE')}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </aside>
    </div>
  );
}
