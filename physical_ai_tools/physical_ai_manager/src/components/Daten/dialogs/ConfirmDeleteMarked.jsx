// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „N Episoden löschen?" (the mockup's confirmDeleteMarked, N4: „Bild und
// Daten der übrigen Episoden"): the marked episodes named, what stays and how
// it is renumbered, whose dataset it is, that the hub keeps the old version.

import React from 'react';
import Dialog from './Dialog';
import Fill from '../Fill';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { fill, plural } from '../../../features/editDataset/model/format';

export default function ConfirmDeleteMarked({
  name, indices, total, ownerName = null, onlineCopy = false, onConfirm, onClose,
}) {
  const count = plural(indices.length, COPY.count.episodeOne, COPY.count.episodeMany);
  const rest = total - indices.length;
  return (
    <Dialog title={fill(COPY.confirm.deleteMarkedTitle, { count })} icon="trash" iconTone="danger" onClose={onClose}>
      <div className="dat-chips">
        {indices.map((i) => <span key={i}>{fill(COPY.tool.chipEpisode, { n: i + 1 })}</span>)}
      </div>
      <p>{fill(COPY.confirm.deleteMarkedBody, { name, rest })}</p>
      {ownerName ? <p><Fill template={COPY.tool.partnerOwns} values={{ name: <b>{ownerName}</b> }} /></p> : null}
      {onlineCopy ? <p>{COPY.tool.onlineStays}</p> : null}
      <p><b>{COPY.tool.irreversible}</b></p>
      <div className="dat-acts">
        <button type="button" className="dat-btn" data-autofocus onClick={(e) => { releasePointerFocus(e); onClose(); }}>
          {COPY.tool.cancel}
        </button>
        <button type="button" className="dat-btn dat-btn-danger-solid" onClick={(e) => { releasePointerFocus(e); onConfirm(); }}>
          {fill(COPY.confirm.deleteMarkedButton, { count })}
        </button>
      </div>
    </Dialog>
  );
}
