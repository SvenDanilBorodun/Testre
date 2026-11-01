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
// warning, red for something that stops the recording or Start, blue for a
// short note. The first sentence is the problem and is set bold; the rest is
// what to do. A problem that sends the student to the Startseite (the
// un-activated robot, Q5) gets the word as a real link.
// Sucher (§8): a wash plate with a light in the problem's tone (amber, red,
// blue) in place of the icon.

import React from 'react';
import clsx from 'clsx';
import { Led } from '../sucher';

const TONE = { warn: 'warn', bad: 'rec', info: 'info' };

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

// The camera station's block (station spec §9.2) carries „Station verlassen":
// a real button, disabled (with its reason) while a program runs.
export default function ProblemBanner({
  problem, homeLabel = '', onGoHome, stationLeaveLabel = '', stationLeaveBlocked = '', onStationLeave,
}) {
  if (!problem || !problem.textDe) return null;
  const kind = TONE[problem.kind] ? problem.kind : 'warn';
  const [lead, rest] = splitFirstSentence(problem.textDe);
  const linked = problem.linkToHome && typeof onGoHome === 'function';
  const leave = problem.stationLeave && typeof onStationLeave === 'function' && stationLeaveLabel;
  return (
    <div className={clsx('rec-banner', kind)} role="status" aria-live="polite" data-testid="rec-banner" data-kind={kind}>
      <Led tone={TONE[kind]} size={10} className="rec-banner-led" />
      <div>
        <b>{linked && !rest ? withHomeLink(lead, homeLabel, onGoHome) : lead}</b>
        {rest ? ' ' : null}
        {rest ? (linked ? withHomeLink(rest, homeLabel, onGoHome) : rest) : null}
        {leave ? ' ' : null}
        {leave ? (
          <button
            type="button"
            className="rec-banner-link"
            onClick={onStationLeave}
            disabled={Boolean(problem.stationLeaveDisabled)}
            title={problem.stationLeaveDisabled ? stationLeaveBlocked : undefined}
            data-testid="rec-station-leave"
          >
            {stationLeaveLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
}
