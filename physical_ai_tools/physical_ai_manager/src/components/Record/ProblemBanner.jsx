// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The one banner above the action bar (spec §3.9 / §3.12). It exists only while
// there IS a problem (the page decides which one; this draws it): amber for a
// warning, red for something that stops the recording or Start, teal for a
// short note. The first sentence is the problem and is set bold; the rest is
// what to do. A problem that sends the student to the Startseite (the
// un-activated robot, Q5) gets the word as a real link.

import React from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';

const ICON = { warn: 'warning', bad: 'warning', info: 'info' };

// „Satz eins. Satz zwei." → ['Satz eins.', 'Satz zwei.']; one sentence → [it, ''].
export function splitFirstSentence(text) {
  const s = String(text || '');
  const m = /^(.+?[.!?])\s+(\S[\s\S]*)$/.exec(s);
  return m ? [m[1], m[2]] : [s, ''];
}

function withHomeLink(text, homeLabel, onGoHome) {
  const at = homeLabel ? text.lastIndexOf(homeLabel) : -1;
  const link = (
    <button key="home" type="button" className="rec-banner-link" onClick={onGoHome}>
      {homeLabel}
    </button>
  );
  if (at < 0) return [text, ' ', link];
  return [text.slice(0, at), link, text.slice(at + homeLabel.length)];
}

export default function ProblemBanner({ problem, homeLabel = '', onGoHome }) {
  if (!problem || !problem.textDe) return null;
  const kind = ICON[problem.kind] ? problem.kind : 'warn';
  const [lead, rest] = splitFirstSentence(problem.textDe);
  const linked = problem.linkToHome && typeof onGoHome === 'function';
  return (
    <div className={clsx('rec-banner', kind)} role="status" aria-live="polite" data-testid="rec-banner" data-kind={kind}>
      <Icon name={ICON[kind]} size={18} />
      <div>
        <b>{linked && !rest ? withHomeLink(lead, homeLabel, onGoHome) : lead}</b>
        {rest ? ' ' : null}
        {rest ? (linked ? withHomeLink(rest, homeLabel, onGoHome) : rest) : null}
      </div>
    </div>
  );
}
