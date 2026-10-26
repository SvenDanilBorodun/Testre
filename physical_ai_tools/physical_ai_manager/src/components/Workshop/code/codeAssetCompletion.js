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
//   * A WARNING for a `replay` whose name is NOT a whole literal (a variable,
//     an f-string, a concatenation; F10 of the 2026-10-18 semantics round): a
//     run sends only the recordings it can read out of the text
//     (codeProject.collectCodeReplayNames), so that one never reaches the
//     robot. Only the robot's own call is judged: `Robot.replay` in Java (a
//     student's own static `replay` method is none of this), in Python
//     `robot.replay`, a bare `replay(` and every top-level alias of either
//     (`import robot as r`, `from robot import replay as abspielen`; E1).
//   * HOVER text in German: „Ziel „Ablage“ · x 12,3 cm · y 4,0 cm".
//
// The asset lint has its OWN ship switch, `ASSET_LINT_LANGUAGES`, separate
// from the parse-marker switch `CODE_LINT_LANGUAGES` (parseMarkers.js), which
// stays empty: a missing Sammlung name is a fact the editor can know, a parse
// failure is not.

import {
  ASSET_CALLS, codeDefinedPlaceNames, codeOnlyText, findAssetCalls, pythonAliasesOf, scanCodeAssets,
} from './codeAssetUsage';
import { CODE_DE, formatCode } from './codeMessagesDe';
import {
  aliasKey, bareNamesOf, escapeRegExp, pythonRobotAliases, receiverAlternation, rowNameOf,
} from './pythonAliases';

/** The languages the asset warnings are ON for. */
export const ASSET_LINT_LANGUAGES = Object.freeze(['python', 'java']);

// `robot.name("…`, `Robot.name("…` or a bare `name("…` up to the cursor, the
// string still open — in Python also `name(…, param="…` (the keyword the
// table gives the asset parameter, review round 2 ni1). The receiver is
// bounded like an identifier (`Größrobot.`, `ßname(` are not calls of the
// robot, ni2). Java strings are double-quoted only. Python's receivers and
// bare names are the DOCUMENT's (`robot` and its top-level aliases,
// code/pythonAliases.js — E1), so its regex is built per alias set.
const PY_CONTEXT = '(?:(?<![\\p{L}\\p{N}_.])(RECEIVERS)\\s*\\.\\s*|(?<![\\p{L}\\p{N}_.]))([\\p{L}_][\\p{L}\\p{N}_]*)\\s*\\(\\s*(?:(?:[^()"\'\\n]*,\\s*)?([A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*)?(["\'])([^"\'\\\\]*)$';
const JAVA_CONTEXT_RE = /(?:(?<![\p{L}\p{N}_.])((?:edubotics\s*\.\s*)?Robot)\s*\.\s*|(?<![\p{L}\p{N}_.]))([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*()(")([^"\\]*)$/u;

const PY_CONTEXT_RES = new Map();
function pyContextRe(aliases) {
  const key = aliasKey(aliases);
  let re = PY_CONTEXT_RES.get(key);
  if (!re) {
    re = new RegExp(PY_CONTEXT.replace('RECEIVERS', receiverAlternation(aliases)), 'u');
    if (PY_CONTEXT_RES.size >= 64) PY_CONTEXT_RES.clear();
    PY_CONTEXT_RES.set(key, re);
  }
  return re;
}

/**
 * The asset argument the cursor sits in, from the text of its line before the
 * cursor: `{asset, method, quote, prefix, from}` (`from` = where the name
 * starts in `textBefore`), or null. `documentText` (the whole file, Python)
 * supplies the file's aliases of `robot`; without it only `robot.` and the
 * rows' own bare names are the robot.
 */
export function assetArgContext(textBefore, language, documentText) {
  if (typeof textBefore !== 'string') return null;
  const table = ASSET_CALLS[language];
  if (!table) return null;
  const aliases = language === 'python' && typeof documentText === 'string'
    ? pythonAliasesOf(documentText) : null;
  const m = (language === 'java' ? JAVA_CONTEXT_RE : pyContextRe(aliases)).exec(textBefore);
  if (!m) return null;
  const hit = table.get(language === 'python' ? rowNameOf(aliases, m[1], m[2]) : m[2]);
  if (!hit || (m[3] && m[3] !== hit.param)) return null;
  return {
    asset: hit.asset, method: hit.method, quote: m[4], prefix: m[5], from: textBefore.length - m[5].length,
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

// The robot's own `replay(` in CODE (strings and comments are blanked first),
// bounded like an identifier on its left, exactly as codeAssetUsage's RECEIVER.
// Python (E1): every receiver of the file × `replay`, plus the bare names that
// call the replay row there — `replay` itself (after `from robot import
// replay` or `*`) and each function alias of it; built per alias set. Java:
// `Robot.replay` only — a student's own static `replay` method is none of
// this.
const JAVA_REPLAY_HEAD_RE = /(?<![\p{L}\p{N}_.])(?:edubotics\s*\.\s*)?Robot\s*\.\s*replay\s*\(/gu;
const PY_REPLAY_HEAD_RES = new Map();
function replayHeadRe(language, aliases) {
  if (language === 'java') return JAVA_REPLAY_HEAD_RE;
  if (language !== 'python') return null;
  const key = aliasKey(aliases);
  let re = PY_REPLAY_HEAD_RES.get(key);
  if (!re) {
    const bare = bareNamesOf(aliases, 'replay').map(escapeRegExp).join('|');
    re = new RegExp(
      `(?<![\\p{L}\\p{N}_.])(?:(?:${receiverAlternation(aliases)})\\s*\\.\\s*replay${bare ? `|${bare}` : ''})\\s*\\(`,
      'gu',
    );
    if (PY_REPLAY_HEAD_RES.size >= 64) PY_REPLAY_HEAD_RES.clear();
    PY_REPLAY_HEAD_RES.set(key, re);
  }
  return re;
}
// A `def replay(…)` / `class replay(…)` is a definition, not a call.
const PY_DEFINITION_TAIL_RE = /(?:^|[^\p{L}\p{N}_])(?:def|class)\s+$/u;

// The top-level arguments of the call whose `(` ends at `open` in blanked
// code: `{args: [{start, end}], close}` (close -1 when the call never closes).
function callArgs(code, open) {
  const args = [];
  let depth = 0;
  let from = open;
  for (let i = open; i < code.length; i += 1) {
    const ch = code[i];
    if (ch === '(' || ch === '[' || ch === '{') depth += 1;
    else if (ch === ')' || ch === ']' || ch === '}') {
      if (depth === 0) {
        if (ch !== ')') return { args, close: -1 };
        args.push({ start: from, end: i });
        return { args, close: i };
      }
      depth -= 1;
    } else if (ch === ',' && depth === 0) {
      args.push({ start: from, end: i });
      from = i + 1;
    }
  }
  return { args, close: -1 };
}

function lineOfOffset(text, offset) {
  let line = 1;
  for (let i = 0; i < offset; i += 1) if (text[i] === '\n') line += 1;
  return line;
}

// F10: every robot `replay` call whose name argument is not one whole
// literal, as `{from, to, line}` over that argument (trimmed). The name is the
// first positional argument or, in Python, the keyword the table gives it
// (`name=`). A call with no name argument, or one still open, is not judged.
function nonLiteralReplays(content, language) {
  const hit = ASSET_CALLS[language] && ASSET_CALLS[language].get('replay');
  if (!hit) return [];
  const code = codeOnlyText(content, language);
  const re = replayHeadRe(language, language === 'python' ? pythonRobotAliases(code) : null);
  if (!re) return [];
  const literals = findAssetCalls(content, language)
    .filter((c) => !c.inComment && c.method === 'replay')
    .map((c) => c.valueStart);
  const out = [];
  re.lastIndex = 0;
  for (let m = re.exec(code); m !== null; m = re.exec(code)) {
    if (language === 'python' && PY_DEFINITION_TAIL_RE.test(code.slice(Math.max(0, m.index - 40), m.index))) continue;
    const { args, close } = callArgs(code, m.index + m[0].length);
    if (close < 0) continue;
    const kwRe = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)/;
    let arg = null;
    for (let k = 0; k < args.length; k += 1) {
      const text = code.slice(args[k].start, args[k].end);
      const kw = language === 'python' ? kwRe.exec(text) : null;
      if (kw) {
        if (kw[1] === hit.param) {
          arg = args[k];
          break;
        }
      } else if (k === 0) {
        arg = args[k];
        break;
      }
    }
    if (!arg) continue;
    const raw = content.slice(arg.start, arg.end);
    if (!raw.trim()) continue;
    if (literals.some((at) => at > arg.start && at <= arg.end)) continue;
    const from = arg.start + (raw.length - raw.trimStart().length);
    const to = arg.start + raw.trimEnd().length;
    out.push({ from, to, line: lineOfOffset(content, from) });
  }
  return out;
}

/**
 * The warnings for ONE file: `[{from, to, severity: 'warning', message,
 * source: 'asset'}]` over the literal's content (a missing recording or
 * place), and over the name argument of a `replay` that is not a literal
 * (F10). Calls in comments and on the cursor's line (1-based `cursorLine`) are
 * skipped. Off for a language outside ASSET_LINT_LANGUAGES.
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
  for (const r of nonLiteralReplays(content, language)) {
    if (r.line === cursorLine) continue;
    out.push({
      from: r.from, to: r.to, severity: 'warning', message: CODE_DE.REPLAY_NAME_NOT_LITERAL, source: 'asset',
    });
  }
  out.sort((a, b) => a.from - b.from);
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
