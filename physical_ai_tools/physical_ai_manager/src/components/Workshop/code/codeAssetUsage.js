/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The Sammlung scanner of a CODE program: which recordings, Ziele/Positionen,
// counters, object types and shown variables the program's text names, where
// (file, line, column) and whether the call sits in a comment — the counterpart
// of a disabled block, counted as `disabled` exactly like one.
//
// WHAT IT READS. Only a string-LITERAL FIRST argument of an asset-tagged call
// (`robot_api.json` → `params[0].asset`): `robot.replay("Winken")`,
// `Robot.moveTo("Ablage");`, a bare `move_to("Ablage")` after
// `from robot import move_to`. The receiver is `robot`/`Robot` or none — so
// `"abc".count("a")` (Python's str.count) is never an object reference. A
// literal must be the WHOLE argument (followed by `,` or `)`), unprefixed or
// `r`/`u`, single-line, with no backslash: an f-string, a concatenation or an
// escape is not a name this module can know, and a rename must only ever
// rewrite text it is sure of. The method set is read from the generated table,
// never kept here.
//
// WHY A TOKENIZER. A regex over the raw text cannot tell a call from the same
// characters inside a string (`print('robot.replay("x")')`) or a comment. The
// tokenizer below knows Python's `#`, quotes, triple quotes and prefixes and
// Java's `//`, `/* … */`, strings, text blocks and char literals — enough to
// split code from strings from comments, which is all the scanner needs.
//
// NOT A REPLACEMENT for `codeProject.collectCodeReplayNames`: that one is the
// RUN path and is loose on purpose (a skipped 404 costs one fetch; a missed
// name aborts the run). This one is for the UI and is exact.
//
// PURE: no DOM, no CodeMirror, no React — CodeEditor, the drawer adapter and
// RunControls all read it, and it may reach the entry bundle.

import robotApi from './robot_api.json';
import { codeBreakpointId } from './codeBreakpoints';

// ── the asset call table (from robot_api.json) ─────────────────────────────

function buildAssetCalls() {
  const python = new Map();
  const java = new Map();
  for (const m of robotApi.methods || []) {
    const first = m && Array.isArray(m.params) ? m.params[0] : null;
    if (!first || typeof first.asset !== 'string') continue;
    python.set(m.name, { method: m.name, asset: first.asset });
    java.set(m.java_name, { method: m.name, asset: first.asset });
  }
  return { python, java };
}

/** language → Map<spelling, {method (Python name), asset}>. */
export const ASSET_CALLS = Object.freeze(buildAssetCalls());

// Which asset tags each scan map collects, and which a rename rewrites.
const SCAN_BUCKETS = Object.freeze({
  recording: 'replay',
  place: 'refs',
  counter: 'counters',
  object: 'objects',
  variable: 'variables',
});
const RENAME_ASSETS = Object.freeze({ recording: 'recording', place: 'place' });

// ── the tokenizer ──────────────────────────────────────────────────────────

const PY_PREFIX_RE = /^(?:[rRuUbBfF]|[bBrR][rRbB]|[fFrR][rRfF])$/;
const LITERAL_PREFIXES = new Set(['', 'r', 'R', 'u', 'U']);

function isIdentChar(ch) {
  return /[A-Za-z0-9_]/.test(ch);
}

// End offset (exclusive) of a string opened at `i` with `quote` (single char)
// — at the closing quote, or at the newline that ends an unterminated one.
function endOfLineString(text, i, quote, raw) {
  let j = i + 1;
  while (j < text.length) {
    const ch = text[j];
    if (ch === '\\' && !raw) {
      j += 2;
      continue;
    }
    if (ch === quote) return j + 1;
    if (ch === '\n') return j;
    j += 1;
  }
  return text.length;
}

function endOfTripleString(text, i, triple, raw) {
  let j = i + 3;
  while (j < text.length) {
    if (text[j] === '\\' && !raw) {
      j += 2;
      continue;
    }
    if (text.startsWith(triple, j)) return j + 3;
    j += 1;
  }
  return text.length;
}

function stringSegment(text, start, quoteAt, end, prefix, triple) {
  const q = triple ? 3 : 1;
  const closed = end - q >= quoteAt + q && text.slice(end - q, end) === text.slice(quoteAt, quoteAt + q);
  const valueStart = quoteAt + q;
  const valueEnd = closed ? end - q : end;
  const value = text.slice(valueStart, valueEnd);
  return {
    type: 'string',
    start,
    end,
    prefix,
    quote: text[quoteAt],
    triple,
    closed,
    valueStart,
    valueEnd,
    value,
  };
}

/**
 * Split `content` into consecutive `code` / `string` / `comment` segments
 * (Java also `char`). Total: every character belongs to exactly one segment,
 * in order, whatever the text — an unterminated string or comment runs to the
 * end of its line / the text.
 */
export function tokenizeCode(content, language) {
  const text = typeof content === 'string' ? content : '';
  const java = language === 'java';
  const segs = [];
  let codeStart = 0;
  const flush = (upTo) => {
    if (upTo > codeStart) segs.push({ type: 'code', start: codeStart, end: upTo });
  };
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (java) {
      if (ch === '/' && text[i + 1] === '/') {
        flush(i);
        const nl = text.indexOf('\n', i);
        const end = nl < 0 ? text.length : nl;
        segs.push({ type: 'comment', start: i, end });
        i = end;
        codeStart = i;
        continue;
      }
      if (ch === '/' && text[i + 1] === '*') {
        flush(i);
        const close = text.indexOf('*/', i + 2);
        const end = close < 0 ? text.length : close + 2;
        segs.push({ type: 'comment', start: i, end });
        i = end;
        codeStart = i;
        continue;
      }
      if (ch === '"') {
        flush(i);
        const triple = text.startsWith('"""', i);
        const end = triple ? endOfTripleString(text, i, '"""', false) : endOfLineString(text, i, '"', false);
        segs.push(stringSegment(text, i, i, end, '', triple));
        i = end;
        codeStart = i;
        continue;
      }
      if (ch === "'") {
        flush(i);
        const end = endOfLineString(text, i, "'", false);
        segs.push({ type: 'char', start: i, end });
        i = end;
        codeStart = i;
        continue;
      }
      i += 1;
      continue;
    }
    // Python
    if (ch === '#') {
      flush(i);
      const nl = text.indexOf('\n', i);
      const end = nl < 0 ? text.length : nl;
      segs.push({ type: 'comment', start: i, end });
      i = end;
      codeStart = i;
      continue;
    }
    if (isIdentChar(ch) && (i === 0 || !isIdentChar(text[i - 1]))) {
      // A whole word: a string prefix when a quote follows it directly.
      let j = i;
      while (j < text.length && isIdentChar(text[j])) j += 1;
      const word = text.slice(i, j);
      if ((text[j] === '"' || text[j] === "'") && PY_PREFIX_RE.test(word)) {
        flush(i);
        const quote = text[j];
        const triple = text.startsWith(quote.repeat(3), j);
        const raw = /r/i.test(word);
        const end = triple
          ? endOfTripleString(text, j, quote.repeat(3), raw)
          : endOfLineString(text, j, quote, raw);
        segs.push(stringSegment(text, i, j, end, word, triple));
        i = end;
        codeStart = i;
        continue;
      }
      i = j;
      continue;
    }
    if (ch === '"' || ch === "'") {
      flush(i);
      const triple = text.startsWith(ch.repeat(3), i);
      const end = triple ? endOfTripleString(text, i, ch.repeat(3), false) : endOfLineString(text, i, ch, false);
      segs.push(stringSegment(text, i, i, end, '', triple));
      i = end;
      codeStart = i;
      continue;
    }
    i += 1;
  }
  flush(text.length);
  return segs;
}

/** `content` with every string, char literal and comment blanked (newlines kept). */
export function codeOnlyText(content, language) {
  const text = typeof content === 'string' ? content : '';
  let out = '';
  for (const seg of tokenizeCode(text, language)) {
    const part = text.slice(seg.start, seg.end);
    out += seg.type === 'code' ? part : part.replace(/[^\n]/g, ' ');
  }
  return out;
}

// ── lines and columns ──────────────────────────────────────────────────────

function lineStarts(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i += 1) if (text[i] === '\n') starts.push(i + 1);
  return starts;
}

function lineColAt(starts, offset) {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= offset) lo = mid;
    else hi = mid - 1;
  }
  return { line: lo + 1, col: offset - starts[lo] };
}

// ── the asset calls ────────────────────────────────────────────────────────

// The call right before a string: `robot.name(` / `Robot.name(` / bare `name(`.
const CALL_TAIL_RE = /(?:\b(robot|Robot)\s*\.\s*|(?<![\w.]))([A-Za-z_]\w*)\s*\(\s*$/;
// The same, whole, inside a comment's text (a comment holds no string tokens).
const COMMENT_CALL_RE = /(?:\b(robot|Robot)\s*\.\s*|(?<![\w.]))([A-Za-z_]\w*)\s*\(\s*(["'])([^"'\\\n]*)\3(?=\s*[,)])/g;
const NUMBER = '[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][-+]?\\d+)?';
const PIN_COORDS_RE = new RegExp(`^\\s*,\\s*(${NUMBER})\\s*,\\s*(${NUMBER})\\s*,\\s*(${NUMBER})\\s*\\)`);

function lookupCall(language, receiver, spelling) {
  const table = ASSET_CALLS[language];
  if (!table) return null;
  return table.get(spelling) || null;
}

function literalIsUsable(seg) {
  return seg.type === 'string'
    && !seg.triple
    && seg.closed
    && LITERAL_PREFIXES.has(seg.prefix)
    && !seg.value.includes('\\')
    && !seg.value.includes('\n');
}

function coordsAfter(text, from) {
  const m = PIN_COORDS_RE.exec(text.slice(from, from + 200));
  if (!m) return { x: NaN, y: NaN, z: NaN };
  return { x: Number(m[1]), y: Number(m[2]), z: Number(m[3]) };
}

/**
 * Every asset call of ONE file with a literal first argument, in text order:
 * `{method, asset, name (trimmed), rawValue, line, col, inComment,
 * valueStart, valueEnd, coords?}` — `coords` on `pin` only ({x, y, z}, NaN
 * where an argument is not a numeric literal). `col` is the literal's opening
 * quote (0-based), `line` 1-based.
 */
export function findAssetCalls(content, language) {
  const text = typeof content === 'string' ? content : '';
  if (!ASSET_CALLS[language]) return [];
  const segs = tokenizeCode(text, language);
  const starts = lineStarts(text);
  const out = [];
  const push = (hit, quoteAt, valueStart, valueEnd, inComment) => {
    const rawValue = text.slice(valueStart, valueEnd);
    const name = rawValue.trim();
    if (!name) return;
    const { line, col } = lineColAt(starts, quoteAt);
    const row = {
      method: hit.method, asset: hit.asset, name, rawValue, line, col, inComment, valueStart, valueEnd,
    };
    if (hit.method === 'pin') row.coords = coordsAfter(text, valueEnd + 1);
    out.push(row);
  };
  segs.forEach((seg, idx) => {
    if (seg.type === 'comment') {
      const body = text.slice(seg.start, seg.end);
      COMMENT_CALL_RE.lastIndex = 0;
      let m = COMMENT_CALL_RE.exec(body);
      while (m !== null) {
        const hit = lookupCall(language, m[1], m[2]);
        if (hit) {
          const quoteAt = seg.start + m.index + m[0].length - m[4].length - 2;
          push(hit, quoteAt, quoteAt + 1, quoteAt + 1 + m[4].length, true);
        }
        m = COMMENT_CALL_RE.exec(body);
      }
      return;
    }
    if (seg.type !== 'string' || !literalIsUsable(seg)) return;
    const prev = segs[idx - 1];
    const next = segs[idx + 1];
    if (!prev || prev.type !== 'code') return;
    if (language === 'java' && seg.quote !== '"') return;
    const tail = CALL_TAIL_RE.exec(text.slice(Math.max(prev.start, seg.start - 200), seg.start));
    if (!tail) return;
    const hit = lookupCall(language, tail[1], tail[2]);
    if (!hit) return;
    // The literal must be the WHOLE first argument.
    if (!next || next.type !== 'code' || !/^\s*[,)]/.test(text.slice(next.start, next.end))) return;
    push(hit, seg.start + seg.prefix.length, seg.valueStart, seg.valueEnd, false);
  });
  out.sort((a, b) => a.valueStart - b.valueStart);
  return out;
}

function emptyScan() {
  return {
    replay: new Map(),
    refs: new Map(),
    pinStatements: new Map(),
    currentStatements: new Map(),
    counters: new Map(),
    objects: new Map(),
    variables: new Map(),
  };
}

function addRow(map, name, row) {
  if (!map.has(name)) map.set(name, []);
  map.get(name).push(row);
}

function projectEntries(files) {
  if (!files || typeof files !== 'object' || Array.isArray(files)) return [];
  return Object.entries(files).filter(([path, content]) => typeof path === 'string' && typeof content === 'string');
}

/**
 * The whole project's asset uses, as Maps name → rows `{file, line, col,
 * inComment}` (pin rows also carry `coords`): `replay` (recordings), `refs`
 * (Ziele/Positionen by reference: move_to, pickup, drop_at, ziel),
 * `pinStatements` / `currentStatements` (the names the PROGRAM defines with
 * pin / pin_current), `counters`, `objects` and `variables` (zeige names).
 */
export function scanCodeAssets(files, language) {
  const scan = emptyScan();
  for (const [file, content] of projectEntries(files)) {
    for (const call of findAssetCalls(content, language)) {
      const row = { file, line: call.line, col: call.col, inComment: call.inComment };
      if (call.method === 'pin') {
        addRow(scan.pinStatements, call.name, { ...row, coords: call.coords });
      } else if (call.method === 'pin_current') {
        addRow(scan.currentStatements, call.name, row);
      } else if (SCAN_BUCKETS[call.asset]) {
        addRow(scan[SCAN_BUCKETS[call.asset]], call.name, row);
      }
    }
  }
  return scan;
}

function countRows(rows) {
  const out = { enabled: 0, disabled: 0, blockIds: [] };
  for (const r of rows) {
    if (r.inComment) out.disabled += 1;
    else out.enabled += 1;
    out.blockIds.push(codeBreakpointId(r.file, r.line));
  }
  return out;
}

// The first ENABLED row of a name wins (blockUsage.js's shouldRecord rule).
function firstRow(rows) {
  return rows.find((r) => !r.inComment) || rows[0];
}

/**
 * A scan in the shape `sammlung/assetIndex.js::buildAssetIndex` reads as
 * `usage` (the same shape `blockUsage.collectBlockUsage` produces for a
 * workspace), row ids `<file>:L<line>` (codeBreakpoints.js). `variables` is
 * `[{id, uses}]` for the variable usage counts.
 */
export function codeUsageMaps(scan, variables = []) {
  const s = scan || emptyScan();
  const replay = new Map();
  for (const [name, rows] of s.replay) replay.set(name, countRows(rows));
  const refs = new Map();
  for (const [name, rows] of s.refs) refs.set(name, countRows(rows));
  const pinStatements = new Map();
  for (const [name, rows] of s.pinStatements) {
    const r = firstRow(rows);
    const c = r.coords || { x: NaN, y: NaN, z: NaN };
    pinStatements.set(name, {
      blockId: codeBreakpointId(r.file, r.line), enabled: !r.inComment, x: c.x, y: c.y, z: c.z,
    });
  }
  const currentStatements = new Map();
  for (const [name, rows] of s.currentStatements) {
    const r = firstRow(rows);
    currentStatements.set(name, { blockId: codeBreakpointId(r.file, r.line), enabled: !r.inComment });
  }
  const variableUses = new Map();
  for (const v of Array.isArray(variables) ? variables : []) {
    if (v && typeof v.id === 'string') variableUses.set(v.id, Number.isFinite(v.uses) ? v.uses : 0);
  }
  return {
    replay, refs, pinStatements, currentStatements, variableUses,
  };
}

/**
 * Rewrite every REFERENCE of `from` to `to`: `kind` 'recording' → replay,
 * 'place' → move_to / pickup / drop_at / ziel. Never a pin/pin_current
 * DEFINITION (Blockly's rename never touches a „Ziel setzen" block either).
 * Calls inside comments are rewritten too — Blockly rewrites a disabled block,
 * because re-enabling it must not point at a name that no longer exists.
 * Unchanged files keep their identity; no match returns the input object.
 * `to` is a validated asset name (no quote, no backslash).
 * @returns {{files: object, count: number}}
 */
export function renameCodeAssetRefs(files, language, kind, from, to) {
  const asset = RENAME_ASSETS[kind];
  const fromName = String(from ?? '').trim();
  const toName = String(to ?? '').trim();
  if (!asset || !fromName || !toName || fromName === toName) return { files, count: 0 };
  let count = 0;
  let out = null;
  for (const [file, content] of projectEntries(files)) {
    const hits = findAssetCalls(content, language)
      .filter((c) => c.asset === asset && c.name === fromName && c.method !== 'pin' && c.method !== 'pin_current');
    if (hits.length === 0) continue;
    let next = content;
    for (const hit of hits.slice().sort((a, b) => b.valueStart - a.valueStart)) {
      next = next.slice(0, hit.valueStart) + toName + next.slice(hit.valueEnd);
    }
    if (!out) out = { ...files };
    out[file] = next;
    count += hits.length;
  }
  return { files: out || files, count };
}

/** The names the program itself defines with pin / pin_current, all files, first-seen order. */
export function codeDefinedPlaceNames(files, language) {
  const seen = new Set();
  const out = [];
  for (const [, content] of projectEntries(files)) {
    for (const call of findAssetCalls(content, language)) {
      if ((call.method === 'pin' || call.method === 'pin_current') && !seen.has(call.name)) {
        seen.add(call.name);
        out.push(call.name);
      }
    }
  }
  return out;
}

// ── variables (display only: the Variablen tab before a run) ───────────────

const PY_ASSIGN_RE = /^[ \t]*([A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*)[ \t]*(?:=(?!=)|\+=|-=|\*=|\/=|\/\/=|%=|\*\*=|\|=|&=|\^=)/gm;
const PY_FOR_RE = /^[ \t]*(?:async[ \t]+)?for[ \t]+([A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*)[ \t]+in\b/gm;
const JAVA_TYPE = '(?:int|long|double|float|boolean|char|byte|short|String|var|[A-Z][A-Za-z0-9_]*(?:<[^;(){}]*?>)?)';
const JAVA_DECL_RE = new RegExp(`\\b${JAVA_TYPE}(?:\\[\\])*\\s+([a-z_][A-Za-z0-9_]*)\\s*(?==(?!=)|;|,|:|\\))`, 'g');
const PY_KEYWORDS = new Set([
  'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
  'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in',
  'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield',
]);

/**
 * The variable names a program DECLARES, for the Variablen tab before any run
 * (the run's [VAR:] values add the rest). Python: assignment and for-loop
 * targets; Java: local, field, parameter and for/for-each declarations. First
 * occurrence per name, `[{name, file, line}]` in project order. Display only —
 * never a rename or delete (no language server, decision D7).
 */
export function collectCodeVariables(files, language) {
  const seen = new Set();
  const out = [];
  const add = (name, file, starts, index) => {
    if (!name || name.startsWith('__') || PY_KEYWORDS.has(name) || seen.has(name)) return;
    seen.add(name);
    out.push({ name, file, line: lineColAt(starts, index).line });
  };
  for (const [file, content] of projectEntries(files)) {
    const code = codeOnlyText(content, language);
    const starts = lineStarts(code);
    const found = [];
    if (language === 'java') {
      JAVA_DECL_RE.lastIndex = 0;
      let m = JAVA_DECL_RE.exec(code);
      while (m !== null) {
        found.push({ index: m.index, names: [m[1]] });
        m = JAVA_DECL_RE.exec(code);
      }
    } else {
      for (const re of [PY_ASSIGN_RE, PY_FOR_RE]) {
        re.lastIndex = 0;
        let m = re.exec(code);
        while (m !== null) {
          found.push({ index: m.index, names: m[1].split(',').map((n) => n.trim()) });
          m = re.exec(code);
        }
      }
    }
    found.sort((a, b) => a.index - b.index);
    for (const f of found) for (const n of f.names) add(n, file, starts, f.index);
  }
  return out;
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Whole-word uses of `name` in the program's CODE (strings and comments do
 * not count, nor an attribute `obj.name`), `[{file, line, col}]`.
 */
export function variableOccurrences(files, language, name) {
  const target = String(name ?? '');
  if (!/^[A-Za-z_]\w*$/.test(target)) return [];
  const re = new RegExp(`(?<![\\w.])${escapeRegExp(target)}(?!\\w)`, 'g');
  const out = [];
  for (const [file, content] of projectEntries(files)) {
    const code = codeOnlyText(content, language);
    const starts = lineStarts(code);
    re.lastIndex = 0;
    let m = re.exec(code);
    while (m !== null) {
      const { line, col } = lineColAt(starts, m.index);
      out.push({ file, line, col });
      m = re.exec(code);
    }
  }
  return out;
}
