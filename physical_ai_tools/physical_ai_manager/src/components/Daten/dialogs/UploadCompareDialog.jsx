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
//
// „Not there yet" is a FACT of the reply, never the absence of one (V2-2): the
// robot answers a never-uploaded dataset with `hub: null` and its decision
// `local`; `hub: null` with any other decision (not asked, unreachable — and
// then `changed` when the copy here has changes of its own) means it could not
// ask. See `hubKnowledge`.

import React from 'react';
import Dialog from './Dialog';
import ConflictChoices from './ConflictChoices';
import useHubState from './useHubState';
import Icon from '../../icons/Icon';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { keepBothTip } from '../../../features/editDataset/model/cardModel';
import {
  fill, fmtDate, fmtKnown, fmtTime, knownNumber, plural,
} from '../../../features/editDataset/model/format';

// An unknown count or duration is „–", never 0 (T2-5).
const eps = (n) => (knownNumber(n) === null ? '–' : plural(n, COPY.count.episodeOne, COPY.count.episodeMany));
const dur = (s) => fmtKnown(s, (v) => fmtTime(v, false));

/**
 * What one `hubstate` reply (§J.4.4) proves about Hugging Face:
 *   'present'  a dataset is there (`hub.exists`);
 *   'absent'   nothing is there yet — an empty repo (`hub.exists` false), or
 *              `hub: null` with the robot's decision `local` (the token's own
 *              namespace listed and the repo not in it);
 *   'unknown'  the reply did not arrive, or the robot could not ask (`hub: null`
 *              with `unknown`, or `changed` decided without the hub).
 */
export function hubKnowledge(status, data) {
  if (status !== 'ready' || !data || typeof data !== 'object') return 'unknown';
  const hub = data.hub;
  if (hub && typeof hub === 'object') return hub.exists ? 'present' : 'absent';
  return data.sync && data.sync.state === 'local' ? 'absent' : 'unknown';
}

export default function UploadCompareDialog({
  card, fetchHubState, onUpload, onKeepBoth, onPull, onClose,
}) {
  const { status, data } = useHubState(fetchHubState, card.id);
  const name = (card.local && card.local.display_name) || card.name;
  const hub = data && data.hub;
  const local = (data && data.local) || {};
  const syncState = (data && data.sync && data.sync.state) || (card.sync && card.sync.state);
  const privateNew = !!(data && data.new_repo_private);
  const knowledge = hubKnowledge(status, data);
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
  if (knowledge !== 'present') {
    const unknown = knowledge === 'unknown';
    // An empty repo that already exists keeps its own visibility (create_repo
    // never changes it, §E2 step 2); a new one gets the recorded choice (N7).
    const willBePrivate = hub && typeof hub.private === 'boolean' ? hub.private : privateNew;
    return (
      <Dialog focusKey={status} title={fill(COPY.upload.newTitle, { name })} icon="cloudUpload" iconTone="accent" onClose={onClose}>
        {unknown ? null : (
          <p data-visibility={willBePrivate ? 'private' : 'public'}>
            {willBePrivate ? COPY.upload.visibilityPrivate : COPY.upload.visibilityPublic}
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
          <span>{fill(COPY.upload.onHubMeta, { dur: dur(hub.duration_s), date: fmtDate(hub.last_modified) })}</span>
        </div>
        <div className="dat-now">
          <small><Icon name="hardDrive" size={14} />{COPY.upload.here}</small>
          <b>{eps(local.total_episodes)}</b>
          <span>{fill(COPY.upload.hereMeta, { dur: dur(local.duration_s), date: fmtDate(local.modified_at) })}</span>
        </div>
      </div>
      {conflict ? (
        <>
          <p>{COPY.banner.conflict}</p>
          <ConflictChoices
            inDialog
            tip={keepBothTip(card)}
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
