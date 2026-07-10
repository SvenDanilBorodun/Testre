// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Author: Kiwoong Park

import React, { useCallback, useState } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import { MdFolderOpen, MdDownload } from 'react-icons/md';
import FileBrowserModal from './FileBrowserModal';
import PolicyDownloadModal from './PolicyDownloadModal';
import TaskPhase from '../constants/taskPhases';
import { DEFAULT_PATHS, TARGET_FILES } from '../constants/paths';
import { setTaskInfo } from '../features/tasks/taskSlice';
import useSupabaseTrainings from '../hooks/useSupabaseTrainings';

const InferencePanel = () => {
  const dispatch = useDispatch();

  const info = useSelector((state) => state.tasks.taskInfo);
  const taskStatus = useSelector((state) => state.tasks.taskStatus);

  // Editable exactly when the node is idle. The former 1 s /task/status SILENCE
  // detector was only ever a PROXY for "no task running" — valid while
  // /task/status was silent when idle, but the server now emits a ~0.5 Hz idle
  // identity tick, so a silence detector would re-LOCK every field for ~1 s per
  // tick (focus/keystroke loss). The explicit phase/running signal the ticks
  // now carry is exact and stable (D8 companion #2).
  const isEditable = taskStatus.phase === TaskPhase.READY && !taskStatus.running;

  // File browser modal states
  const [showPolicyPathModal, setShowPolicyPathModal] = useState(false);

  // Policy download modal states
  const [showPolicyDownloadModal, setShowPolicyDownloadModal] = useState(false);
  // "Neuestes Modell" one-click default (leLab-comparison PR-3): the
  // student's most recent succeeded Modal training, already streamed by
  // the Supabase trainings hook the Training tab uses.
  const { jobs: trainingJobs } = useSupabaseTrainings();
  const latestSucceededModel = (trainingJobs || [])
    .filter((j) => j.status === 'succeeded' && j.model_name)
    .sort((a, b) => new Date(b.requested_at || 0) - new Date(a.requested_at || 0))[0]?.model_name;
  const [downloadPrefill, setDownloadPrefill] = useState('');

  const handleChange = useCallback(
    (field, value) => {
      if (!isEditable) return; // Block changes when not editable
      dispatch(setTaskInfo({ ...info, [field]: value }));
    },
    [isEditable, info, dispatch]
  );

  const handlePolicyPathSelect = useCallback(
    (item) => {
      if (!isEditable) return;
      handleChange('policyPath', item.full_path);
      setShowPolicyPathModal(false);
    },
    [isEditable, handleChange]
  );

  const handleDownloadPolicyComplete = useCallback(
    (repoId) => {
      // Update the policy path with the local cache path
      const localPath = `${DEFAULT_PATHS.POLICY_MODEL_PATH}${repoId}`;
      handleChange('policyPath', localPath);
    },
    [handleChange]
  );

  const classLabel = clsx('text-xs', 'text-gray-600', 'w-[120px]', 'flex-shrink-0', 'font-medium', 'leading-snug', 'break-words');

  const classInfoPanel = clsx(
    'bg-white',
    'border',
    'border-[var(--line)]',
    'rounded-[var(--radius-lg)]',
    'shadow-soft',
    'p-4',
    'w-full',
    'relative',
    'overflow-y-auto',
    'scrollbar-thin',
    'max-h-[calc(100vh-220px)]'
  );

  const classTaskInstructionTextarea = clsx(
    'text-sm',
    'resize-y',
    'min-h-10',
    'max-h-20',
    'w-full',
    'p-2',
    'border',
    'border-gray-300',
    'rounded-md',
    'focus:outline-none',
    'focus:ring-2',
    'focus:ring-teal-500',
    'focus:border-transparent',
    {
      'bg-gray-100 cursor-not-allowed': !isEditable,
      'bg-white': isEditable,
    }
  );

  const classPolicyPathTextarea = clsx(
    'text-sm',
    'resize-y',
    'min-h-16',
    'max-h-24',
    'w-full',
    'p-2',
    'border',
    'border-gray-300',
    'rounded-md',
    'focus:outline-none',
    'focus:ring-2',
    'focus:ring-teal-500',
    'focus:border-transparent',
    {
      'bg-gray-100 cursor-not-allowed': !isEditable,
      'bg-white': isEditable,
    }
  );

  const classTextInput = clsx(
    'text-sm',
    'w-full',
    'h-8',
    'p-2',
    'border',
    'border-gray-300',
    'rounded-md',
    'focus:outline-none',
    'focus:ring-2',
    'focus:ring-teal-500',
    'focus:border-transparent',
    {
      'bg-gray-100 cursor-not-allowed': !isEditable,
      'bg-white': isEditable,
    }
  );

  return (
    <div className={classInfoPanel}>
      <div className={clsx('text-lg', 'font-semibold', 'mb-3', 'text-gray-800')}>
        Aufgabeninformationen
      </div>

      {/* Edit mode indicator */}
      <div
        className={clsx('mb-3', 'p-2', 'rounded-md', 'text-sm', 'font-medium', {
          'bg-green-100 text-green-800': isEditable,
          'bg-gray-100 text-gray-600': !isEditable,
        })}
      >
        {isEditable ? (
          '✏️ Bearbeitungsmodus'
        ) : (
          <div className="leading-tight">
            <div>🔒 Nur lesen</div>
            <div className="text-xs mt-1 opacity-80">Aufgabe läuft oder Roboter nicht verbunden</div>
          </div>
        )}
      </div>

      <div className={clsx('flex', 'items-start', 'mb-2.5')}>
        <span
          className={clsx(
            'text-xs',
            'text-gray-600',
            'w-[120px]',
            'flex-shrink-0',
            'font-medium',
            'leading-snug',
            'break-words',
            'pt-2'
          )}
        >
          Aufgabenanweisung
        </span>
        <div className="flex flex-1 flex-col gap-1">
          <textarea
            className={classTaskInstructionTextarea}
            value={info.taskInstruction || ''}
            onChange={(e) => handleChange('taskInstruction', [e.target.value])}
            disabled={!isEditable}
            placeholder="Aufgabenanweisung eingeben"
          />
          <span className="text-[11px] leading-snug text-gray-400">
            Nur für Sprachmodelle (SmolVLA, Pi0, Pi0-Fast, Pi0.5) nötig — ACT
            ignoriert dieses Feld.
          </span>
        </div>
      </div>

      <div className={clsx('flex', 'items-start', 'mb-2.5')}>
        <span
          className={clsx(
            'text-xs',
            'text-gray-600',
            'w-[120px]',
            'flex-shrink-0',
            'font-medium',
            'leading-snug',
            'break-words',
            'pt-2'
          )}
        >
          Modellpfad
        </span>
        <div className={clsx('flex', 'flex-col', 'flex-1', 'gap-2')}>
          {/* Browse Policy Path Button */}
          <button
            onClick={() => setShowPolicyPathModal(true)}
            disabled={!isEditable}
            className={clsx(
              'flex',
              'items-center',
              'gap-2',
              'px-3',
              'py-2',
              'text-sm',
              'bg-teal-50',
              'text-teal-700',
              'border',
              'border-teal-200',
              'rounded-lg',
              'hover:bg-teal-100',
              'transition-colors',
              'disabled:bg-gray-100',
              'disabled:text-gray-400',
              'disabled:cursor-not-allowed',
              'w-fit'
            )}
          >
            <MdFolderOpen size={16} />
            Modellpfad durchsuchen
          </button>
          {latestSucceededModel && !info.policyPath && (
            <button
              type="button"
              disabled={!isEditable}
              onClick={() => {
                setDownloadPrefill(latestSucceededModel);
                setShowPolicyDownloadModal(true);
              }}
              className={clsx(
                'flex',
                'items-center',
                'gap-2',
                'px-3',
                'py-2',
                'text-sm',
                'bg-blue-50',
                'text-blue-700',
                'border',
                'border-blue-200',
                'rounded-lg',
                'hover:bg-blue-100',
                'transition-colors',
                'disabled:bg-gray-100',
                'disabled:text-gray-400',
                'w-fit'
              )}
              title={latestSucceededModel}
            >
              <MdDownload size={16} />
              <span className="truncate max-w-[220px]">
                Neuestes Modell laden: {latestSucceededModel.split('/').pop()}
              </span>
            </button>
          )}
          {/* Download Policy Button */}
          <button
            disabled={!isEditable}
            onClick={() => {
              setDownloadPrefill('');
              setShowPolicyDownloadModal(true);
            }}
            className={clsx(
              'flex',
              'items-center',
              'gap-2',
              'px-3',
              'py-2',
              'text-sm',
              'bg-green-50',
              'text-green-700',
              'border',
              'border-green-200',
              'rounded-lg',
              'hover:bg-green-100',
              'transition-colors',
              'disabled:bg-gray-100',
              'disabled:text-gray-400',
              'disabled:cursor-not-allowed',
              'w-fit'
            )}
          >
            <MdDownload size={16} />
            Modell herunterladen
          </button>
          <textarea
            className={classPolicyPathTextarea}
            value={info.policyPath || ''}
            onChange={(e) => handleChange('policyPath', e.target.value)}
            disabled={!isEditable}
            placeholder="Modellpfad oder Repo-ID eingeben"
          />
        </div>
      </div>

      <div className="w-full h-1 my-2 border-t border-gray-300"></div>

      <div className={clsx('flex', 'items-center', 'mb-2.5')}>
        <span className={classLabel}>FPS</span>
        <input
          className={classTextInput}
          type="number"
          step="5"
          value={info.fps || ''}
          onChange={(e) => handleChange('fps', Number(e.target.value))}
          disabled={!isEditable}
        />
      </div>

      <div className="text-xs text-gray-400 mt-1 ml-2">
        Aufnahme während der Inferenz wird in einem zukünftigen Update unterstützt
      </div>

      <FileBrowserModal
        isOpen={showPolicyPathModal}
        onClose={() => setShowPolicyPathModal(false)}
        onFileSelect={handlePolicyPathSelect}
        title="Modellpfad auswählen"
        selectButtonText="Auswählen"
        allowDirectorySelect={true}
        targetFileName={[TARGET_FILES.POLICY_MODEL]}
        targetFileLabel="Modelldatei gefunden! 🎯"
        initialPath={DEFAULT_PATHS.POLICY_MODEL_PATH}
        defaultPath={DEFAULT_PATHS.POLICY_MODEL_PATH}
        homePath=""
      />

      <PolicyDownloadModal
        isOpen={showPolicyDownloadModal}
        onClose={() => setShowPolicyDownloadModal(false)}
        onDownloadComplete={handleDownloadPolicyComplete}
        initialRepoId={downloadPrefill}
      />
    </div>
  );
};

export default InferencePanel;
