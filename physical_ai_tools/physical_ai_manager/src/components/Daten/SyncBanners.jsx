// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The player's sync banners (spec §C3, §E10, the mockup's banners): a NEWER
// hub copy („Lade sie, bevor du hier etwas löschst …"), local changes not yet
// uploaded (the training still uses the old version), and a conflict with
// „Beide behalten" first. `hub` is the hub copy's numbers the page read
// (V2-15, `{total_episodes, last_modified}`); without them the sentences go
// without numbers, never with „–".

import React from 'react';
import Icon from '../icons/Icon';
import ConflictChoices from './dialogs/ConflictChoices';
import { releasePointerFocus } from '../Record/ActionBar';
import COPY from '../../features/editDataset/datenCopy';
import { fill, fmtDate } from '../../features/editDataset/model/format';

const B = COPY.banner;

export default function SyncBanners({
  syncState, hub, newerAcked, partnerNote, hfOff, own, onAction, keepBothTip = COPY.keepBoth.tip,
}) {
  const click = (id) => (e) => { releasePointerFocus(e); onAction(id); };
  const hfTitle = hfOff ? COPY.lib.tokenNotActive : undefined;
  const hasHub = !!hub && Number.isFinite(Number(hub.total_episodes)) && hub.total_episodes !== null;
  const hasDate = hasHub && !!hub.last_modified && fmtDate(hub.last_modified) !== '–';
  if (syncState === 'newer' && !newerAcked) {
    return (
      <div className="dat-banner dat-sky" data-banner="newer">
        <Icon name="cloudDownload" size={16} />
        <span className="dat-grow">
          {hasDate ? fill(B.newer, { n: hub.total_episodes, date: fmtDate(hub.last_modified) }) : B.newerShort}
        </span>
        <button type="button" className="dat-btn dat-btn-sm" disabled={hfOff} title={hfTitle} onClick={click('pull')}>
          {B.newerButton}
        </button>
      </div>
    );
  }
  if (syncState === 'changed') {
    return (
      <div className="dat-banner dat-warn" data-banner="changed">
        <Icon name="cloudUpload" size={16} />
        <span className="dat-grow">{hasHub ? fill(B.changed, { n: hub.total_episodes }) : B.changedShort}</span>
        {own ? (
          <button type="button" className="dat-btn dat-btn-sm" disabled={hfOff} title={hfTitle} onClick={click('upload')}>
            {B.changedButton}
          </button>
        ) : null}
      </div>
    );
  }
  if (syncState === 'conflict') {
    return (
      <div className="dat-banner dat-danger" data-banner="conflict">
        <Icon name="syncConflict" size={16} />
        <span className="dat-grow">{B.conflict}</span>
        <ConflictChoices
          partnerNote={partnerNote}
          disabledTitle={hfTitle || ''}
          tip={keepBothTip}
          onKeepBoth={() => onAction('keep_both')}
          onLoadOnline={() => onAction('load_online')}
          onUploadHere={() => onAction('upload_here')}
        />
      </div>
    );
  }
  return null;
}
