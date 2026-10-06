// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Ganzen Datensatz löschen?" (owner decision D1, spec §D6): two variants.
// A hub copy is PROVEN (badge current / newer / conflict, or changed with the
// repo listed) → a plain confirm, plus „Deine Änderungen von hier gehen
// verloren" for changed / conflict. Every other case — only here, unknown for
// any reason, a partner's dataset — needs „Ich habe verstanden, dass es keine
// Kopie gibt." before the button works.

import React, { useState } from 'react';
import Dialog from './Dialog';
import Fill from '../Fill';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { fill, fmtBytes, fmtDate } from '../../../features/editDataset/model/format';

/** The variant for `card` (a libraryCards() row): 'online' when the hub keeps a copy, else 'only_here'. */
export function deleteVariant(card, own) {
  const s = card.sync && card.sync.state;
  if (card.ns !== own) return 'only_here';
  if (!card.hub) return 'only_here';
  if (s === 'current' || s === 'newer' || s === 'conflict' || s === 'changed') return 'online';
  return 'only_here';
}

export default function ConfirmDeleteDataset({ card, own, ownerName = null, onConfirm, onClose }) {
  const [ack, setAck] = useState(false);
  const variant = deleteVariant(card, own);
  const local = card.local || {};
  const name = local.display_name || card.name;
  const s = card.sync && card.sync.state;
  const size = local.size_bytes !== undefined && local.size_bytes !== null ? fmtBytes(local.size_bytes) : null;
  const hub = card.hub || {};
  return (
    <Dialog title={COPY.confirm.deleteDatasetTitle} icon="trash" iconTone="danger" onClose={onClose}>
      {variant === 'online' ? (
        <>
          <p>
            {hub.total_episodes !== undefined && hub.total_episodes !== null
              ? fill(COPY.confirm.deleteOnlineKeeps, { name, n: hub.total_episodes, date: fmtDate(hub.last_modified) })
              : fill(COPY.confirm.deleteOnlineKeepsShort, { name })}
          </p>
          {s === 'changed' || s === 'conflict' ? (
            <p><Fill template={COPY.del.lostChanges} values={{ bold: <b>{COPY.del.lostChangesBold}</b> }} /></p>
          ) : null}
        </>
      ) : (
        <>
          <p>
            {ownerName
              ? <Fill template={COPY.confirm.deleteOnlyHerePartner} values={{ name, owner: <b>{ownerName}</b>, only: <b>{COPY.confirm.onlyHere}</b> }} />
              : <Fill template={COPY.confirm.deleteOnlyHere} values={{ name, only: <b>{COPY.confirm.onlyHere}</b> }} />}
          </p>
          <label className="dat-ack">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            <span>{COPY.confirm.ack}</span>
          </label>
        </>
      )}
      {size ? <p className="dat-small">{fill(COPY.confirm.frees, { size })}</p> : null}
      <div className="dat-acts">
        <button type="button" className="dat-btn" data-autofocus onClick={(e) => { releasePointerFocus(e); onClose(); }}>
          {COPY.tool.cancel}
        </button>
        <button
          type="button"
          className="dat-btn dat-btn-danger-solid"
          disabled={variant !== 'online' && !ack}
          onClick={(e) => { releasePointerFocus(e); onConfirm(); }}
        >
          {COPY.confirm.deleteDatasetButton}
        </button>
      </div>
    </Dialog>
  );
}
