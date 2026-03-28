import React, { useState, useEffect, useCallback } from 'react';
import { useSelector } from 'react-redux';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { MdRefresh, MdCancel, MdCheckCircle, MdError, MdHourglassEmpty } from 'react-icons/md';
import { getTrainingJobs, cancelCloudTraining } from '../services/cloudTrainingApi';

const STATUS_CONFIG = {
  queued: { icon: MdHourglassEmpty, color: 'text-gray-500', bg: 'bg-gray-100', label: 'In Warteschlange' },
  running: { icon: MdRefresh, color: 'text-teal-500', bg: 'bg-teal-100', label: 'Läuft', spin: true },
  succeeded: { icon: MdCheckCircle, color: 'text-green-600', bg: 'bg-green-100', label: 'Erfolgreich' },
  failed: { icon: MdError, color: 'text-red-500', bg: 'bg-red-100', label: 'Fehlgeschlagen' },
  canceled: { icon: MdCancel, color: 'text-gray-400', bg: 'bg-gray-100', label: 'Abgebrochen' },
};

function StatusBadge({ status }) {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.queued;
  const Icon = config.icon;

  return (
    <span className={clsx('inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium', config.bg, config.color)}>
      <Icon size={14} className={config.spin ? 'animate-spin' : ''} />
      {config.label}
    </span>
  );
}

export default function CloudTrainingHistory() {
  const session = useSelector((state) => state.auth.session);
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);

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

  // Initial fetch + auto-refresh every 5 seconds if there are active jobs
  useEffect(() => {
    fetchJobs();

    const hasActiveJobs = jobs.some((j) => j.status === 'queued' || j.status === 'running');
    if (!hasActiveJobs) return;

    const interval = setInterval(fetchJobs, 5000);
    return () => clearInterval(interval);
  }, [fetchJobs, jobs.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleCancel = async (trainingId) => {
    try {
      await cancelCloudTraining(session.access_token, trainingId);
      toast.success('Training abgebrochen');
      fetchJobs();
    } catch (error) {
      toast.error(`Abbrechen fehlgeschlagen: ${error.message}`);
    }
  };

  const formatDate = (dateStr) => {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleString();
  };

  const classCard = clsx(
    'bg-white',
    'border',
    'border-gray-200',
    'rounded-2xl',
    'shadow-lg',
    'p-6',
    'w-full'
  );

  if (jobs.length === 0 && !loading) {
    return (
      <div className={classCard}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-bold text-gray-800">Cloud-Training Verlauf</h2>
          <button
            onClick={fetchJobs}
            className="text-gray-500 hover:text-gray-700"
            disabled={loading}
          >
            <MdRefresh size={20} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
        <p className="text-gray-400 text-center py-4">Noch keine Trainingsaufträge</p>
      </div>
    );
  }

  return (
    <div className={classCard}>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-bold text-gray-800">Cloud-Training Verlauf</h2>
        <button
          onClick={fetchJobs}
          className="text-gray-500 hover:text-gray-700"
          disabled={loading}
        >
          <MdRefresh size={20} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200">
              <th className="text-left py-2 px-2 font-medium text-gray-600">Status</th>
              <th className="text-left py-2 px-2 font-medium text-gray-600">Modell</th>
              <th className="text-left py-2 px-2 font-medium text-gray-600">Typ</th>
              <th className="text-left py-2 px-2 font-medium text-gray-600">Datensatz</th>
              <th className="text-left py-2 px-2 font-medium text-gray-600">Gestartet</th>
              <th className="text-left py-2 px-2 font-medium text-gray-600"></th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.id} className="border-b border-gray-100 hover:bg-gray-50">
                <td className="py-2 px-2">
                  <StatusBadge status={job.status} />
                </td>
                <td className="py-2 px-2">
                  {job.status === 'succeeded' ? (
                    <a
                      href={`https://huggingface.co/${job.model_name}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-teal-600 hover:underline font-medium"
                    >
                      {job.model_name}
                    </a>
                  ) : (
                    <span className="text-gray-700 font-medium">{job.model_name}</span>
                  )}
                </td>
                <td className="py-2 px-2 text-gray-600">{job.model_type}</td>
                <td className="py-2 px-2 text-gray-600">{job.dataset_name}</td>
                <td className="py-2 px-2 text-gray-500 text-xs">{formatDate(job.requested_at)}</td>
                <td className="py-2 px-2">
                  {(job.status === 'queued' || job.status === 'running') && (
                    <button
                      onClick={() => handleCancel(job.id)}
                      className="text-red-500 hover:text-red-700 text-xs font-medium"
                    >
                      Abbrechen
                    </button>
                  )}
                  {job.status === 'failed' && job.error_message && (
                    <span className="text-xs text-red-400" title={job.error_message}>
                      Error
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
