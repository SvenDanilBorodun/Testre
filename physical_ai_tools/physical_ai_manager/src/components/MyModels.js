import React, { useState, useEffect, useCallback } from 'react';
import { useSelector } from 'react-redux';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import {
  MdRefresh,
  MdCheckCircle,
  MdError,
  MdHourglassEmpty,
  MdCancel,
  MdOutlineFileDownload,
  MdContentCopy,
  MdOpenInNew,
} from 'react-icons/md';
import { getTrainingJobs, cancelCloudTraining } from '../services/cloudTrainingApi';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';

const STATUS_CONFIG = {
  queued: { icon: MdHourglassEmpty, color: 'text-gray-500', bg: 'bg-gray-100', label: 'In Warteschlange' },
  running: { icon: MdRefresh, color: 'text-teal-600', bg: 'bg-teal-50', label: 'Training läuft', spin: true },
  succeeded: { icon: MdCheckCircle, color: 'text-green-600', bg: 'bg-green-50', label: 'Erfolgreich' },
  failed: { icon: MdError, color: 'text-red-500', bg: 'bg-red-50', label: 'Fehlgeschlagen' },
  canceled: { icon: MdCancel, color: 'text-gray-400', bg: 'bg-gray-50', label: 'Abgebrochen' },
};

function ModelCard({ job, rosConnected, onDownload, downloadingModel, onCancel }) {
  const config = STATUS_CONFIG[job.status] || STATUS_CONFIG.queued;
  const Icon = config.icon;
  const isActive = job.status === 'queued' || job.status === 'running';
  const isSucceeded = job.status === 'succeeded';

  const formatDate = (dateStr) => {
    if (!dateStr) return '-';
    const d = new Date(dateStr);
    return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' })
      + ', ' + d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  };

  return (
    <div className={clsx(
      'border rounded-xl p-4 transition-all',
      isActive ? 'border-teal-300 bg-teal-50/30 shadow-md' : 'border-gray-200 bg-white shadow-sm hover:shadow-md'
    )}>
      {/* Header: Status + Model Type */}
      <div className="flex items-center justify-between mb-3">
        <span className={clsx('inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold', config.bg, config.color)}>
          <Icon size={14} className={config.spin ? 'animate-spin' : ''} />
          {config.label}
        </span>
        <span className="text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full font-mono">
          {job.model_type}
        </span>
      </div>

      {/* Model Name */}
      <div className="mb-2">
        {isSucceeded ? (
          <a
            href={`https://huggingface.co/${job.model_name}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm font-semibold text-teal-700 hover:text-teal-900 hover:underline flex items-center gap-1"
          >
            {job.model_name}
            <MdOpenInNew size={14} className="flex-shrink-0" />
          </a>
        ) : (
          <span className="text-sm font-semibold text-gray-700">{job.model_name}</span>
        )}
      </div>

      {/* Dataset */}
      <div className="text-xs text-gray-500 mb-3">
        <span className="text-gray-400">Datensatz: </span>
        <span className="font-medium text-gray-600">{job.dataset_name}</span>
      </div>

      {/* Training Progress (for active jobs) */}
      {isActive && job.total_steps > 0 && (
        <div className="mb-3">
          <div className="flex justify-between text-xs text-gray-500 mb-1">
            <span>Schritt {job.current_step?.toLocaleString('de-DE') || 0} / {job.total_steps?.toLocaleString('de-DE')}</span>
            {job.current_loss != null && (
              <span>Loss: {job.current_loss.toFixed(4)}</span>
            )}
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2">
            <div
              className="bg-teal-500 h-2 rounded-full transition-all duration-500"
              style={{ width: `${Math.min(100, ((job.current_step || 0) / job.total_steps) * 100)}%` }}
            />
          </div>
          <div className="text-xs text-gray-400 mt-1 text-right">
            {Math.round(((job.current_step || 0) / job.total_steps) * 100)}%
          </div>
        </div>
      )}

      {/* Waiting indicator (queued, no progress yet) */}
      {isActive && (!job.total_steps || job.total_steps === 0) && (
        <div className="text-xs text-gray-400 mb-3 italic">
          Warte auf GPU-Worker...
        </div>
      )}

      {/* Date */}
      <div className="text-xs text-gray-400 mb-3">
        {formatDate(job.requested_at)}
        {job.terminated_at && job.status !== 'queued' && job.status !== 'running' && (
          <span> — {formatDate(job.terminated_at)}</span>
        )}
      </div>

      {/* Error message */}
      {job.status === 'failed' && job.error_message && (
        <div className="text-xs text-red-500 bg-red-50 border border-red-200 rounded-lg p-2 mb-3">
          <span className="font-medium">Fehler: </span>
          <span className="break-all">{job.error_message.length > 150 ? job.error_message.substring(0, 150) + '...' : job.error_message}</span>
          <div className="mt-1 text-red-400 italic">Trainingsguthaben wurde erstattet.</div>
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-2 pt-2 border-t border-gray-100">
        {isSucceeded && (
          <>
            <button
              onClick={() => onDownload(job.model_name)}
              disabled={downloadingModel === job.model_name || !rosConnected}
              className={clsx(
                'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
                rosConnected
                  ? 'bg-teal-600 text-white hover:bg-teal-700 disabled:bg-teal-300'
                  : 'bg-gray-200 text-gray-500 cursor-not-allowed'
              )}
              title={rosConnected ? 'Modell auf Roboter herunterladen' : 'Roboter-Umgebung muss gestartet sein'}
            >
              <MdOutlineFileDownload size={16} className={downloadingModel === job.model_name ? 'animate-pulse' : ''} />
              {downloadingModel === job.model_name ? 'Lädt...' : 'Herunterladen'}
            </button>
            <a
              href={`https://huggingface.co/${job.model_name}`}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium bg-gray-100 text-gray-700 hover:bg-gray-200 transition-colors"
            >
              <MdOpenInNew size={14} />
              HuggingFace
            </a>
            <button
              onClick={() => {
                navigator.clipboard.writeText(job.model_name);
                toast.success('Modell-ID kopiert');
              }}
              className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
              title="Modell-ID kopieren"
            >
              <MdContentCopy size={14} />
            </button>
          </>
        )}
        {isActive && (
          <button
            onClick={() => onCancel(job.id)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-red-100 text-red-600 hover:bg-red-200 transition-colors"
          >
            <MdCancel size={14} />
            Abbrechen
          </button>
        )}
      </div>
    </div>
  );
}

export default function MyModels() {
  const session = useSelector((state) => state.auth.session);
  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const rosConnected = heartbeatStatus === 'connected';
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [downloadingModel, setDownloadingModel] = useState(null);
  const [filter, setFilter] = useState('all'); // all, succeeded, active, failed
  const { controlHfServer } = useRosServiceCaller();

  const fetchJobs = useCallback(async () => {
    if (!session?.access_token) return;
    setLoading(true);
    try {
      const data = await getTrainingJobs(session.access_token);
      setJobs(data);
    } catch (error) {
      console.error('Failed to fetch training jobs:', error);
    } finally {
      setLoading(false);
    }
  }, [session]);

  // Initial fetch
  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  // Auto-refresh while active jobs exist
  const hasActiveJobs = jobs.some((j) => j.status === 'queued' || j.status === 'running');
  useEffect(() => {
    if (!hasActiveJobs) return;
    const interval = setInterval(fetchJobs, 5000);
    return () => clearInterval(interval);
  }, [hasActiveJobs, fetchJobs]);

  const handleCancel = async (trainingId) => {
    try {
      await cancelCloudTraining(session.access_token, trainingId);
      toast.success('Training abgebrochen');
      fetchJobs();
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

  // Filter jobs
  const filteredJobs = jobs.filter((job) => {
    if (filter === 'all') return true;
    if (filter === 'succeeded') return job.status === 'succeeded';
    if (filter === 'active') return job.status === 'queued' || job.status === 'running';
    if (filter === 'failed') return job.status === 'failed' || job.status === 'canceled';
    return true;
  });

  // Stats
  const succeededCount = jobs.filter((j) => j.status === 'succeeded').length;
  const activeCount = jobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
  const failedCount = jobs.filter((j) => j.status === 'failed' || j.status === 'canceled').length;

  const classCard = clsx(
    'bg-white', 'border', 'border-gray-200', 'rounded-2xl', 'shadow-lg', 'p-6', 'w-full'
  );

  const filterButton = (key, label, count, activeColor) =>
    clsx(
      'px-3 py-1 rounded-full text-xs font-medium transition-colors',
      filter === key
        ? `${activeColor} shadow-sm`
        : 'text-gray-500 hover:bg-gray-100'
    );

  return (
    <div className={classCard}>
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-bold text-gray-800">Meine Modelle</h2>
        <button
          onClick={fetchJobs}
          className="text-gray-500 hover:text-gray-700 p-1 rounded-lg hover:bg-gray-100 transition-colors"
          disabled={loading}
        >
          <MdRefresh size={20} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* Stats bar */}
      <div className="flex items-center gap-3 mb-4 text-xs">
        <span className="text-gray-400">{jobs.length} gesamt</span>
        {succeededCount > 0 && (
          <span className="text-green-600 font-medium">{succeededCount} erfolgreich</span>
        )}
        {activeCount > 0 && (
          <span className="text-teal-600 font-medium">{activeCount} aktiv</span>
        )}
        {failedCount > 0 && (
          <span className="text-red-400">{failedCount} fehlgeschlagen</span>
        )}
      </div>

      {/* Filter tabs */}
      {jobs.length > 0 && (
        <div className="flex items-center gap-2 mb-4 pb-3 border-b border-gray-100">
          <button className={filterButton('all', 'Alle', jobs.length, 'bg-gray-200 text-gray-800')} onClick={() => setFilter('all')}>
            Alle ({jobs.length})
          </button>
          <button className={filterButton('succeeded', 'Erfolgreich', succeededCount, 'bg-green-100 text-green-700')} onClick={() => setFilter('succeeded')}>
            Erfolgreich ({succeededCount})
          </button>
          {activeCount > 0 && (
            <button className={filterButton('active', 'Aktiv', activeCount, 'bg-teal-100 text-teal-700')} onClick={() => setFilter('active')}>
              Aktiv ({activeCount})
            </button>
          )}
          {failedCount > 0 && (
            <button className={filterButton('failed', 'Fehlgeschlagen', failedCount, 'bg-red-100 text-red-600')} onClick={() => setFilter('failed')}>
              Fehlgeschlagen ({failedCount})
            </button>
          )}
        </div>
      )}

      {/* Info banner */}
      {!rosConnected && succeededCount > 0 && (
        <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4">
          Starte die Roboter-Umgebung, um Modelle herunterzuladen und auf der Inferenz-Seite zu verwenden.
        </div>
      )}

      {/* Model cards grid */}
      {filteredJobs.length === 0 ? (
        <div className="text-center py-8 text-gray-400">
          {jobs.length === 0
            ? 'Noch keine Trainingsaufträge. Starte dein erstes Training oben!'
            : `Keine Modelle mit Filter "${filter === 'succeeded' ? 'Erfolgreich' : filter === 'active' ? 'Aktiv' : 'Fehlgeschlagen'}"`
          }
        </div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3 max-h-[500px] overflow-y-auto pr-1">
          {filteredJobs.map((job) => (
            <ModelCard
              key={job.id}
              job={job}
              rosConnected={rosConnected}
              onDownload={handleDownload}
              downloadingModel={downloadingModel}
              onCancel={handleCancel}
            />
          ))}
        </div>
      )}
    </div>
  );
}
