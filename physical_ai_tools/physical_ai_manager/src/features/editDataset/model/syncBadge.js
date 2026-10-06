// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// A card's sync badge (spec §C3 „Badges", §J.6 additions, F-10), pure: the
// seven sync states, the busy overlays from /edubotics/daten_state and the
// neutral „Wird aktualisiert …" of a card waiting for its re-fetch (T-1 a) →
// label, icon, tone, tooltip, and whether „Aktualisieren" belongs beside it.
// Nothing is disabled because of `unknown` (R-4).

import COPY from '../datenCopy';
import { SYNC_STATES } from '../datenContract';
import { fill } from './format';

const STATES = Object.freeze({
  local: { label: COPY.sync.localLabel, tip: COPY.sync.localTip, icon: 'hardDrive', tone: 'neutral' },
  online: { label: COPY.sync.onlineLabel, tip: COPY.sync.onlineTip, icon: 'cloud', tone: 'sky' },
  current: { label: COPY.sync.currentLabel, tip: COPY.sync.currentTip, icon: 'cloudSynced', tone: 'ok' },
  changed: { label: COPY.sync.changedLabel, tip: COPY.sync.changedTip, icon: 'cloudUpload', tone: 'warn' },
  newer: { label: COPY.sync.newerLabel, tip: COPY.sync.newerTip, icon: 'cloudDownload', tone: 'sky' },
  conflict: { label: COPY.sync.conflictLabel, tip: COPY.sync.conflictTip, icon: 'syncConflict', tone: 'danger' },
  unknown: { label: COPY.sync.unknownLabel, tip: '', icon: 'syncUnknown', tone: 'muted' },
});

const OVERLAYS = Object.freeze({
  record: { label: COPY.sync.recordLabel, tip: COPY.sync.recordTip, icon: 'liveRecording', tone: 'rec', spin: false },
  upload: { label: COPY.sync.uploadLabel, tip: '', icon: 'loading', tone: 'busy', spin: true },
  download: { label: COPY.sync.downloadLabel, tip: '', icon: 'loading', tone: 'busy', spin: true },
  edit: { label: COPY.sync.editLabel, tip: '', icon: 'loading', tone: 'busy', spin: true },
  delete: { label: COPY.sync.editLabel, tip: '', icon: 'loading', tone: 'busy', spin: true },
  keep_both: { label: COPY.sync.keepBothLabel, tip: '', icon: 'loading', tone: 'busy', spin: true },
  refreshing: { label: COPY.sync.refreshingLabel, tip: '', icon: 'loading', tone: 'muted', spin: true },
});

/** The reasons whose badge carries the „Aktualisieren" link (a partner's `not_visible` does not). */
export const REFRESHABLE_REASONS = Object.freeze(['not_asked', 'unreachable']);

/**
 * @param {{state?: string, reason?: string|null, overlay?: string|null, ownerName?: string}} input
 * @returns {{key: string, label: string, icon: string, tone: string, tip: string, link: boolean, spin: boolean}}
 */
export function syncBadge({ state, reason = null, overlay = null, ownerName = '' } = {}) {
  if (overlay && Object.prototype.hasOwnProperty.call(OVERLAYS, overlay)) {
    return { key: overlay, link: false, ...OVERLAYS[overlay] };
  }
  const s = SYNC_STATES.includes(state) ? state : 'unknown';
  const base = STATES[s];
  if (s !== 'unknown') return { key: s, link: false, spin: false, ...base };
  const r = Object.prototype.hasOwnProperty.call(COPY.sync.unknownTip, reason) ? reason : 'not_asked';
  return {
    key: s,
    ...base,
    spin: false,
    tip: fill(COPY.sync.unknownTip[r], { name: ownerName || '' }),
    link: REFRESHABLE_REASONS.includes(r),
  };
}
