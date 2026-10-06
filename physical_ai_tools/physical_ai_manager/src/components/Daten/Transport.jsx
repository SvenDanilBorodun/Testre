// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The transport (the mockup's `.transport`): episode ↑/↓, one frame back and
// forward, play/pause, the time „0:03,4 / 0:20,0" and „Bild i / n" (moved by
// REF on every frame of the clock), the speed (the four labels are whole
// literals, §F2), „Markieren", and the timeline. React state changes only on
// play/pause, speed and the mark — never per frame (§F2, §J.B).

import React, { useEffect, useRef } from 'react';
import Icon from '../icons/Icon';
import Scrubber from './Scrubber';
import { releasePointerFocus } from '../Record/ActionBar';
import { countRender } from '../../features/editDataset/renderProbe';
import COPY from '../../features/editDataset/datenCopy';
import { frameLabel, timeLabel } from '../../features/editDataset/model/playerClock';
import { SPEEDS } from '../../features/editDataset/hooks/useEpisodePlayer';

function Transport({
  engine, playing, speed, onSpeed, fps, length, durationS, hints, jointNames,
  marked, onToggleMark, onPrevEpisode, onNextEpisode,
}) {
  countRender('Transport');
  const curRef = useRef(null);
  const totalRef = useRef(null);
  const frameRef = useRef(null);

  useEffect(() => {
    const paint = (idx) => {
      const [cur, total] = timeLabel(idx, fps, length);
      if (curRef.current) curRef.current.textContent = cur;
      if (totalRef.current) totalRef.current.textContent = `/ ${total}`;
      if (frameRef.current) frameRef.current.textContent = frameLabel(idx, length);
    };
    paint(engine.getIdx());
    return engine.subscribe(paint);
  }, [engine, fps, length]);

  const click = (fn) => (e) => { releasePointerFocus(e); fn(); };
  return (
    <div className="dat-transport">
      <div className="dat-tp-row">
        <button type="button" className="dat-tbtn" aria-label={COPY.player.prevEpisode} title={COPY.player.prevEpisodeTitle} onClick={click(onPrevEpisode)}>
          <Icon name="chevronUp" size={16} />
        </button>
        <button type="button" className="dat-tbtn" aria-label={COPY.player.stepBack} title={COPY.player.stepBackTitle} onClick={click(() => engine.step(-1))}>
          <Icon name="stepBack" size={16} />
        </button>
        <button
          type="button"
          className="dat-tbtn dat-play"
          aria-label={playing ? COPY.player.pause : COPY.player.play}
          title={playing ? COPY.player.pauseTitle : COPY.player.playTitle}
          data-action="play"
          onClick={click(() => engine.toggle())}
        >
          <Icon name={playing ? 'pause' : 'play'} size={18} />
        </button>
        <button type="button" className="dat-tbtn" aria-label={COPY.player.stepForward} title={COPY.player.stepForwardTitle} onClick={click(() => engine.step(1))}>
          <Icon name="step" size={16} />
        </button>
        <button type="button" className="dat-tbtn" aria-label={COPY.player.nextEpisode} title={COPY.player.nextEpisodeTitle} onClick={click(onNextEpisode)}>
          <Icon name="chevronDown" size={16} />
        </button>
        <span className="dat-tp-time dat-mono">
          <span ref={curRef} data-time="cur" />
          {' '}
          <span ref={totalRef} className="dat-dim" data-time="total" />
        </span>
        <span ref={frameRef} className="dat-tp-frame dat-mono" data-frame="" />
        <span className="dat-grow" />
        <div className="dat-seg dat-dark" role="group" aria-label={COPY.player.speedLabel}>
          {SPEEDS.map((v, i) => (
            <button key={v} type="button" aria-pressed={speed === v ? 'true' : 'false'} onClick={click(() => onSpeed(v))}>
              {COPY.player.speeds[i]}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="dat-tbtn dat-mark"
          aria-pressed={marked ? 'true' : 'false'}
          title={COPY.player.markTitle}
          onClick={click(onToggleMark)}
        >
          <Icon name="trash" size={16} />
          <span>{marked ? COPY.player.markedOne : COPY.player.mark}</span>
        </button>
      </div>
      <Scrubber engine={engine} fps={fps} length={length} durationS={durationS} hints={hints} jointNames={jointNames} />
    </div>
  );
}

export default React.memo(Transport);
