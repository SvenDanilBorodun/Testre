// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// A hint from the robot (spec §F6, computed by the sidecar; the page only
// renders) → its German title and sentence, and the second the „Zur Stelle"
// button and the scrubber band point at. Hints are never verdicts.

import COPY from '../datenCopy';
import { fill, fmtNum, fmtTime } from './format';
import { jointShort } from './labels';

const num = (x) => {
  const v = Number(x);
  return Number.isFinite(v) ? v : 0;
};

/**
 * @param {object} hint `{type, from_s?, to_s?, at_s?, joint?, value_deg?, ratio?, median_s?}`
 * @param {{jointNames?: string[], durationS?: number}} ctx the dataset's state names, the episode's duration
 * @returns {{type: string, title: string, text: string, t: number}}
 */
export function hintText(hint, ctx = {}) {
  const h = hint || {};
  const type = String(h.type || '');
  const names = Array.isArray(ctx.jointNames) ? ctx.jointNames : [];
  const sec = (s) => fill(COPY.hint.seconds, { s: fmtNum(s, 1) });
  switch (type) {
    case 'still':
      return { type, title: COPY.hint.stillTitle, text: fill(COPY.hint.still, { n: Math.round(num(h.value_deg)) }), t: 0 };
    case 'idle_end': {
      const from = num(h.from_s);
      const to = h.to_s === undefined ? num(ctx.durationS) : num(h.to_s);
      return {
        type,
        title: COPY.hint.idleEndTitle,
        text: fill(COPY.hint.idleEnd, { s: fmtNum(Math.max(0, to - from), 1), t: fmtTime(from) }),
        t: from,
      };
    }
    case 'idle_start': {
      const to = num(h.to_s);
      return {
        type,
        title: COPY.hint.idleStartTitle,
        text: fill(COPY.hint.idleStart, { s: fmtNum(to, 1), t: fmtTime(to) }),
        t: 0,
      };
    }
    case 'no_grasp':
      return { type, title: COPY.hint.noGraspTitle, text: COPY.hint.noGrasp, t: 0 };
    case 'lag': {
      const at = num(h.at_s);
      const k = Number.isInteger(h.joint) ? h.joint : 0;
      return {
        type,
        title: COPY.hint.lagTitle,
        text: fill(COPY.hint.lag, { joint: jointShort(names[k] || `joint${k + 1}`), n: Math.round(num(h.value_deg)), t: fmtTime(at) }),
        t: at,
      };
    }
    case 'short':
    case 'long': {
      const median = num(h.median_s);
      const dur = ctx.durationS !== undefined ? num(ctx.durationS) : median * num(h.ratio);
      return {
        type,
        title: type === 'short' ? COPY.hint.shortTitle : COPY.hint.longTitle,
        text: fill(type === 'short' ? COPY.hint.short : COPY.hint.long, { dur: sec(dur), median: sec(median) }),
        t: 0,
      };
    }
    default:
      return { type, title: type, text: '', t: 0 };
  }
}

/** A hint's band on the scrubber `{from, to}` in seconds, or null (only idle hints have one). */
export function hintBand(hint, durationS) {
  if (!hint) return null;
  if (hint.type === 'idle_start' || hint.type === 'idle_end') {
    const from = num(hint.from_s);
    const to = hint.to_s === undefined ? num(durationS) : num(hint.to_s);
    return to > from ? { from, to } : null;
  }
  return null;
}

/** A hint's tick on the scrubber in seconds, or null (only `lag` has one). */
export function hintTick(hint) {
  return hint && hint.type === 'lag' && Number.isFinite(Number(hint.at_s)) ? Number(hint.at_s) : null;
}
