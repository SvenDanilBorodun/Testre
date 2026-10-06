// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// German number, time, size and date formatting for the Daten tab (spec §J.6:
// decimal comma, `m:ss,d` times, „{x,y} GB"/„{n} MB", dates „3. Okt., 14:12"
// in local time) and the two text helpers every copy string goes through:
// `fill` (a whole-literal template with `{name}` placeholders, §G9) and
// `plural` (§G13 d). Pure; no React, no copy of its own except the month
// abbreviations (a fixed table, so every browser shows the mockup's „Sep.").

const PLACEHOLDER = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

/** `template` with every `{name}` replaced by `values[name]`; unknown names stay as they are. */
export function fill(template, values = {}) {
  return String(template ?? '').replace(PLACEHOLDER, (m, name) => (
    Object.prototype.hasOwnProperty.call(values, name) && values[name] !== undefined && values[name] !== null
      ? String(values[name])
      : m
  ));
}

/**
 * `template` split at its placeholders: `[text, value, text, …]`, where a value
 * may be anything (a React element for bold or mono parts). An unknown name
 * stays as its text.
 */
export function fillParts(template, values = {}) {
  const src = String(template ?? '');
  const out = [];
  let last = 0;
  src.replace(PLACEHOLDER, (m, name, offset) => {
    if (offset > last) out.push(src.slice(last, offset));
    const has = Object.prototype.hasOwnProperty.call(values, name) && values[name] !== undefined && values[name] !== null;
    out.push(has ? values[name] : m);
    last = offset + m.length;
    return m;
  });
  if (last < src.length) out.push(src.slice(last));
  return out;
}

/** `one` for exactly 1, else `many` — both filled with `{n}`. */
export function plural(n, one, many) {
  return fill(Number(n) === 1 ? one : many, { n });
}

/** `x` with `digits` decimals and a decimal comma. */
export function fmtNum(x, digits = 1) {
  const v = Number(x);
  if (!Number.isFinite(v)) return '–';
  return v.toFixed(digits).replace('.', ',');
}

/** A frame rate: an integer as is, else one decimal („29,97" → „30,0" is never shown for 30). */
export function fmtFps(fps) {
  const v = Number(fps);
  if (!Number.isFinite(v) || v <= 0) return '–';
  return Number.isInteger(v) ? String(v) : fmtNum(v, 2).replace(/,?0+$/, '');
}

/** Seconds as `m:ss,d` (tenths, floored) or `m:ss`. Negative or invalid → 0. */
export function fmtTime(sec, tenths = true) {
  let s = Number(sec);
  if (!Number.isFinite(s) || s < 0) s = 0;
  // Work in tenths so 19.99999 never rounds up into the next second's label.
  const totalTenths = Math.floor(s * 10 + 1e-9);
  const m = Math.floor(totalTenths / 600);
  const rest = totalTenths - m * 600;
  const ss = Math.floor(rest / 10);
  const d = rest % 10;
  const base = `${m}:${String(ss).padStart(2, '0')}`;
  return tenths ? `${base},${d}` : base;
}

const MB = 1e6;
const GB = 1e9;

/** Bytes as „{x,y} GB" (from 1 GB on) or „{n} MB" (at least 1 MB for anything > 0). */
export function fmtBytes(bytes) {
  const b = Number(bytes);
  if (!Number.isFinite(b) || b < 0) return '–';
  if (b >= GB) return `${fmtNum(b / GB, 1)} GB`;
  const mb = Math.round(b / MB);
  return `${b > 0 ? Math.max(1, mb) : 0} MB`;
}

/**
 * A progress pair „{done} von {total}" and its percentage that never disagree
 * (V2-14: „1 MB von 4 MB 0 %"): both numbers in the TOTAL's unit (GB from
 * 1 GB, else MB) with one decimal below ten units, whole units above; `done`
 * and the percentage are rounded DOWN (neither claims more than arrived),
 * `done` is never more than `total`.
 * @returns {{done: string, total: string, pct: number}}
 */
export function fmtBytesProgress(done, total) {
  const t = Number(total);
  if (!Number.isFinite(t) || t <= 0) return { done: '–', total: '–', pct: 0 };
  const d = Math.min(Math.max(Number(done) || 0, 0), t);
  const unit = t >= GB ? GB : MB;
  const label = unit === GB ? 'GB' : 'MB';
  const digits = t / unit < 10 ? 1 : 0;
  const k = 10 ** digits;
  const show = (x, round) => `${fmtNum(round((x / unit) * k + 1e-9) / k, digits)} ${label}`;
  return {
    done: show(d, Math.floor),
    total: show(t, Math.round),
    pct: Math.floor((d / t) * 100 + 1e-9),
  };
}

/** Bytes as „{x,y} GB" always (the free-disk chip). */
export function fmtGB(bytes) {
  const b = Number(bytes);
  if (!Number.isFinite(b) || b < 0) return '–';
  return `${fmtNum(b / GB, 1)} GB`;
}

export const MONTHS_DE = Object.freeze([
  'Jan.', 'Feb.', 'März', 'Apr.', 'Mai', 'Juni', 'Juli', 'Aug.', 'Sep.', 'Okt.', 'Nov.', 'Dez.',
]);

function toDate(value) {
  if (value === null || value === undefined || value === '') return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** „3. Okt., 14:12" in local time; '–' when unknown. */
export function fmtDate(value) {
  const d = toDate(value);
  if (!d) return '–';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getDate()}. ${MONTHS_DE[d.getMonth()]}, ${hh}:${mm}`;
}

/** „30. Sep." in local time; '–' when unknown. */
export function fmtDay(value) {
  const d = toDate(value);
  if (!d) return '–';
  return `${d.getDate()}. ${MONTHS_DE[d.getMonth()]}`;
}

/** `[a]` → a, `[a, b]` → „a und b", `[a, b, c]` → „a, b und c" (`andTemplate` = „{a} und {b}"). */
export function joinAnd(items, andTemplate) {
  const list = (items || []).map(String);
  if (list.length === 0) return '';
  if (list.length === 1) return list[0];
  return fill(andTemplate, { a: list.slice(0, -1).join(', '), b: list[list.length - 1] });
}

/** Whole minutes since `atMs` at `nowMs` (never negative). */
export function minutesSince(atMs, nowMs) {
  const d = Number(nowMs) - Number(atMs);
  if (!Number.isFinite(d) || d < 0) return 0;
  return Math.floor(d / 60000);
}
