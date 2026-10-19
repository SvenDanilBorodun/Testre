import React, { useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { cancelCloudTraining } from '../services/cloudTrainingApi';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import { setSelectedTrainingId } from '../features/training/trainingSlice';
import Icon from './icons/Icon';
import { Led } from './sucher';
import { releasePointerFocus } from './sucher/pointerFocus';

// The status light of a model card (Sucher: a light + a caption).
const STATUS_LED = { queued: 'warn', running: 'sig', succeeded: 'ok', failed: 'rec', canceled: 'off' };

// A failed row's error_message is cut on the card only when it is longer than
// this: a legacy worker writes up to 2 KB of LeRobot output there, while every
// German sentence of the managed training's S-table (training_texts_de.py, the
// longest S4 at 178 characters) is shown whole. Fenced by
// robotis_ai_setup/tests/test_managed_training_lockstep.py (every S sentence fits).
export const ERROR_MESSAGE_MAX_CHARS = 300;

export function cardErrorText(message) {
  if (typeof message !== 'string') return '';
  return message.length > ERROR_MESSAGE_MAX_CHARS
    ? message.substring(0, ERROR_MESSAGE_MAX_CHARS) + '...'
    : message;
}

const STATUS_CONFIG = {
  queued: { icon: 'hourglass', color: 'text-gray-500', bg: 'bg-gray-100', label: 'In Warteschlange' },
  running: { icon: 'loading', color: 'text-teal-600', bg: 'bg-teal-50', label: 'Training läuft', spin: true },
  succeeded: { icon: 'checkCircle', color: 'text-green-600', bg: 'bg-green-50', label: 'Erfolgreich' },
  failed: { icon: 'failed', color: 'text-red-500', bg: 'bg-red-50', label: 'Fehlgeschlagen' },
  canceled: { icon: 'cancel', color: 'text-gray-400', bg: 'bg-gray-50', label: 'Abgebrochen' },
};

function ModelCard({ job, rosConnected, onDownload, downloadingModel, onCancel, selected, onSelect }) {
  const config = STATUS_CONFIG[job.status] || STATUS_CONFIG.queued;
  const isActive = job.status === 'queued' || job.status === 'running';
  const isSucceeded = job.status === 'succeeded';

  const formatDate = (dateStr) => {
    if (!dateStr) return '-';
    const d = new Date(dateStr);
    return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' })
      + ', ' + d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  };

  const pct = isActive && job.total_steps > 0
    ? Math.min(100, ((job.current_step || 0) / job.total_steps) * 100)
    : 0;

  return (
    <div
      className={clsx('su-model', selected && 'su-model--selected', isActive && 'su-model--active')}
      onClick={() => onSelect(job.id)}
      title="Als Live-Ansicht oben auswählen"
    >
      {/* Header: Status + Live view */}
      <div className="su-model__head">
        <span className="su-model__status">
          <Led tone={STATUS_LED[job.status] || 'off'} pulse={job.status === 'running' ? 'breathe' : 'none'} />
          <span className="su-lab">{config.label}</span>
        </span>
        {selected && (
          <span className="su-model__live">
            <Icon name="zap" size={11} /> Live-Ansicht
          </span>
        )}
      </div>

      {/* Model name in a thin-bezel screen strip, the policy under it */}
      <div className="su-model__name su-screen su-screen--thin">
        {isSucceeded ? (
          <a
            href={`https://huggingface.co/${job.model_name}`}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="su-model__title su-model__title--link"
            title={job.model_name}
          >
            <span className="truncate">{job.model_name}</span>
            <Icon name="externalLink" size={13} className="flex-shrink-0" />
          </a>
        ) : (
          <span className="su-model__title" title={job.model_name}>
            <span className="truncate">{job.model_name}</span>
          </span>
        )}
        <span className="su-lab">{job.model_type}</span>
      </div>

      {/* Dataset */}
      <div className="su-model__meta">
        Datensatz: <span className="text-[var(--ink)]">{job.dataset_name}</span>
      </div>

      {/* Training Progress (for active jobs) */}
      {isActive && job.total_steps > 0 && (
        <div className="su-model__progress">
          <div className="flex justify-between gap-2 su-model__meta">
            <span>Schritt <span className="su-dot">{job.current_step?.toLocaleString('de-DE') || 0} / {job.total_steps?.toLocaleString('de-DE')}</span></span>
            {job.current_loss != null && (
              <span>Loss: <span className="su-dot">{job.current_loss.toFixed(4)}</span></span>
            )}
          </div>
          <div className="su-model__track">
            <div className="su-model__fill" style={{ width: `${pct}%` }} />
          </div>
          <div className="su-model__meta text-right">
            {Math.round(((job.current_step || 0) / job.total_steps) * 100)}%
          </div>
        </div>
      )}

      {/* Waiting indicator (queued, no progress yet) */}
      {isActive && (!job.total_steps || job.total_steps === 0) && (
        <div className="su-model__meta italic">
          Warte auf GPU-Worker...
        </div>
      )}

      {/* Date */}
      <div className="su-model__meta">
        {formatDate(job.requested_at)}
        {job.terminated_at && job.status !== 'queued' && job.status !== 'running' && (
          <span> — {formatDate(job.terminated_at)}</span>
        )}
      </div>

      {/* Error message */}
      {job.status === 'failed' && job.error_message && (
        <div className="su-model__error">
          <span className="font-medium">Fehler: </span>
          <span className="break-all">{cardErrorText(job.error_message)}</span>
          <div className="mt-1 italic">Trainingsguthaben wurde erstattet.</div>
        </div>
      )}

      {/* Actions */}
      <div className="su-model__actions" onClick={(e) => e.stopPropagation()}>
        {isSucceeded && (
          <>
            <button
              type="button"
              onClick={(e) => { releasePointerFocus(e); onDownload(job.model_name); }}
              disabled={downloadingModel === job.model_name || !rosConnected}
              className="su-key su-key--signal su-key--sm"
              title={rosConnected ? 'Modell auf Roboter herunterladen' : 'Roboter-Umgebung muss gestartet sein'}
            >
              <Icon name="download" size={16} className={downloadingModel === job.model_name ? 'animate-pulse' : ''} />
              {downloadingModel === job.model_name ? 'Lädt...' : 'Herunterladen'}
            </button>
            <a
              href={`https://huggingface.co/${job.model_name}`}
              target="_blank"
              rel="noopener noreferrer"
              className="su-key su-key--sm"
            >
              <Icon name="externalLink" size={14} />
              HuggingFace
            </a>
            <button
              type="button"
              onClick={(e) => {
                releasePointerFocus(e);
                navigator.clipboard.writeText(job.model_name);
                toast.success('Modell-ID kopiert');
              }}
              className="su-rkey"
              title="Modell-ID kopieren"
              aria-label="Modell-ID kopieren"
            >
              <span className="su-rkey__disc" style={{ width: 36, height: 36 }}>
                <Icon name="copy" size={14} />
              </span>
            </button>
          </>
        )}
        {isActive && (
          <button
            type="button"
            onClick={(e) => { releasePointerFocus(e); onCancel(job.id); }}
            className="su-key su-key--flat su-key--sm su-model__cancel"
          >
            <Icon name="cancel" size={14} />
            Abbrechen
          </button>
        )}
      </div>
    </div>
  );
}

export default function MyModels({ jobs = [], loading = false, refetch, isRealtime = false }) {
  const dispatch = useDispatch();
  const session = useSelector((state) => state.auth.session);
  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const rosConnected = heartbeatStatus === 'connected';
  const selectedTrainingId = useSelector((state) => state.training.selectedTrainingId);
  const [downloadingModel, setDownloadingModel] = useState(null);
  const [filter, setFilter] = useState('all');
  const { controlHfServer } = useRosServiceCaller();

  const handleCancel = async (trainingId) => {
    try {
      await cancelCloudTraining(session.access_token, trainingId);
      toast.success('Training abgebrochen');
      refetch?.();
    } catch (error) {
      toast.error(`Abbrechen fehlgeschlagen: ${error.message}`);
    }
  };

  const handleDownload = async (modelName) => {
    if (!rosConnected) {
      toast.error('Roboter-Umgebung muss gestartet sein, um Modelle herunterzuladen');
      return;
    }
    setDownloadingModel(modelName);
    try {
      await controlHfServer('download', modelName, 'model');
      toast.success(`Download gestartet: ${modelName}\nNach dem Download kann das Modell auf der Inferenz-Seite verwendet werden.`, { duration: 6000 });
    } catch (error) {
      toast.error(`Download fehlgeschlagen: ${error.message}`);
    } finally {
      setDownloadingModel(null);
    }
  };

  const handleSelect = (trainingId) => {
    dispatch(setSelectedTrainingId(trainingId));
  };

  const filteredJobs = jobs.filter((job) => {
    if (filter === 'all') return true;
    if (filter === 'succeeded') return job.status === 'succeeded';
    if (filter === 'active') return job.status === 'queued' || job.status === 'running';
    if (filter === 'failed') return job.status === 'failed' || job.status === 'canceled';
    return true;
  });

  const succeededCount = jobs.filter((j) => j.status === 'succeeded').length;
  const activeCount = jobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
  const failedCount = jobs.filter((j) => j.status === 'failed' || j.status === 'canceled').length;

  // The filter is a light segmented control (Sucher); same buttons, same
  // handlers. A mouse click gives the focus back (D11).
  const filterProps = (key) => ({
    type: 'button',
    'aria-pressed': filter === key,
    onClick: (e) => {
      releasePointerFocus(e);
      setFilter(key);
    },
  });

  return (
    <div className="su-models">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 mb-4">
        <div className="flex items-baseline gap-4 flex-wrap min-w-0">
          <h2 className="su-serif su-models__title">Meine <em>Modelle</em></h2>
          <span className="su-lab">Lokal + in der Cloud</span>
        </div>
        <div className="flex items-center gap-4 shrink-0">
          {isRealtime && (
            <span className="inline-flex items-center gap-2">
              <Led tone="ok" pulse="breathe" />
              <span className="su-lab">Live</span>
            </span>
          )}
          <button
            type="button"
            onClick={(e) => { releasePointerFocus(e); refetch?.(); }}
            className="su-rkey"
            disabled={loading}
            title="Aktualisieren"
            aria-label="Aktualisieren"
          >
            <span className="su-rkey__disc" style={{ width: 40, height: 40 }}>
              <Icon name="refresh" size={16} className={loading ? 'animate-spin' : ''} />
            </span>
          </button>
        </div>
      </div>

      {/* Stats bar + filter */}
      <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
        <div className="flex items-center gap-1.5 text-[13.5px] text-[var(--ink-2)] flex-wrap">
          <span>{jobs.length} gesamt</span>
          {succeededCount > 0 && <span>· {succeededCount} erfolgreich</span>}
          {activeCount > 0 && <span>· {activeCount} aktiv</span>}
          {failedCount > 0 && <span>· {failedCount} fehlgeschlagen</span>}
        </div>

        {/* Filter tabs */}
        {jobs.length > 0 && (
          <div className="su-seg su-seg--light su-seg--sm" role="group" aria-label="Modelle filtern">
            <button {...filterProps('all')}>
              Alle ({jobs.length})
            </button>
            <button {...filterProps('succeeded')}>
              Erfolgreich ({succeededCount})
            </button>
            {activeCount > 0 && (
              <button {...filterProps('active')}>
                Aktiv ({activeCount})
              </button>
            )}
            {failedCount > 0 && (
              <button {...filterProps('failed')}>
                Fehlgeschlagen ({failedCount})
              </button>
            )}
          </div>
        )}
      </div>

      {/* Info banner */}
      {!rosConnected && succeededCount > 0 && (
        <div className="text-[13px] text-[color:var(--amber)] bg-[var(--amber-wash)] rounded-[14px] p-3 mb-4">
          Starte die Roboter-Umgebung, um Modelle herunterzuladen und auf der Inferenz-Seite zu verwenden.
        </div>
      )}

      {/* Model cards grid */}
      {filteredJobs.length === 0 ? (
        <div className="text-center py-8 text-[var(--ink-2)]">
          {jobs.length === 0
            ? 'Noch keine Trainingsaufträge. Starte dein erstes Training oben!'
            : `Keine Modelle mit Filter "${filter === 'succeeded' ? 'Erfolgreich' : filter === 'active' ? 'Aktiv' : 'Fehlgeschlagen'}"`
          }
        </div>
      ) : (
        <div className="su-models__grid">
          {filteredJobs.map((job) => (
            <ModelCard
              key={job.id}
              job={job}
              rosConnected={rosConnected}
              onDownload={handleDownload}
              downloadingModel={downloadingModel}
              onCancel={handleCancel}
              selected={selectedTrainingId === job.id}
              onSelect={handleSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}
