// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Hochladen" (owner decision D4, the mockup's confirmUpload, spec §E2, N7):
// the dialog asks the robot what Hugging Face holds (`hubstate`) and says it
// before anything is written.
//   - no dataset there yet: „„…“ hochladen?" and the visibility the new repo
//     gets (the recorded choice, else public: `new_repo_private`), sent as
//     `expected_hub_sha: null` — the robot refuses if one appeared meanwhile;
//   - one there: both versions side by side, „Ersetzen und hochladen" sending
//     the head the dialog showed; a NEWER hub copy says what is lost; on a
//     conflict „Beide behalten" comes first and is the default (§E10);
//   - Hugging Face could not be asked: the upload decides at upload time (no
//     `expected_hub_sha` key at all).

import React from 'react';
import Dialog from './Dialog';
import ConflictChoices from './ConflictChoices';
import useHubState from './useHubState';
import Icon from '../../icons/Icon';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { fill, fmtDate, fmtTime, plural } from '../../../features/editDataset/model/format';

const eps = (n) => plural(n ?? 0, COPY.count.episodeOne, COPY.count.episodeMany);

export default function UploadCompareDialog({
  card, fetchHubState, onUpload, onKeepBoth, onPull, onClose,
}) {
  const { status, data } = useHubState(fetchHubState, card.id);
  const name = (card.local && card.local.display_name) || card.name;
  const hub = data && data.hub;
  const local = (data && data.local) || {};
  const syncState = (data && data.sync && data.sync.state) || (card.sync && card.sync.state);
  const privateNew = !!(data && data.new_repo_private);
  const run = (fn) => (e) => { releasePointerFocus(e); fn(); };

  if (status === 'loading') {
    return (
      <Dialog focusKey={status} title={fill(COPY.upload.newTitle, { name })} icon="cloudUpload" iconTone="accent" onClose={onClose}>
        <p className="dat-small"><Icon name="loading" size={14} className="animate-spin" /> {COPY.upload.checking}</p>
        <div className="dat-acts">
          <button type="button" className="dat-btn" data-autofocus onClick={run(onClose)}>{COPY.tool.cancel}</button>
        </div>
      </Dialog>
    );
  }

  // Not on Hugging Face yet (or it could not be asked): one plain confirm.
  if (!hub || !hub.exists) {
    const unknown = status !== 'ready' || !hub;
    return (
      <Dialog focusKey={status} title={fill(COPY.upload.newTitle, { name })} icon="cloudUpload" iconTone="accent" onClose={onClose}>
        {unknown ? null : (
          <p data-visibility={privateNew ? 'private' : 'public'}>
            {privateNew ? COPY.upload.visibilityPrivate : COPY.upload.visibilityPublic}
          </p>
        )}
        <div className="dat-acts">
          <button type="button" className="dat-btn" data-autofocus onClick={run(onClose)}>{COPY.tool.cancel}</button>
          <button
            type="button"
            className="dat-btn dat-btn-primary"
            onClick={run(() => onUpload(unknown
              ? { private: privateNew }
              : { expected_hub_sha: null, private: privateNew }))}
          >
            <Icon name="cloudUpload" size={16} />
            {COPY.upload.newButton}
          </button>
        </div>
      </Dialog>
    );
  }

  const head = hub.head || null;
  const conflict = syncState === 'conflict';
  return (
    <Dialog focusKey={status} title={COPY.upload.compareTitle} icon="cloudUpload" iconTone="accent" onClose={onClose} wide={conflict}>
      <div className="dat-cmp">
        <div>
          <small><Icon name="cloud" size={14} />{COPY.upload.onHub}</small>
          <b>{eps(hub.total_episodes)}</b>
          <span>{fill(COPY.upload.onHubMeta, { dur: fmtTime(hub.duration_s || 0, false), date: fmtDate(hub.last_modified) })}</span>
        </div>
        <div className="dat-now">
          <small><Icon name="hardDrive" size={14} />{COPY.upload.here}</small>
          <b>{eps(local.total_episodes)}</b>
          <span>{fill(COPY.upload.hereMeta, { dur: fmtTime(local.duration_s || 0, false), date: fmtDate(local.modified_at) })}</span>
        </div>
      </div>
      {conflict ? (
        <>
          <p>{COPY.banner.conflict}</p>
          <ConflictChoices
            inDialog
            onKeepBoth={() => onKeepBoth(head)}
            onLoadOnline={() => onPull(head)}
            onUploadHere={() => onUpload({ expected_hub_sha: head, private: privateNew })}
          />
          <div className="dat-acts">
            <button type="button" className="dat-btn" onClick={run(onClose)}>{COPY.tool.cancel}</button>
          </div>
        </>
      ) : (
        <>
          <p>{COPY.upload.replaces}</p>
          {syncState === 'newer' ? (
            <p style={{ color: 'var(--danger-ink)' }}><b>{COPY.upload.attention}</b> {COPY.upload.newerLoses}</p>
          ) : null}
          <div className="dat-acts">
            <button type="button" className="dat-btn" data-autofocus onClick={run(onClose)}>{COPY.tool.cancel}</button>
            <button
              type="button"
              className="dat-btn dat-btn-primary"
              onClick={run(() => onUpload({ expected_hub_sha: head, private: privateNew }))}
            >
              {COPY.upload.replaceButton}
            </button>
          </div>
        </>
      )}
    </Dialog>
  );
}
