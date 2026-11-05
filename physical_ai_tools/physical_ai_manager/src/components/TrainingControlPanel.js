// Copyright 2025 ROBOTIS CO., LTD.
// Modifications copyright 2026 Sven Danil Borodun (EduBotics)
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

import React, { useRef, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { Key } from './sucher';
import toast from 'react-hot-toast';
import { setIsTraining, triggerCloudJobsRefresh } from '../features/training/trainingSlice';
import { setQuota } from '../features/auth/authSlice';
import { startCloudTraining, getQuota, newRequestId } from '../services/cloudTrainingApi';
import StartTrainingDialog, {
  MEMBER_LIMIT_DE,
  NO_HOURS_DE,
  NO_HOURS_LABEL_DE,
  START_DIALOG_DE,
  noHoursKind,
} from './StartTrainingDialog';

export default function TrainingControlPanel() {
  const dispatch = useDispatch();
  const isTraining = useSelector((state) => state.training.isTraining);
  const datasetRepoId = useSelector((state) => state.training.trainingInfo.datasetRepoId);
  const selectedPolicy = useSelector((state) => state.training.trainingInfo.policyType);
  const outputFolderName = useSelector((state) => state.training.trainingInfo.outputFolderName);
  const trainingInfo = useSelector((state) => state.training.trainingInfo);
  const session = useSelector((state) => state.auth.session);
  const nextReservationHours = useSelector((state) => state.auth.nextReservationHours);
  const maxHoursPerTraining = useSelector((state) => state.auth.maxHoursPerTraining);
  const hoursFree = useSelector((state) => state.auth.hoursFree);
  const memberLimitHours = useSelector((state) => state.auth.memberLimitHours);
  const memberLeftHours = useSelector((state) => state.auth.memberLeftHours);
  const account = useSelector((state) => state.auth.account);
  const workgroupName = useSelector((state) => state.auth.workgroupName);

  const [dialogOpen, setDialogOpen] = useState(false);
  // The idempotency key of the open dialog: minted on its first confirm, kept
  // for a retry of the same send, dropped when the dialog closes.
  const keyRef = useRef(null);
  // A second click before the re-render that disables the key sends nothing.
  const sendingRef = useRef(false);

  const canStart = typeof nextReservationHours === 'number' && nextReservationHours >= 1;
  const noHours = noHoursKind({ nextReservationHours, hoursFree, memberLimitHours, memberLeftHours });

  const handleStartTraining = () => {
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
    if (!canStart) {
      toast.error(noHours === 'member_limit' ? MEMBER_LIMIT_DE : NO_HOURS_DE);
      return;
    }
    keyRef.current = null;
    setDialogOpen(true);
  };

  const closeDialog = () => {
    keyRef.current = null;
    setDialogOpen(false);
  };

  const confirmStart = async () => {
    if (sendingRef.current) return;
    sendingRef.current = true;
    if (!keyRef.current) keyRef.current = newRequestId();
    dispatch(setIsTraining(true));
    let result;
    try {
      result = await startCloudTraining(session.access_token, {
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
        idempotencyKey: keyRef.current,
      });
    } catch (error) {
      // The dialog stays open: a second „Training starten" is a retry of the
      // same send and reuses the key.
      toast.error(START_DIALOG_DE.failed(error.message));
      dispatch(setIsTraining(false));
      sendingRef.current = false;
      return;
    }

    toast.success(START_DIALOG_DE.started(result?.reserved_hours), { duration: 5000 });
    closeDialog();
    dispatch(setIsTraining(false));
    sendingRef.current = false;

    // Signal MyModels to refetch immediately so the new training appears
    // without waiting for the 5s poll cycle or a manual refresh.
    dispatch(triggerCloudJobsRefresh());

    // The reservation changed the free hours. A failed refresh is not a failed
    // start (the next focus refetches the quota).
    try {
      dispatch(setQuota(await getQuota(session.access_token)));
    } catch {
      // ignored on purpose
    }
  };

  const hoursKnownZero = typeof nextReservationHours === 'number' && nextReservationHours < 1;

  return (
    <div className="flex items-center justify-end gap-4 flex-wrap">
      <Key
        variant="signal"
        size="lg"
        icon="play"
        onClick={handleStartTraining}
        disabled={isTraining || !canStart}
      >
        {isTraining ? 'Wird gesendet...' : 'Training starten'}
      </Key>

      {hoursKnownZero && (
        <span className="su-trn-nocredit">
          {NO_HOURS_LABEL_DE}
        </span>
      )}

      {dialogOpen && (
        <StartTrainingDialog
          hours={nextReservationHours}
          maxHours={maxHoursPerTraining}
          workgroupName={account === 'group' ? workgroupName : null}
          busy={isTraining}
          onConfirm={confirmStart}
          onCancel={closeDialog}
        />
      )}
    </div>
  );
}
