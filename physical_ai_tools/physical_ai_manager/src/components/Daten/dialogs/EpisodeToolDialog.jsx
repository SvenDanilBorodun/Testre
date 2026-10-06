// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Episoden löschen" and „Datensatz aufteilen" (owner decisions D9, D11; the
// mockup's tool dialogs, spec §G6): episode numbers typed as the page shows
// them („3, 5, 10-15"), the quick picks, and a live preview of exactly what
// will happen. The parser validates a range BEFORE expanding it, so a typo
// like „0-1000000" is one German error and no hang.

import React, { useMemo, useRef, useState } from 'react';
import Dialog from './Dialog';
import Fill from '../Fill';
import Icon from '../../icons/Icon';
import { releasePointerFocus } from '../../Record/ActionBar';
import COPY from '../../../features/editDataset/datenCopy';
import { everyFifth, parseEpisodeNumbers, toRangeText } from '../../../features/editDataset/model/episodeNumbers';
import { fill, plural } from '../../../features/editDataset/model/format';
import { safeTaskName } from '../../../utils/datasetName';

const CHIP_MAX = 40;

export default function EpisodeToolDialog({
  kind, name, total, marks = [], hintIndices = [], ownerName = null, onlineCopy = false,
  own, robotType, localIds, onConfirm, onClose,
}) {
  const isSplit = kind === 'split';
  const [nums, setNums] = useState(() => toRangeText(marks));
  const [newName, setNewName] = useState(() => fill(COPY.tool.splitDefaultName, { name }));
  const inputRef = useRef(null);
  const { indices, errors } = useMemo(() => parseEpisodeNumbers(nums, total), [nums, total]);

  const pick = (v) => {
    const idx = v === 'marked' ? marks : v === 'hints' ? hintIndices : v === 'fifth' ? everyFifth(total) : [];
    setNums(toRangeText(idx));
    if (inputRef.current) inputRef.current.focus();
  };

  const all = indices.length === total && total > 0;
  const some = indices.length > 0 && indices.length < total;
  const trimmedName = newName.trim();
  const safe = safeTaskName(trimmedName);
  const target = safe ? `${own}/${robotType}_${safe}` : '';
  const extra = [];
  if (all) extra.push(isSplit ? COPY.tool.errSplitAll : COPY.tool.errAll);
  if (isSplit) {
    if (!trimmedName || !target) extra.push(COPY.tool.errNoName);
    else if (localIds && localIds.has(target)) extra.push(COPY.tool.errNameExists);
  }
  const enabled = errors.length === 0 && some && extra.length === 0;
  const count = plural(indices.length, COPY.count.episodeOne, COPY.count.episodeMany);
  const rest = total - indices.length;

  return (
    <Dialog
      title={isSplit ? COPY.tool.splitTitle : COPY.tool.deleteTitle}
      icon={isSplit ? 'split' : 'trash'}
      iconTone={isSplit ? 'accent' : 'danger'}
      wide
      onClose={onClose}
    >
      <p className="dat-small" style={{ marginTop: 0 }}>
        {isSplit
          ? fill(COPY.tool.splitSub, { name })
          : fill(COPY.tool.deleteSub, { name, count: plural(total, COPY.count.episodeOne, COPY.count.episodeMany) })}
      </p>
      <label className="dat-field">
        {isSplit ? COPY.tool.splitLabel : COPY.tool.deleteLabel}
        <input
          ref={inputRef}
          value={nums}
          onChange={(e) => setNums(e.target.value)}
          autoComplete="off"
          placeholder={COPY.tool.placeholder}
          data-autofocus
          data-field="episodes"
        />
      </label>
      <div className="dat-picks">
        <button type="button" disabled={!marks.length} onClick={() => pick('marked')}>{fill(COPY.tool.pickMarked, { n: marks.length })}</button>
        <button type="button" disabled={!hintIndices.length} onClick={() => pick('hints')}>{fill(COPY.tool.pickHints, { n: hintIndices.length })}</button>
        {isSplit ? <button type="button" onClick={() => pick('fifth')}>{COPY.tool.pickFifth}</button> : null}
        <button type="button" onClick={() => pick('clear')}>{COPY.tool.pickClear}</button>
      </div>
      {isSplit ? (
        <label className="dat-field" style={{ marginTop: 12 }}>
          {COPY.tool.splitNameLabel}
          <input value={newName} onChange={(e) => setNewName(e.target.value)} autoComplete="off" data-field="name" />
        </label>
      ) : null}

      <div data-preview="">
        {errors.length || extra.length ? (
          <ul className="dat-errs">
            {errors.map((e) => <li key={e}>{e}</li>)}
            {extra.map((e) => <li key={e}>{e}</li>)}
          </ul>
        ) : null}
        {!isSplit && some ? (
          <div className="dat-preview">
            <div className="dat-small">
              <Fill
                template={COPY.tool.previewDelete}
                values={{ part: <b>{fill(COPY.tool.previewPart, { k: indices.length, max: total })}</b>, rest }}
              />
            </div>
            <div className="dat-chips">
              {indices.slice(0, CHIP_MAX).map((i) => <span key={i}>{fill(COPY.tool.chipEpisode, { n: i + 1 })}</span>)}
              {indices.length > CHIP_MAX ? <span>{fill(COPY.tool.chipMore, { n: indices.length - CHIP_MAX })}</span> : null}
            </div>
            {ownerName ? (
              <p className="dat-small" style={{ margin: '8px 0 0' }}>
                <Fill template={COPY.tool.partnerOwns} values={{ name: <b>{ownerName}</b> }} />
              </p>
            ) : null}
            {onlineCopy ? <p className="dat-small" style={{ margin: '6px 0 0' }}>{COPY.tool.onlineStays}</p> : null}
            <p className="dat-small" style={{ margin: '6px 0 0', color: 'var(--danger-ink)' }}><b>{COPY.tool.irreversible}</b></p>
          </div>
        ) : null}
        {isSplit && some ? (
          <>
            <div className="dat-split2">
              <div>
                <small>{COPY.tool.splitKeeps}</small>
                <b title={name}>{name}</b>
                <span className="dat-mono">{plural(rest, COPY.count.episodeOne, COPY.count.episodeMany)}</span>
              </div>
              <div className="dat-new">
                <small>{COPY.tool.splitNew}</small>
                <b title={trimmedName}>{trimmedName || '–'}</b>
                <span className="dat-mono">{fill(COPY.tool.splitEpisodesList, { count, list: toRangeText(indices) })}</span>
              </div>
            </div>
            <p className="dat-small" style={{ margin: '8px 0 0' }}>
              {COPY.tool.splitOnlyHere}
              {onlineCopy ? ` ${fill(COPY.tool.splitChangesOriginal, { name })}` : ''}
            </p>
          </>
        ) : null}
      </div>

      <div className="dat-acts">
        <button type="button" className="dat-btn" onClick={(e) => { releasePointerFocus(e); onClose(); }}>{COPY.tool.cancel}</button>
        <button
          type="button"
          className={isSplit ? 'dat-btn dat-btn-primary' : 'dat-btn dat-btn-danger-solid'}
          disabled={!enabled}
          data-action="tool-ok"
          onClick={(e) => {
            releasePointerFocus(e);
            onConfirm(isSplit ? { indices, name: trimmedName, target } : { indices });
          }}
        >
          {isSplit ? <Icon name="split" size={16} /> : null}
          {isSplit ? COPY.tool.splitButton : (some ? fill(COPY.tool.deleteCountButton, { count }) : COPY.tool.deleteButton)}
        </button>
      </div>
    </Dialog>
  );
}
