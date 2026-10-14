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
import { Key } from './sucher';
import toast from 'react-hot-toast';
import { setIsTraining, triggerCloudJobsRefresh } from '../features/training/trainingSlice';
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

      // Signal MyModels to refetch immediately so the new training appears
      // without waiting for the 5s poll cycle or a manual refresh.
      dispatch(triggerCloudJobsRefresh());

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
    <div className="flex items-center justify-end gap-4 flex-wrap">
      <Key
        variant="signal"
        size="lg"
        icon="play"
        onClick={handleStartTraining}
        disabled={isTraining || remaining <= 0}
      >
        {isTraining ? 'Wird gesendet...' : 'Training starten'}
      </Key>

      {remaining <= 0 && (
        <span className="su-trn-nocredit">
          Kein Guthaben mehr
        </span>
      )}
    </div>
  );
}
