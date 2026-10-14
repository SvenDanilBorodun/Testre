// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// A dataset's sync badge (spec §C3, §J.6): the label, the tone and, for a
// running job, its loader icon come from model/syncBadge.js; the
// „Aktualisieren" link beside an `unknown` badge asks Hugging Face again
// (never for a partner's `not_visible`). Nothing is disabled because of
// `unknown` (R-4).

import React from 'react';
import Icon from '../icons/Icon';
import { Led } from '../sucher';
import COPY from '../../features/editDataset/datenCopy';
import { syncBadge } from '../../features/editDataset/model/syncBadge';

const TONE_CLASS = {
  ok: 'dat-badge-ok',
  warn: 'dat-badge-warn',
  neutral: 'dat-badge-neutral',
  sky: 'dat-badge-sky',
  rec: 'dat-badge-rec',
  busy: 'dat-badge-busy',
  danger: 'dat-badge-danger',
  muted: 'dat-badge-muted',
};

// Sucher: the badge's tone as a light (current ok, changed warn, online/newer
// info, conflict rec, local off); a running job keeps its loader icon.
const TONE_LED = {
  ok: 'ok', warn: 'warn', neutral: 'off', sky: 'info', rec: 'rec', busy: 'sig', danger: 'rec', muted: 'off',
};

export default function SyncBadge({
  state, reason = null, overlay = null, ownerName = '', onRefresh = null, refreshDisabled = false,
}) {
  const b = syncBadge({ state, reason, overlay, ownerName });
  return (
    <>
      <span className={`dat-badge ${TONE_CLASS[b.tone] || 'dat-badge-neutral'}`} title={b.tip || undefined} data-sync={b.key}>
        {b.spin
          ? <Icon name={b.icon} size={14} className="animate-spin" />
          : <Led tone={TONE_LED[b.tone] || 'off'} pulse={b.tone === 'rec' ? 'blink' : 'none'} size={8} />}
        {b.label}
      </span>
      {b.link && onRefresh ? (
        <button type="button" className="dat-badge-link" onClick={onRefresh} disabled={refreshDisabled} title={b.tip}>
          {COPY.sync.refresh}
        </button>
      ) : null}
    </>
  );
}
