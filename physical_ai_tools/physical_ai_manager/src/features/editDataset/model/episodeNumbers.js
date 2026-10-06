// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Episode numbers as a student types them (owner decision D9, spec §G6):
// „3, 5, 10-15", 1-based as the page shows them. A range is validated BEFORE it
// is expanded, so „0-1000000" with 12 episodes is one German error and no
// million-step loop; the loop only ever runs for a range inside 1…max.

import COPY from '../datenCopy';
import { fill } from './format';

const TOKEN = /^(\d{1,9})(?:-(\d{1,9}))?$/;

/**
 * @param {string} text the typed numbers
 * @param {number} max the dataset's episode count
 * @returns {{indices: number[], errors: string[]}} 0-based, sorted, unique; one German error per bad token
 */
export function parseEpisodeNumbers(text, max) {
  const limit = Math.max(0, Math.floor(Number(max) || 0));
  const set = new Set();
  const errors = [];
  const norm = String(text ?? '').replace(/[–—]/g, '-').replace(/\s*-\s*/g, '-');
  for (const token of norm.split(/[,;\s]+/)) {
    if (!token) continue;
    const m = TOKEN.exec(token);
    if (!m) {
      errors.push(fill(COPY.tool.errNotNumber, { token }));
      continue;
    }
    let a = Number(m[1]);
    let b = m[2] !== undefined ? Number(m[2]) : a;
    if (a > b) [a, b] = [b, a];
    if (a < 1 || b > limit) {
      errors.push(fill(COPY.tool.errRange, { token, max: limit }));
      continue;
    }
    for (let i = a; i <= b; i += 1) set.add(i - 1);
  }
  return { indices: [...set].sort((x, y) => x - y), errors };
}

/** 0-based indices → „3, 5, 10-15" (1-based, runs collapsed). */
export function toRangeText(indices) {
  const s = [...new Set((indices || []).map(Number).filter((i) => Number.isInteger(i) && i >= 0))]
    .sort((x, y) => x - y);
  const out = [];
  for (let i = 0; i < s.length; i += 1) {
    let j = i;
    while (j + 1 < s.length && s[j + 1] === s[j] + 1) j += 1;
    out.push(j > i ? `${s[i] + 1}-${s[j] + 1}` : `${s[i] + 1}`);
    i = j;
  }
  return out.join(', ');
}

/** The „Jede 5. Episode" pick (split only): 0-based indices 4, 9, 14 … below `max`. */
export function everyFifth(max) {
  const out = [];
  for (let i = 4; i < Math.max(0, Number(max) || 0); i += 5) out.push(i);
  return out;
}
