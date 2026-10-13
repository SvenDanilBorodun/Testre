// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// What the robot reports about a cloud run (spec §6.6) — facts only, nothing
// inferred: a fact the state topic does not carry renders nothing, and no
// state at all (an older image, a stale topic) renders no facts.

import React from 'react';

import TaskPhase from '../../constants/taskPhases';
import Icon from '../icons/Icon';
import { Led } from '../sucher';
import { cloudCopy, fill } from './cloudCopy';

export function formatMmSs(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null;
  const total = Math.floor(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function rounded(value) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null;
}

/**
 * @param {object} p
 * @param {?object} p.state       useCloudInferenceState's answer (null = unknown)
 * @param {number}  p.robotPhase  /task/status phase
 */
export default function CloudRunFacts({ state, robotPhase }) {
  const running = robotPhase === TaskPhase.INFERENCING;
  const facts = [];
  if (state && state.active) {
    const rtt = rounded(state.rtt_ms_p50);
    const planAge = rounded(state.plan_age_ms);
    const gpu = rounded(state.gpu_ms_p50);
    const endsIn = formatMmSs(state.ends_in_s);
    if (rtt !== null) facts.push(['rtt', fill(cloudCopy.status.rtt, { ms: rtt })]);
    if (planAge !== null) facts.push(['planAge', fill(cloudCopy.status.planAge, { ms: planAge })]);
    if (gpu !== null) facts.push(['gpu', fill(cloudCopy.status.gpu, { ms: gpu })]);
    if (endsIn !== null) facts.push(['endsIn', fill(cloudCopy.status.endsIn, { mmss: endsIn })]);
  }
  const holding = state && state.active && state.phase === 'holding';
  const holdS = holding ? rounded(state.hold_s) : null;
  const staleCamera = state && state.active && state.stale_camera ? state.stale_camera : null;
  const endText = state && !state.active && state.end_text_de ? state.end_text_de : null;

  if (!running && facts.length === 0 && !holding && !staleCamera && !endText) return null;
  return (
    <section aria-label={cloudCopy.status.running} className="flex flex-col gap-1.5 text-sm" data-testid="cloud-run-facts">
      {running && (
        <div className="flex items-center gap-2 font-semibold text-[var(--ink)]">
          <Led tone="sig" pulse="breathe" />
          <Icon name="cloud" size={16} className="text-[color:var(--accent)]" />
          {cloudCopy.status.running}
        </div>
      )}
      {facts.length > 0 && (
        <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[13px] text-[var(--ink-2)]">
          {facts.map(([key, text]) => <li key={key}>{text}</li>)}
        </ul>
      )}
      {holding && (
        <div role="status" className="flex items-center gap-2 text-[color:var(--amber)]">
          <Icon name="warning" size={14} />
          {fill(cloudCopy.status.holding, { s: holdS ?? '–' })}
        </div>
      )}
      {staleCamera && (
        <div className="flex items-center gap-2 text-[color:var(--amber)]">
          <Icon name="warning" size={14} />
          {fill(cloudCopy.status.staleCamera, { name: staleCamera })}
        </div>
      )}
      {endText && <div role="status" className="text-[var(--ink-2)]">{endText}</div>}
    </section>
  );
}
