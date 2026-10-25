/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * German display formatting for the Sammlung (cards and drawer). Pure: no
 * Blockly, no Redux.
 */

import { DE } from '../blocks/messages_de';

/** Metres → whole millimetres, with a real minus sign („−64"). */
export function formatMmDe(m) {
  return String(Math.round(m * 1000)).replace('-', '−');
}

/** Seconds with one decimal and a German comma („4,2"). */
export function formatSecondsDe(s) {
  return s.toLocaleString('de-DE', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

const pad2 = (n) => String(n).padStart(2, '0');

// LOCAL date parts. `dateStyle: 'short'` would print a two-digit year
// („13.09.26, 10:42"), and `new Date(null)` would print 1970 — both refused.
function localParts(iso) {
  if (typeof iso !== 'string') return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return {
    day: pad2(d.getDate()),
    month: pad2(d.getMonth() + 1),
    year: String(d.getFullYear()),
    hours: pad2(d.getHours()),
    minutes: pad2(d.getMinutes()),
  };
}

/** `dd.mm.yyyy, hh:mm`, or „—". */
export function formatRecordedAtDe(iso) {
  const p = localParts(iso);
  if (!p) return '—';
  return `${p.day}.${p.month}.${p.year}, ${p.hours}:${p.minutes}`;
}

/** `dd.mm. hh:mm` (older-version rows), or „—". */
export function formatVersionDateDe(iso) {
  const p = localParts(iso);
  if (!p) return '—';
  return `${p.day}.${p.month}. ${p.hours}:${p.minutes}`;
}

// Own-property lookup: an id such as 'constructor' must not reach
// Object.prototype.
function lookup(map, id) {
  return typeof id === 'string' && Object.prototype.hasOwnProperty.call(map, id) ? map[id] : null;
}

// The legacy tags of the two retired arms. Rows stored with them keep their id
// (the cloud and the destination store accept it), and they all share ONE
// German label — never the raw id, never the old product name.
export const LEGACY_ROBOT_TYPES = Object.freeze(['edu6_studio', 'edu1_studio']);

const isLegacy = (id) => typeof id === 'string' && LEGACY_ROBOT_TYPES.includes(id);

export function robotLongLabelDe(id) {
  if (isLegacy(id)) return DE.ROBOT_LABEL_RETIRED;
  return lookup({
    omx_f: DE.ROBOT_LABEL_OMX,
    v1_studio: DE.ROBOT_LABEL_V1,
  }, id);
}

export function robotShortLabelDe(id) {
  if (isLegacy(id)) return DE.ROBOT_SHORT_RETIRED;
  return lookup({
    omx_f: DE.ROBOT_SHORT_OMX,
    v1_studio: DE.ROBOT_SHORT_V1,
  }, id);
}
