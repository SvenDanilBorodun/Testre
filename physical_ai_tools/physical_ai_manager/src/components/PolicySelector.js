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

import React, { useState, useEffect, useCallback } from 'react';
import clsx from 'clsx';
import { MdRefresh } from 'react-icons/md';
import { useSelector, useDispatch } from 'react-redux';
import {
  selectPolicyType,
  setPolicyList,
} from '../features/training/trainingSlice';
import { useRosServiceCaller } from '../hooks/useRosServiceCaller';
import { getPolicies } from '../services/cloudTrainingApi';
import toast from 'react-hot-toast';

// Env-gated allowlist — students only see ACT, admin/dev builds override via
// REACT_APP_ALLOWED_POLICIES=tdmpc,diffusion,act,vqbet,pi0,pi0fast,smolvla.
const ALLOWED_POLICIES = (process.env.REACT_APP_ALLOWED_POLICIES || 'act')
  .split(',')
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);

export default function PolicySelector({ readonly = false }) {
  const dispatch = useDispatch();

  const selectedPolicy = useSelector((state) => state.training.trainingInfo.policyType);
  const policyList = useSelector((state) => state.training.policyList);
  const isTraining = useSelector((state) => state.training.isTraining);
  // Cloud-only mode has no rosbridge, so the ROS service call hangs and the
  // dropdown stays empty. Falling back to the cloud /policies endpoint keeps
  // training startable without a robot connected.
  const heartbeatStatus = useSelector((state) => state.tasks.heartbeatStatus);
  const rosConnected = heartbeatStatus === 'connected';
  const accessToken = useSelector((state) => state.auth.session?.access_token);

  const title = 'Modellauswahl';

  const [loading] = useState(false);
  const [fetching, setFetching] = useState(false);

  const { getPolicyList } = useRosServiceCaller();

  const fetchFromCloud = useCallback(async () => {
    if (!accessToken) {
      toast.error('Nicht angemeldet — Modellliste nicht abrufbar.');
      return;
    }
    const result = await getPolicies(accessToken);
    const names = (result?.policies || []).map((p) => String(p.name).toLowerCase());
    const filtered = names.filter((p) => ALLOWED_POLICIES.includes(p));
    dispatch(setPolicyList(filtered));
    if (filtered.length === 0) {
      toast.error('Keine Modelle für dieses Konto freigeschaltet.');
    } else {
      toast.success('Modellliste aus Cloud geladen');
    }
  }, [accessToken, dispatch]);

  const fetchItemList = useCallback(async () => {
    setFetching(true);
    try {
      // Cloud-only: skip the ROS call entirely — it would block on a missing
      // service and end with an empty list.
      if (!rosConnected) {
        await fetchFromCloud();
        return;
      }
      const result = await getPolicyList();
      console.log('Policies received:', result);
      if (result && result.policy_list) {
        const filtered = result.policy_list.filter((p) =>
          ALLOWED_POLICIES.includes(String(p).toLowerCase())
        );
        dispatch(setPolicyList(filtered));
        toast.success('Modellliste erfolgreich geladen');
      } else {
        // ROS responded but with garbage — try cloud rather than show empty.
        await fetchFromCloud();
      }
    } catch (error) {
      console.error('Error fetching policy list from ROS, falling back to cloud:', error);
      try {
        await fetchFromCloud();
      } catch (e2) {
        toast.error(`Modellliste konnte nicht geladen werden: ${e2.message}`);
      }
    } finally {
      setFetching(false);
    }
  }, [getPolicyList, dispatch, rosConnected, fetchFromCloud]);

  // When exactly one policy is allowed (the student case), auto-select it so
  // the dropdown isn't a no-op interaction.
  useEffect(() => {
    if (policyList.length === 1 && !selectedPolicy) {
      dispatch(selectPolicyType(policyList[0]));
    }
  }, [policyList, selectedPolicy, dispatch]);

  const classCard = clsx(
    'bg-white',
    'border',
    'border-[var(--line)]',
    'rounded-[var(--radius-lg)]',
    'shadow-soft',
    'p-5',
    'w-full',
    'min-w-[200px]'
  );

  const classSelect = clsx(
    'w-full',
    'px-3',
    'py-2',
    'border',
    'border-gray-300',
    'rounded-md',
    'focus:outline-none',
    'focus:ring-2',
    'focus:ring-teal-500',
    'focus:border-transparent',
    'disabled:bg-gray-100',
    'disabled:cursor-not-allowed',
    'disabled:text-gray-500',
    'disabled:border-gray-300'
  );

  const classRefreshButton = clsx(
    'w-full',
    'px-4',
    'py-2',
    'bg-gray-500',
    'text-white',
    'rounded-md',
    'font-medium',
    'transition-colors',
    'hover:bg-gray-600',
    'disabled:bg-gray-400',
    'disabled:cursor-not-allowed'
  );

  const classTitle = clsx('text-xl', 'font-bold', 'mb-6', 'text-left', {
    'text-gray-500': readonly,
    'text-gray-800': !readonly,
  });
  const classLabel = clsx('text-sm', 'font-medium', 'text-gray-700', 'mb-2', 'block');

  useEffect(() => {
    fetchItemList();
  }, [fetchItemList]);

  return (
    <div className={classCard}>
      <h1 className={classTitle}>{title}</h1>
      <label className={classLabel}>{!readonly ? 'Modell auswählen:' : 'Ausgewähltes Modell:'}</label>

      <select
        className={classSelect}
        value={selectedPolicy || ''}
        onChange={(e) => dispatch(selectPolicyType(e.target.value))}
        disabled={fetching || loading || readonly}
      >
        <option value="" disabled={readonly}>
          Choose policy...
        </option>
        {policyList.map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>

      <div className="mb-2" />

      <p className="text-xs text-gray-400">
        Training läuft auf Cloud-GPU (CUDA)
      </p>

      {!readonly && (
        <>
          <div className="mb-4" />
          <button
            className={classRefreshButton}
            onClick={fetchItemList}
            disabled={fetching || loading || isTraining || readonly}
          >
            <div className="flex items-center justify-center gap-2">
              <MdRefresh size={16} className={fetching ? 'animate-spin' : ''} />
              {fetching ? 'Laden...' : `Aktualisieren`}
            </div>
          </button>
        </>
      )}
    </div>
  );
}
