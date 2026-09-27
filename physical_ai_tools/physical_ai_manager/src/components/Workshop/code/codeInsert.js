/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Putting Sammlung assets INTO a code program (owner decision O6): the calls a
// Vormachen round or a Sammlung row stands for, spelled in the program's
// language, and where they go.
//
//   * The spellings come from robot_api.json (Python name / Java name), never a
//     second copy: `robot.move_to("Ablage")` / `Robot.moveTo("Ablage");`.
//   * INDENTATION is the FILE's own (review round 2, R2-O2): detectIndentUnit
//     reads the step the file uses; a file with no indented line uses
//     CODE_INDENT_UNIT (4 spaces). The editor's indentUnit is the same
//     function of the same text (CodeEditor re-derives it as the file
//     changes), so an insertion and the student's Enter/Tab/Backspace agree.
//   * WHERE, from the cursor (review round 2, mi3): the STATEMENT on the
//     cursor's line, found by structure, not by the line alone — a line inside
//     brackets, a `\` continuation, a multi-line string or a multi-line call
//     moves to the statement's end; a decorator to its def's body; a block
//     opener (`:` / `{`) gets its body's first line. A statement that never
//     lets the next line run (`return`, `raise`/`throw`, `break`, `continue`,
//     an endless loop) gets the lines BEFORE it. A Java cursor outside a
//     method body (an import, a class or field line) writes nothing and says
//     so in German.
//   * WHERE, without a cursor (review R-O2, round 2 mi2): the end of the body
//     that runs LAST — Python: main.py's top level, descending into an
//     `if __name__ == "__main__":` block and into the function a final
//     top-level call runs; Java: `static void main(String[] …)` of a top-level
//     class, modifiers in any order. In both, BEFORE a last statement that
//     cannot complete normally (the JLS rule javac enforces as „unreachable
//     statement"): a loop whose condition is a constant (or might be one) with
//     no break out of it, `return`/`throw`, an if/else or try whose every way
//     out leaves, a labelled endless loop. Whatever the scanner cannot place
//     SAFELY (no main, unbalanced brackets, an unterminated string) is a German
//     hint, never a guess.
//   * A statement that shares its line with other code (a one-line main, a
//     brace on the statement's line) is opened up: the lines go on lines of
//     their own. A CRLF file gets CRLF line breaks.
//
// PURE — strings in, strings out. CodeEditor (a drop) and the code asset
// adapter (a row's „Einfügen", Vormachen's „Als Programm einfügen") call it.

import robotApi from './robot_api.json';
import { ENTRY_FILE } from './codeProject';
import { codeOnlyText, tokenizeCode } from './codeAssetUsage';
import { CODE_DE } from './codeMessagesDe';

/**
 * The drag-and-drop type of a Sammlung row dropped into the code editor: the
 * drawer sets it, CodeEditor reads it. The payload is `{kind, name}` JSON.
 */
export const SNIPPET_MIME = 'application/x-edubotics-snippet';

const METHODS = new Map((robotApi.methods || []).map((m) => [m.name, m]));
// An asset name that can sit between double quotes in both languages as is.
const SAFE_NAME_RE = /^[^"\\\n\r]+$/;
/** One indentation level for a file that has none yet (PEP 8's 4 spaces; the
 *  Java starter file uses 4 too). */
export const CODE_INDENT_UNIT = '    ';

/** One call as a line: `robot.name(args)` / `Robot.javaName(args);`. */
export function codeCallLine(method, args, language) {
  const row = METHODS.get(method);
  if (!row) return null;
  const list = (Array.isArray(args) ? args : []).join(', ');
  return language === 'java'
    ? `Robot.${row.java_name}(${list});`
    : `robot.${row.name}(${list})`;
}

function quoted(name) {
  const s = typeof name === 'string' ? name.trim() : '';
  return s && SAFE_NAME_RE.test(s) ? `"${s}"` : null;
}

function stepLine(step, language) {
  if (!step || typeof step !== 'object') return null;
  if (step.type === 'replay' || step.type === 'move_to') {
    const arg = quoted(step.name);
    return arg ? codeCallLine(step.type, [arg], language) : null;
  }
  if (step.type === 'close_gripper' || step.type === 'open_gripper') {
    return codeCallLine(step.type, [], language);
  }
  return null;
}

/** `teach/insertProgram.js::buildProgramSteps` output → the program's lines. */
export function stepsToCode(steps, language) {
  return (Array.isArray(steps) ? steps : []).map((s) => stepLine(s, language)).filter(Boolean);
}

/** A Sammlung row (`{kind, name}`) as the line(s) that use it; [] when none. */
export function snippetLines(asset, language) {
  if (!asset || typeof asset !== 'object') return [];
  if (asset.kind === 'recording') return stepsToCode([{ type: 'replay', name: asset.name }], language);
  if (asset.kind === 'pin' || asset.kind === 'pose') {
    return stepsToCode([{ type: 'move_to', name: asset.name }], language);
  }
  return [];
}

// ── indentation ────────────────────────────────────────────────────────────

function leadingWhitespace(line) {
  const m = /^[ \t]*/.exec(line || '');
  return m ? m[0] : '';
}

/**
 * The indentation step `content` uses: the most common increase from one
 * code line to the next (a tab when tabs win; ties go to the smaller step),
 * or null when nothing is indented. Strings and comments do not count.
 */
export function detectIndentUnit(content, language = 'python') {
  const code = codeOnlyText(typeof content === 'string' ? content : '', language);
  const votes = new Map();
  let prev = '';
  for (const row of code.split('\n')) {
    if (row.trim() === '') continue;
    const own = leadingWhitespace(row);
    if (own.length > prev.length && own.startsWith(prev)) {
      const delta = own.slice(prev.length);
      const key = delta.includes('\t') ? '\t' : delta;
      votes.set(key, (votes.get(key) || 0) + 1);
    }
    prev = own;
  }
  let best = null;
  for (const [unit, n] of votes) {
    const bestN = best === null ? -1 : votes.get(best);
    if (n > bestN || (n === bestN && unit.length < best.length)) best = unit;
  }
  return best;
}

/** The indentation unit of a file — the editor's AND the insertion's (R2-O2). */
export function fileIndentUnit(content, language = 'python') {
  return detectIndentUnit(content, language) || CODE_INDENT_UNIT;
}

// ── the smallest editor change ─────────────────────────────────────────────

/**
 * The smallest single replacement `{from, to, insert}` that turns `prev` into
 * `next` (common prefix and suffix left alone), or null when they are equal.
 * An edit made OUTSIDE the editor (a rename, an insertion from the drawer) is
 * dispatched as this change, so the student's cursor survives wherever the
 * text around it did not change, and Strg+Z undoes exactly that edit.
 */
export function minimalChange(prev, next) {
  const a = typeof prev === 'string' ? prev : '';
  const b = typeof next === 'string' ? next : '';
  if (a === b) return null;
  let start = 0;
  const max = Math.min(a.length, b.length);
  while (start < max && a.charCodeAt(start) === b.charCodeAt(start)) start += 1;
  let end = 0;
  while (end < max - start && a.charCodeAt(a.length - 1 - end) === b.charCodeAt(b.length - 1 - end)) end += 1;
  return { from: start, to: a.length - end, insert: b.slice(start, b.length - end) };
}

// ── rows ───────────────────────────────────────────────────────────────────

function lineStarts(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i += 1) if (text[i] === '\n') starts.push(i + 1);
  return starts;
}

function rowAt(starts, offset) {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= offset) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

function rowEnd(text, starts, row) {
  return row + 1 < starts.length ? starts[row + 1] - 1 : text.length;
}

const notFound = (hint) => ({ notFound: true, hint });

// ══ Python ══════════════════════════════════════════════════════════════════

const PY_TERMINAL_WORDS = new Set(['return', 'raise', 'break', 'continue']);
const PY_COMPOUND = new Set(['if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally',
  'with', 'def', 'class', 'async', 'match', 'case']);
const PY_FIRST_RE = /^\s*(@|[\p{L}_][\p{L}\p{N}_]*)/u;
// `while True:` / `while 1:` / `while (True):` — a literal truthy condition.
const PY_ENDLESS_RE = /^\s*while\s*\(?\s*(?:True|[1-9][0-9]*)\s*\)?\s*:/;
const PY_MAIN_GUARD_RE = /^\s*if\s+(?:__name__\s*==\s*(['"])__main__\1|(['"])__main__\2\s*==\s*__name__)\s*:\s*(?:#.*)?$/;
const PY_BARE_CALL_RE = /^\s*([\p{L}_][\p{L}\p{N}_]*)\s*\(/u;
const PY_DEF_RE = /^\s*(?:async\s+)?def\s+([\p{L}_][\p{L}\p{N}_]*)\s*\(/u;

// Split code-only text at `;` outside brackets.
function simpleParts(code) {
  const parts = [];
  let depth = 0;
  let from = 0;
  for (let i = 0; i < code.length; i += 1) {
    const ch = code[i];
    if (ch === '(' || ch === '[' || ch === '{') depth += 1;
    else if (ch === ')' || ch === ']' || ch === '}') depth = Math.max(0, depth - 1);
    else if (ch === ';' && depth === 0) {
      parts.push(code.slice(from, i));
      from = i + 1;
    }
  }
  parts.push(code.slice(from));
  return parts.filter((p) => p.trim() !== '');
}

/**
 * The logical statements of a Python file: `{startRow, endRow, indent, code,
 * raw, first, opener}` (0-based rows), a statement spanning every row its
 * brackets, `\` continuations and multi-line strings reach. `broken` when the
 * file ends inside brackets or an unterminated triple-quoted string.
 */
function pythonStatements(text) {
  const segs = tokenizeCode(text, 'python');
  const code = codeOnlyText(text, 'python');
  const starts = lineStarts(text);
  const codeRows = code.split('\n');
  const rawRows = text.split('\n');
  const n = codeRows.length;
  // A row that begins inside a string started on an earlier row continues
  // it; a row on which a string starts has content even though its code-only
  // text is blank (a docstring is a statement).
  const inString = new Array(n).fill(false);
  const stringStarts = new Array(n).fill(false);
  let broken = false;
  for (const seg of segs) {
    if (seg.type !== 'string') continue;
    if (seg.triple && !seg.closed) broken = true;
    const r0 = rowAt(starts, seg.start);
    const r1 = rowAt(starts, Math.max(seg.start, seg.end - 1));
    stringStarts[r0] = true;
    for (let r = r0 + 1; r <= r1; r += 1) inString[r] = true;
  }
  const stmts = [];
  let cur = null;
  let depth = 0;
  let backslash = false;
  for (let r = 0; r < n; r += 1) {
    const row = codeRows[r];
    const continues = cur !== null && (depth > 0 || backslash || inString[r]);
    if (continues) {
      cur.endRow = r;
    } else if (row.trim() !== '' || stringStarts[r]) {
      cur = { startRow: r, endRow: r, indent: leadingWhitespace(rawRows[r]) };
      stmts.push(cur);
    } else {
      cur = null;
    }
    for (const ch of row) {
      if (ch === '(' || ch === '[' || ch === '{') depth += 1;
      else if (ch === ')' || ch === ']' || ch === '}') depth = Math.max(0, depth - 1);
    }
    backslash = /\\\s*$/.test(row);
  }
  if (depth > 0) broken = true;
  for (const s of stmts) {
    s.code = codeRows.slice(s.startRow, s.endRow + 1).join('\n');
    s.raw = rawRows.slice(s.startRow, s.endRow + 1).join('\n');
    const m = PY_FIRST_RE.exec(s.code);
    s.first = m ? m[1] : '';
    s.opener = /:\s*$/.test(s.code);
  }
  return { stmts, broken, starts };
}

// The index one past the last statement of `i`'s extent (its header plus every
// deeper-indented statement after it).
function pyExtentEnd(stmts, i) {
  let j = i + 1;
  while (j < stmts.length && stmts[j].indent.length > stmts[i].indent.length) j += 1;
  return j;
}

// The direct children of opener `i`: the statements at its body's indentation.
function pyChildren(stmts, i) {
  const end = pyExtentEnd(stmts, i);
  if (end === i + 1) return [];
  const bodyIndent = stmts[i + 1].indent.length;
  const out = [];
  for (let j = i + 1; j < end; j += 1) if (stmts[j].indent.length === bodyIndent) out.push(j);
  return out;
}

// Never lets the next line in its block run: return/raise/break/continue
// (in any of a line's `;`-separated statements), or an endless `while`
// (a block header or a one-line loop).
function pyNeverFallsThrough(s) {
  if (PY_ENDLESS_RE.test(s.code)) return true;
  if (s.opener || PY_COMPOUND.has(s.first)) return false;
  return simpleParts(s.code).some((p) => {
    const m = PY_FIRST_RE.exec(p);
    return m && PY_TERMINAL_WORDS.has(m[1]);
  });
}

function pyRowOp(text, starts, s, mode, indent) {
  if (mode === 'before') return { mode, at: starts[s.startRow], indent };
  return { mode, at: rowEnd(text, starts, s.endRow), indent };
}

// Before a statement that never falls through, else after its whole extent.
function pyEndOfBody(text, parsed, indices) {
  const { stmts, starts } = parsed;
  const last = indices[indices.length - 1];
  const s = stmts[last];
  if (pyNeverFallsThrough(s)) return pyRowOp(text, starts, s, 'before', s.indent);
  const endStmt = stmts[pyExtentEnd(stmts, last) - 1];
  return { mode: 'after', at: rowEnd(text, starts, endStmt.endRow), indent: s.indent };
}

function pythonMainOp(text) {
  const parsed = pythonStatements(text);
  const { stmts } = parsed;
  if (parsed.broken) return notFound(CODE_DE.NO_SAFE_PLACE_HINT);
  if (stmts.length === 0) return { mode: 'after', at: text.length, indent: '' };
  const defs = new Map();
  let body = [];
  stmts.forEach((s, i) => {
    if (s.indent === '') {
      body.push(i);
      const m = PY_DEF_RE.exec(s.code);
      if (m && s.opener) defs.set(m[1], i);
    }
  });
  // Descend to the body that runs last: the `__main__` block, then the
  // top-level function its (or the module's) final bare call runs.
  const seen = new Set();
  let atModule = true;
  for (let hops = 0; hops < 16 && body.length > 0; hops += 1) {
    const last = stmts[body[body.length - 1]];
    if (atModule && last.opener && PY_MAIN_GUARD_RE.test(last.raw)) {
      const children = pyChildren(stmts, body[body.length - 1]);
      if (children.length === 0) break;
      body = children;
      atModule = false;
      continue;
    }
    const call = !last.opener && simpleParts(last.code).length === 1 ? PY_BARE_CALL_RE.exec(last.code) : null;
    const target = call && defs.has(call[1]) && !seen.has(call[1]) ? defs.get(call[1]) : null;
    if (target === null) break;
    const children = pyChildren(stmts, target);
    if (children.length === 0) break;
    seen.add(call[1]);
    body = children;
    atModule = false;
  }
  return pyEndOfBody(text, parsed, body);
}

// The last row that holds text (a final newline opens no row of its own).
function lastRealRow(text, starts) {
  return Math.max(0, text.endsWith('\n') ? starts.length - 2 : starts.length - 1);
}

function pythonCursorOp(text, line) {
  const parsed = pythonStatements(text);
  const { stmts, starts } = parsed;
  const r = Math.min(Math.max(line - 1, 0), lastRealRow(text, starts));
  let idx = -1;
  for (let i = 0; i < stmts.length; i += 1) {
    if (stmts[i].startRow <= r) idx = i;
    else break;
  }
  if (idx < 0) {
    // Above the first statement: the lines go below the cursor, top level.
    return { mode: 'after', at: rowEnd(text, starts, r), indent: '' };
  }
  const contains = stmts[idx].endRow >= r;
  // A decorator belongs to its def/class: the lines go into that body.
  while (stmts[idx].first === '@' && idx + 1 < stmts.length && stmts[idx + 1].indent === stmts[idx].indent) {
    idx += 1;
  }
  const s = stmts[idx];
  const unit = fileIndentUnit(text, 'python');
  if (s.opener) {
    const body = idx + 1 < stmts.length && stmts[idx + 1].indent.length > s.indent.length
      ? stmts[idx + 1].indent : s.indent + unit;
    return { mode: 'after', at: rowEnd(text, starts, s.endRow), indent: body };
  }
  if (pyNeverFallsThrough(s)) return pyRowOp(text, starts, s, 'before', s.indent);
  // Below the cursor's own row when it sits on a blank or comment line after
  // the statement; else below the statement's last row.
  const row = contains ? s.endRow : Math.max(s.endRow, r);
  return { mode: 'after', at: rowEnd(text, starts, row), indent: s.indent };
}

// ══ Java ════════════════════════════════════════════════════════════════════

const JAVA_IDENT_START = /[\p{L}_$]/u;
const JAVA_IDENT_CHAR = /[\p{L}\p{N}_$]/u;
const JAVA_KEYWORDS = new Set(['abstract', 'assert', 'boolean', 'break', 'byte', 'case', 'catch',
  'char', 'class', 'const', 'continue', 'default', 'do', 'double', 'else', 'enum', 'extends', 'final',
  'finally', 'float', 'for', 'goto', 'if', 'implements', 'import', 'instanceof', 'int', 'interface',
  'long', 'native', 'new', 'package', 'private', 'protected', 'public', 'return', 'short', 'static',
  'strictfp', 'super', 'switch', 'synchronized', 'this', 'throw', 'throws', 'transient', 'try',
  'void', 'volatile', 'while', 'true', 'false', 'null', 'var', 'yield', 'record', 'sealed',
  'permits', 'non']);
const JAVA_TYPE_WORDS = new Set(['class', 'interface', 'enum', 'record']);
const JAVA_MODIFIERS = new Set(['public', 'protected', 'private', 'static', 'final', 'abstract',
  'strictfp', 'sealed', 'non-sealed', 'synchronized', 'native', 'transient', 'volatile', 'default']);
const JAVA_BLOCK_KEYWORDS = new Set(['if', 'while', 'for', 'switch', 'catch', 'synchronized', 'try']);

function javaBrackets(code) {
  const pair = new Map();
  const stack = [];
  for (let i = 0; i < code.length; i += 1) {
    const ch = code[i];
    if (ch === '(' || ch === '[' || ch === '{') stack.push(i);
    else if (ch === ')' || ch === ']' || ch === '}') {
      const open = stack.pop();
      const want = ch === ')' ? '(' : ch === ']' ? '[' : '{';
      if (open === undefined || code[open] !== want) return null;
      pair.set(open, i);
      pair.set(i, open);
    }
  }
  return stack.length === 0 ? pair : null;
}

function skipWs(code, i, end = code.length) {
  let j = i;
  while (j < end && /\s/.test(code[j])) j += 1;
  return j;
}

function skipWsBack(code, i) {
  let j = i;
  while (j >= 0 && /\s/.test(code[j])) j -= 1;
  return j;
}

function wordAt(code, i) {
  if (i >= code.length || !JAVA_IDENT_START.test(code[i])) return '';
  let j = i + 1;
  while (j < code.length && JAVA_IDENT_CHAR.test(code[j])) j += 1;
  return code.slice(i, j);
}

// The identifier ending at `i` (inclusive), and where it starts.
function wordBefore(code, i) {
  let j = i;
  while (j >= 0 && JAVA_IDENT_CHAR.test(code[j])) j -= 1;
  return { word: code.slice(j + 1, i + 1), start: j + 1 };
}

const or3 = (a, b) => (a === 'yes' || b === 'yes' ? 'yes' : a === 'no' && b === 'no' ? 'no' : 'unsure');
const and3 = (a, b) => (a === 'no' || b === 'no' ? 'no' : a === 'yes' && b === 'yes' ? 'yes' : 'unsure');

/**
 * A Java statement parser over code-only text — just enough to answer, for
 * each statement of a block, where it starts and ends and whether it can
 * COMPLETE NORMALLY (JLS §14.22, the rule javac's „unreachable statement"
 * enforces): 'yes' | 'no' | 'unsure'. `breaks` carries the break targets that
 * escape the statement ('' = an unlabelled break).
 */
class JavaParser {
  constructor(code, pair) {
    this.code = code;
    this.pair = pair;
    this.broken = false;
  }

  block(from, to) {
    const out = [];
    let i = skipWs(this.code, from, to);
    while (i < to && !this.broken) {
      const st = this.statement(i, to);
      if (!st || st.end <= i) {
        this.broken = true;
        break;
      }
      out.push(st);
      i = skipWs(this.code, st.end, to);
    }
    return out;
  }

  // One statement starting at `i`, or null.
  statement(i, to) {
    const { code, pair } = this;
    const start = i;
    const ch = code[i];
    if (ch === '{') {
      const close = pair.get(i);
      const inner = this.block(i + 1, close);
      return this.made(start, close + 1, inner.length ? inner[inner.length - 1].completes : 'yes',
        this.unionBreaks(inner));
    }
    if (ch === ';') return this.made(start, i + 1, 'yes');
    if (ch === '@') {
      // An annotation on a local declaration: skip it, the statement follows.
      let j = i + 1;
      j += wordAt(code, j).length;
      while (code[j] === '.') {
        j += 1;
        j += wordAt(code, j).length;
      }
      j = skipWs(code, j, to);
      if (code[j] === '(') j = pair.get(j) + 1;
      const inner = this.statement(skipWs(code, j, to), to);
      return inner ? { ...inner, start } : null;
    }
    const word = wordAt(code, i);
    const after = skipWs(code, i + word.length, to);
    if (word && !JAVA_KEYWORDS.has(word) && code[after] === ':' && code[after + 1] !== ':') {
      const inner = this.statement(skipWs(code, after + 1, to), to);
      if (!inner) return null;
      const breaks = new Set(inner.breaks);
      const brokenOut = breaks.delete(word);
      return this.made(start, inner.end, brokenOut ? 'yes' : inner.completes, breaks);
    }
    switch (word) {
      case 'if': return this.ifStatement(start, after, to);
      case 'while': return this.whileStatement(start, after, to);
      case 'do': return this.doStatement(start, after, to);
      case 'for': return this.forStatement(start, after, to);
      case 'try': return this.tryStatement(start, after, to);
      case 'switch': {
        const close = this.closeOf(after);
        if (close === null) return null;
        const body = skipWs(code, close + 1, to);
        if (code[body] !== '{') return this.made(start, null);
        return this.made(start, pair.get(body) + 1, 'unsure');
      }
      case 'synchronized': {
        const close = this.closeOf(after);
        if (close === null) return null;
        const inner = this.statement(skipWs(code, close + 1, to), to);
        return inner ? this.made(start, inner.end, inner.completes, inner.breaks) : null;
      }
      case 'return': case 'throw': case 'yield':
        return this.made(start, this.toSemicolon(after, to), 'no');
      case 'continue':
        return this.made(start, this.toSemicolon(after, to), 'no', new Set(), true);
      case 'break': {
        const label = wordAt(code, after);
        return this.made(start, this.toSemicolon(after, to), 'no', new Set([label]));
      }
      case 'case': case 'default': {
        // A switch label: up to its `:` or `->`, the statement follows.
        let j = after;
        while (j < to && code[j] !== ':' && !(code[j] === '-' && code[j + 1] === '>')) {
          j = pair.has(j) && '([{'.includes(code[j]) ? pair.get(j) + 1 : j + 1;
        }
        return this.made(start, code[j] === ':' ? j + 1 : j + 2, 'yes');
      }
      default: break;
    }
    if (this.startsTypeDeclaration(i, to)) {
      let j = i;
      while (j < to && code[j] !== '{') j = pair.has(j) && code[j] === '(' ? pair.get(j) + 1 : j + 1;
      if (j >= to) return null;
      return this.made(start, pair.get(j) + 1, 'yes');
    }
    return this.made(start, this.toSemicolon(i, to), 'yes');
  }

  made(start, end, completes, breaks = new Set(), continues = false) {
    if (!Number.isInteger(end) || end <= start) {
      this.broken = true;
      return null;
    }
    return {
      start, end, completes, breaks, continues,
    };
  }

  unionBreaks(list) {
    const out = new Set();
    for (const s of list) for (const b of s.breaks) out.add(b);
    return out;
  }

  // Past the `;` that ends an expression/declaration statement (brackets,
  // lambdas and anonymous class bodies skipped whole).
  toSemicolon(i, to) {
    const { code, pair } = this;
    let j = i;
    while (j < to) {
      if (code[j] === ';') return j + 1;
      j = pair.has(j) && '([{'.includes(code[j]) ? pair.get(j) + 1 : j + 1;
    }
    this.broken = true;
    return null;
  }

  startsTypeDeclaration(i, to) {
    let j = i;
    for (let k = 0; k < 8 && j < to; k += 1) {
      const w = wordAt(this.code, j);
      if (JAVA_TYPE_WORDS.has(w)) return true;
      if (!JAVA_MODIFIERS.has(w)) return false;
      j = skipWs(this.code, j + w.length, to);
    }
    return false;
  }

  body(i, to) {
    return this.statement(skipWs(this.code, i, to), to);
  }

  // The `)` closing the `(` at `paren`, or null (and the parse is broken).
  closeOf(paren) {
    if (this.code[paren] !== '(' || !this.pair.has(paren)) {
      this.broken = true;
      return null;
    }
    return this.pair.get(paren);
  }

  ifStatement(start, paren, to) {
    const { code } = this;
    const close = this.closeOf(paren);
    if (close === null) return null;
    const then = this.body(close + 1, to);
    if (!then) return null;
    const j = skipWs(code, then.end, to);
    if (wordAt(code, j) === 'else') {
      const other = this.body(j + 4, to);
      if (!other) return null;
      return this.made(start, other.end, or3(then.completes, other.completes),
        this.unionBreaks([then, other]));
    }
    return this.made(start, then.end, 'yes', then.breaks);
  }

  loop(start, end, condKind, body) {
    const breaks = new Set(body.breaks);
    const brokenOut = breaks.delete('');
    let completes;
    if (condKind === 'true') completes = brokenOut ? 'yes' : 'no';
    else if (condKind === 'unsure') completes = 'unsure';
    else completes = 'yes';
    return this.made(start, end, completes, breaks);
  }

  whileStatement(start, paren, to) {
    const close = this.closeOf(paren);
    if (close === null) return null;
    const body = this.body(close + 1, to);
    if (!body) return null;
    return this.loop(start, body.end, this.condKind(this.code.slice(paren + 1, close)), body);
  }

  doStatement(start, after, to) {
    const { code } = this;
    const body = this.body(after, to);
    if (!body) return null;
    const w = skipWs(code, body.end, to);
    if (wordAt(code, w) !== 'while') return this.made(start, null);
    const paren = skipWs(code, w + 5, to);
    const close = this.closeOf(paren);
    if (close === null) return null;
    const end = this.toSemicolon(close + 1, to);
    const kind = this.condKind(code.slice(paren + 1, close));
    const breaks = new Set(body.breaks);
    const brokenOut = breaks.delete('');
    let completes;
    if (kind === 'true') completes = brokenOut ? 'yes' : 'no';
    else if (kind === 'unsure') completes = 'unsure';
    else completes = brokenOut || body.continues ? 'yes' : body.completes;
    return this.made(start, end, completes, breaks);
  }

  forStatement(start, paren, to) {
    const { code } = this;
    const close = this.closeOf(paren);
    if (close === null) return null;
    const header = code.slice(paren + 1, close);
    const body = this.body(close + 1, to);
    if (!body) return null;
    const parts = [];
    let depth = 0;
    let from = 0;
    for (let k = 0; k < header.length; k += 1) {
      const c = header[k];
      if ('([{'.includes(c)) depth += 1;
      else if (')]}'.includes(c)) depth -= 1;
      else if (c === ';' && depth === 0) {
        parts.push(header.slice(from, k));
        from = k + 1;
      }
    }
    parts.push(header.slice(from));
    if (parts.length < 3) {
      // Enhanced for: it ends when the collection does.
      const breaks = new Set(body.breaks);
      breaks.delete('');
      return this.made(start, body.end, 'yes', breaks);
    }
    const cond = parts[1].trim();
    const loopVars = new Set();
    for (const m of parts[0].matchAll(/([\p{L}_$][\p{L}\p{N}_$]*)\s*=(?!=)/gu)) loopVars.add(m[1]);
    const kind = cond === '' ? 'true' : this.condKind(cond, loopVars);
    return this.loop(start, body.end, kind, body);
  }

  tryStatement(start, after, to) {
    const { code, pair } = this;
    let j = after;
    if (code[j] === '(') j = skipWs(code, pair.get(j) + 1, to);
    const tryBlock = this.statement(j, to);
    if (!tryBlock) return null;
    let completes = tryBlock.completes;
    const parts = [tryBlock];
    let end = tryBlock.end;
    let k = skipWs(code, end, to);
    while (wordAt(code, k) === 'catch') {
      const close = this.closeOf(skipWs(code, k + 5, to));
      if (close === null) return null;
      const block = this.statement(skipWs(code, close + 1, to), to);
      if (!block) return null;
      completes = or3(completes, block.completes);
      parts.push(block);
      end = block.end;
      k = skipWs(code, end, to);
    }
    if (wordAt(code, k) === 'finally') {
      const block = this.statement(skipWs(code, k + 7, to), to);
      if (!block) return null;
      completes = and3(completes, block.completes);
      parts.push(block);
      end = block.end;
    }
    return this.made(start, end, completes, this.unionBreaks(parts));
  }

  /**
   * A loop condition: 'true' (the literal `true`), 'var' (provably NOT a
   * constant expression: a call, an assignment, an increment, `new`, or only
   * names this file declares without `final` / the loop's own variables), or
   * 'unsure' (literals only — `1 == 1` IS a constant — or a name that could be
   * a constant variable). An unsure loop gets the lines before it, which is
   * always reachable when the loop is.
   */
  condKind(cond, loopVars = new Set()) {
    let c = cond.replace(/\s+/g, ' ').trim();
    while (c.startsWith('(') && this.wrapped(c)) c = c.slice(1, -1).trim();
    if (c === 'true') return 'true';
    if (/\b(?:new|instanceof)\b|\+\+|--|[^=!<>]=(?!=)|[\p{L}\p{N}_$]\s*\(|\[/u.test(c)) return 'var';
    const names = [...c.matchAll(/[\p{L}_$][\p{L}\p{N}_$.]*/gu)].map((m) => m[0])
      .filter((w) => !['true', 'false', 'null'].includes(w));
    if (names.length === 0) return 'unsure';
    return names.every((w) => loopVars.has(w) || this.declaredNonFinal(w)) ? 'var' : 'unsure';
  }

  wrapped(c) {
    let depth = 0;
    for (let k = 0; k < c.length; k += 1) {
      if (c[k] === '(') depth += 1;
      else if (c[k] === ')') {
        depth -= 1;
        if (depth === 0 && k < c.length - 1) return false;
      }
    }
    return c.endsWith(')');
  }

  // `name` is declared in this file, and never with `final`.
  declaredNonFinal(name) {
    if (!/^[\p{L}_$][\p{L}\p{N}_$]*$/u.test(name)) return false;
    const esc = name.replace(/\$/g, '\\$');
    const decl = new RegExp(
      `(?:^|[;{}(,])\\s*((?:(?:@[\\p{L}_$][\\p{L}\\p{N}_$.]*|[a-z]+)\\s+)*)`
      + `[\\p{L}_$][\\p{L}\\p{N}_$.]*(?:<[^;{}()]*>)?(?:\\s*\\[\\s*\\])*\\s+${esc}\\s*(?:=|;|,|:|\\))`, 'gu',
    );
    let found = false;
    for (const m of this.code.matchAll(decl)) {
      found = true;
      if (/\bfinal\b/.test(m[1])) return false;
    }
    return found;
  }
}

// What an opening brace opens: 'block' (statements go there), 'class' (a
// type body: fields and methods) or 'array' (an initializer: an expression).
function braceKind(code, pair, open, enclosing) {
  const j = skipWsBack(code, open - 1);
  if (j < 0) return 'class';
  const ch = code[j];
  if (ch === ')') {
    const paren = pair.get(j);
    const k = skipWsBack(code, paren - 1);
    const { word, start } = wordBefore(code, k);
    if (JAVA_BLOCK_KEYWORDS.has(word)) return 'block';
    // `new Type(…) {` (possibly qualified or generic) is an anonymous class.
    let m = skipWsBack(code, start - 1);
    while (m >= 0 && (code[m] === '.' || code[m] === '>')) {
      if (code[m] === '>') {
        let depth = 0;
        while (m >= 0) {
          if (code[m] === '>') depth += 1;
          else if (code[m] === '<') {
            depth -= 1;
            if (depth === 0) break;
          }
          m -= 1;
        }
        m = skipWsBack(code, m - 1);
      } else {
        m = skipWsBack(code, wordBefore(code, skipWsBack(code, m - 1)).start - 1);
      }
    }
    if (wordBefore(code, m).word === 'new') return 'class';
    if (wordBefore(code, skipWsBack(code, start - 1)).word === 'record') return 'class';
    return 'block';
  }
  if (ch === '>' && code[j - 1] === '-') return 'block';
  if (JAVA_IDENT_CHAR.test(ch) || ch === '>') {
    const { word } = wordBefore(code, j);
    if (['else', 'try', 'finally', 'do', 'static'].includes(word)) return 'block';
    // `) throws A, B {` is a method body.
    const head = code.slice(Math.max(0, j - 400), j + 1);
    if (/\)\s*throws\s+[\p{L}\p{N}_$.<>,\s]+$/u.test(head)) return 'block';
    return 'class';
  }
  if (ch === '{' || ch === ';' || ch === '}') return enclosing === 'array' ? 'array' : 'block';
  if (ch === ':') return 'block';
  return 'array';
}

// The open brackets around `pos` (innermost last), each `{kind, open}`.
function javaEnclosing(code, pair, pos) {
  const stack = [];
  for (let i = 0; i < pos && i < code.length; i += 1) {
    const ch = code[i];
    if (ch === '(' || ch === '[') stack.push({ kind: 'paren', open: i });
    else if (ch === '{') {
      const outer = stack.length ? stack[stack.length - 1].kind : 'top';
      stack.push({ kind: braceKind(code, pair, i, outer), open: i });
    } else if (ch === ')' || ch === ']' || ch === '}') stack.pop();
  }
  return stack;
}

const MAIN_PARAMS_RE = /^\s*(?:final\s+)?(?:@[\p{L}_$][\p{L}\p{N}_$.]*\s+)*(?:final\s+)?(?:java\s*\.\s*lang\s*\.\s*)?String\s*(?:\[\s*\]\s*[\p{L}_$][\p{L}\p{N}_$]*|\.\.\.\s*[\p{L}_$][\p{L}\p{N}_$]*|[\p{L}_$][\p{L}\p{N}_$]*\s*\[\s*\])\s*$/u;

/**
 * The body `{open, close}` of the program's `static void main(String[] …)`:
 * a method of a TOP-LEVEL type (never a nested class's, never an overload
 * with other parameters), modifiers in any order, generics allowed. When
 * several top-level types have one, the type named `Main` wins; else null.
 */
function javaMainBody(code, pair) {
  const found = [];
  let depth = 0;
  for (let i = 0; i < code.length; i += 1) {
    const ch = code[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') depth -= 1;
    if (depth !== 0 || !JAVA_IDENT_START.test(ch) || (i > 0 && JAVA_IDENT_CHAR.test(code[i - 1]))) continue;
    const w = wordAt(code, i);
    if (!JAVA_TYPE_WORDS.has(w)) continue;
    const nameAt = skipWs(code, i + w.length);
    const typeName = wordAt(code, nameAt);
    let b = nameAt + typeName.length;
    while (b < code.length && code[b] !== '{') b = pair.has(b) && code[b] === '(' ? pair.get(b) + 1 : b + 1;
    if (b >= code.length) break;
    const bodyEnd = pair.get(b);
    // Members directly in this body.
    let member = b + 1;
    let k = b + 1;
    while (k < bodyEnd) {
      const c = code[k];
      if (c === '(' || c === '[') {
        const w2 = wordBefore(code, skipWsBack(code, k - 1)).word;
        const close = pair.get(k);
        if (c === '(' && w2 === 'main') {
          const head = code.slice(member, k).replace(/\s+/g, ' ');
          const params = code.slice(k + 1, close);
          let after = skipWs(code, close + 1, bodyEnd);
          if (wordAt(code, after) === 'throws') {
            while (after < bodyEnd && code[after] !== '{' && code[after] !== ';') after += 1;
          }
          if (code[after] === '{' && /\bstatic\b/.test(head) && /\bvoid\s+main\s*$/.test(head)
              && MAIN_PARAMS_RE.test(params)) {
            found.push({ typeName, open: after, close: pair.get(after) });
          }
        }
        k = close + 1;
        continue;
      }
      if (c === '{') {
        k = pair.get(k) + 1;
        member = k;
        continue;
      }
      if (c === ';' || c === '}') member = k + 1;
      k += 1;
    }
    i = bodyEnd;
  }
  if (found.length === 1) return found[0];
  return found.find((f) => f.typeName === 'Main') || null;
}

function javaIndents(text, starts, open, unit) {
  const owner = leadingWhitespace(text.slice(starts[rowAt(starts, open)]));
  return { owner, inner: owner + unit };
}

// Where a statement's own line indentation is, if it starts its line.
function ownLineIndent(text, starts, offset) {
  const row = rowAt(starts, offset);
  const head = text.slice(starts[row], offset);
  return head.trim() === '' ? head : null;
}

// Before the last statement when it cannot complete normally (or might not),
// else after it; an empty block gets the lines at its start.
function javaBlockEndOp(text, starts, code, parser, block, unit) {
  const stmts = parser.block(block.open + 1, block.close);
  if (parser.broken) return notFound(CODE_DE.NO_SAFE_PLACE_HINT);
  const { owner, inner } = javaIndents(text, starts, block.open, unit);
  if (stmts.length === 0) return { mode: 'start', at: block.open + 1, indent: inner, closeIndent: owner };
  const last = stmts[stmts.length - 1];
  const indent = ownLineIndent(text, starts, last.start) ?? inner;
  if (last.completes === 'yes') return { mode: 'after', at: last.end, indent, closeIndent: owner };
  return { mode: 'before', at: last.start, indent, closeIndent: owner };
}

function javaMainOp(text) {
  const code = codeOnlyText(text, 'java');
  const pair = javaBrackets(code);
  if (!pair) return notFound(CODE_DE.NO_SAFE_PLACE_HINT);
  const main = javaMainBody(code, pair);
  if (!main) return notFound(CODE_DE.NO_MAIN_HINT);
  const starts = lineStarts(text);
  return javaBlockEndOp(text, starts, code, new JavaParser(code, pair), main, fileIndentUnit(text, 'java'));
}

// The innermost statement block around `pos`, or null when a type body (a
// class line, a field) or the top level is closer.
function javaBlockAround(code, pair, pos) {
  const stack = javaEnclosing(code, pair, pos);
  for (let k = stack.length - 1; k >= 0; k -= 1) {
    const b = stack[k];
    if (b.kind === 'block') return { open: b.open, close: pair.get(b.open) };
    if (b.kind === 'class') return null;
  }
  return null;
}

function javaCursorOp(text, line) {
  const code = codeOnlyText(text, 'java');
  const pair = javaBrackets(code);
  if (!pair) return notFound(CODE_DE.NO_SAFE_PLACE_HINT);
  const starts = lineStarts(text);
  const row = Math.min(Math.max(line - 1, 0), lastRealRow(text, starts));
  let pos = rowEnd(text, starts, row);
  let block = javaBlockAround(code, pair, pos);
  if (!block) {
    // A line that closes a block (a method's `}`, a one-line main): the lines
    // go into the last block closed on it, never into the class around it.
    const rowCode = code.slice(starts[row], rowEnd(text, starts, row));
    const close = rowCode.lastIndexOf('}');
    if (close >= 0) {
      pos = starts[row] + close;
      block = javaBlockAround(code, pair, pos);
    }
  }
  if (!block) return notFound(CODE_DE.NOT_IN_METHOD_HINT);
  const parser = new JavaParser(code, pair);
  const stmts = parser.block(block.open + 1, block.close);
  if (parser.broken) return notFound(CODE_DE.NO_SAFE_PLACE_HINT);
  const unit = fileIndentUnit(text, 'java');
  const { owner, inner } = javaIndents(text, starts, block.open, unit);
  let anchor = null;
  for (const s of stmts) {
    if (s.start < pos) anchor = s;
    else break;
  }
  if (!anchor) {
    const first = stmts[0];
    const indent = first ? ownLineIndent(text, starts, first.start) ?? inner : inner;
    return { mode: 'start', at: block.open + 1, indent, closeIndent: owner };
  }
  const indent = ownLineIndent(text, starts, anchor.start) ?? inner;
  if (anchor.completes === 'yes') return { mode: 'after', at: anchor.end, indent, closeIndent: owner };
  return { mode: 'before', at: anchor.start, indent, closeIndent: owner };
}

// ══ applying an insertion ═══════════════════════════════════════════════════

/**
 * Apply an insertion target (insertionTarget / insertionTargetAt) to
 * `content`: `{content, firstLine, lastLine}` (1-based lines of what was
 * inserted). The lines go on lines of their own; code sharing the anchor's
 * line is moved to a line of its own, a closing brace indented like its
 * block's opening line. A CRLF file gets CRLF line breaks.
 */
export function insertAtTarget(content, target, lines, language) {
  const text = typeof content === 'string' ? content : '';
  const body = (Array.isArray(lines) ? lines : []).filter((l) => typeof l === 'string');
  if (!target || target.notFound || body.length === 0) {
    return { content: text, firstLine: 0, lastLine: -1 };
  }
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  const indented = body.map((l) => `${target.indent || ''}${l}`);
  const block = indented.join(eol);
  const at = Math.min(Math.max(target.at, 0), text.length);
  const lineStart = text.lastIndexOf('\n', at - 1) + 1;
  const nl = text.indexOf('\n', at);
  const lineEnd = nl < 0 ? text.length : nl;
  let next;
  let insertAt;
  if (text.trim() === '') {
    next = `${text}${block}${eol}`;
    insertAt = text.length;
  } else if (target.mode === 'before') {
    if (text.slice(lineStart, at).trim() === '') {
      next = text.slice(0, lineStart) + block + eol + text.slice(lineStart);
      insertAt = lineStart;
    } else {
      const head = text.slice(0, at).replace(/[ \t]+$/, '');
      next = `${head}${eol}${block}${eol}${target.indent || ''}${text.slice(at)}`;
      insertAt = head.length + eol.length;
    }
  } else {
    const tail = text.slice(at, lineEnd);
    if (codeOnlyText(tail, language).trim() === '') {
      if (lineEnd < text.length) {
        next = text.slice(0, lineEnd + 1) + block + eol + text.slice(lineEnd + 1);
        insertAt = lineEnd + 1;
      } else {
        next = `${text}${eol}${block}`;
        insertAt = text.length + eol.length;
      }
    } else {
      const rest = tail.replace(/^[ \t]+/, '');
      const restIndent = rest.startsWith('}') ? (target.closeIndent ?? '') : (target.indent || '');
      next = `${text.slice(0, at)}${eol}${block}${eol}${restIndent}${rest}${text.slice(lineEnd)}`;
      insertAt = at + eol.length;
    }
  }
  const firstLine = next.slice(0, insertAt).split('\n').length;
  return { content: next, firstLine, lastLine: firstLine + body.length - 1 };
}

/**
 * The insertion as ONE editor change: `{change: {from, to, insert}, content,
 * firstLine, lastLine}` — the smallest replacement from `content` to the
 * result, so the editor keeps its cursor and undo history.
 */
export function insertionChange(content, target, lines, language) {
  const res = insertAtTarget(content, target, lines, language);
  return { ...res, change: minimalChange(content, res.content) };
}

/**
 * Where lines go in ONE file whose cursor is on `line` (1-based) — the same
 * rule „Einfügen" uses; a drop uses its drop line. `{mode, at, indent,
 * closeIndent?}` or `{notFound: true, hint}`.
 */
export function insertionTargetAt(content, language, line) {
  const text = typeof content === 'string' ? content : '';
  const n = Number.isInteger(line) ? line : 1;
  return language === 'java' ? javaCursorOp(text, n) : pythonCursorOp(text, n);
}

/** Where lines go without a cursor: the end of the body that runs last. */
export function mainTarget(content, language) {
  const text = typeof content === 'string' ? content : '';
  return language === 'java' ? javaMainOp(text) : pythonMainOp(text);
}

/**
 * The insertion point for `files`: the last cursor `{file, line}` when that
 * file exists and has that line, else the end of the entry file's main (the
 * body that runs last, before a last statement that never lets the next line
 * run). `notFound` with a German `hint` when there is no SAFE place — the
 * caller writes nothing and says so. Apply it with insertAtTarget.
 * @returns {{file: string, fromCursor: boolean, mode?, at?, indent?,
 *   closeIndent?, notFound?: true, hint?: string}}
 */
export function insertionTarget(files, language, cursor) {
  const project = files && typeof files === 'object' ? files : {};
  if (cursor && typeof cursor.file === 'string' && Number.isInteger(cursor.line)
      && typeof project[cursor.file] === 'string') {
    const lines = project[cursor.file].split('\n').length;
    if (cursor.line >= 1 && cursor.line <= lines) {
      return { file: cursor.file, fromCursor: true, ...insertionTargetAt(project[cursor.file], language, cursor.line) };
    }
  }
  const entry = ENTRY_FILE[language];
  return { file: entry, fromCursor: false, ...mainTarget(project[entry] || '', language) };
}
