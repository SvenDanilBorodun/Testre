// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The end of a session (spec §3.11) and „Diese Sitzung" (§3.10), as the
// FinishCard and SessionCard draw them. Pure.
//
// finishSteps → {visible, state, eyebrow, title, note, steps, barPct, savedAs, hint, actions}
//   steps: [{key: 'finalize'|'upload'|'register', label, state, detail, pct}]
//   state: 'done' | 'now' | 'failed' | 'skipped' | 'unknown' | '' (still to come);
//   step 3 is 'failed' / 'skipped' when the cloud registration failed / was
//   skipped after a successful upload (V2-5)
// sessionView → {title, chip, rows, emptyText, note, sum, raw}

import { datasetRepoId, safeTaskName } from '../../../utils/datasetName';
import { formatMinSec } from '../../../utils/recordTaskInfo';
import RECORD_COPY, { clockDe } from './recordCopy';

/** No upload status this long after the end (and the link was fine): it never began. */
export const UPLOAD_START_GRACE_MS = 15000;

const F = RECORD_COPY.finish;
const S = RECORD_COPY.session;

const HIDDEN = Object.freeze({
  visible: false, state: 'idle', eyebrow: '', title: '', note: '', steps: [], barPct: null,
  savedAs: null, hint: '', actions: [],
});

function uploadLabel(privateMode) {
  if (privateMode === true) return F.stepUploadPrivate;
  if (privateMode === false) return F.stepUploadPublic;
  return F.stepUpload;
}

/** The dataset's id as the robot wrote it (the upload id, or the local name). */
export function datasetIdOf(session) {
  const f = session?.finish || {};
  if (f.repoId) return f.repoId;
  if (f.expectedRepoId) return f.expectedRepoId;
  const snap = session?.snapshot || {};
  if (snap.userId) return datasetRepoId(snap.userId, snap.robotType, snap.taskName);
  if (snap.taskName) return `${snap.robotType || ''}_${safeTaskName(snap.taskName)}`;
  return '';
}

const step = (key, label, state = '', detail = '', pct = '') => ({ key, label, state, detail, pct });
const NEW = () => ({ id: 'newRecording', label: F.newRecording, variant: 'ghost' });
const TRAINING = () => ({ id: 'toTraining', label: F.toTraining, variant: 'primary' });

/** The finish card for `session` (§3.11 table). */
export function finishSteps(session, { nowWallMs = Date.now(), heartbeat = 'connected' } = {}) {
  const f = session?.finish;
  if (!f || f.state === 'idle' || f.state === 'nothing' || f.dismissed) return HIDDEN;
  const k = Number(session.savedCount) || 0;
  const privateMode = session.snapshot ? session.snapshot.privateMode : null;
  const upLabel = uploadLabel(privateMode);
  const base = { visible: true, state: f.state, note: f.endNote || '', barPct: null, savedAs: null, hint: '', actions: [] };
  const localSaved = { label: F.savedLocal, repoId: datasetIdOf(session) };

  switch (f.state) {
    case 'finalizing':
      return {
        ...base,
        eyebrow: F.eyebrowRunning,
        title: F.titleRunning(k),
        steps: [step('finalize', F.stepFinalize, 'now'), step('upload', upLabel), step('register', F.stepRegister)],
      };

    case 'uploading': {
      const running = { ...base, eyebrow: F.eyebrowRunning, title: F.titleRunning(k), actions: [NEW()] };
      if (f.linkLost) {
        return {
          ...running,
          steps: [step('finalize', F.stepFinalize, 'done'), step('upload', upLabel, 'unknown', F.unknown),
            step('register', F.stepRegister)],
        };
      }
      const neverBegan = heartbeat === 'connected' && !f.repoId && !(f.uploadPct > 0)
        && Number.isFinite(f.endedAt) && nowWallMs - f.endedAt >= UPLOAD_START_GRACE_MS;
      if (neverBegan) {
        return {
          ...running,
          eyebrow: F.eyebrowDone,
          title: F.titleFailed,
          steps: [step('finalize', F.stepFinalize, 'done'), step('upload', upLabel, 'failed', F.notStarted),
            step('register', F.stepRegister)],
          savedAs: localSaved,
        };
      }
      const pct = Math.max(0, Math.min(100, Math.round(Number(f.uploadPct) || 0)));
      return {
        ...running,
        steps: [step('finalize', F.stepFinalize, 'done'), step('upload', upLabel, 'now', '', F.pct(pct)),
          step('register', F.stepRegister)],
        barPct: pct,
        hint: F.background,
      };
    }

    case 'registering':
      return {
        ...base,
        eyebrow: F.eyebrowRunning,
        title: F.titleRunning(k),
        steps: [step('finalize', F.stepFinalize, 'done'), step('upload', upLabel, 'done'),
          step('register', F.stepRegister, 'now')],
        hint: F.background,
        actions: [NEW()],
      };

    case 'done': {
      const label = privateMode === false ? F.savedPublic : privateMode === true ? F.savedPrivate : F.saved;
      const registerFailed = f.registerState === 'failed' || f.registerState === 'skipped';
      return {
        ...base,
        eyebrow: F.eyebrowDone,
        title: F.titleDone,
        steps: [step('finalize', F.stepFinalize, 'done'), step('upload', upLabel, 'done'),
          // V2-5: a registration that failed or was skipped is not a green check
          step('register', F.stepRegister, registerFailed ? f.registerState : 'done')],
        savedAs: { label, repoId: datasetIdOf(session) },
        hint: registerFailed ? F.registerHint : '',
        actions: [TRAINING(), NEW()],
      };
    }

    case 'upload_failed':
      return {
        ...base,
        eyebrow: F.eyebrowDone,
        title: F.titleFailed,
        steps: [step('finalize', F.stepFinalize, 'done'),
          step('upload', upLabel, 'failed', [f.message, F.later].filter(Boolean).join(' ')),
          step('register', F.stepRegister)],
        savedAs: localSaved,
        actions: [NEW()],
      };

    case 'finalize_failed': {
      // V1-2: the dataset on disk is incomplete — nothing was uploaded and
      // nothing is „bereit".
      const hub = session.snapshot ? session.snapshot.pushToHub !== false : true;
      return {
        ...base,
        eyebrow: F.eyebrowFinalizeFailed,
        title: F.titleFinalizeFailed,
        steps: [
          step('finalize', F.stepFinalize, 'failed', f.message || ''),
          hub ? step('upload', upLabel) : step('upload', F.stepUpload, 'skipped', F.uploadOff),
          step('register', F.stepRegister, hub ? '' : 'skipped'),
        ],
        actions: [NEW()],
      };
    }

    case 'local_done':
      return {
        ...base,
        eyebrow: F.eyebrowDone,
        title: F.titleDone,
        steps: [step('finalize', F.stepFinalize, 'done'), step('upload', F.stepUpload, 'skipped', F.uploadOff),
          step('register', F.stepRegister, 'skipped')],
        savedAs: localSaved,
        actions: [NEW()],
      };

    case 'stopped_error':
      return {
        ...base,
        eyebrow: RECORD_COPY.pill.stopped,
        title: F.titleStopped,
        steps: [step('finalize', F.stepFinalize, 'failed', session.errorText || ''), step('upload', upLabel),
          step('register', F.stepRegister)],
        actions: [NEW()],
      };

    default:
      return HIDDEN;
  }
}

/** The „Diese Sitzung" card: rows newest first, the chip and the sum line. */
export function sessionView(session, { numEpisodes = 0, nowWallMs = Date.now() } = {}) {
  const s = session || {};
  const episodes = Array.isArray(s.episodes) ? s.episodes : [];
  const rows = episodes.map((e, i) => {
    const saved = e.outcome === 'saved';
    const hhmm = clockDe(e.wallMs);
    return {
      key: `${s.id || 0}:${i}`,
      num: saved ? String(e.n) : '–',
      title: saved ? S.episode(e.n) : (S.outcome[e.outcome] || S.outcome.redo),
      sub: saved ? (e.early ? S.savedEarly(hhmm) : S.saved(hhmm)) : S.discardedSub(e.n, hhmm),
      duration: formatMinSec(e.durationS),
      discarded: !saved,
      outcome: e.outcome,
      isNew: Number.isFinite(e.wallMs) && nowWallMs - e.wallMs < 1500,
    };
  }).reverse();
  const savedRows = episodes.filter((e) => e.outcome === 'saved');
  const k = savedRows.length;
  const total = Number(s.snapshot?.numEpisodes) || Number(numEpisodes) || 0;
  const savedTotal = Math.max(Number(s.savedCount) || 0, k);
  return {
    title: S.title,
    chip: { text: S.chip(k), ok: k > 0 },
    rows,
    emptyText: S.empty,
    note: s.adopted && s.adoptedSavedCount > 0 ? S.adopted(s.adoptedSavedCount) : '',
    sum: {
      left: S.sumLeft(savedTotal, total),
      right: S.sumRight(formatMinSec(savedRows.reduce((acc, e) => acc + (Number(e.durationS) || 0), 0))),
    },
    raw: session,
  };
}
