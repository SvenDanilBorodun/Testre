// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Auffälligkeiten in Episode N" (spec §F6): what the robot's hint worker
// found in this episode, each with „Zur Stelle". Hints only — they never
// block anything.

import React from 'react';
import Icon from '../icons/Icon';
import { releasePointerFocus } from '../Record/ActionBar';
import COPY from '../../features/editDataset/datenCopy';
import { fill } from '../../features/editDataset/model/format';
import { hintText } from '../../features/editDataset/model/hintText';

export default function HintsPanel({ episode, hints, jointNames, durationS, onSeekSeconds }) {
  const list = (hints || []).map((h) => hintText(h, { jointNames, durationS }));
  const title = fill(COPY.player.hintsTitle, { n: episode + 1 });
  return (
    <div className="dat-panel dat-hints">
      <h2>
        {list.length
          ? <Icon name="warning" size={16} style={{ color: 'var(--amber)' }} />
          : <Icon name="checkCircle" size={16} style={{ color: 'var(--success)' }} />}
        {title}
      </h2>
      {list.length === 0 ? (
        <div className="dat-hint-none"><Icon name="check" size={16} />{COPY.player.hintsNone}</div>
      ) : list.map((h, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <div className="dat-hint" key={`${h.type}-${i}`} data-hint={h.type}>
          <Icon name="warning" size={16} />
          <div className="dat-grow"><b>{h.title}</b><p>{h.text}</p></div>
          <button
            type="button"
            className="dat-btn dat-btn-sm"
            onClick={(e) => { releasePointerFocus(e); onSeekSeconds(h.t); }}
          >
            {COPY.player.toPlace}
          </button>
        </div>
      ))}
    </div>
  );
}
