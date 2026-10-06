// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The episode list (spec §F8, the mockup's `.eplist`): the dataset's summary,
// „N mit Hinweisen" and „Nur mit Hinweisen", one row per episode — its mark box
// (an OUTLINE square, `markOff`), „Episode N", the duration, the hint count or
// a check, a flag where the clip cannot play — and the marked episodes' footer.
// Rows are buttons with `aria-current` on the open episode.

import React, { useEffect, useRef } from 'react';
import Icon from '../icons/Icon';
import Fill from './Fill';
import { releasePointerFocus } from '../Record/ActionBar';
import { countRender } from '../../features/editDataset/renderProbe';
import COPY from '../../features/editDataset/datenCopy';
import {
  fill, fmtBytes, fmtDate, fmtFps, fmtKnown, fmtTime,
} from '../../features/editDataset/model/format';
import { hintText } from '../../features/editDataset/model/hintText';

const P = COPY.player;

function EpisodeList({
  summary, entry, current, marks, onlyHints, onToggleOnlyHints, onSelect, onToggleMark,
  onClearMarks, onDeleteMarked, jointNames,
}) {
  countRender('EpisodeList');
  const listRef = useRef(null);
  const episodes = (summary && summary.episodes) || [];
  const marked = new Set(marks);
  const withHints = episodes.filter((e) => (e.hints || []).length > 0).length;
  const shown = episodes.filter((e, i) => !onlyHints || (e.hints || []).length > 0 || i === current);
  const totalS = episodes.reduce((a, e) => a + (Number(e.duration_s) || 0), 0);
  const tasks = (summary && summary.tasks) || [];

  useEffect(() => {
    const row = listRef.current && listRef.current.querySelector('[aria-current="true"]');
    if (row && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'nearest' });
  }, [current]);

  const click = (fn) => (e) => { releasePointerFocus(e); fn(); };
  return (
    <aside className="dat-panel dat-eplist" aria-label={P.episodesLabel}>
      <div className="dat-ds-sum">
        <dl className="dat-kv">
          <div><dt>{P.episodes}</dt><dd>{episodes.length}</dd></div>
          <div><dt>{P.duration}</dt><dd>{fill(P.durationMin, { t: fmtTime(totalS, false) })}</dd></div>
          <div><dt>{P.fps}</dt><dd>{fmtFps(summary && summary.fps)}</dd></div>
          <div><dt>{P.size}</dt><dd>{fmtKnown(entry && entry.size_bytes, fmtBytes)}</dd></div>
          <div><dt>{P.cameras}</dt><dd>{((summary && summary.cameras) || []).length}</dd></div>
          <div><dt>{P.changedAt}</dt><dd className="dat-plain">{fmtDate(entry && entry.modified_at)}</dd></div>
        </dl>
        {tasks.map((t) => <div className="dat-task" key={t}>{fill(P.task, { task: t })}</div>)}
      </div>
      <div className="dat-ep-tools">
        <span className="dat-small">{fill(P.withHints, { n: withHints })}</span>
        <button type="button" className="dat-linkbtn" aria-pressed={onlyHints ? 'true' : 'false'} onClick={click(onToggleOnlyHints)}>
          <Icon name={onlyHints ? 'list' : 'listFilter'} size={14} />
          {onlyHints ? P.showAll : P.onlyHints}
        </button>
      </div>
      <ul className="dat-eps" ref={listRef}>
        {shown.map((e) => {
          const i = episodes.indexOf(e);
          const on = i === current;
          const m = marked.has(i);
          const hints = e.hints || [];
          return (
            <li key={i} className={`dat-ep-row${on ? ' dat-on' : ''}${m ? ' dat-marked' : ''}`} data-episode={i}>
              <button
                type="button"
                className="dat-ep-mark"
                aria-pressed={m ? 'true' : 'false'}
                aria-label={fill(P.markEpisode, { n: i + 1 })}
                onClick={click(() => onToggleMark(i))}
              >
                <Icon name={m ? 'markOn' : 'markOff'} size={16} />
              </button>
              <button type="button" className="dat-ep-open" aria-current={on ? 'true' : 'false'} onClick={click(() => onSelect(i))}>
                <span className="dat-ep-name">{fill(P.episode, { n: i + 1 })}</span>
                <span className="dat-ep-dur dat-mono">{fmtTime(e.duration_s, false)}</span>
                {e.playable === false ? (
                  <span className="dat-ep-flag dat-bad" title={P.unplayable}><Icon name="failed" size={14} /></span>
                ) : null}
                {hints.length ? (
                  <span className="dat-ep-flag" title={hints.map((h) => hintText(h, { jointNames, durationS: e.duration_s }).title).join(', ')}>
                    <Icon name="warning" size={14} />{hints.length}
                  </span>
                ) : (
                  <span className="dat-ep-flag dat-ok" title={P.noHints}><Icon name="check" size={14} /></span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
      <div className="dat-ep-foot">
        {marks.length ? (
          <>
            <span className="dat-grow"><Fill template={P.marked} values={{ n: <b>{marks.length}</b> }} /></span>
            <button type="button" className="dat-btn dat-btn-sm dat-btn-ghost" onClick={click(onClearMarks)}>{P.clearMarks}</button>
            <button type="button" className="dat-btn dat-btn-sm dat-btn-danger" onClick={click(onDeleteMarked)}>{P.deleteMarked}</button>
          </>
        ) : (
          <span className="dat-grow dat-small"><Fill template={P.markHint} values={{ key: <kbd className="dat-kbd">{P.keyDelete}</kbd> }} /></span>
        )}
      </div>
    </aside>
  );
}

export default React.memo(EpisodeList);
