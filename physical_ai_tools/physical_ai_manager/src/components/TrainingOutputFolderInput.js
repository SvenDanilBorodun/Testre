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

import React, { useEffect, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import clsx from 'clsx';
import toast from 'react-hot-toast';
import { setOutputFolderName, setModelWeightList } from '../features/training/trainingSlice';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import { Led } from './sucher';

export default function TrainingOutputFolderInput({ readonly = false }) {
  const dispatch = useDispatch();

  const { getModelWeightList } = useRosServiceCaller();

  const [checkingDuplicate, setCheckingDuplicate] = useState(false);
  const [duplicateChecked, setDuplicateChecked] = useState(false);
  const [isOutputFolderAvailable, setIsOutputFolderAvailable] = useState(false);
  const [tempOutputFolderName, setTempOutputFolderName] = useState('');

  const outputFolderName = useSelector((state) => state.training.trainingInfo.outputFolderName);

  const classCard = clsx('su-plate', 'su-plate--plate', 'su-trn-card', 'w-full', 'min-w-[200px]');

  const classInput = clsx('su-field', 'w-full', 'h-11', 'px-3', 'disabled:cursor-not-allowed');

  const classButton = clsx('su-key', 'su-key--md', 'w-full');

  const classTitle = clsx('su-trn-card__title', { 'su-trn-card__title--ro': readonly });

  const handleCheckDuplicate = async () => {
    setCheckingDuplicate(true);
    console.log('check duplicate');

    try {
      const result = await getModelWeightList();

      if (result && result.model_weight_list) {
        dispatch(setModelWeightList(result.model_weight_list));
        console.log('result:', result);
        toast.success('Liste der Modellordner geladen.');
      } else {
        toast.error('Liste der Modellordner konnte nicht geladen werden: ungültige Antwort.');
      }
      if (result.model_weight_list.includes(tempOutputFolderName)) {
        toast.error(`„${tempOutputFolderName}“ gibt es schon.`);
        setIsOutputFolderAvailable(false);
      } else {
        toast.success(`„${tempOutputFolderName}“ ist frei.`);
        dispatch(setOutputFolderName(tempOutputFolderName));
        setIsOutputFolderAvailable(true);
      }
      setDuplicateChecked(true);
    } catch (error) {
      console.error('Error checking duplicate:', error);
      toast.error(`Prüfung auf Duplikat fehlgeschlagen: ${error.message}`);
    } finally {
      setCheckingDuplicate(false);
    }
  };

  useEffect(() => {
    if (outputFolderName) {
      console.log('outputFolderName:', outputFolderName);
      setTempOutputFolderName(outputFolderName);
    }
  }, [outputFolderName]);

  return (
    <div className={classCard}>
      <h1 className={classTitle}>Ausgabeordnername</h1>
      <input
        type="text"
        className={classInput}
        disabled={checkingDuplicate || readonly}
        placeholder="Text hier eingeben..."
        value={tempOutputFolderName}
        onChange={(e) => {
          setTempOutputFolderName(e.target.value);
          dispatch(setOutputFolderName(undefined));
          setDuplicateChecked(false);
        }}
      />
      <div className="su-trn-avail">
        {!duplicateChecked && !readonly && (
          <p className="text-[var(--ink-2)]"><Led tone="off" /> Bitte auf Duplikat prüfen</p>
        )}
        {duplicateChecked &&
          (isOutputFolderAvailable ? (
            <p className="text-[var(--ink)] break-all"><Led tone="ok" /> „{tempOutputFolderName}“ ist frei.</p>
          ) : (
            <p className="text-[color:var(--danger)] break-all"><Led tone="rec" /> „{tempOutputFolderName}“ gibt es schon.</p>
          ))}
      </div>
      {!readonly && (
        <>
          <div className="mb-4 mt-auto" />
          <button
            className={classButton}
            onClick={handleCheckDuplicate}
            disabled={
              checkingDuplicate || duplicateChecked || tempOutputFolderName === '' || readonly
            }
          >
            Duplikat prüfen
          </button>
        </>
      )}
    </div>
  );
}
