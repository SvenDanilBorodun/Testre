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
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import {
  setSeed,
  setNumWorkers,
  setBatchSize,
  setSteps,
  setLogFreq,
  setSaveFreq,
  setDefaultTrainingInfo,
} from '../features/training/trainingSlice';

const TrainingOptionInput = () => {
  const dispatch = useDispatch();

  const seed = useSelector((state) => state.training.trainingInfo.seed);
  const numWorkers = useSelector((state) => state.training.trainingInfo.numWorkers);
  const batchSize = useSelector((state) => state.training.trainingInfo.batchSize);
  const steps = useSelector((state) => state.training.trainingInfo.steps);
  const logFreq = useSelector((state) => state.training.trainingInfo.logFreq);
  const saveFreq = useSelector((state) => state.training.trainingInfo.saveFreq);

  const isTraining = useSelector((state) => state.training.isTraining);

  const classLabel = clsx('su-trn-opt__label', 'flex-1', 'min-w-0');

  const classPanel = clsx('su-plate', 'su-plate--plate', 'su-trn-card', 'w-full', 'min-w-[250px]', 'relative');

  const classTextInput = clsx('su-field', 'su-trn-opt__input', 'h-10', 'px-3', { 'cursor-not-allowed': isTraining });


  const classResetButton = clsx('su-key', 'su-key--md', 'w-full');

  return (
    <div className={classPanel}>
      <div className="su-trn-card__title">
        Weitere Optionen
      </div>

      <div className={clsx('flex', 'items-center', 'gap-3', 'mb-2.5')}>
        <span className={classLabel}>Seed</span>
        <input
          className={classTextInput}
          type="number"
          step="1"
          min={0}
          max={65535}
          value={seed || ''}
          onChange={(e) => dispatch(setSeed(Number(e.target.value) || 0))}
          disabled={isTraining}
          placeholder="Seed eingeben"
        />
      </div>

      <div className={clsx('flex', 'items-center', 'gap-3', 'mb-2.5')}>
        <span className={classLabel}>Worker</span>
        <input
          className={classTextInput}
          type="number"
          step="1"
          min={0}
          max={65535}
          value={numWorkers || ''}
          onChange={(e) => dispatch(setNumWorkers(Number(e.target.value) || 0))}
          disabled={isTraining}
          placeholder="Worker eingeben"
        />
      </div>

      <div className={clsx('flex', 'items-center', 'gap-3', 'mb-2.5')}>
        <span className={classLabel}>Batch-Größe</span>
        <input
          className={classTextInput}
          type="number"
          step="1"
          min={0}
          max={65535}
          value={batchSize || ''}
          onChange={(e) => dispatch(setBatchSize(Number(e.target.value) || 0))}
          disabled={isTraining}
          placeholder="Batch-Größe eingeben"
        />
      </div>

      <div className={clsx('flex', 'items-center', 'gap-3', 'mb-2.5')}>
        <span className={classLabel}>Schritte</span>
        <input
          className={classTextInput}
          type="number"
          step="1"
          min={0}
          max={65535}
          value={steps || ''}
          onChange={(e) => dispatch(setSteps(Number(e.target.value) || 0))}
          disabled={isTraining}
          placeholder="Schritte eingeben"
        />
      </div>

      <div className={clsx('flex', 'items-center', 'gap-3', 'mb-2.5')}>
        <span className={classLabel}>Log-Intervall</span>
        <input
          className={classTextInput}
          type="number"
          step="1"
          min={0}
          max={65535}
          value={logFreq || ''}
          onChange={(e) => dispatch(setLogFreq(Number(e.target.value) || 0))}
          disabled={isTraining}
          placeholder="Log-Intervall eingeben"
        />
      </div>

      <div className={clsx('flex', 'items-center', 'gap-3', 'mb-2.5')}>
        <span className={classLabel}>Speicher-Intervall</span>
        <input
          className={classTextInput}
          type="number"
          step="1"
          min={0}
          max={65535}
          value={saveFreq || ''}
          onChange={(e) => dispatch(setSaveFreq(Number(e.target.value) || 0))}
          disabled={isTraining}
          placeholder="Speicher-Intervall eingeben"
        />
      </div>

      <div className="mb-2 mt-auto" />
      <button type="button" className={classResetButton} onClick={() => dispatch(setDefaultTrainingInfo())}>
        Standardwerte
      </button>
    </div>
  );
};

export default TrainingOptionInput;
