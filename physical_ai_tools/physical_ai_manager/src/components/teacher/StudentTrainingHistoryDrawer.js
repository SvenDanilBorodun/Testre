import React, { useEffect, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdClose } from 'react-icons/md';
import { useSelector } from 'react-redux';
import { listStudentTrainings } from '../../services/teacherApi';
import { Avatar, Pill } from '../EbUI';

const STATUS_TONE = {
  queued: 'neutral',
  running: 'accent',
  succeeded: 'success',
  failed: 'danger',
  canceled: 'amber',
};

const STATUS_LABELS = {
  queued: 'In Warteschlange',
  running: 'Läuft',
  succeeded: 'Erfolgreich',
  failed: 'Fehlgeschlagen',
  canceled: 'Abgebrochen',
};

function formatDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('de-DE');
  } catch {
    return iso;
  }
}

export default function StudentTrainingHistoryDrawer({ student, onClose }) {
  const token = useSelector((s) => s.auth.session?.access_token);
  const [trainings, setTrainings] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token || !student) return;
    setLoading(true);
    listStudentTrainings(token, student.id)
      .then((t) => setTrainings(t))
      .catch((err) => toast.error(err.message || 'Fehler beim Laden'))
      .finally(() => setLoading(false));
  }, [token, student]);

  if (!student) return null;

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end"
      onClick={onClose}
    >
      <div className="absolute inset-0 bg-black/30 backdrop-blur-sm" />
      <aside
        className="relative w-[460px] max-w-full h-full bg-white border-l border-[var(--line)] shadow-pop flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-5 border-b border-[var(--line)] flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Avatar name={student.full_name || student.username} />
            <div className="min-w-0">
              <h3 className="font-semibold text-[var(--ink)] truncate">
                {student.full_name || student.username}
              </h3>
              <div className="font-mono text-[11px] text-[var(--ink-3)] truncate">
                Trainings-Historie · {student.username}
              </div>
            </div>
          </div>
          <button
            className="w-9 h-9 rounded-[var(--radius-sm)] text-[var(--ink-3)] hover:bg-[var(--bg-sunk)] hover:text-[var(--ink)] flex items-center justify-center transition shrink-0"
            onClick={onClose}
            aria-label="Schließen"
          >
            <MdClose size={20} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-3">
          {loading ? (
            <div className="text-[var(--ink-3)] text-sm">Laden…</div>
          ) : trainings.length === 0 ? (
            <div className="text-[var(--ink-3)] text-sm">
              Dieser Schüler hat noch kein Training gestartet.
            </div>
          ) : (
            trainings.map((t) => (
              <div
                key={t.id}
                className="p-4 border border-[var(--line)] rounded-[var(--radius)] bg-white"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="font-mono text-sm font-semibold text-[var(--ink)] truncate">
                      {t.model_name}
                    </div>
                    <div className="text-[11px] text-[var(--ink-3)] mt-0.5 font-mono truncate">
                      {t.model_type} · {t.dataset_name}
                    </div>
                  </div>
                  <Pill tone={STATUS_TONE[t.status] || 'neutral'}>
                    {STATUS_LABELS[t.status] || t.status}
                  </Pill>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-[var(--ink-2)] font-mono">
                  <div>
                    <span className="text-[var(--ink-4)]">Start:</span>{' '}
                    {formatDate(t.requested_at)}
                  </div>
                  <div>
                    <span className="text-[var(--ink-4)]">Ende:</span>{' '}
                    {formatDate(t.terminated_at)}
                  </div>
                  <div>
                    <span className="text-[var(--ink-4)]">Schritt:</span>{' '}
                    {t.current_step ?? 0} / {t.total_steps ?? 0}
                  </div>
                  <div>
                    <span className="text-[var(--ink-4)]">Loss:</span>{' '}
                    {t.current_loss != null ? t.current_loss.toFixed(4) : '—'}
                  </div>
                </div>
                {t.error_message && (
                  <div
                    className={clsx(
                      'mt-2 text-[11px] rounded-[var(--radius-sm)] px-2 py-1',
                      'text-[color:var(--danger)] bg-[var(--danger-wash)] border border-[var(--line)]'
                    )}
                  >
                    {t.error_message}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </aside>
    </div>
  );
}
