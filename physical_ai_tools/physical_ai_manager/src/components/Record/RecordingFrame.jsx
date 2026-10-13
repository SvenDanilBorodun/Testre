// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// What the stage shows while an episode is being recorded (spec §3.5): the red
// „REC mm:ss" badge, the seconds left until it is saved (red in the last three
// seconds) and a line along the bottom that shrinks to nothing. The frame
// itself is drawn round the device (record.css, from RecordStage's classes).
// The line moves every frame through `subscribeFrame` and a ref; the texts
// change once per second.
//
// Sucher (§8, M11): the badge is a red chip in Doto with a blinking white
// light, centred over the tiles; the seconds left are a big serif italic
// number bottom right — the number and its unit split at render from the one
// handed-in string (the copy is unchanged); in the last three seconds the
// number pops once per second (keyed on its value, CSS only).

import React, { useEffect, useRef } from 'react';
import clsx from 'clsx';
import Icon from '../icons/Icon';

export default function RecordingFrame({
  active = false,
  hot = false,
  text = null,
  subscribeFrame = null,
  staticFrac = null,
}) {
  const lineRef = useRef(null);

  useEffect(() => {
    const line = lineRef.current;
    if (!active || !line) return undefined;
    const paint = (frac) => {
      const f = Math.max(0, Math.min(1, Number.isFinite(frac) ? frac : 0));
      line.style.width = `${(1 - f) * 100}%`;
    };
    if (staticFrac !== null && staticFrac !== undefined) {
      paint(staticFrac);
      return undefined;
    }
    paint(0);
    if (typeof subscribeFrame !== 'function') return undefined;
    return subscribeFrame((f) => paint(f && f.frac));
  }, [active, subscribeFrame, staticFrac]);

  if (!active || !text) return null;
  // „13 s" → „13" + „ s" (presentation only; anything else is drawn as is).
  const m = /^(\d+)(\s*\S.*)$/.exec(String(text.remaining || ''));
  return (
    <>
      <div className="rec-recinfo" data-testid="rec-recording-frame">
        <span className="rec-recbadge">
          <Icon name="liveRecording" size={10} className="rec-blink" />
          {text.badge}
        </span>
        <div className={clsx('rec-recleft', hot && 'hot')} data-testid="rec-remaining">
          <div className="big">
            {m ? (
              <>
                <span className="num" key={hot ? m[1] : 'steady'}>{m[1]}</span>
                <small>{m[2]}</small>
              </>
            ) : text.remaining}
          </div>
          <div className="sm">{text.until}</div>
        </div>
      </div>
      <div className="rec-recbar" aria-hidden="true">
        <i ref={lineRef} data-testid="rec-recbar" />
      </div>
    </>
  );
}
