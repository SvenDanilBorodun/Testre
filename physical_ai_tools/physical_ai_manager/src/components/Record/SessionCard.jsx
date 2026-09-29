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

import React from 'react';
import clsx from 'clsx';

export default function SessionCard({
  title,
  chip,
  rows = [],
  emptyText = '',
  note = '',
  sum = null,
}) {
  return (
    <section className="rec-card" aria-labelledby="rec-session-title" data-testid="rec-session-card">
      <div className="rec-card-h">
        <h3 id="rec-session-title">{title}</h3>
        {chip ? <span className={clsx('rec-chip', chip.ok && 'ok')}>{chip.text}</span> : null}
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
                <span className="n">{r.num}</span>
                <span className="t">
                  <b>{r.title}</b>
                  <span>{r.sub}</span>
                </span>
                <span className="d">{r.duration}</span>
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
    </section>
  );
}
