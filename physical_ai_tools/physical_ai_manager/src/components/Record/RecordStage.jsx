// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The dark stage (spec §3.5 / §3.12): the stage bar (Phasenleiste + view
// switch) on top, the tiles below, and the overlay layers (phase overlay,
// recording frame, chips, stage and finish cards) absolutely on top of both.
// `color` is the phase colour every layer inherits as `--c`. While recording,
// the stage draws the red frame; in the last three seconds it pulses.

import React from 'react';
import clsx from 'clsx';

export default function RecordStage({
  color,
  recording = false,
  hot = false,
  view = 'cams',
  oneCamera = false,
  bar = null,
  tiles = null,
  children = null,
}) {
  return (
    <div
      className={clsx('rec-stage', recording && 'is-run', recording && hot && 'is-last3')}
      style={{ '--c': color }}
      data-testid="rec-stage"
    >
      <div className="rec-stage-bar">{bar}</div>
      <div className={clsx('rec-tiles', `v-${view}`, oneCamera && 'one-cam')} data-testid="rec-tiles">
        {tiles}
      </div>
      {children}
    </div>
  );
}
