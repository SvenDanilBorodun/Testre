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
// `from robot import move_to` — and, in Python, the same argument given by
// its keyword wherever it stands (`robot.replay(speed=2, name="Winken")`, the
// parameter name read from the table; review round 2, ni1). The receiver is
// `robot`/`Robot` (Java also `edubotics.Robot`) or none, bounded like an
// identifier in both languages: `Größrobot.replay`, `ßreplay` and
// `x.robot.replay` are not the robot (ni2), and `"abc".count("a")` (Python's
// str.count) is never an object reference. A literal must be the WHOLE
// argument (followed by `,` or `)`), unprefixed or `r`/`u`, single-line, with
// no backslash: an f-string, a concatenation or an escape is not a name this
// module can know, and a rename must only ever rewrite text it is sure of.
// The method set is read from the generated table, never kept here.
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
    python.set(m.name, { method: m.name, asset: first.asset, param: first.name });
    java.set(m.java_name, { method: m.name, asset: first.asset, param: first.name });
  }
  return { python, java };
}

/** language → Map<spelling, {method (Python name), asset, param (its first
 *  parameter's name — the keyword a Python call may give it by)}>. */
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

// Identifier characters: Python and Java both allow Unicode letters and
// digits (`größe`, `Würfel`), so do the scanners (review m3).
const IDENT_CHAR_RE = /[\p{L}\p{N}_]/u;
function isIdentChar(ch) {
  return IDENT_CHAR_RE.test(ch);
}

// End offset (exclusive) of a string opened at `i` with `quote` (single char)
// — at the closing quote, or at the newline that ends an unterminated one.
// A backslash always skips the next character, in a RAW string too: Python's
// lexer lets `\'` not end `r'…'` (it keeps the backslash in the value), so
// `r'\''` is one string (review n1) — raw or not, termination is the same.
function endOfLineString(text, i, quote) {
  let j = i + 1;
  while (j < text.length) {
    const ch = text[j];
    if (ch === '\\') {
      j += 2;
      continue;
    }
    if (ch === quote) return j + 1;
    if (ch === '\n') return j;
    j += 1;
  }
  return text.length;
}

function endOfTripleString(text, i, triple) {
  let j = i + 3;
  while (j < text.length) {
    if (text[j] === '\\') {
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
        const end = triple ? endOfTripleString(text, i, '"""') : endOfLineString(text, i, '"');
        segs.push(stringSegment(text, i, i, end, '', triple));
        i = end;
        codeStart = i;
        continue;
      }
      if (ch === "'") {
        flush(i);
        const end = endOfLineString(text, i, "'");
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
        const end = triple
          ? endOfTripleString(text, j, quote.repeat(3))
          : endOfLineString(text, j, quote);
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
      const end = triple ? endOfTripleString(text, i, ch.repeat(3)) : endOfLineString(text, i, ch);
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

// The receiver of a call, bounded like an identifier on its left (a letter,
// a digit, `_` or a `.` before it means it is not the robot).
const RECEIVER = {
  python: '(?<![\\p{L}\\p{N}_.])(robot)\\s*\\.\\s*',
  java: '(?<![\\p{L}\\p{N}_.])((?:edubotics\\s*\\.\\s*)?Robot)\\s*\\.\\s*',
};
const CALLEE = '(?:RECV|(?<![\\p{L}\\p{N}_.]))([A-Za-z_][A-Za-z0-9_]*)';
const callee = (language) => CALLEE.replace('RECV', RECEIVER[language] || RECEIVER.python);
// The call right before a string: `robot.name(` / `Robot.name(` / bare `name(`.
const CALL_TAIL_RE = {
  python: new RegExp(`${callee('python')}\\s*\\(\\s*$`, 'u'),
  java: new RegExp(`${callee('java')}\\s*\\(\\s*$`, 'u'),
};
// A call's name right before its `(` (the keyword path scans back to it).
const CALL_NAME_RE = {
  python: new RegExp(`${callee('python')}\\s*$`, 'u'),
  java: new RegExp(`${callee('java')}\\s*$`, 'u'),
};
// A keyword right before a string: `name=`, `target = `.
const KEYWORD_TAIL_RE = /(?<![\p{L}\p{N}_])([A-Za-z_][A-Za-z0-9_]*)\s*=\s*$/u;
// The same call, whole, inside a comment's text (a comment holds no string
// tokens): an optional keyword, the literal, then `,` or `)`.
const COMMENT_CALL_RE = {
  python: new RegExp(`${callee('python')}\\s*\\(\\s*(?:([A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*)?(["'])([^"'\\\\\\n]*)\\4(?=\\s*[,)])`, 'gu'),
  java: new RegExp(`${callee('java')}\\s*\\(\\s*()(["'])([^"'\\\\\\n]*)\\4(?=\\s*[,)])`, 'gu'),
};
const NUMBER = '[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][-+]?\\d+)?';
const PIN_COORD_KEYS = ['x', 'y', 'z'];
// Farther back than this a keyword's call is not looked for.
const KEYWORD_SCAN_MAX = 2000;

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

// `text` with every string, char literal and comment blanked, built from the
// segments already at hand (newlines and length kept).
function blankedFrom(text, segs) {
  let out = '';
  for (const seg of segs) {
    const part = text.slice(seg.start, seg.end);
    out += seg.type === 'code' ? part : part.replace(/[^\n]/g, ' ');
  }
  return out;
}

// The `(` that opens the call whose argument list holds offset `at`, scanning
// back through code-only text over nested brackets; -1 when `at` is not in
// a call's parentheses.
function enclosingParen(code, at) {
  let depth = 0;
  for (let i = at - 1; i >= 0 && at - i <= KEYWORD_SCAN_MAX; i -= 1) {
    const ch = code[i];
    if (ch === ')' || ch === ']' || ch === '}') depth += 1;
    else if (ch === '(' || ch === '[' || ch === '{') {
      if (depth === 0) return ch === '(' ? i : -1;
      depth -= 1;
    }
  }
  return -1;
}

// The code-only text of the arguments of the call opened at `paren`.
function argsOf(code, paren) {
  let depth = 0;
  for (let i = paren; i < code.length && i - paren <= KEYWORD_SCAN_MAX; i += 1) {
    const ch = code[i];
    if (ch === '(' || ch === '[' || ch === '{') depth += 1;
    else if (ch === ')' || ch === ']' || ch === '}') {
      depth -= 1;
      if (depth === 0) return code.slice(paren + 1, i);
    }
  }
  return code.slice(paren + 1, paren + 1 + 200);
}

// The pinned point of a pin() call from its arguments (code-only text, the
// name literal blanked): positional after the name, or `x=`/`y=`/`z=`
// keywords; NaN where an argument is not a numeric literal.
function coordsOf(args) {
  const parts = [];
  let depth = 0;
  let from = 0;
  for (let i = 0; i < args.length; i += 1) {
    const ch = args[i];
    if (ch === '(' || ch === '[' || ch === '{') depth += 1;
    else if (ch === ')' || ch === ']' || ch === '}') depth -= 1;
    else if (ch === ',' && depth === 0) {
      parts.push(args.slice(from, i));
      from = i + 1;
    }
  }
  parts.push(args.slice(from));
  const value = (t) => (new RegExp(`^\\s*${NUMBER}\\s*$`).test(t) ? Number(t) : NaN);
  const out = { x: NaN, y: NaN, z: NaN };
  let positional = 0;
  for (const part of parts) {
    const kw = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)([\s\S]*)$/.exec(part);
    if (kw) {
      if (PIN_COORD_KEYS.includes(kw[1])) out[kw[1]] = value(kw[2]);
    } else {
      if (positional >= 1 && positional <= 3) out[PIN_COORD_KEYS[positional - 1]] = value(part);
      positional += 1;
    }
  }
  return out;
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
  const code = blankedFrom(text, segs);
  const starts = lineStarts(text);
  const out = [];
  const push = (hit, quoteAt, valueStart, valueEnd, inComment, paren) => {
    const rawValue = text.slice(valueStart, valueEnd);
    const name = rawValue.trim();
    if (!name) return;
    const { line, col } = lineColAt(starts, quoteAt);
    const row = {
      method: hit.method, asset: hit.asset, name, rawValue, line, col, inComment, valueStart, valueEnd,
    };
    if (hit.method === 'pin') {
      row.coords = paren >= 0 ? coordsOf(argsOf(code, paren)) : { x: NaN, y: NaN, z: NaN };
    }
    out.push(row);
    return row;
  };
  segs.forEach((seg, idx) => {
    if (seg.type === 'comment') {
      const body = text.slice(seg.start, seg.end);
      const re = COMMENT_CALL_RE[language];
      re.lastIndex = 0;
      let m = re.exec(body);
      while (m !== null) {
        const hit = lookupCall(language, m[1], m[2]);
        if (hit && (!m[3] || m[3] === hit.param)) {
          const quoteAt = seg.start + m.index + m[0].length - m[5].length - 2;
          const row = push(hit, quoteAt, quoteAt + 1, quoteAt + 1 + m[5].length, true, -1);
          if (row && hit.method === 'pin') {
            // A comment holds no string tokens: its arguments, quotes blanked.
            const args = body.slice(m.index + m[0].indexOf('(') + 1);
            const close = args.indexOf(')');
            row.coords = coordsOf((close < 0 ? args : args.slice(0, close))
              .replace(/(["'])[^"'\n]*\1/g, (q) => ' '.repeat(q.length)));
          }
        }
        m = re.exec(body);
      }
      return;
    }
    if (seg.type !== 'string' || !literalIsUsable(seg)) return;
    const prev = segs[idx - 1];
    const next = segs[idx + 1];
    if (!prev || prev.type !== 'code') return;
    if (language === 'java' && seg.quote !== '"') return;
    // The literal must be the WHOLE argument.
    if (!next || next.type !== 'code' || !/^\s*[,)]/.test(text.slice(next.start, next.end))) return;
    const before = code.slice(Math.max(0, seg.start - 200), seg.start);
    const tail = CALL_TAIL_RE[language].exec(before);
    if (tail) {
      const hit = lookupCall(language, tail[1], tail[2]);
      if (hit) {
        push(hit, seg.start + seg.prefix.length, seg.valueStart, seg.valueEnd, false,
          seg.start - before.length + before.lastIndexOf('('));
      }
      return;
    }
    if (language !== 'python') return;
    // `name="…"` anywhere in a call's arguments: the call is found by
    // scanning back to its `(`, and the keyword must be the name the table
    // gives the asset parameter (review round 2, ni1).
    const kw = KEYWORD_TAIL_RE.exec(before);
    if (!kw) return;
    const paren = enclosingParen(code, seg.start - kw[0].length);
    if (paren < 0) return;
    const call = CALL_NAME_RE.python.exec(code.slice(Math.max(0, paren - 200), paren));
    const hit = call ? lookupCall(language, call[1], call[2]) : null;
    if (!hit || hit.param !== kw[1]) return;
    push(hit, seg.start + seg.prefix.length, seg.valueStart, seg.valueEnd, false, paren);
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

/**
 * The names the program itself defines with pin / pin_current, all files,
 * first-seen order. A call inside a comment defines nothing (review n2) —
 * unless `includeComments`, which the auto-name RESERVATION uses: a
 * commented-out pin keeps its name taken, as a disabled Blockly block does
 * (destinationStore.takenDestinationNames), so un-commenting it later never
 * collides with a Ziel taught meanwhile.
 */
export function codeDefinedPlaceNames(files, language, { includeComments = false } = {}) {
  const seen = new Set();
  const out = [];
  for (const [, content] of projectEntries(files)) {
    for (const call of findAssetCalls(content, language)) {
      if (call.inComment && !includeComments) continue;
      if ((call.method === 'pin' || call.method === 'pin_current') && !seen.has(call.name)) {
        seen.add(call.name);
        out.push(call.name);
      }
    }
  }
  return out;
}

// ── variables (display only: the Variablen tab before a run) ───────────────

// Identifiers are Unicode (Python 3, Java): `größe`, `Würfel` (review m3).
const ID = '[\\p{L}_][\\p{L}\\p{N}_]*';
// One target list and its `=` (or an augmented assignment) at the START of
// what is left of a line — applied again after each `=`, so `a = b = 1`
// lists both (review round 2, ni2).
const PY_TARGETS_RE = new RegExp(
  `^[ \\t]*(${ID}(?:[ \\t]*,[ \\t]*${ID})*)[ \\t]*(=(?!=)|\\+=|-=|\\*=|\\/=|\\/\\/=|%=|\\*\\*=|\\|=|&=|\\^=)`, 'u',
);
// An annotated assignment `x: int = 5` (the annotation holds no `=`).
const PY_ANNOTATED_RE = new RegExp(`^[ \\t]*(${ID})[ \\t]*:[ \\t]*[^=\\n]+?[ \\t]*=(?!=)`, 'u');
const PY_FOR_RE = new RegExp(`^[ \\t]*(?:async[ \\t]+)?for[ \\t]+(${ID}(?:[ \\t]*,[ \\t]*${ID})*)[ \\t]+in(?![\\p{L}\\p{N}_])`, 'u');
// Python's soft keywords can start a line with a `:` that is no annotation.
const PY_SOFT_KEYWORDS = new Set(['match', 'case', 'type']);
const JAVA_TYPE = '(?:int|long|double|float|boolean|char|byte|short|String|var|\\p{Lu}[\\p{L}\\p{N}_]*(?:<[^;(){}]*?>)?)';
// A declared name starts lower-case (or `_`) — the Java convention that tells
// `Würfel w` (type, name) from a statement.
const JAVA_DECL_RE = new RegExp(
  `(?<![\\p{L}\\p{N}_])${JAVA_TYPE}(?:\\[\\])*\\s+([\\p{Ll}_][\\p{L}\\p{N}_]*)\\s*(?==(?!=)|;|,|:|\\))`, 'gu',
);
const IDENT_RE = new RegExp(`^${ID}$`, 'u');
const WORD_RE = new RegExp(`(?<![\\p{L}\\p{N}_.])${ID}`, 'gu');
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
      // Only a line that starts a statement: inside brackets `x=0.1,` is a
      // keyword argument and `"k": 1,` a dict entry, not a variable.
      let depth = 0;
      for (let row = 0; row < starts.length; row += 1) {
        const from = starts[row];
        const to = row + 1 < starts.length ? starts[row + 1] - 1 : code.length;
        const line = code.slice(from, to);
        if (depth === 0) {
          const annotated = PY_ANNOTATED_RE.exec(line);
          const forLoop = PY_FOR_RE.exec(line);
          if (forLoop) {
            found.push({ index: from, names: forLoop[1].split(',').map((n) => n.trim()) });
          } else if (annotated && !PY_KEYWORDS.has(annotated[1]) && !PY_SOFT_KEYWORDS.has(annotated[1])) {
            found.push({ index: from, names: [annotated[1]] });
          } else {
            let rest = line;
            let m = PY_TARGETS_RE.exec(rest);
            while (m !== null) {
              found.push({ index: from, names: m[1].split(',').map((n) => n.trim()) });
              if (m[2] !== '=') break;
              rest = rest.slice(m[0].length);
              m = PY_TARGETS_RE.exec(rest);
            }
          }
        }
        for (const ch of line) {
          if (ch === '(' || ch === '[' || ch === '{') depth += 1;
          else if (ch === ')' || ch === ']' || ch === '}') depth = Math.max(0, depth - 1);
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
  if (!IDENT_RE.test(target)) return [];
  const re = new RegExp(`(?<![\\p{L}\\p{N}_.])${escapeRegExp(target)}(?![\\p{L}\\p{N}_])`, 'gu');
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

/**
 * variableOccurrences for MANY names in one pass per file (review m8: the
 * Variablen tab asked once per variable, re-tokenizing the whole project each
 * time): `Map name → [{file, line, col}]`, every requested name present.
 */
export function variableOccurrencesAll(files, language, names) {
  const out = new Map();
  for (const n of Array.isArray(names) ? names : []) out.set(String(n), []);
  if (out.size === 0) return out;
  for (const [file, content] of projectEntries(files)) {
    const code = codeOnlyText(content, language);
    const starts = lineStarts(code);
    WORD_RE.lastIndex = 0;
    let m = WORD_RE.exec(code);
    while (m !== null) {
      const rows = out.get(m[0]);
      if (rows) {
        const { line, col } = lineColAt(starts, m.index);
        rows.push({ file, line, col });
      }
      m = WORD_RE.exec(code);
    }
  }
  return out;
}
