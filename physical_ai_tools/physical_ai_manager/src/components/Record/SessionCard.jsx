// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// „Diese Sitzung" (spec §3.10): every attempt of this session, newest first —
// a saved episode with its number, a discarded one („–") with why it was
// discarded — each with how long it ran, a chip with the saved count and a sum
// line. The rows are built by the page from the session record in Redux, so
// they survive a tab switch but not a reload.
//
// Sucher (§8, M9): a paper receipt — the title in the serif with its last
// word in italics (split at render, the copy is unchanged), each row „Episode
// N" with a dotted leader to its Doto time and its line below; a row appended
// while the card is shown feeds in (CSS only). The row number stays for screen
// readers (its line already names the episode on screen).

import React from 'react';
import clsx from 'clsx';
import { Led, Plate } from '../sucher';

// The title's last word is set in italics: [„Diese ", „Sitzung"].
function splitLastWord(text) {
  const t = String(text || '');
  const at = t.lastIndexOf(' ');
  return at > 0 ? [t.slice(0, at + 1), t.slice(at + 1)] : ['', t];
}

export default function SessionCard({
  title,
  chip,
  rows = [],
  emptyText = '',
  note = '',
  sum = null,
}) {
  const [lead, last] = splitLastWord(title);
  return (
    <Plate
      as="section"
      tone="paper"
      className="rec-card rec-receipt"
      aria-labelledby="rec-session-title"
      data-testid="rec-session-card"
    >
      <div className="rec-card-h">
        <h3 id="rec-session-title" className="su-serif">{lead}<em>{last}</em></h3>
        {chip ? (
          <span className="rec-chipwrap">
            <Led tone={chip.ok ? 'ok' : 'off'} size={8} />
            <span className={clsx('rec-chip', chip.ok && 'ok')}>{chip.text}</span>
          </span>
        ) : null}
      </div>
      <div className="rec-card-b">
        {note ? <div className="rec-help" style={{ paddingTop: 8 }}>{note}</div> : null}
        {rows.length ? (
          <ul className="rec-eplist" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {rows.map((r) => (
              <li
                key={r.key}
                className={clsx('rec-ep', r.discarded && 'x', r.isNew && 'newrow')}
                data-outcome={r.outcome || undefined}
              >
                <span className="n su-sr">{r.num}</span>
                <span className="t">
                  <b>{r.title}</b>
                  <span className="rec-lead" aria-hidden="true" />
                  <span className="d">{r.duration}</span>
                </span>
                <span className="s">{r.sub}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="rec-empty">{emptyText}</div>
        )}
        {sum ? (
          <div className="rec-sumline">
            <span>{sum.left}</span>
            <span>{sum.right}</span>
          </div>
        ) : null}
      </div>
    </Plate>
  );
}
