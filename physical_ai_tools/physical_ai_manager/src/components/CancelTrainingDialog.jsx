// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// „Training abbrechen?" (credit system v2, spec §10.2). Before the student
// confirms, the dialog asks the API what a stop would do NOW
// (`GET /trainings/{id}/stop-preview`, advisory): nothing charged while no GPU
// has trained; after that the started hours are charged and the newest ready
// checkpoint is published (continuable), or, without one, the training is
// canceled without a model. A failed preview still lets the student decide.

import React, { useEffect, useState } from 'react';
import { Key } from './sucher';
import Icon from './icons/Icon';
import { TrainingDialog } from './StartTrainingDialog';
import { getStopPreview } from '../services/cloudTrainingApi';
import { hoursDe, numberDe } from '../utils/hoursDe';

export const CANCEL_DE = {
  title: 'Training abbrechen?',
  loading: 'Stand wird abgefragt …',
  notTrained: (r) =>
    `Das Training hat noch nicht begonnen. Es wird nichts berechnet – reserviert waren ${hoursDe(r)}, alles geht zurück an dein Guthaben.`,
  publish: (step, c, b) =>
    `Der GPU-Server wird gestoppt und der letzte Zwischenstand (Schritt ${numberDe(step)}) als Modell veröffentlicht. Berechnet: ${hoursDe(c)}. Zurück an dein Guthaben: ${hoursDe(b)}.`,
  noCheckpoint: (nextStep, c, b) =>
    `Der GPU-Server wird gestoppt. Es gibt noch keinen Zwischenstand (der nächste wird bei Schritt ${numberDe(nextStep)} gespeichert), also wird kein Modell veröffentlicht. Berechnet: ${hoursDe(c)}. Zurück an dein Guthaben: ${hoursDe(b)}.`,
  previewFailed:
    'Der Stand konnte nicht abgefragt werden. Wenn das Training schon auf einer GPU läuft, wird die angefangene Trainingszeit berechnet.',
  confirm: 'Ja, abbrechen',
  keepTraining: 'Weiter trainieren',
  publishing: (step) => `Training gestoppt – der Zwischenstand bei Schritt ${numberDe(step)} wird veröffentlicht.`,
  canceled: (c) => `Training abgebrochen. Berechnet: ${hoursDe(c ?? 0)}.`,
  cancelRequested: 'Der Abbruch wird an den GPU-Server übermittelt …',
};

/**
 * The preview's sentence: one of the four the spec names. `preview` is the
 * API's answer, or null when the request failed (or answered something that
 * is not a preview).
 */
export function previewSentence(preview) {
  if (!preview || typeof preview !== 'object' || typeof preview.trained !== 'boolean') {
    return CANCEL_DE.previewFailed;
  }
  if (!preview.trained) return CANCEL_DE.notTrained(preview.reserved_hours);
  if (preview.outcome === 'publish') {
    return CANCEL_DE.publish(preview.checkpoint_step, preview.charged_hours_now, preview.hours_back_now);
  }
  return CANCEL_DE.noCheckpoint(preview.next_checkpoint_step, preview.charged_hours_now, preview.hours_back_now);
}

/** The toast after `POST /trainings/cancel`, from its `outcome` (spec §8.2/§10.2). */
export function cancelResultToast(res) {
  switch (res?.outcome) {
    case 'publishing':
      return CANCEL_DE.publishing(res.checkpoint_step);
    case 'canceled':
    case 'failed':
      return CANCEL_DE.canceled(res.charged_hours);
    case 'cancel_requested':
      return CANCEL_DE.cancelRequested;
    default:
      // An answer without an outcome (an older API).
      return 'Training abgebrochen';
  }
}

export default function CancelTrainingDialog({ token, trainingId, busy = false, onConfirm, onClose }) {
  // 'loading' → the request is out; then the preview, or null when it failed.
  const [preview, setPreview] = useState('loading');

  useEffect(() => {
    let alive = true;
    setPreview('loading');
    getStopPreview(token, trainingId)
      .then((p) => { if (alive) setPreview(p && typeof p === 'object' ? p : null); })
      .catch(() => { if (alive) setPreview(null); });
    return () => { alive = false; };
  }, [token, trainingId]);

  const loading = preview === 'loading';
  return (
    <TrainingDialog
      title={CANCEL_DE.title}
      locked={busy}
      onClose={onClose}
      testId="cancel-training-dialog"
      actions={(
        <>
          <Key variant="default" onClick={onClose} disabled={busy} data-autofocus="">
            {CANCEL_DE.keepTraining}
          </Key>
          <Key variant="danger" icon="cancel" onClick={onConfirm} disabled={busy || loading}>
            {CANCEL_DE.confirm}
          </Key>
        </>
      )}
    >
      {loading ? (
        <p role="status" className="inline-flex items-center gap-2">
          <Icon name="loading" size={16} className="animate-spin" />
          {CANCEL_DE.loading}
        </p>
      ) : (
        <p role="status">{previewSentence(preview)}</p>
      )}
    </TrainingDialog>
  );
}
