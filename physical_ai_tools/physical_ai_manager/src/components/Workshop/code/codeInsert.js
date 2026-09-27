/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Putting Sammlung assets INTO a code program, and the program's own block
// structure the editor indents by.
//
//   * The spellings come from robot_api.json (Python name / Java name), never a
//     second copy: `robot.move_to("Ablage")` / `Robot.moveTo("Ablage");`.
//   * WHERE a line goes is the STUDENT's choice (owner decision R3-O4): the
//     line goes directly below the line the cursor is on (a drop: the drop
//     line). Without a known cursor nothing is inserted. This module only
//     CHECKS the chosen spot (`insertionTargetAt`) and refuses it with a short
//     German reason when the line could not stand there or could never run:
//     inside the file's leading block (docstring, comments, `from __future__`,
//     imports), right below a decorator, on a match/case or switch/case line,
//     inside a multi-line expression, a `\` continuation or a string, outside a
//     Java method body, or below a statement that never lets the next line of
//     its block run (return/raise/throw/break/continue, an exit call, an
//     endless loop, an if/else or try whose every way out leaves). The line is
//     NEVER moved somewhere else.
//   * INDENTATION of the inserted line is what Enter at the end of the chosen
//     line gives (`newlineIndentAt`, the function the editor's Enter uses):
//     inside an existing block the block's OWN sibling indentation; a new
//     block's first line the unit of the block its opener sits in, else the
//     file's (`detectIndentUnit`, which only statement starts vote for), else 4.
//     Python's Tab and Backspace go to the next / previous indentation level
//     of the block structure (`indentStepAt`).
//   * A CRLF file gets CRLF line breaks, and every line regex here is CRLF-safe.
//
// PURE — strings in, strings out. CodeEditor (Enter, Tab, Backspace, a drop)
// and the code asset adapter (a row's „Einfügen", Vormachen's „Als Programm
// einfügen") call it. It is NOT in the entry bundle: the lazy editor imports
// it, and the asset adapter loads it on demand (codeAssetDocument
// .loadCodeInsert — review round 2, ni4).

import robotApi from './robot_api.json';
import { tokenizeCode } from './codeAssetUsage';
import { CODE_DE, formatCode } from './codeMessagesDe';

export { SNIPPET_MIME } from './snippetMime';

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

// ── rows ───────────────────────────────────────────────────────────────────

function leadingWhitespace(line) {
  const m = /^[ \t]*/.exec(line || '');
  return m ? m[0] : '';
}

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

// Where row `row`'s content ends: before its `\n`, and before a `\r` in front
// of that (a CRLF file), or at the end of the text.
function rowContentEnd(text, starts, row) {
  let end = row + 1 < starts.length ? starts[row + 1] - 1 : text.length;
  if (end > starts[row] && text[end - 1] === '\r') end -= 1;
  return end;
}

function rowText(text, starts, row) {
  return text.slice(starts[row], rowContentEnd(text, starts, row));
}

// The last row that holds text (a final newline opens no row of its own).
function lastRealRow(text, starts) {
  return Math.max(0, text.endsWith('\n') ? starts.length - 2 : starts.length - 1);
}

// `text` with every non-code segment blanked (newlines and length kept).
function blankedCode(text, segs) {
  let out = '';
  for (const seg of segs) {
    const part = text.slice(seg.start, seg.end);
    out += seg.type === 'code' ? part : part.replace(/[^\n]/g, ' ');
  }
  return out;
}

// `pos` lies strictly inside a string, a char literal or a /* block comment */
// (a line comment ends at its row; in a CRLF file it holds the `\r`).
function insideNonCode(text, segs, pos) {
  return segs.some((seg) => seg.start < pos && pos < seg.end
    && (seg.type === 'string' || seg.type === 'char'
      || (seg.type === 'comment' && text.startsWith('/*', seg.start))));
}

const notFound = (hint) => ({ notFound: true, hint });

// A one-line string or char literal that is never closed on its line: text
// this module cannot read (a Python 3.12 f-string whose `{…}` spans lines is
// one — refused as unreadable, never blamed on a bracket).
function hasUnclosedLineLiteral(text, segs) {
  return segs.some((seg) => (seg.type === 'string' && !seg.triple && !seg.closed)
    || (seg.type === 'char' && !(seg.end - seg.start >= 2 && text[seg.end - 1] === "'")));
}

// ══ Python ══════════════════════════════════════════════════════════════════

const PY_TERMINAL_WORDS = new Set(['return', 'raise', 'break', 'continue']);
const PY_COMPOUND = new Set(['if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally',
  'with', 'def', 'class', 'async', 'match', 'case']);
// The clause words that CONTINUE a compound statement at its own indentation.
const PY_CLAUSE_WORDS = new Set(['elif', 'else', 'except', 'finally', 'case']);
const PY_FIRST_RE = /^\s*(@|[\p{L}_][\p{L}\p{N}_]*)/u;
// An exit call: the program ends there (sys.exit, exit, quit, os._exit).
const PY_EXIT_CALL_RE = /^\s*(?:sys\s*\.\s*exit|os\s*\.\s*_exit|exit|quit)\s*\(/;
const PY_IMPORT_RE = /^\s*(?:import\s|from\s[\s\S]*\bimport\b)/;
const PY_BREAK_RE = /(?<![\p{L}\p{N}_])break(?![\p{L}\p{N}_])/u;
const PY_LOOP_WORDS = new Set(['for', 'while']);
const PY_SCOPE_WORDS = new Set(['def', 'class']);

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

// The first word of `code` (`@` for a decorator), after `async`.
function firstWord(code) {
  const m = PY_FIRST_RE.exec(code);
  if (!m) return '';
  if (m[1] !== 'async') return m[1];
  const rest = PY_FIRST_RE.exec(code.slice(m.index + m[0].length));
  return rest ? rest[1] : 'async';
}

// Everything after the header's own top-level `:` (a one-line compound's body).
function inlineBody(code) {
  let depth = 0;
  for (let i = 0; i < code.length; i += 1) {
    const ch = code[i];
    if (ch === '(' || ch === '[' || ch === '{') depth += 1;
    else if (ch === ')' || ch === ']' || ch === '}') depth = Math.max(0, depth - 1);
    else if (ch === ':' && depth === 0 && code[i + 1] !== '=') return code.slice(i + 1);
  }
  return '';
}

/**
 * The logical statements of a Python file: `{startRow, endRow, indent, code,
 * first, opener, onlyString}` (0-based rows), a statement spanning every row
 * its brackets, `\` continuations and multi-line strings reach. `broken` when
 * the file ends inside brackets or an unterminated triple-quoted string.
 * Rows are CRLF-safe: a `\r` before a line break is never part of a row.
 */
function pythonStatements(text) {
  const segs = tokenizeCode(text, 'python');
  const code = blankedCode(text, segs);
  const starts = lineStarts(text);
  const codeRows = code.split('\n').map((r) => (r.endsWith('\r') ? r.slice(0, -1) : r));
  const rawRows = text.split('\n').map((r) => (r.endsWith('\r') ? r.slice(0, -1) : r));
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
    s.first = firstWord(s.code);
    s.opener = /:\s*$/.test(s.code);
    s.onlyString = s.code.trim() === '';
  }
  return {
    stmts, broken, starts, segs, code, rawRows, unreadable: hasUnclosedLineLiteral(text, segs),
  };
}

/**
 * The indentation stack CPython keeps as each statement starts (the stack
 * after that statement's own indentation), or null when the file's
 * indentation is inconsistent (an IndentationError/TabError at run time).
 */
function pyStacks(stmts) {
  const out = [];
  let stack = [''];
  for (const s of stmts) {
    const top = stack[stack.length - 1];
    if (s.indent !== top) {
      if (s.indent.length > top.length && s.indent.startsWith(top)) {
        stack = [...stack, s.indent];
      } else {
        let k = stack.length - 1;
        while (k >= 0 && stack[k] !== s.indent) k -= 1;
        if (k < 0) return null;
        stack = stack.slice(0, k + 1);
      }
    }
    out.push(stack);
  }
  return out;
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

// The clause headers continuing compound `i` (elif / else / except / finally).
function pyClauses(stmts, i) {
  const out = [];
  let j = pyExtentEnd(stmts, i);
  while (j < stmts.length && stmts[j].indent === stmts[i].indent && PY_CLAUSE_WORDS.has(stmts[j].first)
    && stmts[j].first !== 'case') {
    out.push(j);
    j = pyExtentEnd(stmts, j);
  }
  return out;
}

// The head of the compound that clause `i` continues (walking back over the
// clauses and bodies at its indentation), or -1.
function pyChainHead(stmts, i) {
  for (let j = i - 1; j >= 0; j -= 1) {
    const s = stmts[j];
    if (s.indent.length > stmts[i].indent.length) continue;
    if (s.indent !== stmts[i].indent) return -1;
    if (!PY_CLAUSE_WORDS.has(s.first)) return j;
  }
  return -1;
}

// ── a constant condition (`while not False:`, `while 1 == 1:`) ────────────

const PY_CONST_TOKEN_RE = /\s*(?:(0[xX][\da-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|\d[\d_]*\.?[\d_]*(?:[eE][-+]?\d+)?|\.\d[\d_]*(?:[eE][-+]?\d+)?)|([\p{L}_][\p{L}\p{N}_]*)|(\*\*|\/\/|==|!=|<=|>=|[-+*/%<>()]))/uy;

function pyConstTokens(src) {
  const out = [];
  PY_CONST_TOKEN_RE.lastIndex = 0;
  let i = 0;
  while (i < src.length) {
    if (/\s/.test(src[i])) {
      i += 1;
      continue;
    }
    PY_CONST_TOKEN_RE.lastIndex = i;
    const m = PY_CONST_TOKEN_RE.exec(src);
    if (!m) return null;
    if (m[1] !== undefined) {
      const v = Number(m[1].replace(/_/g, ''));
      if (!Number.isFinite(v)) return null;
      out.push({ t: 'num', v });
    } else if (m[2] !== undefined) out.push({ t: 'name', v: m[2] });
    else out.push({ t: 'op', v: m[3] });
    i = PY_CONST_TOKEN_RE.lastIndex;
  }
  return out;
}

const truthy = (v) => v !== null && v !== false && v !== 0;

/**
 * The value of a condition made only of literals (numbers, True/False/None)
 * and operators (not/and/or, comparisons, arithmetic), or undefined when it
 * is not such a constant (a name, a call, a string — strings are blanked in
 * code-only text — or anything this small evaluator does not know).
 */
function pyConstValue(src) {
  const toks = pyConstTokens(src);
  if (!toks || toks.length === 0) return undefined;
  let p = 0;
  const peek = () => toks[p];
  const isOp = (v) => peek() && peek().t === 'op' && peek().v === v;
  const isWord = (v) => peek() && peek().t === 'name' && peek().v === v;
  const fail = () => { throw new Error('not constant'); };
  const atom = () => {
    const tok = toks[p];
    if (!tok) fail();
    p += 1;
    if (tok.t === 'num') return tok.v;
    if (tok.t === 'name') {
      if (tok.v === 'True') return true;
      if (tok.v === 'False') return false;
      if (tok.v === 'None') return null;
      return fail();
    }
    if (tok.v === '(') {
      // eslint-disable-next-line no-use-before-define
      const v = orExpr();
      if (!isOp(')')) fail();
      p += 1;
      return v;
    }
    return fail();
  };
  const num = (v) => (v === true ? 1 : v === false ? 0 : v);
  const power = () => {
    const base = atom();
    if (isOp('**')) {
      p += 1;
      // eslint-disable-next-line no-use-before-define
      const e = unary();
      if (typeof num(base) !== 'number' || typeof num(e) !== 'number') fail();
      return num(base) ** num(e);
    }
    return base;
  };
  const unary = () => {
    if (isOp('-') || isOp('+')) {
      const neg = peek().v === '-';
      p += 1;
      const v = num(unary());
      if (typeof v !== 'number') fail();
      return neg ? -v : v;
    }
    return power();
  };
  const term = () => {
    let v = unary();
    while (isOp('*') || isOp('/') || isOp('//') || isOp('%')) {
      const op = peek().v;
      p += 1;
      const r = num(unary());
      const l = num(v);
      if (typeof l !== 'number' || typeof r !== 'number') fail();
      if ((op !== '*') && r === 0) fail();
      if (op === '*') v = l * r;
      else if (op === '/') v = l / r;
      else if (op === '//') v = Math.floor(l / r);
      else v = l - Math.floor(l / r) * r;
    }
    return v;
  };
  const arith = () => {
    let v = term();
    while (isOp('+') || isOp('-')) {
      const op = peek().v;
      p += 1;
      const r = num(term());
      const l = num(v);
      if (typeof l !== 'number' || typeof r !== 'number') fail();
      v = op === '+' ? l + r : l - r;
    }
    return v;
  };
  const CMP = new Set(['==', '!=', '<', '<=', '>', '>=']);
  const comparison = () => {
    let left = arith();
    let result = true;
    let chained = false;
    for (;;) {
      let op = null;
      if (peek() && peek().t === 'op' && CMP.has(peek().v)) {
        op = peek().v;
        p += 1;
      } else if (isWord('is')) {
        p += 1;
        op = 'is';
        if (isWord('not')) {
          p += 1;
          op = 'is not';
        }
      } else break;
      const right = arith();
      chained = true;
      const l = left;
      const r = right;
      let ok;
      if (op === 'is') ok = l === r;
      else if (op === 'is not') ok = l !== r;
      else {
        const ln = num(l);
        const rn = num(r);
        if (op === '==') ok = ln === rn;
        else if (op === '!=') ok = ln !== rn;
        else {
          if (typeof ln !== 'number' || typeof rn !== 'number') fail();
          ok = op === '<' ? ln < rn : op === '<=' ? ln <= rn : op === '>' ? ln > rn : ln >= rn;
        }
      }
      result = result && ok;
      left = right;
    }
    return chained ? result : left;
  };
  const notExpr = () => {
    if (isWord('not')) {
      p += 1;
      return !truthy(notExpr());
    }
    return comparison();
  };
  const andExpr = () => {
    let v = notExpr();
    while (isWord('and')) {
      p += 1;
      const r = notExpr();
      v = truthy(v) ? r : v;
    }
    return v;
  };
  const orExpr = () => {
    let v = andExpr();
    while (isWord('or')) {
      p += 1;
      const r = andExpr();
      v = truthy(v) ? v : r;
    }
    return v;
  };
  try {
    const v = orExpr();
    return p === toks.length ? v : undefined;
  } catch (_) {
    return undefined;
  }
}

// The condition of a `while` header (code-only text between `while` and `:`).
function pyWhileCondition(code) {
  const m = /^\s*while\b/.exec(code);
  if (!m) return null;
  const rest = code.slice(m[0].length);
  let depth = 0;
  for (let i = 0; i < rest.length; i += 1) {
    const ch = rest[i];
    if (ch === '(' || ch === '[' || ch === '{') depth += 1;
    else if (ch === ')' || ch === ']' || ch === '}') depth = Math.max(0, depth - 1);
    else if (ch === ':' && depth === 0 && rest[i + 1] !== '=') return rest.slice(0, i);
  }
  return null;
}

// A `break` in the body of loop `i` that leaves THAT loop (not one of an
// inner loop, def or class; a loop's own `else:` clause breaks the outer one).
function pyLoopBreaksOut(stmts, i) {
  const s = stmts[i];
  if (!s.opener) return PY_BREAK_RE.test(inlineBody(s.code));
  const end = pyExtentEnd(stmts, i);
  let skipUntil = -1;
  for (let j = i + 1; j < end; j += 1) {
    if (j < skipUntil) continue;
    const t = stmts[j];
    if ((PY_LOOP_WORDS.has(t.first) || PY_SCOPE_WORDS.has(t.first)) && t.opener) {
      skipUntil = pyExtentEnd(stmts, j);
      continue;
    }
    if (PY_LOOP_WORDS.has(t.first)) continue; // a one-line inner loop's break is its own
    if (PY_BREAK_RE.test(t.code)) return true;
  }
  return false;
}

function pyIsEndlessLoopHead(stmts, i) {
  const s = stmts[i];
  if (s.first !== 'while') return false;
  const cond = pyWhileCondition(s.code);
  if (cond === null) return false;
  const v = pyConstValue(cond);
  return v !== undefined && truthy(v) && !pyLoopBreaksOut(stmts, i);
}

// Why a simple statement never lets the next one run, or null.
function pySimpleNever(code) {
  for (const part of simpleParts(code)) {
    const m = PY_FIRST_RE.exec(part);
    if (m && PY_TERMINAL_WORDS.has(m[1])) return m[1];
    if (PY_EXIT_CALL_RE.test(part)) return 'exit';
  }
  return null;
}

// Why the body of compound/clause `i` never falls through, or null.
function pyBodyNever(stmts, i) {
  const s = stmts[i];
  if (!s.opener) return pySimpleNever(inlineBody(s.code));
  for (const c of pyChildren(stmts, i)) {
    // eslint-disable-next-line no-use-before-define
    const why = pyNever(stmts, c);
    if (why) return why;
  }
  return null;
}

/**
 * Why statement `i` never lets the NEXT statement of its block run, or null:
 * 'return' | 'raise' | 'break' | 'continue' | 'exit' | 'loop' | 'ifelse' |
 * 'try'. A clause header answers null (its compound answers at its head).
 */
function pyNever(stmts, i) {
  const s = stmts[i];
  if (PY_CLAUSE_WORDS.has(s.first)) return null;
  if (s.first === 'while' && pyIsEndlessLoopHead(stmts, i)) return 'loop';
  if (PY_LOOP_WORDS.has(s.first)) {
    // A loop's `else:` runs whenever it ends without a break.
    const orElse = pyClauses(stmts, i).find((c) => stmts[c].first === 'else');
    if (orElse === undefined || pyLoopBreaksOut(stmts, i)) return null;
    return pyBodyNever(stmts, orElse);
  }
  if (s.first === 'if') {
    const clauses = pyClauses(stmts, i);
    if (!clauses.some((c) => stmts[c].first === 'else')) return null;
    return [i, ...clauses].every((c) => pyBodyNever(stmts, c)) ? 'ifelse' : null;
  }
  if (s.first === 'try') {
    const clauses = pyClauses(stmts, i);
    const fin = clauses.find((c) => stmts[c].first === 'finally');
    if (fin !== undefined && pyBodyNever(stmts, fin)) return 'try';
    const handlers = clauses.filter((c) => stmts[c].first === 'except');
    const orElse = clauses.find((c) => stmts[c].first === 'else');
    const bodyEnds = pyBodyNever(stmts, i) || (orElse !== undefined && pyBodyNever(stmts, orElse));
    if (handlers.length > 0 && bodyEnds && handlers.every((c) => pyBodyNever(stmts, c))) return 'try';
    return null;
  }
  if (s.first === 'with') return pyBodyNever(stmts, i);
  if (PY_COMPOUND.has(s.first) || s.opener || s.first === '@') return null;
  return pySimpleNever(s.code);
}

/**
 * The indentation step `content` uses: the most common step from a block
 * OPENER to the first statement of its body (a tab when tabs win; ties go to
 * the smaller step), or null when no block has a body. Only statement starts
 * vote (review round 3, MB1): a continuation line — inside brackets, after a
 * `\`, inside a string — never does, so aligned multi-line literals cannot
 * make a 2-space program read as 5-space. Strings and comments do not count.
 */
export function detectIndentUnit(content, language = 'python') {
  const text = typeof content === 'string' ? content : '';
  const votes = new Map();
  const vote = (outer, inner) => {
    if (!(inner.length > outer.length && inner.startsWith(outer))) return;
    const delta = inner.slice(outer.length);
    const key = delta.includes('\t') ? '\t' : delta;
    votes.set(key, (votes.get(key) || 0) + 1);
  };
  if (language === 'java') {
    const code = blankedCode(text, tokenizeCode(text, 'java'));
    const rows = code.split('\n').map((r) => (r.endsWith('\r') ? r.slice(0, -1) : r));
    let paren = 0;
    for (let r = 0; r < rows.length; r += 1) {
      const trimmed = rows[r].trim();
      const startsInParen = paren > 0;
      for (const ch of rows[r]) {
        if (ch === '(' || ch === '[') paren += 1;
        else if (ch === ')' || ch === ']') paren = Math.max(0, paren - 1);
      }
      if (startsInParen || !trimmed.endsWith('{')) continue;
      // An array initializer (`= {`, `, {`, `[] {`) opens no block of statements.
      const before = trimmed.slice(0, -1).trimEnd();
      if (/[=,([\]]$/.test(before) || before === '') continue;
      let next = r + 1;
      while (next < rows.length && rows[next].trim() === '') next += 1;
      if (next < rows.length) vote(leadingWhitespace(rows[r]), leadingWhitespace(rows[next]));
    }
  } else {
    const { stmts } = pythonStatements(text);
    for (let i = 0; i + 1 < stmts.length; i += 1) {
      if (stmts[i].opener) vote(stmts[i].indent, stmts[i + 1].indent);
    }
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

// The unit a NEW block opened by statement `i` gets: the step of the block
// `i` itself sits in, else the file's unit (review round 3, MB1).
function pyContextUnit(parsed, stacks, i, text) {
  const s = parsed.stmts[i];
  const stack = stacks[i];
  const parent = stack.length >= 2 ? stack[stack.length - 2] : '';
  if (s.indent.length > parent.length && s.indent.startsWith(parent)) return s.indent.slice(parent.length);
  return fileIndentUnit(text, 'python');
}

// Index of the last statement that starts at or before `row`, or -1.
function pyStatementAtOrBefore(stmts, row) {
  let idx = -1;
  for (let i = 0; i < stmts.length; i += 1) {
    if (stmts[i].startRow <= row) idx = i;
    else break;
  }
  return idx;
}

// Index of the last statement that ENDS before `row`, or -1.
function pyLastEndingBefore(stmts, row) {
  for (let i = stmts.length - 1; i >= 0; i -= 1) if (stmts[i].endRow < row) return i;
  return -1;
}

// The body indentation of opener `i`: its first body statement's, if it has
// one, else null.
function pyExistingBody(stmts, i) {
  const next = stmts[i + 1];
  const s = stmts[i];
  if (next && next.indent.length > s.indent.length && next.indent.startsWith(s.indent)) return next.indent;
  return null;
}

// Bracket depth and a trailing `\` of statement `s`'s code-only text up to
// `pos` (an offset into the file).
function pyOpenAt(parsed, s, pos) {
  const from = parsed.starts[s.startRow];
  const part = parsed.code.slice(from, pos);
  let depth = 0;
  for (const ch of part) {
    if (ch === '(' || ch === '[' || ch === '{') depth += 1;
    else if (ch === ')' || ch === ']' || ch === '}') depth = Math.max(0, depth - 1);
  }
  return { depth, backslash: /\\[ \t]*$/.test(part) };
}

/**
 * The indentation (a string) of a NEW Python line started at `pos` — what
 * Enter puts there — or null to leave it to the editor (inside brackets, a
 * string, after a `\`, before a clause word, an inconsistent file).
 *
 *   * The rest of the line moves down with the break: in front of a
 *     statement the statement keeps its own indentation.
 *   * After a block opener: the body's existing indentation, else the
 *     opener's plus the unit of the block it sits in (else the file's).
 *   * After an ordinary statement: that statement's own indentation — the
 *     block's SIBLING indentation, whatever unit other blocks use.
 *   * On a blank or comment line the student's own indentation is kept when
 *     it is a level a statement may take there (a deliberate dedent).
 */
function pyNewlineIndent(text, pos, parsed = pythonStatements(text)) {
  const { stmts, starts, segs } = parsed;
  if (parsed.unreadable || insideNonCode(text, segs, pos)) return null;
  const stacks = pyStacks(stmts);
  if (!stacks) return null;
  const row = rowAt(starts, pos);
  const lineStart = starts[row];
  const before = text.slice(lineStart, pos);
  const after = text.slice(pos, rowContentEnd(text, starts, row));
  const idx = pyStatementAtOrBefore(stmts, row);
  const s = idx >= 0 && stmts[idx].endRow >= row ? stmts[idx] : null;
  // Inside a statement that is still open at the cursor (brackets, a `\`):
  // the editor aligns continuation lines itself.
  if (s && (s.startRow < row || before.trim() !== '')) {
    const open = pyOpenAt(parsed, s, pos);
    if (open.depth > 0 || open.backslash) return null;
  }
  const afterCode = after.trim();
  if (afterCode !== '' && !afterCode.startsWith('#')) {
    // A clause word lines up with its compound: the editor's own rule.
    if (PY_CLAUSE_WORDS.has(firstWord(afterCode))) return null;
    if (before.trim() === '') return leadingWhitespace(rowText(text, starts, row));
  }
  // The statement the new line follows, and whether the cursor's row is blank.
  const blankRow = !(s && before.trim() !== '');
  const prev = blankRow ? pyLastEndingBefore(stmts, row) : idx;
  const own = leadingWhitespace(before);
  if (prev < 0) return '';
  const p = stmts[prev];
  // When the cursor splits the statement, only what stands before it counts.
  const opener = blankRow || p.endRow > row
    ? p.opener
    : /:\s*$/.test(parsed.code.slice(starts[p.startRow], pos));
  if (opener) {
    const body = pyExistingBody(stmts, prev);
    if (body !== null && stmts[prev + 1].startRow > row) return body;
    if (blankRow && own.length > p.indent.length && own.startsWith(p.indent)) return own;
    return p.indent + pyContextUnit(parsed, stacks, prev, text);
  }
  if (blankRow && stacks[prev].includes(own)) return own;
  return p.indent;
}

/**
 * Python Tab (`direction` +1) and Backspace (-1) inside a line's leading
 * whitespace: the indentation the line goes to — the next deeper / next
 * shallower level a statement may take there (the block stack, plus a body
 * after an opener) — or null to leave it to the editor. Backspace never
 * lands between two levels (a column no block uses was an IndentationError
 * in a mixed file, review round 3 MB1).
 */
function pyIndentStep(text, row, direction, parsed = pythonStatements(text)) {
  const { stmts, starts } = parsed;
  const stacks = pyStacks(stmts);
  if (!stacks) return null;
  const idx = pyStatementAtOrBefore(stmts, row);
  if (idx >= 0 && stmts[idx].startRow < row && stmts[idx].endRow >= row) return null;
  const prev = pyLastEndingBefore(stmts, row);
  const own = leadingWhitespace(rowText(text, starts, row));
  const levels = prev >= 0 ? [...stacks[prev]] : [''];
  if (prev >= 0 && stmts[prev].opener) {
    const body = pyExistingBody(stmts, prev);
    levels.push(body !== null ? body : stmts[prev].indent + pyContextUnit(parsed, stacks, prev, text));
  }
  if (direction < 0) {
    let best = null;
    for (const l of levels) {
      if (l.length < own.length && own.startsWith(l) && (best === null || l.length > best.length)) best = l;
    }
    return best;
  }
  let best = null;
  for (const l of levels) {
    if (l.length > own.length && l.startsWith(own) && (best === null || l.length < best.length)) best = l;
  }
  return best;
}

// The German reason a spot is refused, for a „never runs" verdict.
const NEVER_REASON_DE = Object.freeze({
  return: '„return“',
  raise: '„raise“',
  throw: '„throw“',
  break: '„break“',
  continue: '„continue“',
  yield: '„yield“',
  exit: 'ein Programmende (exit)',
  loop: 'eine Endlosschleife',
  ifelse: 'ein if/else, das in jedem Zweig endet',
  try: 'ein try, das in jedem Zweig endet',
  switch: 'ein switch, das in jedem Fall endet',
});

function neverRunsHint(reason) {
  return formatCode(CODE_DE.INSERT_NEVER_RUNS_HINT, NEVER_REASON_DE[reason] || NEVER_REASON_DE.loop);
}

// Why a line at level `level` right below row `row` could never run, or null:
// a statement before it in its block (or before an enclosing block's header,
// walking out) never lets the next one run, or its block is the `else:` of
// an endless loop.
function pyUnreachableReason(stmts, row, level) {
  let lvl = level;
  let j = pyStatementAtOrBefore(stmts, row);
  while (j >= 0) {
    const s = stmts[j];
    if (s.indent.length > lvl.length) {
      j -= 1;
      continue;
    }
    if (s.indent === lvl) {
      if (!PY_CLAUSE_WORDS.has(s.first)) {
        const why = pyNever(stmts, j);
        if (why) return why;
      }
      j -= 1;
      continue;
    }
    // A shorter statement is the header of the block we are in: walk out to
    // the compound it belongs to, and on from before it.
    let head = j;
    if (PY_CLAUSE_WORDS.has(s.first) && s.first !== 'case') {
      head = pyChainHead(stmts, j);
      if (head < 0) return null;
      if (s.first === 'else' && pyIsEndlessLoopHead(stmts, head)) return 'loop';
    }
    lvl = stmts[head].indent;
    j = head - 1;
  }
  return null;
}

// The last statement of the file's LEADING block (a module docstring first,
// then `import` / `from … import` statements, comments and blank lines in
// between): its end row, or -1 when the file does not start with one.
function pyLeadingBlockEnd(stmts) {
  let end = -1;
  for (let i = 0; i < stmts.length; i += 1) {
    const s = stmts[i];
    if (s.indent !== '') break;
    if ((i === 0 && s.onlyString) || PY_IMPORT_RE.test(s.code)) end = s.endRow;
    else break;
  }
  return end;
}

/** Validate the spot right below Python row `row` (0-based): the insertion
 *  op, or notFound with the German reason. */
function pythonInsertionOp(text, row) {
  const parsed = pythonStatements(text);
  const { stmts, starts } = parsed;
  if (parsed.broken) return notFound(CODE_DE.NO_SAFE_PLACE_HINT);
  if (parsed.unreadable) return notFound(CODE_DE.INSERT_UNREADABLE_HINT);
  const stacks = pyStacks(stmts);
  if (!stacks) return notFound(CODE_DE.INSERT_INDENT_BROKEN_HINT);
  if (row < pyLeadingBlockEnd(stmts)) return notFound(CODE_DE.INSERT_LEADING_BLOCK_HINT);
  const idx = pyStatementAtOrBefore(stmts, row);
  const here = idx >= 0 && stmts[idx].endRow >= row ? stmts[idx] : null;
  if (here && here.first === '@') return notFound(CODE_DE.INSERT_DECORATOR_HINT);
  if (here && here.opener && (here.first === 'match' || here.first === 'case')) {
    return notFound(CODE_DE.INSERT_MATCH_HINT);
  }
  // Never above a def's or class's own description (its docstring).
  if (here && here.opener && PY_SCOPE_WORDS.has(here.first)) {
    const body = stmts[idx + 1];
    if (body && body.onlyString && body.indent.length > here.indent.length) {
      return notFound(CODE_DE.INSERT_DOCSTRING_HINT);
    }
  }
  const at = rowContentEnd(text, starts, row);
  // No new line starts here (null): the row's end is inside brackets, after a
  // `\` or inside a string — the statement goes on below.
  const indent = pyNewlineIndent(text, at, parsed);
  if (indent === null) return notFound(CODE_DE.INSERT_INSIDE_EXPRESSION_HINT);
  // What follows must still be a program: nothing deeper than the new line
  // (it opens no block), and no clause cut off from its compound.
  // A match's body holds only `case` blocks.
  for (let j = idx; j >= 0; j -= 1) {
    if (stmts[j].indent.length < indent.length) {
      if (stmts[j].first === 'match' && stmts[j].opener) return notFound(CODE_DE.INSERT_MATCH_HINT);
      break;
    }
  }
  const next = stmts.find((s) => s.startRow > row);
  if (next) {
    const prevIdx = here ? idx : pyLastEndingBefore(stmts, row);
    const base = prevIdx >= 0 ? stacks[prevIdx] : [''];
    const opener = prevIdx >= 0 && stmts[prevIdx].opener;
    const levels = [...base.filter((l) => l.length <= indent.length && indent.startsWith(l))];
    if (!levels.includes(indent)) levels.push(indent);
    if (opener && !(indent.length > stmts[prevIdx].indent.length)) {
      return notFound(CODE_DE.INSERT_INDENT_HINT);
    }
    if (!levels.includes(next.indent)) return notFound(CODE_DE.INSERT_INDENT_HINT);
    if (next.indent === indent && PY_CLAUSE_WORDS.has(next.first)) {
      return notFound(next.first === 'case' ? CODE_DE.INSERT_MATCH_HINT : CODE_DE.INSERT_CLAUSE_HINT);
    }
  }
  const why = pyUnreachableReason(stmts, row, indent);
  if (why) return notFound(neverRunsHint(why));
  return { mode: 'after', at, indent };
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
const JAVA_BLOCK_KEYWORDS = new Set(['if', 'while', 'for', 'catch', 'synchronized', 'try']);
// `System.exit(…)` ends the program: nothing after it in its block runs.
const JAVA_EXIT_RE = /^(?:java\s*\.\s*lang\s*\.\s*)?System\s*\.\s*exit\s*\(/;

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

// A qualified, possibly generic type ending at `i` (inclusive): where it
// starts (`java.util.Comparator<Integer>` in `new java.util.Comparator<Integer>(`).
function typeStartBefore(code, i) {
  let m = skipWsBack(code, i);
  for (;;) {
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
    }
    const { word, start } = wordBefore(code, m);
    if (!word) return m + 1;
    const dot = skipWsBack(code, start - 1);
    if (code[dot] !== '.') return start;
    m = skipWsBack(code, dot - 1);
  }
}

const or3 = (a, b) => (a === 'yes' || b === 'yes' ? 'yes' : a === 'no' && b === 'no' ? 'no' : 'unsure');
const and3 = (a, b) => (a === 'no' || b === 'no' ? 'no' : a === 'yes' && b === 'yes' ? 'yes' : 'unsure');

// ── a constant condition (JLS §15.29) ─────────────────────────────────────

const JAVA_CONST_TOKEN_RE = /\s*(?:(0[xX][\da-fA-F_]+[lL]?|\d[\d_]*\.?[\d_]*(?:[eE][-+]?\d+)?[lLdDfF]?)|([\p{L}_$][\p{L}\p{N}_$]*(?:\s*\.\s*[\p{L}_$][\p{L}\p{N}_$]*)*)|(&&|\|\||==|!=|<=|>=|[-+*/%<>()!]))/uy;

/**
 * What a Java expression is (JLS §15.29): 'var' (provably NOT a constant
 * expression — a call, an assignment, `new`, an array access, a conditional,
 * or at least ONE operand that is not a constant variable), `{value}` (a
 * constant expression; `value` is undefined when this evaluator cannot
 * compute it, e.g. a string or char literal, blanked in code-only text), or
 * null (it might be a constant this file cannot see: a name declared
 * elsewhere). `lookup(name)` answers a name the same way.
 */
function javaExprVerdict(expr, lookup) {
  const src = String(expr).trim();
  if (/\b(?:new|instanceof)\b|\+\+|--|[^=!<>]=(?!=)|[\p{L}\p{N}_$]\s*\(|\[|\?/u.test(src)) return 'var';
  const toks = [];
  let unknown = false;
  let i = 0;
  while (i < src.length) {
    if (/\s/.test(src[i])) {
      i += 1;
      continue;
    }
    JAVA_CONST_TOKEN_RE.lastIndex = i;
    const m = JAVA_CONST_TOKEN_RE.exec(src);
    if (!m) return { value: undefined };
    // Read before `lookup` runs: a name's initializer is tokenized with the
    // same sticky regex.
    const next = JAVA_CONST_TOKEN_RE.lastIndex;
    if (m[1] !== undefined) {
      const v = Number(m[1].replace(/_/g, '').replace(/[lLdDfF]$/, ''));
      toks.push({ t: 'num', v: Number.isFinite(v) ? v : undefined });
    } else if (m[2] !== undefined) {
      const name = m[2].replace(/\s+/g, '');
      if (name === 'true' || name === 'false') toks.push({ t: 'bool', v: name === 'true' });
      else {
        const verdict = lookup(name);
        if (verdict === 'var') return 'var';
        if (verdict === null) unknown = true;
        else toks.push({ t: 'const', v: verdict.value });
      }
    } else toks.push({ t: 'op', v: m[3] });
    i = next;
  }
  if (unknown) return null;
  if (toks.length === 0 || toks.some((t) => t.v === undefined)) return { value: undefined };
  // eslint-disable-next-line no-use-before-define
  return { value: javaEvalTokens(toks) };
}

/**
 * A Java loop condition's kind: 'true' (a constant expression whose value is
 * true: the loop ends only by a break), 'var' (it can end — not a constant
 * expression, or a constant false), or 'unsure' (a constant this evaluator
 * cannot compute, or a name it cannot see).
 */
function javaConditionKind(cond, lookup) {
  if (String(cond).trim() === '') return 'true';
  const verdict = javaExprVerdict(cond, lookup);
  if (verdict === 'var') return 'var';
  if (verdict === null || verdict.value === undefined) return 'unsure';
  if (verdict.value === true) return 'true';
  return verdict.value === false ? 'var' : 'unsure';
}

// Evaluate a token list of numbers/booleans/operators (Java precedence), or
// undefined.
function javaEvalTokens(toks) {
  let p = 0;
  const peek = () => toks[p];
  const isOp = (v) => peek() && peek().t === 'op' && peek().v === v;
  const fail = () => { throw new Error('not constant'); };
  const atom = () => {
    const tok = toks[p];
    if (!tok) fail();
    p += 1;
    if (tok.t === 'num' || tok.t === 'bool' || tok.t === 'const') return tok.v;
    if (tok.v === '(') {
      // eslint-disable-next-line no-use-before-define
      const v = orExpr();
      if (!isOp(')')) fail();
      p += 1;
      return v;
    }
    return fail();
  };
  const unary = () => {
    if (isOp('!')) {
      p += 1;
      const v = unary();
      if (typeof v !== 'boolean') fail();
      return !v;
    }
    if (isOp('-') || isOp('+')) {
      const neg = peek().v === '-';
      p += 1;
      const v = unary();
      if (typeof v !== 'number') fail();
      return neg ? -v : v;
    }
    return atom();
  };
  const mul = () => {
    let v = unary();
    while (isOp('*') || isOp('/') || isOp('%')) {
      const op = peek().v;
      p += 1;
      const r = unary();
      if (typeof v !== 'number' || typeof r !== 'number' || (op !== '*' && r === 0)) fail();
      v = op === '*' ? v * r : op === '/' ? v / r : v % r;
    }
    return v;
  };
  const add = () => {
    let v = mul();
    while (isOp('+') || isOp('-')) {
      const op = peek().v;
      p += 1;
      const r = mul();
      if (typeof v !== 'number' || typeof r !== 'number') fail();
      v = op === '+' ? v + r : v - r;
    }
    return v;
  };
  const rel = () => {
    let v = add();
    while (isOp('<') || isOp('<=') || isOp('>') || isOp('>=')) {
      const op = peek().v;
      p += 1;
      const r = add();
      if (typeof v !== 'number' || typeof r !== 'number') fail();
      v = op === '<' ? v < r : op === '<=' ? v <= r : op === '>' ? v > r : v >= r;
    }
    return v;
  };
  const eq = () => {
    let v = rel();
    while (isOp('==') || isOp('!=')) {
      const op = peek().v;
      p += 1;
      const r = rel();
      if (typeof v !== typeof r) fail();
      v = op === '==' ? v === r : v !== r;
    }
    return v;
  };
  const andExpr = () => {
    let v = eq();
    while (isOp('&&')) {
      p += 1;
      const r = eq();
      if (typeof v !== 'boolean' || typeof r !== 'boolean') fail();
      v = v && r;
    }
    return v;
  };
  const orExpr = () => {
    let v = andExpr();
    while (isOp('||')) {
      p += 1;
      const r = andExpr();
      if (typeof v !== 'boolean' || typeof r !== 'boolean') fail();
      v = v || r;
    }
    return v;
  };
  try {
    const v = orExpr();
    return p === toks.length ? v : undefined;
  } catch (_) {
    return undefined;
  }
}

/**
 * A Java statement parser over code-only text — just enough to answer, for
 * each statement of a block, where it starts and ends and whether it can
 * COMPLETE NORMALLY (JLS §14.22, the rule javac's „unreachable statement"
 * enforces): 'yes' | 'no' | 'unsure', with the `reason` of a 'no'.
 * `breaks` carries the break targets that escape the statement ('' = an
 * unlabelled break).
 */
class JavaParser {
  constructor(code, pair) {
    this.code = code;
    this.pair = pair;
    this.broken = false;
    this.declCache = new Map();
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
      const last = inner.length ? inner[inner.length - 1] : null;
      return this.made(start, close + 1, last ? last.completes : 'yes', this.unionBreaks(inner),
        false, last ? last.reason : null);
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
      return this.made(start, inner.end, brokenOut ? 'yes' : inner.completes, breaks, false,
        brokenOut ? null : inner.reason);
    }
    switch (word) {
      case 'if': return this.ifStatement(start, after, to);
      case 'while': return this.whileStatement(start, after, to);
      case 'do': return this.doStatement(start, after, to);
      case 'for': return this.forStatement(start, after, to);
      case 'try': return this.tryStatement(start, after, to);
      case 'switch': return this.switchStatement(start, after, to);
      case 'synchronized': {
        const close = this.closeOf(after);
        if (close === null) return null;
        const inner = this.statement(skipWs(code, close + 1, to), to);
        return inner ? this.made(start, inner.end, inner.completes, inner.breaks, false, inner.reason) : null;
      }
      case 'return': case 'throw': case 'yield':
        return this.made(start, this.toSemicolon(after, to), 'no', new Set(), false, word);
      case 'continue':
        return this.made(start, this.toSemicolon(after, to), 'no', new Set(), true, 'continue');
      case 'break': {
        const label = wordAt(code, after);
        return this.made(start, this.toSemicolon(after, to), 'no', new Set([label]), false, 'break');
      }
      case 'case': case 'default': {
        // A switch label: up to its `:` or `->`, the statement follows.
        let j = after;
        while (j < to && code[j] !== ':' && !(code[j] === '-' && code[j + 1] === '>')) {
          j = pair.has(j) && '([{'.includes(code[j]) ? pair.get(j) + 1 : j + 1;
        }
        const arrow = code[j] !== ':';
        const st = this.made(start, arrow ? j + 2 : j + 1, 'yes');
        if (st) {
          st.label = arrow ? 'arrow' : 'colon';
          st.isDefault = word === 'default';
        }
        return st;
      }
      default: break;
    }
    if (this.startsTypeDeclaration(i, to)) {
      let j = i;
      while (j < to && code[j] !== '{') j = pair.has(j) && code[j] === '(' ? pair.get(j) + 1 : j + 1;
      if (j >= to) return null;
      return this.made(start, pair.get(j) + 1, 'yes');
    }
    const end = this.toSemicolon(i, to);
    if (JAVA_EXIT_RE.test(code.slice(i, end === null ? i : end))) return this.made(start, end, 'no', new Set(), false, 'exit');
    return this.made(start, end, 'yes');
  }

  made(start, end, completes, breaks = new Set(), continues = false, reason = null) {
    if (!Number.isInteger(end) || end <= start) {
      this.broken = true;
      return null;
    }
    return {
      start, end, completes, breaks, continues, reason: completes === 'no' ? reason || 'loop' : null,
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
      const completes = or3(then.completes, other.completes);
      return this.made(start, other.end, completes, this.unionBreaks([then, other]), false, 'ifelse');
    }
    return this.made(start, then.end, 'yes', then.breaks);
  }

  loop(start, end, condKind, body) {
    const breaks = new Set(body.breaks);
    const brokenOut = breaks.delete('');
    let completes;
    if (condKind === 'true') completes = brokenOut ? 'yes' : 'no';
    else if (condKind === 'unsure') completes = brokenOut ? 'yes' : 'unsure';
    else completes = 'yes';
    return this.made(start, end, completes, breaks, false, 'loop');
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
    else if (brokenOut) completes = 'yes';
    else if (kind === 'unsure') completes = 'unsure';
    else completes = body.continues ? 'yes' : body.completes;
    return this.made(start, end, completes, breaks, false, completes === 'no' && kind !== 'true' ? body.reason : 'loop');
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
    return this.loop(start, body.end, cond === '' ? 'true' : this.condKind(cond), body);
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
    let reason = 'try';
    if (wordAt(code, k) === 'finally') {
      const block = this.statement(skipWs(code, k + 7, to), to);
      if (!block) return null;
      completes = and3(completes, block.completes);
      if (block.completes === 'no') reason = block.reason || 'try';
      parts.push(block);
      end = block.end;
    }
    return this.made(start, end, completes, this.unionBreaks(parts), false, reason);
  }

  /**
   * A switch STATEMENT (JLS §14.22): it can complete normally when it has
   * no `default`, when its last statement group can (colon labels) or one
   * of its rules can (arrow labels), or when a break leaves it.
   */
  switchStatement(start, paren, to) {
    const { code, pair } = this;
    const close = this.closeOf(paren);
    if (close === null) return null;
    const open = skipWs(code, close + 1, to);
    if (code[open] !== '{') return this.made(start, null);
    const items = this.block(open + 1, pair.get(open));
    if (this.broken) return null;
    const breaks = this.unionBreaks(items);
    const brokenOut = breaks.delete('');
    const labels = items.filter((s) => s.label);
    const hasDefault = labels.some((s) => s.isDefault);
    let completes;
    if (!hasDefault || brokenOut || items.length === 0) completes = 'yes';
    else if (labels.some((s) => s.label === 'arrow')) {
      completes = 'no';
      for (let k = 0; k < items.length; k += 1) {
        if (!items[k].label) continue;
        const bodyStmt = items[k + 1];
        if (!bodyStmt || bodyStmt.label) {
          completes = 'unsure';
          break;
        }
        completes = or3(completes, bodyStmt.completes);
      }
    } else {
      const last = items[items.length - 1];
      completes = last.label ? 'yes' : last.completes;
    }
    return this.made(start, pair.get(open) + 1, completes, breaks, false, 'switch');
  }

  condKind(cond) {
    return javaConditionKind(cond, (name) => this.constantVerdict(name));
  }

  /**
   * What a name in a condition is: 'var' (provably not a constant variable:
   * a variable never declared `final` — an interface field is implicitly
   * final — a parameter, a member of a variable like `args.length`, or a
   * final one whose initializer is not constant), `{value}` (a constant
   * variable declared in this file), or null (declared elsewhere, or declared
   * here more than once in ways that disagree: it might be a constant).
   */
  constantVerdict(name, depth = 0) {
    if (depth > 8) return null;
    if (this.declCache.has(name)) return this.declCache.get(name);
    const parts = name.split('.');
    const decls = this.declarations(parts[0]);
    let verdict = null;
    if (parts.length > 1) {
      // A member of a declared variable is never a constant expression.
      verdict = decls.length > 0 ? 'var' : null;
    } else if (decls.length > 0 && decls.every((d) => !d.final)) {
      verdict = 'var';
    } else if (decls.length === 1) {
      const { init } = decls[0];
      if (init === null) verdict = 'var';
      else {
        const inner = javaExprVerdict(init, (n) => this.constantVerdict(n, depth + 1));
        verdict = inner;
      }
    }
    this.declCache.set(name, verdict);
    return verdict;
  }

  // Every declaration of simple name `name` in the file: {final, init}.
  declarations(name) {
    if (!/^[\p{L}_$][\p{L}\p{N}_$]*$/u.test(name)) return [];
    const { code } = this;
    const esc = name.replace(/\$/g, '\\$');
    const decl = new RegExp(
      `(?:^|[;{}(,])\\s*((?:(?:@[\\p{L}_$][\\p{L}\\p{N}_$.]*|[a-z]+)\\s+)*)`
      + `[\\p{L}_$][\\p{L}\\p{N}_$.]*(?:<[^;{}()]*>)?(?:\\s*\\[\\s*\\])*\\s+${esc}\\s*(=(?!=)|;|,|:|\\))`, 'gu',
    );
    const out = [];
    for (const m of code.matchAll(decl)) {
      const at = m.index + m[0].length;
      let final = /\bfinal\b/.test(m[1]) || this.inInterfaceBody(m.index);
      let init = null;
      if (m[2].startsWith('=')) {
        let depth = 0;
        let j = at;
        for (; j < code.length; j += 1) {
          const c = code[j];
          if ('([{'.includes(c)) depth += 1;
          else if (')]}'.includes(c)) {
            if (depth === 0) break;
            depth -= 1;
          } else if ((c === ';' || c === ',') && depth === 0) break;
        }
        init = code.slice(at, j);
      } else if (m[2] === ':' || m[2] === ')') {
        // An enhanced-for variable or a parameter: never a constant.
        final = false;
      }
      out.push({ final, init });
    }
    return out;
  }

  // Offset `at` sits directly in an interface's (or annotation type's) body:
  // its fields are implicitly `static final` (review round 3, mb2).
  inInterfaceBody(at) {
    const { code, pair } = this;
    let depth = 0;
    for (let i = at; i >= 0; i -= 1) {
      const c = code[i];
      if (c === '}') depth += 1;
      else if (c === '{') {
        if (depth === 0) {
          let b = i - 1;
          while (b >= 0 && !';{}'.includes(code[b])) b -= 1;
          return pair.has(i) && /\binterface\b/.test(code.slice(b + 1, i));
        }
        depth -= 1;
      }
    }
    return false;
  }
}

// What an opening brace opens: 'block' (statements go there), 'class' (a
// type body: fields and methods), 'switch' (case labels) or 'array' (an
// initializer: an expression).
function braceKind(code, pair, open, enclosing) {
  const j = skipWsBack(code, open - 1);
  if (j < 0) return 'class';
  const ch = code[j];
  if (ch === ')') {
    const paren = pair.get(j);
    const k = skipWsBack(code, paren - 1);
    const { word, start } = wordBefore(code, k);
    if (word === 'switch') return 'switch';
    if (JAVA_BLOCK_KEYWORDS.has(word)) return 'block';
    // `new Type(…) {` (qualified and/or generic) is an anonymous class body
    // (review round 3, nb1: `new Foo<T>() {` read as a block of statements).
    const typeStart = typeStartBefore(code, k);
    if (typeStart <= k && wordBefore(code, skipWsBack(code, typeStart - 1)).word === 'new') return 'class';
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

// The leading whitespace of the row holding offset `at`.
function rowIndentAt(text, starts, at) {
  return leadingWhitespace(rowText(text, starts, rowAt(starts, at)));
}

// The unit a new block opened at brace `open` gets: the step from the row of
// the brace around it to the brace's own row, else the file's.
function javaContextUnit(text, code, pair, starts, open) {
  const outer = javaEnclosing(code, pair, open);
  const own = rowIndentAt(text, starts, open);
  for (let k = outer.length - 1; k >= 0; k -= 1) {
    if (outer[k].kind === 'paren') continue;
    const parent = rowIndentAt(text, starts, outer[k].open);
    if (own.length > parent.length && own.startsWith(parent)) return own.slice(parent.length);
    break;
  }
  return fileIndentUnit(text, 'java');
}

function javaContext(text) {
  const segs = tokenizeCode(text, 'java');
  const code = blankedCode(text, segs);
  return {
    segs, code, pair: javaBrackets(code), starts: lineStarts(text),
  };
}

/**
 * The indentation of a NEW Java line started at `pos` (Enter), or null to
 * leave it to the editor: inside a statement block, after a statement its
 * own row's indentation (the block's sibling indentation, review round 3
 * MB1 / 3-B N1); as a block's first line, the block's existing first
 * statement's, else the brace row's plus the unit of the block around it.
 */
function javaNewlineIndent(text, pos, ctx = javaContext(text)) {
  const {
    segs, code, pair, starts,
  } = ctx;
  if (!pair || insideNonCode(text, segs, pos) || hasUnclosedLineLiteral(text, segs)) return null;
  const row = rowAt(starts, pos);
  const before = text.slice(starts[row], pos);
  const after = text.slice(pos, rowContentEnd(text, starts, row)).trim();
  if (after !== '') {
    // A closing bracket, a brace of its own or a case label: the editor's own rule.
    if (/^[{})\]]|^(?:case\b|default\b)/.test(after)) return null;
    if (before.trim() === '') return leadingWhitespace(rowText(text, starts, row));
  }
  const stack = javaEnclosing(code, pair, pos);
  const inner = stack[stack.length - 1];
  if (!inner || (inner.kind !== 'block' && inner.kind !== 'switch')) return null;
  const parser = new JavaParser(code, pair);
  const stmts = parser.block(inner.open + 1, pair.get(inner.open));
  if (parser.broken) return null;
  let anchor = null;
  for (const s of stmts) {
    if (s.start < pos) anchor = s;
    else break;
  }
  if (anchor && anchor.end > pos) return null;
  if (anchor && anchor.label) {
    // The first statement of a case group: the group's own indentation.
    const next = stmts.find((s) => s.start >= pos);
    if (next && !next.label && rowAt(starts, next.start) > row) {
      const head = text.slice(starts[rowAt(starts, next.start)], next.start);
      if (head.trim() === '') return head;
    }
    return rowIndentAt(text, starts, anchor.start) + javaContextUnit(text, code, pair, starts, inner.open);
  }
  if (anchor) return rowIndentAt(text, starts, anchor.start);
  if (inner.kind === 'switch') return null;
  const first = stmts.find((s) => s.start >= pos);
  if (first && rowAt(starts, first.start) > row) {
    const head = text.slice(starts[rowAt(starts, first.start)], first.start);
    if (head.trim() === '') return head;
  }
  return rowIndentAt(text, starts, inner.open) + javaContextUnit(text, code, pair, starts, inner.open);
}

const JAVA_SWITCH_ROW_RE = /^(?:case\b|default\s*(?::|->))|\bswitch\s*\(/;

/** Validate the spot right below Java row `row` (0-based). */
function javaInsertionOp(text, row) {
  const ctx = javaContext(text);
  const {
    segs, code, pair, starts,
  } = ctx;
  if (!pair) return notFound(CODE_DE.NO_SAFE_PLACE_HINT);
  if (hasUnclosedLineLiteral(text, segs)) return notFound(CODE_DE.INSERT_UNREADABLE_HINT);
  const at = rowContentEnd(text, starts, row);
  if (insideNonCode(text, segs, at)) return notFound(CODE_DE.INSERT_INSIDE_EXPRESSION_HINT);
  if (JAVA_SWITCH_ROW_RE.test(code.slice(starts[row], at).trim())) return notFound(CODE_DE.INSERT_SWITCH_HINT);
  const stack = javaEnclosing(code, pair, at);
  const inner = stack[stack.length - 1];
  if (!inner || inner.kind === 'class') return notFound(CODE_DE.NOT_IN_METHOD_HINT);
  if (inner.kind !== 'block' && inner.kind !== 'switch') return notFound(CODE_DE.INSERT_INSIDE_EXPRESSION_HINT);
  const parser = new JavaParser(code, pair);
  const stmts = parser.block(inner.open + 1, pair.get(inner.open));
  // Balanced brackets the parser still cannot read: never blamed on a bracket.
  if (parser.broken) return notFound(CODE_DE.INSERT_UNREADABLE_HINT);
  let before = stmts.filter((s) => s.start < at);
  if (inner.kind === 'switch') {
    // Directly in a switch body only a colon-style case group takes a line,
    // after its label; an arrow rule's body is its own block.
    const lastLabel = before.map((s) => Boolean(s.label)).lastIndexOf(true);
    if (stmts.some((s) => s.label === 'arrow') || lastLabel < 0) return notFound(CODE_DE.INSERT_SWITCH_HINT);
    before = before.slice(lastLabel);
  }
  for (const s of before) {
    if (s.end > at) return notFound(CODE_DE.INSERT_INSIDE_EXPRESSION_HINT);
    if (s.completes === 'no') return notFound(neverRunsHint(s.reason));
    if (s.completes === 'unsure') return notFound(CODE_DE.INSERT_UNSURE_HINT);
  }
  const indent = javaNewlineIndent(text, at, ctx);
  if (indent === null) return notFound(CODE_DE.INSERT_UNREADABLE_HINT);
  return { mode: 'after', at, indent };
}

// ══ the shared entry points ═════════════════════════════════════════════════

/**
 * The indentation of a new line started at `pos` in `content` — what Enter
 * puts there (CodeEditor's indent service and its Enter key) and what an
 * insertion below a line uses — or null to leave it to the editor.
 */
export function newlineIndentAt(content, pos, language) {
  const text = typeof content === 'string' ? content : '';
  const at = Math.min(Math.max(Number.isInteger(pos) ? pos : 0, 0), text.length);
  return language === 'java' ? javaNewlineIndent(text, at) : pyNewlineIndent(text, at);
}

/**
 * Python's Tab (+1) / Backspace (-1) in the leading whitespace of line
 * `lineNumber` (1-based): the whitespace the line should get, or null to
 * leave it to the editor (Java; a continuation line; no level to go to).
 */
export function indentStepAt(content, language, lineNumber, direction) {
  if (language === 'java') return null;
  const text = typeof content === 'string' ? content : '';
  const starts = lineStarts(text);
  const row = Math.min(Math.max((Number.isInteger(lineNumber) ? lineNumber : 1) - 1, 0), starts.length - 1);
  return pyIndentStep(text, row, direction < 0 ? -1 : 1);
}

/**
 * Apply an insertion target (insertionTarget / insertionTargetAt) to
 * `content`: `{content, firstLine, lastLine}` (1-based lines of what was
 * inserted). The lines go on lines of their own, directly below the chosen
 * line, each with the target's indentation. A CRLF file gets CRLF breaks.
 */
export function insertAtTarget(content, target, lines) {
  const text = typeof content === 'string' ? content : '';
  const body = (Array.isArray(lines) ? lines : []).filter((l) => typeof l === 'string');
  if (!target || target.notFound || body.length === 0) {
    return { content: text, firstLine: 0, lastLine: -1 };
  }
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  const block = body.map((l) => `${target.indent || ''}${l}`).join(eol);
  if (text === '') return { content: `${block}${eol}`, firstLine: 1, lastLine: body.length };
  const at = Math.min(Math.max(target.at, 0), text.length);
  const next = `${text.slice(0, at)}${eol}${block}${text.slice(at)}`;
  const firstLine = text.slice(0, at).split('\n').length + 1;
  return { content: next, firstLine, lastLine: firstLine + body.length - 1 };
}

/**
 * The insertion as ONE editor change: `{change: {from, to, insert}, content,
 * firstLine, lastLine}` — the smallest replacement from `content` to the
 * result, so the editor keeps its cursor and undo history.
 */
export function insertionChange(content, target, lines, language) {
  const res = insertAtTarget(content, target, lines, language);
  // eslint-disable-next-line no-use-before-define
  return { ...res, change: minimalChange(content, res.content) };
}

/**
 * The spot directly below line `line` (1-based) of ONE file, checked (owner
 * decision R3-O4): `{mode: 'after', at, indent}` when a line can stand there
 * and run, else `{notFound: true, hint}` with the German reason. The line is
 * never moved anywhere else. A drop uses its drop line.
 */
export function insertionTargetAt(content, language, line) {
  const text = typeof content === 'string' ? content : '';
  const starts = lineStarts(text);
  const n = Number.isInteger(line) ? line : 1;
  const row = Math.min(Math.max(n - 1, 0), lastRealRow(text, starts));
  if (language === 'java') return javaInsertionOp(text, row);
  if (text.trim() === '') return { mode: 'after', at: text.length, indent: '' };
  return pythonInsertionOp(text, row);
}

/**
 * Where lines go in `files` for the student's cursor `{file, line}`: that
 * file, directly below that line, checked by insertionTargetAt. Without a
 * cursor (or one whose file or line no longer exists) NOTHING goes anywhere:
 * `{notFound: true, noCursor: true, hint}` asks the student to click first.
 * @returns {{file?: string, mode?, at?, indent?, notFound?: true,
 *   noCursor?: true, hint?: string}}
 */
export function insertionTarget(files, language, cursor) {
  const project = files && typeof files === 'object' ? files : {};
  if (cursor && typeof cursor.file === 'string' && Number.isInteger(cursor.line)
      && typeof project[cursor.file] === 'string') {
    const lines = project[cursor.file].split('\n').length;
    if (cursor.line >= 1 && cursor.line <= lines) {
      return { file: cursor.file, ...insertionTargetAt(project[cursor.file], language, cursor.line) };
    }
  }
  return { notFound: true, noCursor: true, hint: CODE_DE.CLICK_FIRST_HINT };
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
