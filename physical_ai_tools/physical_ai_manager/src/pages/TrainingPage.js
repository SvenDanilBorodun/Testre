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

import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import clsx from 'clsx';
import toast, { useToasterStore } from 'react-hot-toast';
import DatasetSelector from '../components/DatasetSelector';
import PolicySelector from '../components/PolicySelector';
import TrainingOutputFolderInput from '../components/TrainingOutputFolderInput';
import TrainingControlPanel from '../components/TrainingControlPanel';
import TrainingOptionInput from '../components/TrainingOptionInput';
import TrainingLiveChart, { pickSelectedJob } from '../components/TrainingLiveChart';
import MyModels from '../components/MyModels';
import LoginForm from '../components/LoginForm';
import HeartbeatStatus from '../components/HeartbeatStatus';
import { Card, Btn, SectionHeader } from '../components/EbUI';
import { Led, Plate, Screen } from '../components/sucher';
import '../components/sucher-pages/training.css';
import {
  setQuota,
  requestProfileRefetch,
} from '../features/auth/authSlice';
import { getQuota } from '../services/cloudTrainingApi';
import useSupabaseTrainings from '../hooks/useSupabaseTrainings';
import useRefetchOnFocus from '../hooks/useRefetchOnFocus';
import Icon from '../components/icons/Icon';
import useDatasetLocalChanges from '../features/editDataset/hooks/useDatasetLocalChanges';
import DATEN_COPY from '../features/editDataset/datenCopy';
import { moveToPage } from '../features/ui/uiSlice';
import PageType from '../constants/pageType';
import { MEMBER_LIMIT_DE, NO_HOURS_DE, noHoursKind } from '../components/StartTrainingDialog';
import { hoursDe, numberDe } from '../utils/hoursDe';

// Names no provider (RunPod spec §4.11): a training runs on a RunPod GPU or on
// Modal, whichever the API placed it on, so the monitor says „Cloud".
export function statusSubtitle(status) {
  switch (status) {
    case 'queued':
      return 'Wird eingereiht · Cloud';
    case 'running':
      return 'Live · aktualisiert laufend · Cloud';
    case 'succeeded':
      return 'Erfolgreich · Cloud';
    case 'failed':
      return 'Fehlgeschlagen · Cloud';
    case 'canceled':
      return 'Abgebrochen · Cloud';
    default:
      return 'Bereit · Cloud';
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
  const profileLoaded = useSelector((state) => state.auth.profileLoaded);
  const profileError = useSelector((state) => state.auth.profileError);
  const hfUsername = useSelector((state) => state.auth.hfUsername);
  const session = useSelector((state) => state.auth.session);
  const hoursFree = useSelector((state) => state.auth.hoursFree);
  const hoursReserved = useSelector((state) => state.auth.hoursReserved);
  const nextReservationHours = useSelector((state) => state.auth.nextReservationHours);
  const account = useSelector((state) => state.auth.account);
  const memberLimitHours = useSelector((state) => state.auth.memberLimitHours);
  const memberLeftHours = useSelector((state) => state.auth.memberLeftHours);
  const selectedTrainingId = useSelector((state) => state.training.selectedTrainingId);
  const cloudJobsRefreshCounter = useSelector((state) => state.training.cloudJobsRefreshCounter);

  const { jobs, loading, refetch, isRealtime } = useSupabaseTrainings();

  // After "Training starten", TrainingControlPanel dispatches
  // triggerCloudJobsRefresh() which bumps this counter. Pull the new row in via
  // REST immediately so the job card appears instantly — a guaranteed path that
  // does not depend on the Realtime channel having delivered the INSERT yet.
  const prevRefreshCounterRef = useRef(cloudJobsRefreshCounter);
  useEffect(() => {
    if (cloudJobsRefreshCounter === prevRefreshCounterRef.current) return;
    prevRefreshCounterRef.current = cloudJobsRefreshCounter;
    refetch();
  }, [cloudJobsRefreshCounter, refetch]);

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

  // NO page-level "Abmelden" here any more. StudentApp's own chrome — the
  // desktop rail and the mobile header, both rendered on EVERY page — carries
  // one now, so a second control on this page (plus the e-mail the rail already
  // identifies the student with) meant two buttons and the identity twice.

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

  // Since the student login gate (utils/authGate) StudentApp's <main> asks for
  // a login before this page renders, so this is no longer the PRIMARY gate —
  // it is the second line of defence for the one path that gets past it, „Ohne
  // Anmeldung fortfahren". KEEP IT: that path is exactly what it now guards,
  // and the teacher web (WebApp) has no StudentApp gate at all.
  if (!isAuthenticated) {
    return <LoginForm />;
  }

  // Profile load failed in a non-auth way (404 = no public.users row; 5xx /
  // network after retries). The JWT is still valid — DON'T show the login
  // form (that loops). Offer a retry that bumps the shared refetch nonce.
  if (profileError) {
    return (
      <div className="flex items-center justify-center h-full px-4">
        <Card className="max-w-md text-center">
          <h2 className="text-lg font-semibold tracking-tight text-[var(--ink)] mb-2">
            Profil konnte nicht geladen werden
          </h2>
          <p className="text-sm text-[var(--ink-3)] mb-5">{profileError}</p>
          <div className="flex items-center justify-center gap-3">
            <Btn variant="primary" onClick={() => dispatch(requestProfileRefetch())}>
              <Icon name="refresh" /> Erneut versuchen
            </Btn>
          </div>
        </Card>
      </div>
    );
  }

  // Session is live but GET /me hasn't resolved yet — slim spinner, exactly
  // like InferencePage's profileLoaded gate. Without this the hours readout +
  // dataset list rendered with stale zeros before the profile landed.
  if (!profileLoaded) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-[var(--ink-3)] text-lg">Profil wird geladen…</div>
      </div>
    );
  }

  // The hours readout (credit system v2, spec §10.1): the free hours, toned by
  // what a start would reserve now; „—" until the quota has answered.
  const hoursKnown = typeof nextReservationHours === 'number';
  const hoursRead = !hoursKnown ? null
    : nextReservationHours <= 0 ? 'rec' : nextReservationHours <= 2 ? 'warn' : 'ok';
  const hoursCaption = 'Std. frei'
    + (typeof hoursReserved === 'number' && hoursReserved > 0 ? ` · ${numberDe(hoursReserved)} Std. reserviert` : '')
    + (account === 'group' ? ' · Gruppenguthaben' : '');
  const showMemberLimit = typeof memberLimitHours === 'number' && typeof memberLeftHours === 'number';
  const noHours = noHoursKind({ nextReservationHours, hoursFree, memberLimitHours, memberLeftHours });

  const status = selectedJob?.status;
  const subtitle = statusSubtitle(status);
  const pill = statusPill(status);
  // The status light for the old pill tone; a running job breathes.
  const pillLed = status === 'running'
    ? 'sig'
    : pill.tone === 'success' ? 'ok' : pill.tone === 'danger' ? 'rec' : pill.tone === 'amber' ? 'warn' : 'off';

  return (
    <div className="h-full w-full overflow-y-auto" style={{ background: 'var(--bg)' }}>
      <div className="eb-shell su-trn flex flex-col gap-5 md:gap-6">
        {/* Header rail */}
        <SectionHeader
          size="xl"
          eyebrow={<><span aria-hidden="true">03 · </span>Training</>}
          title={<>Modell <em>trainieren</em></>}
          description="Wähle Datensatz und Policy, dann starte das Training in der Cloud."
          className="items-end"
          right={
            <div className="flex items-center gap-3 flex-wrap justify-end">
              <HeartbeatStatus />
              <span className="inline-flex flex-col items-end gap-1">
                <span className="su-read su-read--md su-trn-credits" data-testid="hours-readout">
                  <span className={hoursRead ? `su-read--${hoursRead}` : undefined} data-testid="hours-free">
                    {typeof hoursFree === 'number' ? numberDe(hoursFree) : '—'}
                  </span>
                  <span className="su-trn-credits__text">{hoursCaption}</span>
                </span>
                {showMemberLimit && (
                  <span className="text-[12.5px] text-[var(--ink-2)]">
                    {`Dein Gruppenlimit: noch ${hoursDe(memberLeftHours)}`}
                  </span>
                )}
              </span>
            </div>
          }
        />

        {/* Cloud-login vs HF-token clarification. The single most common
            student confusion: "ich habe den HF-Token gesetzt, warum geht
            Training nicht?" — the HF token (entered once on the Startseite,
            stored with the account) drives recording uploads; cloud Training
            needs THIS separate cloud login. Also surfaces the linked
            Benutzer-ID so a mis-link is visible. */}
        <div
          className={clsx(
            'su-trn-info text-sm leading-snug',
            hfUsername ? 'su-plate su-plate--plate' : 'su-trn-info--warn'
          )}
        >
          {hfUsername && <Led tone="ok" className="su-trn-info__led" />}
          <div className="min-w-0">
            <p>
              Cloud-Training läuft über deine <strong>Cloud-Anmeldung</strong>{' '}
              (angemeldet als {session?.user?.email || 'dein Konto'}) — das
              ist getrennt vom Hugging-Face-Token, das du auf der Startseite
              hinterlegt hast.
            </p>
            {hfUsername ? (
              <p className="mt-1.5">
                Verknüpfte HuggingFace-ID:{' '}
                <span className="font-semibold">{hfUsername}</span> —
                deine Datensätze werden hierüber gefunden.
              </p>
            ) : (
              <p className="mt-1.5">
                Es ist noch kein Hugging-Face-Konto mit deinem Konto verknüpft.
                Hinterlege dein Token auf der Startseite, dann wird es
                automatisch verknüpft und deine Datensätze erscheinen hier.
              </p>
            )}
          </div>
        </div>

        {/* Setup rail */}
        <div>
          <div className="flex items-baseline gap-4 mb-5 flex-wrap">
            <h2 className="su-serif text-[34px] leading-none text-[var(--ink)]">
              Setup
            </h2>
            <span className="su-lab">
              Datensatz · Policy · Ausgabe · Optionen
            </span>
          </div>
          <div className="su-trn-setup grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-x-4 gap-y-8 items-stretch">
            <div className="su-trn-step min-w-0">
              <StepLabel n={1} title="Datensatz" />
              <DatasetSelector />
            </div>
            <div className="su-trn-step min-w-0">
              <StepLabel n={2} title="Policy" />
              <PolicySelector />
            </div>
            <div className="su-trn-step min-w-0">
              <StepLabel n={3} title="Ausgabeordner" />
              <TrainingOutputFolderInput />
            </div>
            <div className="su-trn-step min-w-0">
              <StepLabel n={4} title="Optionen" />
              <TrainingOptionInput />
            </div>
          </div>
        </div>

        {/* Daten 2.0 (owner decision D6, spec §E9): the picked dataset was
            changed here and not uploaded, or lives only on this robot —
            the cloud trains on Hugging Face's version. Silent on anything
            that is not proof. */}
        <DatenWarning />

        {/* Monitor rail (full width): a powered dark screen */}
        <Screen as="section" power className="su-trn-monitor" aria-label="Trainingsverlauf">
          <div className="su-trn-monitor__head">
            <div className="min-w-0">
              <h2 className="su-serif su-trn-monitor__title">
                Trainings<em>verlauf</em>
              </h2>
              <p className="su-lab mt-1">{subtitle}</p>
            </div>
            <div className="flex gap-4 items-center flex-wrap justify-end">
              <span className="su-trn-monitor__status">
                <Led tone={pillLed} pulse={status === 'running' ? 'breathe' : 'none'} />
                <span className="su-lab">{pill.label}</span>
              </span>
              <TrainingControlPanel />
            </div>
          </div>
          <TrainingLiveChart
            jobs={jobs}
            isRealtime={isRealtime}
            nextReservationHours={nextReservationHours}
            onContinued={refetchQuota}
          />
          {noHours && (
            <div className="su-trn-monitor__nocredit" role="status">
              {noHours === 'member_limit' ? MEMBER_LIMIT_DE : NO_HOURS_DE}
            </div>
          )}
        </Screen>

        {/* My Models */}
        <Plate className="su-trn-models">
          <MyModels
            jobs={jobs}
            loading={loading}
            refetch={refetch}
            isRealtime={isRealtime}
            nextReservationHours={nextReservationHours}
            onContinued={refetchQuota}
            onHoursChanged={refetchQuota}
          />
        </Plate>
      </div>
    </div>
  );
}

function DatenWarning() {
  const dispatch = useDispatch();
  const repoId = useSelector((state) => state.training.trainingInfo.datasetRepoId);
  const warning = useDatasetLocalChanges(repoId);
  if (!warning) return null;
  return (
    <div
      className="flex items-center gap-3 flex-wrap rounded-[18px] px-4 py-3 text-sm leading-snug bg-[var(--amber-wash)] text-[color:var(--amber)]"
      role="status"
      data-daten-warning={warning}
    >
      <Icon name="warning" size={16} />
      <span className="flex-1 min-w-[200px]">
        {warning === 'local' ? DATEN_COPY.train.localWarning : DATEN_COPY.train.changedWarning}
      </span>
      {warning === 'changed' ? (
        <Btn variant="ghost" onClick={() => dispatch(moveToPage(PageType.EDIT_DATASET))}>
          {DATEN_COPY.train.toDaten}
        </Btn>
      ) : null}
    </div>
  );
}

// A dark disc with the step's number on the plate's top edge; the step title
// stays for screen readers.
function StepLabel({ n, title }) {
  return (
    <div className="su-trn-steplabel">
      <span className="su-trn-steplabel__disc" aria-hidden="true">{n}</span>
      <span className="su-sr">{title}</span>
    </div>
  );
}
