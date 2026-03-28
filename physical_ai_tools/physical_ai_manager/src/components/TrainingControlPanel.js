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

import React from 'react';
import { useDispatch, useSelector } from 'react-redux';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { setIsTraining } from '../features/training/trainingSlice';
import { setQuota } from '../features/auth/authSlice';
import { startCloudTraining, getQuota } from '../services/cloudTrainingApi';

export default function TrainingControlPanel() {
  const dispatch = useDispatch();
  const isTraining = useSelector((state) => state.training.isTraining);
  const datasetRepoId = useSelector((state) => state.training.trainingInfo.datasetRepoId);
  const selectedPolicy = useSelector((state) => state.training.trainingInfo.policyType);
  const outputFolderName = useSelector((state) => state.training.trainingInfo.outputFolderName);
  const trainingInfo = useSelector((state) => state.training.trainingInfo);
  const session = useSelector((state) => state.auth.session);
  const trainingCredits = useSelector((state) => state.auth.trainingCredits);
  const trainingsUsed = useSelector((state) => state.auth.trainingsUsed);

  const remaining = trainingCredits - trainingsUsed;

  const classContainer = clsx('flex', 'items-center', 'justify-center', 'p-2', 'gap-6', 'm-2');

  const classButton = clsx(
    'h-full',
    'px-8',
    'py-3',
    'rounded-2xl',
    'font-semibold',
    'text-lg',
    'transition-all',
    'duration-200',
    'transform',
    'active:scale-95',
    'shadow-lg'
  );

  const classStartButton = clsx(
    classButton,
    'bg-teal-600',
    'text-white',
    'hover:bg-teal-700',
    'hover:shadow-xl',
    'disabled:bg-gray-400',
    'disabled:cursor-not-allowed',
    'disabled:hover:bg-gray-400',
    'disabled:hover:shadow-lg'
  );

  const handleStartTraining = async () => {
    if (!datasetRepoId) {
      toast.error('Bitte wähle einen Datensatz aus');
      return;
    }
    if (!selectedPolicy) {
      toast.error('Bitte wähle eine Richtlinie aus');
      return;
    }
    if (!outputFolderName) {
      toast.error('Bitte gib einen Ausgabeordnernamen ein');
      return;
    }
    if (remaining <= 0) {
      toast.error('Kein Trainingsguthaben mehr. Kontaktiere deinen Dozenten für mehr.');
      return;
    }

    dispatch(setIsTraining(true));

    try {
      const result = await startCloudTraining(session.access_token, {
        datasetName: datasetRepoId,
        modelType: selectedPolicy,
        trainingParams: {
          seed: trainingInfo.seed,
          num_workers: trainingInfo.numWorkers,
          batch_size: trainingInfo.batchSize,
          steps: trainingInfo.steps,
          eval_freq: trainingInfo.evalFreq,
          log_freq: trainingInfo.logFreq,
          save_freq: trainingInfo.saveFreq,
          output_folder_name: outputFolderName,
        },
      });

      toast.success(
        `Training an Cloud gesendet! Modell: ${result.model_name}`,
        { duration: 5000 }
      );

      // Refresh quota
      const quota = await getQuota(session.access_token);
      dispatch(setQuota(quota));
    } catch (error) {
      toast.error(`Cloud-Training konnte nicht gestartet werden: ${error.message}`);
    } finally {
      dispatch(setIsTraining(false));
    }
  };

  return (
    <div className={classContainer}>
      <button
        onClick={handleStartTraining}
        className={classStartButton}
        disabled={isTraining || remaining <= 0}
      >
        {isTraining ? 'Wird gesendet...' : 'Training starten'}
      </button>

      {remaining <= 0 && (
        <span className="text-sm text-red-500 font-medium">
          Kein Guthaben mehr
        </span>
      )}
    </div>
  );
}
