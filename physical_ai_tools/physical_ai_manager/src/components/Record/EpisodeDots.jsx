// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The Episodenleiste in the action bar (spec §3.4 / §3.5): one dot per episode
// (at most twelve, then „+k"). A saved episode is green with a check, the
// current one pulses in the phase colour (during Zurücksetzen that is the NEXT
// episode, F6d), and an episode that had to be recorded again carries a small
// amber „again" badge.
//
// Sucher (§8, M8, D19): drawn as a strip of film — a frame per episode, the
// saved ones with a check (no picture), the current one outlined in the
// signal colour, a redo one in amber; the label sits above the strip. A frame
// that turns saved WHILE this strip is mounted drops in once (`fresh`, CSS
// only); the frames already saved when it mounted do not.

import React, { useRef } from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';

export default function EpisodeDots({ label, dots = [], more = '', color }) {
  // Which episodes were already saved when the strip mounted (lazy, once).
  const savedAtMountRef = useRef(null);
  if (savedAtMountRef.current === null) {
    savedAtMountRef.current = new Set(dots.filter((d) => d.done).map((d) => d.n));
  }
  if (!dots.length) return null;
  return (
    <div className="rec-eps" role="group" aria-label={label} style={{ '--c': color }} data-testid="rec-dots">
      <span className="lbl" aria-hidden="true">{label}</span>
      <span className="rec-film">
        {dots.map((d) => (
          <span
            key={d.n}
            className={clsx(
              'rec-epd',
              d.done && 'done',
              d.done && !savedAtMountRef.current.has(d.n) && 'fresh',
              !d.done && d.current && 'cur',
              d.redo && 'redo',
            )}
            data-state={d.done ? 'done' : d.current ? 'current' : 'open'}
            data-redo={d.redo ? 'true' : undefined}
          >
            {d.done ? <Icon name="check" size={14} strokeWidth={3} /> : d.n}
            {d.redo ? (
              <span className="rec-rr"><Icon name="again" size={8} strokeWidth={3.5} /></span>
            ) : null}
          </span>
        ))}
        {more ? <span className="lbl">{more}</span> : null}
      </span>
    </div>
  );
}
