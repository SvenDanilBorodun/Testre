// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Neuere Version laden?" (the mockup's pullNewer, spec §E3, §G14 T-1 b) —
// „Online-Version laden?" when it is opened for a conflict or a crashed card
// (`online`), whose hub copy is different, not newer (V2-15): the
// two versions side by side, and — when the copy here has changes of its own
// (changed, conflict, or a crashed session's leftovers) — „Deine Änderungen
// von hier gehen dabei verloren." The download sends the head the dialog
// SHOWED as its `revision` and the card's `meta_digest`: the robot refuses
// (`stale`) when the copy here changed since the card was drawn (T-1 c). On a
// conflict „Beide behalten" comes first and is the default (§E10).

import React from 'react';
import Dialog from './Dialog';
import ConflictChoices from './ConflictChoices';
import useHubState from './useHubState';
import Icon from '../../icons/Icon';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { fmtDate, knownNumber, plural } from '../../../features/editDataset/model/format';

// An unknown count is „–", never 0 (T2-5).
const eps = (n) => (knownNumber(n) === null ? '–' : plural(n, COPY.count.episodeOne, COPY.count.episodeMany));

export default function PullNewerDialog({
  card, crashed = false, online = false, partnerNote = null, fetchHubState, onPull, onKeepBoth, onUpload, onClose,
}) {
  const { status, data } = useHubState(fetchHubState, card.id);
  const hub = data && data.hub;
  const local = (data && data.local) || {};
  const syncState = (data && data.sync && data.sync.state) || (card.sync && card.sync.state);
  const conflict = syncState === 'conflict' && !crashed;
  const losesHere = crashed || syncState === 'changed' || syncState === 'conflict';
  const neutral = online || crashed || syncState === 'conflict';
  const title = neutral ? COPY.pull.titleOnline : COPY.pull.title;
  const run = (fn) => (e) => { releasePointerFocus(e); fn(); };

  if (status === 'loading') {
    return (
      <Dialog focusKey={status} title={title} icon="cloudDownload" iconTone="sky" onClose={onClose}>
        <p className="dat-small"><Icon name="loading" size={14} className="animate-spin" /> {COPY.upload.checking}</p>
        <div className="dat-acts">
          <button type="button" className="dat-btn" data-autofocus onClick={run(onClose)}>{COPY.tool.cancel}</button>
        </div>
      </Dialog>
    );
  }
  if (!hub || !hub.exists || !hub.head) {
    return (
      <Dialog focusKey={status} title={title} icon="cloudDownload" iconTone="sky" onClose={onClose}>
        <p>{status === 'ready' ? COPY.fetch.notFound : COPY.fetch.unreachable}</p>
        <div className="dat-acts">
          <button type="button" className="dat-btn" data-autofocus onClick={run(onClose)}>{COPY.tool.cancel}</button>
        </div>
      </Dialog>
    );
  }
  const head = hub.head;
  return (
    <Dialog focusKey={status} title={title} icon="cloudDownload" iconTone="sky" onClose={onClose} wide={conflict}>
      <div className="dat-cmp">
        <div>
          <small><Icon name="hardDrive" size={14} />{COPY.pull.here}</small>
          <b>{eps(local.total_episodes)}</b>
          <span>{fmtDate(local.modified_at)}</span>
        </div>
        <div className="dat-now">
          <small><Icon name="cloud" size={14} />{COPY.pull.onHub}</small>
          <b>{eps(hub.total_episodes)}</b>
          <span>{fmtDate(hub.last_modified)}</span>
        </div>
      </div>
      <p>{neutral ? COPY.pull.replacesOnline : COPY.pull.replaces}</p>
      {losesHere && !conflict ? <p data-loses-here=""><b>{COPY.conflict.losesHere}</b></p> : null}
      {conflict ? (
        <>
          <ConflictChoices
            inDialog
            partnerNote={partnerNote}
            onKeepBoth={() => onKeepBoth(head)}
            onLoadOnline={() => onPull(head)}
            onUploadHere={() => onUpload(head, !!(data && data.new_repo_private))}
          />
          <div className="dat-acts">
            <button type="button" className="dat-btn" onClick={run(onClose)}>{COPY.tool.cancel}</button>
          </div>
        </>
      ) : (
        <div className="dat-acts">
          <button type="button" className="dat-btn" data-autofocus onClick={run(onClose)}>{COPY.tool.cancel}</button>
          <button type="button" className="dat-btn dat-btn-primary" onClick={run(() => onPull(head))}>{COPY.pull.button}</button>
        </div>
      )}
    </Dialog>
  );
}
