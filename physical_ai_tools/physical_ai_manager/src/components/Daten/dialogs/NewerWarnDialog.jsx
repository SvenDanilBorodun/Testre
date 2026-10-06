// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Erst die neuere Version laden?" (owner decision D8, the mockup's
// newerWarn): editing a local copy that is OLDER than the one on Hugging Face
// would lose the newer episodes at the next upload. „Neuere Version laden" is
// the default; „Trotzdem hier bearbeiten" goes on (asked once per dataset).

import React from 'react';
import Dialog from './Dialog';
import Fill from '../Fill';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { fill, fmtDate, plural } from '../../../features/editDataset/model/format';

export default function NewerWarnDialog({ card, onPull, onAnyway, onClose }) {
  const local = card.local || {};
  const hub = card.hub || {};
  const name = local.display_name || card.name;
  const known = hub.total_episodes !== undefined && hub.total_episodes !== null;
  return (
    <Dialog title={COPY.newer.title} icon="cloudDownload" iconTone="sky" onClose={onClose}>
      <p>
        {known ? (
          <Fill
            template={COPY.newer.body}
            values={{
              name,
              bold: <b>{plural(hub.total_episodes, COPY.count.episodeOne, COPY.count.episodeMany)}</b>,
              date: fmtDate(hub.last_modified),
              m: local.total_episodes ?? '–',
            }}
          />
        ) : fill(COPY.newer.bodyShort, { name })}
      </p>
      <p>{COPY.newer.loses}</p>
      <div className="dat-acts">
        <button type="button" className="dat-btn" onClick={(e) => { releasePointerFocus(e); onAnyway(); }}>
          {COPY.newer.anyway}
        </button>
        <button type="button" className="dat-btn dat-btn-primary" data-autofocus onClick={(e) => { releasePointerFocus(e); onPull(); }}>
          {COPY.newer.load}
        </button>
      </div>
    </Dialog>
  );
}
