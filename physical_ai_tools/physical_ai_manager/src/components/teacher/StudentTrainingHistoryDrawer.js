import React, { useEffect, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdClose } from 'react-icons/md';
import { useSelector } from 'react-redux';
import { listStudentTrainings } from '../../services/teacherApi';

const STATUS_STYLES = {
  queued: 'bg-gray-100 text-gray-700',
  running: 'bg-blue-100 text-blue-700',
  succeeded: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  canceled: 'bg-yellow-100 text-yellow-800',
};

const STATUS_LABELS = {
  queued: 'In Warteschlange',
  running: 'Laeuft',
  succeeded: 'Erfolgreich',
  failed: 'Fehlgeschlagen',
  canceled: 'Abgebrochen',
};

function formatDate(iso) {
  if (!iso) return '-';
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
    <div className="fixed inset-0 z-40 flex" onClick={onClose}>
      <div className="flex-1 bg-black/40" />
      <aside
        className="w-full max-w-2xl bg-white shadow-2xl flex flex-col h-screen"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
          <div>
            <h3 className="text-lg font-semibold text-gray-800">
              Trainings-Historie
            </h3>
            <p className="text-sm text-gray-500">
              {student.full_name} (<span className="font-mono">{student.username}</span>)
            </p>
          </div>
          <button
            className="text-gray-500 hover:text-gray-800"
            onClick={onClose}
            aria-label="Schliessen"
          >
            <MdClose size={22} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {loading ? (
            <div className="text-gray-500">Laden...</div>
          ) : trainings.length === 0 ? (
            <div className="text-gray-500 text-sm">
              Dieser Schueler hat noch kein Training gestartet.
            </div>
          ) : (
            <ul className="flex flex-col gap-3">
              {trainings.map((t) => (
                <li
                  key={t.id}
                  className="border border-gray-200 rounded-xl p-4 bg-gray-50"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-gray-800 truncate">
                        {t.model_name}
                      </div>
                      <div className="text-xs text-gray-500 mt-0.5">
                        {t.model_type} · {t.dataset_name}
                      </div>
                    </div>
                    <span
                      className={clsx(
                        'text-xs font-semibold px-2 py-1 rounded-full whitespace-nowrap',
                        STATUS_STYLES[t.status] || 'bg-gray-100 text-gray-700'
                      )}
                    >
                      {STATUS_LABELS[t.status] || t.status}
                    </span>
                  </div>
                  <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-600">
                    <div>
                      <span className="text-gray-400">Gestartet:</span>{' '}
                      {formatDate(t.requested_at)}
                    </div>
                    <div>
                      <span className="text-gray-400">Beendet:</span>{' '}
                      {formatDate(t.terminated_at)}
                    </div>
                    <div>
                      <span className="text-gray-400">Schritt:</span>{' '}
                      {t.current_step ?? 0} / {t.total_steps ?? 0}
                    </div>
                    <div>
                      <span className="text-gray-400">Loss:</span>{' '}
                      {t.current_loss != null ? t.current_loss.toFixed(4) : '-'}
                    </div>
                  </div>
                  {t.error_message && (
                    <div className="mt-2 text-xs text-red-700 bg-red-50 border border-red-100 rounded px-2 py-1">
                      {t.error_message}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
    </div>
  );
}
