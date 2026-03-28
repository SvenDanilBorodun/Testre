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

import React, { useEffect } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import HeartbeatStatus from '../components/HeartbeatStatus';
import DatasetSelector from '../components/DatasetSelector';
import PolicySelector from '../components/PolicySelector';
import TrainingOutputFolderInput from '../components/TrainingOutputFolderInput';
import TrainingControlPanel from '../components/TrainingControlPanel';
import TrainingOptionInput from '../components/TrainingOptionInput';
import TrainingProgressBar from '../components/TrainingProgressBar';
import TrainingLossDisplay from '../components/TrainingLossDisplay';
import CloudTrainingHistory from '../components/CloudTrainingHistory';
import LoginForm from '../components/LoginForm';
import { supabase } from '../lib/supabaseClient';
import { clearSession } from '../features/auth/authSlice';
import { getQuota } from '../services/cloudTrainingApi';
import { setQuota } from '../features/auth/authSlice';

export default function TrainingPage() {
  const dispatch = useDispatch();
  const isAuthenticated = useSelector((state) => state.auth.isAuthenticated);
  const isLoading = useSelector((state) => state.auth.isLoading);
  const session = useSelector((state) => state.auth.session);
  const trainingCredits = useSelector((state) => state.auth.trainingCredits);
  const trainingsUsed = useSelector((state) => state.auth.trainingsUsed);

  // Toast limit implementation using useToasterStore
  const { toasts } = useToasterStore();
  const TOAST_LIMIT = 3;

  useEffect(() => {
    toasts
      .filter((t) => t.visible)
      .filter((_, i) => i >= TOAST_LIMIT)
      .forEach((t) => toast.dismiss(t.id));
  }, [toasts]);

  // Fetch quota when authenticated
  useEffect(() => {
    if (isAuthenticated && session?.access_token) {
      getQuota(session.access_token)
        .then((quota) => dispatch(setQuota(quota)))
        .catch(() => {});
    }
  }, [isAuthenticated, session, dispatch]);

  const handleLogout = async () => {
    await supabase.auth.signOut();
    dispatch(clearSession());
    toast.success('Abgemeldet');
  };

  const classContainer = clsx(
    'w-full',
    'h-full',
    'flex',
    'flex-col',
    'items-start',
    'justify-start',
    'pt-10'
  );

  const classHeartbeatStatus = clsx('absolute', 'top-5', 'left-35', 'z-10');

  const classComponentsContainer = clsx(
    'w-full',
    'flex',
    'p-10',
    'gap-8',
    'items-start',
    'justify-center'
  );

  // Show loading spinner while checking auth
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-gray-500 text-lg">Laden...</div>
      </div>
    );
  }

  // Show login form if not authenticated
  if (!isAuthenticated) {
    return <LoginForm />;
  }

  const remaining = trainingCredits - trainingsUsed;

  return (
    <div className={classContainer}>
      <div className={classHeartbeatStatus}>
        <HeartbeatStatus />
      </div>

      {/* User info bar */}
      <div className="absolute top-4 right-6 flex items-center gap-4 z-10">
        <div className="flex items-center gap-2">
          <span className="text-sm text-gray-600">{session?.user?.email}</span>
          <span
            className={clsx(
              'text-xs font-semibold px-2 py-0.5 rounded-full',
              remaining > 0
                ? 'bg-green-100 text-green-700'
                : 'bg-red-100 text-red-700'
            )}
          >
            {remaining} / {trainingCredits} Trainingsguthaben
          </span>
        </div>
        <button
          onClick={handleLogout}
          className="text-sm text-gray-500 hover:text-gray-700 underline"
        >
          Abmelden
        </button>
      </div>

      {/* Training components */}
      <div className="overflow-scroll h-full w-full">
        <div className={classComponentsContainer}>
          <DatasetSelector />
          <PolicySelector />
          <TrainingOutputFolderInput />
          <TrainingOptionInput />
        </div>
        <div className="flex justify-center items-center mt-5 mb-8">
          <div className="rounded-full bg-gray-200 w-32 h-3"></div>
        </div>
        <div className="flex justify-center items-center mb-10">
          <div className="w-full max-w-md">
            <TrainingLossDisplay />
          </div>
        </div>

        {/* Cloud training history */}
        <div className="flex justify-center items-center mb-10 px-10">
          <div className="w-full max-w-5xl">
            <CloudTrainingHistory />
          </div>
        </div>
      </div>

      {/* Training Control Buttons */}
      <div className="w-full flex items-center justify-around gap-2 bg-gray-100 p-2">
        <div className="flex-shrink-0">
          <TrainingControlPanel />
        </div>
        <div className="flex-1 min-w-0 max-w-4xl flex gap-10 justify-center items-center">
          <div className="flex-1 max-w-md">
            <TrainingProgressBar />
          </div>
        </div>
      </div>
    </div>
  );
}
