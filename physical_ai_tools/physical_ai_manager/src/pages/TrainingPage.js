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

import React, { useCallback, useEffect, useMemo } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import { MdLogout } from 'react-icons/md';
import DatasetSelector from '../components/DatasetSelector';
import PolicySelector from '../components/PolicySelector';
import TrainingOutputFolderInput from '../components/TrainingOutputFolderInput';
import TrainingControlPanel from '../components/TrainingControlPanel';
import TrainingOptionInput from '../components/TrainingOptionInput';
import TrainingLiveChart, { pickSelectedJob } from '../components/TrainingLiveChart';
import MyModels from '../components/MyModels';
import LoginForm from '../components/LoginForm';
import HeartbeatStatus from '../components/HeartbeatStatus';
import { Card, Pill, Btn, SectionHeader } from '../components/EbUI';
import { supabase } from '../lib/supabaseClient';
import { clearSession, setQuota } from '../features/auth/authSlice';
import { getQuota } from '../services/cloudTrainingApi';
import useSupabaseTrainings from '../hooks/useSupabaseTrainings';
import useRefetchOnFocus from '../hooks/useRefetchOnFocus';

function statusSubtitle(status) {
  switch (status) {
    case 'queued':
      return 'Wird eingereiht · Modal';
    case 'running':
      return 'Live · aktualisiert laufend · Modal';
    case 'succeeded':
      return 'Erfolgreich · Modal';
    case 'failed':
      return 'Fehlgeschlagen · Modal';
    case 'canceled':
      return 'Abgebrochen · Modal';
    default:
      return 'Bereit · Modal';
  }
}

function statusPill(status) {
  switch (status) {
    case 'queued':
      return { tone: 'amber', label: 'wartet' };
    case 'running':
      return { tone: 'danger', label: 'aktiv' };
    case 'succeeded':
      return { tone: 'success', label: 'fertig' };
    case 'failed':
      return { tone: 'danger', label: 'Fehler' };
    case 'canceled':
      return { tone: 'neutral', label: 'abgebrochen' };
    default:
      return { tone: 'accent', label: 'idle' };
  }
}

export default function TrainingPage() {
  const dispatch = useDispatch();
  const isAuthenticated = useSelector((state) => state.auth.isAuthenticated);
  const isLoading = useSelector((state) => state.auth.isLoading);
  const session = useSelector((state) => state.auth.session);
  const trainingCredits = useSelector((state) => state.auth.trainingCredits);
  const trainingsUsed = useSelector((state) => state.auth.trainingsUsed);
  const selectedTrainingId = useSelector((state) => state.training.selectedTrainingId);

  const { jobs, loading, refetch, isRealtime } = useSupabaseTrainings();

  const { toasts } = useToasterStore();
  const TOAST_LIMIT = 3;

  useEffect(() => {
    toasts
      .filter((t) => t.visible)
      .filter((_, i) => i >= TOAST_LIMIT)
      .forEach((t) => toast.dismiss(t.id));
  }, [toasts]);

  const refetchQuota = useCallback(() => {
    if (!isAuthenticated || !session?.access_token) return;
    getQuota(session.access_token)
      .then((quota) => dispatch(setQuota(quota)))
      .catch(() => {});
  }, [isAuthenticated, session, dispatch]);

  useEffect(() => {
    refetchQuota();
  }, [refetchQuota]);

  useRefetchOnFocus(refetchQuota);

  const handleLogout = async () => {
    await supabase.auth.signOut();
    dispatch(clearSession());
    toast.success('Abgemeldet');
  };

  const selectedJob = useMemo(
    () => pickSelectedJob(jobs, selectedTrainingId),
    [jobs, selectedTrainingId],
  );

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-[var(--ink-3)] text-lg">Laden…</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <LoginForm />;
  }

  const remaining = trainingCredits - trainingsUsed;
  const creditTone = remaining <= 0 ? 'danger' : remaining <= 2 ? 'amber' : 'success';

  const status = selectedJob?.status;
  const subtitle = statusSubtitle(status);
  const pill = statusPill(status);

  return (
    <div className="h-full w-full overflow-y-auto" style={{ background: 'var(--bg)' }}>
      <div className="eb-shell flex flex-col gap-5 md:gap-6">
        {/* Header rail */}
        <SectionHeader
          eyebrow="Training"
          title="Modell trainieren"
          description="Wähle Datensatz und Policy, dann starte das Training in der Cloud."
          right={
            <div className="flex items-center gap-3 flex-wrap justify-end">
              <HeartbeatStatus />
              <div className="text-right">
                <div className="text-xs text-[var(--ink-3)]">{session?.user?.email}</div>
                <Pill tone={creditTone} dot>
                  <span className="font-mono">
                    {remaining} / {trainingCredits}
                  </span>{' '}
                  Trainingsguthaben{trainingsUsed > 0 ? ` · ${trainingsUsed} verbraucht` : ''}
                </Pill>
              </div>
              <Btn variant="ghost" size="sm" onClick={handleLogout}>
                <MdLogout /> Abmelden
              </Btn>
            </div>
          }
        />

        {/* Setup rail */}
        <div>
          <div className="flex items-center gap-3 mb-3">
            <h2 className="text-[15px] font-semibold tracking-tight text-[var(--ink)]">
              Setup
            </h2>
            <span className="text-xs text-[var(--ink-3)]">
              Datensatz · Policy · Ausgabe · Optionen
            </span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4 items-start">
            <div className="min-w-0">
              <StepLabel n={1} title="Datensatz" />
              <DatasetSelector />
            </div>
            <div className="min-w-0">
              <StepLabel n={2} title="Policy" />
              <PolicySelector />
            </div>
            <div className="min-w-0">
              <StepLabel n={3} title="Ausgabeordner" />
              <TrainingOutputFolderInput />
            </div>
            <div className="min-w-0">
              <StepLabel n={4} title="Optionen" />
              <TrainingOptionInput />
            </div>
          </div>
        </div>

        {/* Monitor rail (full width) */}
        <Card
          title="Trainingsverlauf"
          subtitle={subtitle}
          right={
            <div className="flex gap-2 items-center flex-wrap justify-end">
              <Pill tone={pill.tone} dot>
                {pill.label}
              </Pill>
              <TrainingControlPanel />
            </div>
          }
        >
          <TrainingLiveChart jobs={jobs} isRealtime={isRealtime} />
          {remaining <= 0 && (
            <div className="mt-4 text-xs text-[color:var(--danger)] bg-[var(--danger-wash)] px-3 py-2 rounded-[var(--radius-sm)] leading-snug">
              Kein Guthaben mehr. Kontaktiere deinen Lehrer für mehr Credits.
            </div>
          )}
        </Card>

        {/* My Models */}
        <Card
          title="Meine Modelle"
          subtitle="Lokal + in der Cloud"
          padded={false}
        >
          <div className="p-2">
            <MyModels
              jobs={jobs}
              loading={loading}
              refetch={refetch}
              isRealtime={isRealtime}
            />
          </div>
        </Card>
      </div>
    </div>
  );
}

function StepLabel({ n, title }) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <span
        className={clsx(
          'w-6 h-6 rounded-full flex items-center justify-center font-mono text-xs font-semibold',
          'bg-[var(--accent-wash)] text-[var(--accent-ink)]'
        )}
      >
        {n}
      </span>
      <span className="text-xs font-semibold uppercase tracking-wide text-[var(--ink-3)]">
        {title}
      </span>
    </div>
  );
}
