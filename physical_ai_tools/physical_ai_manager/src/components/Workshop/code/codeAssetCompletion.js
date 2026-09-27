/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The code editor's Sammlung helpers (owner decision O7), as pure functions
// over plain strings — CodeEditor.jsx, the one student file allowed to import
// CodeMirror, wires them in (a completion source, a linter, a hover tooltip).
//
//   * COMPLETION inside the string argument of an asset call: the recordings
//     for `replay("`, the Ziele/Positionen AND the names the program pins
//     itself for `move_to("` / `pickup` / `drop_at` / `ziel`, the counters,
//     the object types.
//   * A WARNING (never an error) for a name the Sammlung verifiably lacks, by
//     the rules of sammlung/referenceValidators.js: a recording only once the
//     recording list is `ready` (a list still loading proves nothing); a place
//     only when it is neither in the store nor pinned anywhere in the program;
//     an object type NEVER (a student's own `Greifobjekt` class is not
//     knowable here). The cursor's line is skipped — a half-typed name is on
//     it by construction (parseMarkers.js's rule).
//   * HOVER text in German: „Ziel „Ablage“ · x 12,3 cm · y 4,0 cm".
//
// The asset lint has its OWN ship switch, `ASSET_LINT_LANGUAGES`, separate
// from the parse-marker switch `CODE_LINT_LANGUAGES` (parseMarkers.js), which
// stays empty: a missing Sammlung name is a fact the editor can know, a parse
// failure is not.

import {
  ASSET_CALLS, codeDefinedPlaceNames, findAssetCalls, scanCodeAssets,
} from './codeAssetUsage';
import { CODE_DE, formatCode } from './codeMessagesDe';

/** The languages the asset warnings are ON for. */
export const ASSET_LINT_LANGUAGES = Object.freeze(['python', 'java']);

// `robot.name("…`, `Robot.name("…` or a bare `name("…` up to the cursor, the
// string still open. Java strings are double-quoted only.
const PY_CONTEXT_RE = /(?:\b(robot|Robot)\s*\.\s*|(?<![\w.]))([A-Za-z_]\w*)\s*\(\s*(["'])([^"'\\]*)$/;
const JAVA_CONTEXT_RE = /(?:\b(robot|Robot)\s*\.\s*|(?<![\w.]))([A-Za-z_]\w*)\s*\(\s*(")([^"\\]*)$/;

/**
 * The asset argument the cursor sits in, from the text of its line before the
 * cursor: `{asset, method, quote, prefix, from}` (`from` = where the name
 * starts in `textBefore`), or null.
 */
export function assetArgContext(textBefore, language) {
  if (typeof textBefore !== 'string') return null;
  const table = ASSET_CALLS[language];
  if (!table) return null;
  const m = (language === 'java' ? JAVA_CONTEXT_RE : PY_CONTEXT_RE).exec(textBefore);
  if (!m) return null;
  const hit = table.get(m[2]);
  if (!hit) return null;
  return {
    asset: hit.asset, method: hit.method, quote: m[3], prefix: m[4], from: textBefore.length - m[4].length,
  };
}

function uniqueBy(list, key) {
  const seen = new Set();
  const out = [];
  for (const item of list) {
    const k = key(item);
    if (typeof k !== 'string' || !k || seen.has(k)) continue;
    seen.add(k);
    out.push(item);
  }
  return out;
}

const names = (list) => (Array.isArray(list) ? list : []).filter((n) => typeof n === 'string' && n);

/**
 * What to offer for an `asset` kind: `[{label, detail}]`, no name twice.
 * `known`: `{recordings: [{name, duration_s?, versions?}], places: store
 * entries, codePinnedNames, counters, objects, variables}`.
 */
export function assetOptions(asset, known) {
  if (!known || typeof known !== 'object') return [];
  if (asset === 'recording') {
    return uniqueBy((Array.isArray(known.recordings) ? known.recordings : [])
      .map((r) => ({ label: r && r.name, detail: CODE_DE.ASSET_KIND_RECORDING })), (o) => o.label);
  }
  if (asset === 'place') {
    const store = (Array.isArray(known.places) ? known.places : [])
      .filter((e) => e && typeof e.name === 'string')
      .map((e) => ({ label: e.name, detail: e.kind === 'pose' ? CODE_DE.ASSET_KIND_POSE : CODE_DE.ASSET_KIND_PIN }));
    const pinned = names(known.codePinnedNames).map((n) => ({ label: n, detail: CODE_DE.ASSET_KIND_CODE_PIN }));
    return uniqueBy([...store, ...pinned], (o) => o.label);
  }
  if (asset === 'counter') {
    return uniqueBy(names(known.counters).map((n) => ({ label: n, detail: CODE_DE.ASSET_KIND_COUNTER })), (o) => o.label);
  }
  if (asset === 'object') {
    return uniqueBy(names(known.objects).map((n) => ({ label: n, detail: CODE_DE.ASSET_KIND_OBJECT })), (o) => o.label);
  }
  return [];
}

function recordingNames(known) {
  return new Set((Array.isArray(known.recordings) ? known.recordings : [])
    .map((r) => r && r.name).filter((n) => typeof n === 'string'));
}

function placeNames(known) {
  const out = new Set(names(known.codePinnedNames));
  for (const e of Array.isArray(known.places) ? known.places : []) {
    if (e && typeof e.name === 'string') out.add(e.name);
  }
  return out;
}

// The German reason `name` of `asset` is missing, or null when it is not
// (or cannot be judged).
function missingReason(asset, name, known) {
  if (asset === 'recording') {
    if (known.recordingsStatus !== 'ready') return null;
    return recordingNames(known).has(name) ? null : formatCode(CODE_DE.ASSET_MISSING_RECORDING, name);
  }
  if (asset === 'place') {
    return placeNames(known).has(name) ? null : formatCode(CODE_DE.ASSET_MISSING_PLACE, name);
  }
  return null;
}

/**
 * The warnings for ONE file: `[{from, to, severity: 'warning', message,
 * source: 'asset'}]` over the literal's content. Calls in comments and on the
 * cursor's line (1-based `cursorLine`) are skipped. Off for a language outside
 * ASSET_LINT_LANGUAGES.
 */
export function assetDiagnostics(content, language, known, { cursorLine = 0 } = {}) {
  if (!ASSET_LINT_LANGUAGES.includes(language) || typeof content !== 'string') return [];
  const k = known && typeof known === 'object' ? known : {};
  const out = [];
  for (const call of findAssetCalls(content, language)) {
    if (call.inComment || call.line === cursorLine) continue;
    const message = missingReason(call.asset, call.name, k);
    if (!message) continue;
    out.push({
      from: call.valueStart, to: call.valueEnd, severity: 'warning', message, source: 'asset',
    });
  }
  return out;
}

/** The asset literal covering `offset` (quotes included), `{asset, name, from, to}`, or null. */
export function assetAtOffset(content, language, offset) {
  if (typeof content !== 'string' || !Number.isFinite(offset)) return null;
  for (const call of findAssetCalls(content, language)) {
    if (offset >= call.valueStart - 1 && offset <= call.valueEnd + 1) {
      return {
        asset: call.asset, name: call.name, from: call.valueStart, to: call.valueEnd,
      };
    }
  }
  return null;
}

function cmDe(metres) {
  const cm = Math.round(metres * 1000) / 10;
  return cm.toLocaleString('de-DE', { minimumFractionDigits: 1, maximumFractionDigits: 1 }).replace('-', '−');
}

function secondsDe(s) {
  return Number.isFinite(s)
    ? s.toLocaleString('de-DE', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
    : '—';
}

/** The German hover text for an asset name, or null (nothing worth saying). */
export function assetHoverText(asset, name, known) {
  const k = known && typeof known === 'object' ? known : {};
  if (asset === 'place') {
    const entry = (Array.isArray(k.places) ? k.places : []).find((e) => e && e.name === name);
    if (entry) {
      return entry.kind === 'pose'
        ? formatCode(CODE_DE.HOVER_POSE, name, cmDe(entry.x), cmDe(entry.y), cmDe(entry.z))
        : formatCode(CODE_DE.HOVER_PIN, name, cmDe(entry.x), cmDe(entry.y));
    }
    if (names(k.codePinnedNames).includes(name)) return formatCode(CODE_DE.HOVER_CODE_PIN, name);
    return missingReason('place', name, k);
  }
  if (asset === 'recording') {
    const rec = (Array.isArray(k.recordings) ? k.recordings : []).find((r) => r && r.name === name);
    if (rec) {
      const versions = Number.isInteger(rec.versions) ? rec.versions : 1;
      return versions > 1
        ? formatCode(CODE_DE.HOVER_RECORDING_VERSIONS, name, secondsDe(rec.duration_s), versions)
        : formatCode(CODE_DE.HOVER_RECORDING, name, secondsDe(rec.duration_s));
    }
    return missingReason('recording', name, k);
  }
  return null;
}

function timeOf(item) {
  const t = Date.parse(item && item.created_at);
  return Number.isFinite(t) ? t : 0;
}

/**
 * What the editor helpers above know, built from what the page has: the code
 * files, the document's Ziele store entries, the provider's recording list
 * (`{status, items}`) and the object types of the catalog. Recordings are
 * grouped by name — one row per recording, its newest version's duration and
 * the version count — newest recording first. Counters and extra object names
 * come from the program's own (non-comment) calls.
 */
export function buildCodeAssetKnowledge({
  files, language, entries, trajectories, objectTypes,
} = {}) {
  const items = trajectories && Array.isArray(trajectories.items) ? trajectories.items : [];
  const groups = new Map();
  for (const item of items) {
    const name = item && typeof item.name === 'string' ? item.name : '';
    if (!name) continue;
    const g = groups.get(name);
    if (!g) groups.set(name, { newest: item, versions: 1 });
    else {
      g.versions += 1;
      if (timeOf(item) > timeOf(g.newest)) g.newest = item;
    }
  }
  const recordings = [...groups.entries()]
    .sort((a, b) => timeOf(b[1].newest) - timeOf(a[1].newest))
    .map(([name, g]) => ({
      name,
      duration_s: Number.isFinite(g.newest.duration_s) ? g.newest.duration_s : null,
      versions: g.versions,
    }));
  const scan = files && typeof files === 'object' ? scanCodeAssets(files, language) : null;
  const enabledNames = (map) => (map ? [...map.entries()]
    .filter(([, rows]) => rows.some((r) => !r.inComment)).map(([n]) => n) : []);
  const objects = [];
  const seen = new Set();
  for (const n of [...names(objectTypes), ...enabledNames(scan && scan.objects)]) {
    if (!seen.has(n)) {
      seen.add(n);
      objects.push(n);
    }
  }
  return {
    recordings,
    recordingsStatus: trajectories && typeof trajectories.status === 'string' ? trajectories.status : 'none',
    places: (Array.isArray(entries) ? entries : []).filter((e) => e && typeof e.name === 'string'),
    codePinnedNames: files && typeof files === 'object' ? codeDefinedPlaceNames(files, language) : [],
    counters: enabledNames(scan && scan.counters),
    objects,
  };
}
