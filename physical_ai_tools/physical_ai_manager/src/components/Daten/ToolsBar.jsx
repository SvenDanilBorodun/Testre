// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Werkzeuge" (owner decision D13, the mockup's tools bar): Episoden löschen
// (with the marked count), Aufteilen, Zusammenführen (the hand-off into the
// library's merge mode, §G13), Hochladen (aria-disabled on a partner's dataset
// and explaining itself on a click; off while it is already current), Weiter
// zum Training (off for local / changed / conflict / private, with the
// reason), Ganzen Datensatz löschen.

import React from 'react';
import Icon from '../icons/Icon';
import { releasePointerFocus } from '../Record/ActionBar';
import COPY from '../../features/editDataset/datenCopy';
import { fill } from '../../features/editDataset/model/format';

const T = COPY.tools;

export default function ToolsBar({
  markCount, own, ownerName, syncState, trainingBlock, hfOff, onAction,
}) {
  const click = (id) => (e) => { releasePointerFocus(e); onAction(id); };
  const current = syncState === 'current';
  const uploadLabel = syncState === 'changed' ? T.uploadNow : T.upload;
  return (
    <div className="dat-tools" role="toolbar" aria-label={T.label}>
      <span className="dat-eyebrow">{T.label}</span>
      <button type="button" className="dat-btn dat-btn-sm" onClick={click('delete_tool')}>
        <Icon name="trash" size={16} />
        {T.deleteEpisodes}
        {markCount ? <span className="dat-count">{markCount}</span> : null}
      </button>
      <button type="button" className="dat-btn dat-btn-sm" onClick={click('split_tool')}>
        <Icon name="split" size={16} />
        {T.split}
      </button>
      <button type="button" className="dat-btn dat-btn-sm" onClick={click('merge_with')}>
        <Icon name="mergeData" size={16} />
        {T.merge}
      </button>
      <span className="dat-sep" />
      {own ? (
        <button
          type="button"
          className="dat-btn dat-btn-sm"
          disabled={current || hfOff}
          title={hfOff ? COPY.lib.tokenNotActive : (current ? T.alreadyCurrent : undefined)}
          onClick={click('upload')}
        >
          <Icon name="cloudUpload" size={16} />
          {uploadLabel}
        </button>
      ) : (
        <button
          type="button"
          className="dat-btn dat-btn-sm"
          aria-disabled="true"
          title={fill(COPY.card.partnerOnly, { name: ownerName })}
          onClick={click('upload_partner')}
        >
          <Icon name="cloudUpload" size={16} />
          {T.upload}
        </button>
      )}
      <button
        type="button"
        className="dat-btn dat-btn-sm"
        disabled={!!trainingBlock}
        title={trainingBlock || undefined}
        onClick={click('training')}
      >
        <Icon name="chart" size={16} />
        {T.toTraining}
      </button>
      <span className="dat-grow" />
      <button type="button" className="dat-btn dat-btn-sm dat-btn-danger" onClick={click('delete_dataset')}>
        <Icon name="trash" size={16} />
        {T.deleteWhole}
      </button>
    </div>
  );
}
