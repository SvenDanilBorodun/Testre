import React, { useCallback, useEffect, useState } from 'react';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { useSelector } from 'react-redux';
import {
  getStudentStopPreview,
  listStudentTrainings,
  listStudentInferenceRuns,
  listTrainingCheckpoints,
  stopStudentTraining,
} from '../../services/teacherApi';
import { Avatar, Btn, Pill } from '../EbUI';
import Icon from '../icons/Icon';
import Modal from './Modal';
import { hourLines, hoursDe, lostTimeLine, numberDe } from '../../utils/hoursDe';

// „Training stoppen" (decision 13, spec §10.4): the teacher's stop of a running
// managed training, confirmed against the same stop preview the student sees.
export const STOP_DE = {
  button: 'Training stoppen',
  title: 'Training stoppen?',
  publish: (step, c, r) =>
    `Der GPU-Server wird gestoppt und der letzte Zwischenstand (Schritt ${numberDe(step)}) als Modell veröffentlicht. Berechnet: ${hoursDe(c)} von ${hoursDe(r)}.`,
  noCheckpoint: (c, r) =>
    `Der GPU-Server wird gestoppt. Es gibt noch keinen Zwischenstand, also wird kein Modell veröffentlicht. Berechnet: ${hoursDe(c)} von ${hoursDe(r)}.`,
  notTrained: 'Das Training hat noch nicht begonnen. Es wird nichts berechnet.',
  previewFailed:
    'Der Stand konnte nicht abgefragt werden. Wenn das Training schon auf einer GPU läuft, wird die angefangene Trainingszeit berechnet.',
  loading: 'Stand wird abgefragt …',
  confirm: 'Ja, stoppen',
  back: 'Zurück',
  done: 'Training gestoppt.',
};

/** The teacher's preview sentence (spec §10.4); null preview = the request failed. */
export function stopPreviewSentence(p) {
  if (!p || typeof p !== 'object' || typeof p.trained !== 'boolean') return STOP_DE.previewFailed;
  if (!p.trained) return STOP_DE.notTrained;
  if (p.outcome === 'publish') return STOP_DE.publish(p.checkpoint_step, p.charged_hours_now, p.reserved_hours);
  return STOP_DE.noCheckpoint(p.charged_hours_now, p.reserved_hours);
}

/** Only a managed training that is still waiting or running can be stopped. */
export const isStoppable = (t) => t?.managed === true && (t.status === 'queued' || t.status === 'running');

function StopTrainingDialog({ token, studentId, training, onClose, onStopped }) {
  const [preview, setPreview] = useState('loading');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    getStudentStopPreview(token, studentId, training.id)
      .then((p) => { if (alive) setPreview(p && typeof p === 'object' ? p : null); })
      .catch(() => { if (alive) setPreview(null); });
    return () => { alive = false; };
  }, [token, studentId, training.id]);

  const confirm = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await stopStudentTraining(token, studentId, training.id);
      toast.success(STOP_DE.done);
      onStopped?.();
      onClose();
    } catch (err) {
      toast.error(err.message || 'Fehler');
      setBusy(false);
    }
  };

  const loading = preview === 'loading';
  return (
    <Modal
      title={STOP_DE.title}
      onClose={() => { if (!busy) onClose(); }}
      footer={(
        <>
          <Btn variant="ghost" onClick={onClose} disabled={busy}>{STOP_DE.back}</Btn>
          <Btn variant="danger" onClick={confirm} disabled={busy || loading}>{STOP_DE.confirm}</Btn>
        </>
      )}
    >
      <p role="status" className="text-sm text-[var(--ink-2)] leading-relaxed">
        {loading ? STOP_DE.loading : stopPreviewSentence(preview)}
      </p>
    </Modal>
  );
}

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
  // training id -> sorted checkpoint steps (or 'loading')
  const [checkpoints, setCheckpoints] = useState({});

  const loadCheckpoints = (trainingId) => {
    setCheckpoints((prev) => ({ ...prev, [trainingId]: 'loading' }));
    listTrainingCheckpoints(token, student.id, trainingId)
      .then((res) =>
        setCheckpoints((prev) => ({ ...prev, [trainingId]: res.steps || [] }))
      )
      .catch((err) => {
        setCheckpoints((prev) => ({ ...prev, [trainingId]: [] }));
        toast.error(err.message || 'Checkpoints konnten nicht geladen werden');
      });
  };

  const [inferenceRuns, setInferenceRuns] = useState([]);
  const [stopTarget, setStopTarget] = useState(null);

  const loadTrainings = useCallback(() => {
    if (!token || !student) return;
    setLoading(true);
    listStudentTrainings(token, student.id)
      .then((t) => setTrainings(t))
      .catch((err) => toast.error(err.message || 'Fehler beim Laden'))
      .finally(() => setLoading(false));
  }, [token, student]);

  useEffect(() => {
    if (!token || !student) return;
    loadTrainings();
    // Inference run records (Cloud-Inferenz; older rows from the retired
    // classroom Jetson) — best-effort side load.
    listStudentInferenceRuns(token, student.id)
      .then((runs) => setInferenceRuns(runs))
      .catch(() => setInferenceRuns([]));
  }, [token, student, loadTrainings]);

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
            <Icon name="close" size={20} />
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
                {(hourLines(t, { withStopper: false }).length > 0 || lostTimeLine(t)) && (
                  <div className="mt-2 text-[11px] text-[var(--ink-2)] space-y-0.5" data-testid={`hours-${t.id}`}>
                    {hourLines(t, { withStopper: false }).map((line) => <div key={line}>{line}</div>)}
                    {lostTimeLine(t) && <div>{lostTimeLine(t)}</div>}
                  </div>
                )}
                {isStoppable(t) && (
                  <Btn
                    variant="secondary"
                    size="sm"
                    className="mt-2"
                    onClick={() => setStopTarget(t)}
                  >
                    <Icon name="stop" /> {STOP_DE.button}
                  </Btn>
                )}
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
                {t.log_url && (
                  <a
                    href={t.log_url}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-2 inline-flex items-center gap-1 text-[11px] font-medium text-[var(--accent)] hover:underline"
                    title="Vollständiges Trainingsprotokoll (training_log.txt im privaten HF-Repo)"
                  >
                    Vollständiges Protokoll öffnen
                    <Icon name="externalLink" />
                  </a>
                )}
                {(t.status === 'succeeded' || t.status === 'running') && (
                  <div className="mt-2 text-[11px] font-mono">
                    {checkpoints[t.id] === undefined ? (
                      <button
                        type="button"
                        onClick={() => loadCheckpoints(t.id)}
                        className="text-[var(--accent)] hover:underline"
                      >
                        Zwischenstände anzeigen
                      </button>
                    ) : checkpoints[t.id] === 'loading' ? (
                      <span className="text-[var(--ink-3)]">Lade Zwischenstände…</span>
                    ) : checkpoints[t.id].length === 0 ? (
                      <span className="text-[var(--ink-3)]">Keine Zwischenstände hochgeladen.</span>
                    ) : (
                      <span className="text-[var(--ink-2)]">
                        Zwischenstände (Schritt):{' '}
                        {checkpoints[t.id].map((s) => s.toLocaleString('de-DE')).join(', ')}
                      </span>
                    )}
                  </div>
                )}
              </div>
            ))
          )}
          {inferenceRuns.length > 0 && (
            <div className="pt-4 mt-2 border-t border-[var(--line)]">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)] mb-2">
                Inferenz-Läufe
              </h4>
              <div className="space-y-2">
                {inferenceRuns.map((run) => (
                  <div
                    key={run.id}
                    data-testid={`inference-run-${run.id}`}
                    className="p-3 border border-[var(--line)] rounded-[var(--radius)] bg-white text-[11px] font-mono"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-[var(--ink)]">{run.policy_repo}</span>
                      {/* Where the run ran: 'cloud' (Cloud-Inferenz, 048), else
                          an older run on the retired classroom Jetson (written
                          before migration 053; an API before 048 sends no
                          `backend` at all). */}
                      <Pill tone="neutral" className="shrink-0">
                        {run.backend === 'cloud' ? 'Cloud' : 'Klassen-Jetson'}
                      </Pill>
                      <Pill
                        tone={
                          run.exit_reason === 'error'
                            ? 'danger'
                            : run.exit_reason === 'finished'
                              ? 'success'
                              : 'neutral'
                        }
                      >
                        {run.exit_reason === 'error'
                          ? 'Fehler'
                          : run.exit_reason === 'finished'
                            ? 'Beendet'
                            : 'Gestoppt'}
                      </Pill>
                    </div>
                    <div className="mt-1 text-[var(--ink-3)]">
                      {formatDate(run.created_at)} ·{' '}
                      {run.duration_s != null ? `${Math.round(run.duration_s)} s` : '—'}
                    </div>
                    {run.error_message_de && (
                      <div className="mt-1 text-[color:var(--danger)]">
                        {run.error_message_de}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
        {/* Inside the aside, whose click handler keeps a click in the dialog
            (a portal, but a React child) from closing the drawer. */}
        {stopTarget && (
          <StopTrainingDialog
            token={token}
            studentId={student.id}
            training={stopTarget}
            onClose={() => setStopTarget(null)}
            onStopped={loadTrainings}
          />
        )}
      </aside>
    </div>
  );
}
