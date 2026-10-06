// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The timeline (spec §F3, the mockup's `.scrub`): a slider. Dragging seeks
// frame by frame with the pointer captured, playback paused for the drag and
// resumed after; the hint bands (`from_s`–`to_s`) and ticks (`at_s`) mark where
// the hints are. Its fill, thumb and aria values move by REF on every frame of
// the clock — this component never re-renders per frame. Its keys are the
// player's own (←/→ one frame, Pos1/Ende), heard on the window (§F4).

import React, { useEffect, useRef } from 'react';
import { countRender } from '../../features/editDataset/renderProbe';
import COPY from '../../features/editDataset/datenCopy';
import {
  ariaValueText, frameFraction, scrubTarget,
} from '../../features/editDataset/model/playerClock';
import { hintBand, hintTick, hintText } from '../../features/editDataset/model/hintText';

export default function Scrubber({ engine, fps, length, durationS, hints, jointNames }) {
  countRender('Scrubber');
  const rootRef = useRef(null);
  const fillRef = useRef(null);
  const thumbRef = useRef(null);
  const dragRef = useRef(null);

  useEffect(() => {
    const paint = (idx) => {
      const pct = `${frameFraction(idx, length) * 100}%`;
      if (fillRef.current) fillRef.current.style.width = pct;
      if (thumbRef.current) thumbRef.current.style.left = pct;
      const root = rootRef.current;
      if (root) {
        root.setAttribute('aria-valuenow', (idx / fps).toFixed(2));
        root.setAttribute('aria-valuetext', ariaValueText(idx, fps, length));
      }
    };
    paint(engine.getIdx());
    return engine.subscribe(paint);
  }, [engine, fps, length]);

  const at = (e) => {
    const r = rootRef.current.getBoundingClientRect();
    const frac = r.width > 0 ? (e.clientX - r.left) / r.width : 0;
    engine.seekFrame(scrubTarget(frac, length));
  };

  const total = Math.max(0, Number(durationS) || length / fps);
  return (
    <div
      ref={rootRef}
      className="dat-scrub"
      role="slider"
      tabIndex={0}
      aria-label={COPY.player.timeline}
      aria-valuemin={0}
      aria-valuemax={Number(total.toFixed(2))}
      aria-valuenow={0}
      aria-valuetext={ariaValueText(0, fps, length)}
      onPointerDown={(e) => {
        dragRef.current = { wasPlaying: engine.isPlaying() };
        if (e.currentTarget.setPointerCapture) {
          try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* jsdom */ }
        }
        at(e);
      }}
      onPointerMove={(e) => { if (dragRef.current) at(e); }}
      onPointerUp={() => {
        const drag = dragRef.current;
        dragRef.current = null;
        if (drag && drag.wasPlaying) engine.play();
      }}
      onPointerCancel={() => { dragRef.current = null; }}
    >
      <div className="dat-track" />
      {(hints || []).map((h, i) => {
        const band = hintBand(h, total);
        if (!band || total <= 0) return null;
        return (
          <div
            // eslint-disable-next-line react/no-array-index-key
            key={`b${i}`}
            className="dat-band"
            style={{ left: `${(band.from / total) * 100}%`, width: `${((band.to - band.from) / total) * 100}%` }}
            title={hintText(h, { jointNames, durationS: total }).title}
          />
        );
      })}
      <div ref={fillRef} className="dat-fill" />
      {(hints || []).map((h, i) => {
        const t = hintTick(h);
        if (t === null || total <= 0) return null;
        return (
          <div
            // eslint-disable-next-line react/no-array-index-key
            key={`t${i}`}
            className="dat-tick"
            style={{ left: `${(t / total) * 100}%` }}
            title={hintText(h, { jointNames, durationS: total }).title}
          />
        );
      })}
      <div ref={thumbRef} className="dat-thumbk" />
    </div>
  );
}
