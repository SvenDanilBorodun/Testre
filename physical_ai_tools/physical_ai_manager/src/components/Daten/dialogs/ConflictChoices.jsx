// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The three ways out of a conflict (owner decisions N1, F-3, G-2; spec §E10),
// in this order and with „Beide behalten" the default (focused): keep both
// (a three-way merge: every new episode of both sides stays, what was deleted
// since the last sync stays deleted — or, for a dataset with no recorded sync,
// the union, where deleted episodes come back: `tip` says which, T2-1), load
// the online version, upload this one — the last two each saying what they
// lose. On a partner's dataset only „Online-Version laden" remains (H-3); the
// sentence why stands in place of the other two.

import React from 'react';
import Icon from '../../icons/Icon';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';

export default function ConflictChoices({
  partnerNote = null, onKeepBoth, onLoadOnline, onUploadHere, disabledTitle = '', inDialog = false,
  tip = COPY.keepBoth.tip,
}) {
  const off = !!disabledTitle;
  const run = (fn) => (e) => { releasePointerFocus(e); fn(); };
  return (
    <div className="dat-banner-actions" data-conflict-choices="">
      {partnerNote ? null : (
        <div className="dat-keep">
          <button
            type="button"
            className="dat-btn dat-btn-sm dat-btn-primary"
            data-autofocus={inDialog ? '' : undefined}
            data-action="keep_both"
            disabled={off}
            title={disabledTitle || tip}
            onClick={run(onKeepBoth)}
          >
            <Icon name="keepBoth" size={16} />
            {COPY.keepBoth.button}
          </button>
          <small>{tip}</small>
        </div>
      )}
      <div className="dat-keep">
        <button
          type="button"
          className="dat-btn dat-btn-sm"
          data-autofocus={inDialog && partnerNote ? '' : undefined}
          data-action="load_online"
          disabled={off}
          title={disabledTitle || undefined}
          onClick={run(onLoadOnline)}
        >
          <Icon name="cloudDownload" size={16} />
          {COPY.conflict.loadOnline}
        </button>
        <small>{COPY.conflict.losesHere}</small>
      </div>
      {partnerNote ? <span className="dat-small">{partnerNote}</span> : (
        <div className="dat-keep">
          <button
            type="button"
            className="dat-btn dat-btn-sm"
            data-action="upload_here"
            disabled={off}
            title={disabledTitle || undefined}
            onClick={run(onUploadHere)}
          >
            <Icon name="cloudUpload" size={16} />
            {COPY.conflict.uploadHere}
          </button>
          <small>{COPY.conflict.losesOnline}</small>
        </div>
      )}
    </div>
  );
}
