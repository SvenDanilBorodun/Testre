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
// `hubFacts` ({total_episodes, last_modified} from the page's hubstate read,
// V2-15) gives the mockup's numbers; without them the short sentence.

import React from 'react';
import Dialog from './Dialog';
import Fill from '../Fill';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { fill, fmtDate, plural } from '../../../features/editDataset/model/format';

export default function NewerWarnDialog({
  card, hubFacts = null, onPull, onAnyway, onClose,
}) {
  const local = card.local || {};
  const hub = hubFacts || {};
  const name = local.display_name || card.name;
  // every number of the long sentence, or the short one — never a „–" in it
  const isCount = (v) => v !== undefined && v !== null && Number.isFinite(Number(v));
  const known = isCount(hub.total_episodes) && isCount(local.total_episodes) && fmtDate(hub.last_modified) !== '–';
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
              m: local.total_episodes,
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
