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
//   * WHERE: on new lines below the last line the student's cursor was on,
//     indented like it. After a line that opens a block (`:` or `{`) the lines
//     take the indentation of the body line below it; with no body yet, the
//     opener's own indentation plus the unit the FILE uses (detectIndentUnit),
//     else CODE_INDENT_UNIT — which is also the editor's indentUnit, so the two
//     can never disagree (a fixed 4 spaces under a 2-space body was an
//     IndentationError, review M2).
//   * A student who never clicked into the editor gets the end of main
//     (review R-O2): the end of main.py at the top level, or just inside
//     `main()`'s closing brace in Java (a brace scan that skips strings and
//     comments) — BEFORE a trailing endless loop (`while True:`,
//     `while (true)`, `for (;;)`, `do … while (true);`) or a trailing
//     `return`, where code would never run (Java: „unreachable statement").
//     A Java program with no findable `main` gets no place at all
//     (`notFound`): the caller says so in German instead of writing outside
//     the class; a `main` whose closing brace shares a line with code is
//     opened up (`split`).
//
// PURE — strings in, strings out. CodeEditor (a drop) and the code asset
// adapter (a row's „Einfügen", Vormachen's „Als Programm einfügen") call it.

import robotApi from './robot_api.json';
import { ENTRY_FILE } from './codeProject';
import { codeOnlyText } from './codeAssetUsage';

/**
 * The drag-and-drop type of a Sammlung row dropped into the code editor: the
 * drawer sets it, CodeEditor reads it. The payload is `{kind, name}` JSON.
 */
export const SNIPPET_MIME = 'application/x-edubotics-snippet';

const METHODS = new Map((robotApi.methods || []).map((m) => [m.name, m]));
// An asset name that can sit between double quotes in both languages as is.
const SAFE_NAME_RE = /^[^"\\\n\r]+$/;
/** One indentation level: the editor's indentUnit AND the insertion's
 *  fallback (PEP 8's 4 spaces; the Java starter file uses 4 too). */
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

function leadingWhitespace(line) {
  const m = /^[ \t]*/.exec(line || '');
  return m ? m[0] : '';
}

// The line's code with strings and comments blanked, right-trimmed.
function codePart(line, language) {
  return codeOnlyText(line, language).replace(/\s+$/, '');
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

// The indentation of the first non-empty line below row `index` when it is
// deeper than `own`, else null.
function deeperBodyIndent(rows, index, own) {
  for (let j = index + 1; j < rows.length; j += 1) {
    if (rows[j].trim() === '') continue;
    const o = leadingWhitespace(rows[j]);
    return o.length > own.length && o.startsWith(own) ? o : null;
  }
  return null;
}

// Where the lines go and how they are indented; shared by the string form
// (insertLinesAt) and the editor-change form (insertionEdit), so the two can
// never disagree.
function planInsertion(content, afterLine, lines, opts) {
  const text = typeof content === 'string' ? content : '';
  const rows = text.split('\n');
  const body = Array.isArray(lines) ? lines : [];
  const at = Math.min(Math.max(Number.isInteger(afterLine) ? afterLine : 0, 0), rows.length);
  const language = opts.language || 'python';
  let indent = typeof opts.indent === 'string' ? opts.indent : null;
  if (indent === null) {
    indent = '';
    for (let i = at - 1; i >= 0; i -= 1) {
      if (rows[i].trim() === '') continue;
      const own = leadingWhitespace(rows[i]);
      if (/[:{]$/.test(codePart(rows[i], language))) {
        indent = deeperBodyIndent(rows, i, own)
          ?? own + (detectIndentUnit(text, language) || CODE_INDENT_UNIT);
      } else {
        indent = own;
      }
      break;
    }
  }
  const inserted = body.map((l) => `${indent}${l}`);
  // A text ending in '\n' splits to a final '' row; inserting after the last
  // real line keeps that trailing newline where it was.
  const index = at === rows.length && rows[rows.length - 1] === '' ? rows.length - 1 : at;
  return {
    text, rows, inserted, index,
  };
}

/**
 * Insert `lines` as new lines after line `afterLine` (1-based; 0 = before the
 * first line; clamped). Each line gets the indentation of the anchor line —
 * the nearest non-empty line at or above `afterLine` — plus one unit when that
 * line opens a block (ends with `:` or `{` outside strings and comments).
 * `opts.indent` overrides the computed indentation; `opts.language` picks the
 * comment syntax for the block test (default python).
 * @returns {{content: string, firstLine: number, lastLine: number}}
 */
export function insertLinesAt(content, afterLine, lines, opts = {}) {
  const { rows, inserted, index } = planInsertion(content, afterLine, lines, opts);
  const next = [...rows.slice(0, index), ...inserted, ...rows.slice(index)];
  return { content: next.join('\n'), firstLine: index + 1, lastLine: index + inserted.length };
}

/**
 * The same insertion as ONE editor change `{from, insert, firstLine,
 * lastLine}` (an insertion at `from`, nothing replaced), so the editor keeps
 * its cursor and its undo history — applied to `content` it yields exactly
 * `insertLinesAt(...).content`.
 */
export function insertionEdit(content, afterLine, lines, opts = {}) {
  const {
    text, rows, inserted, index,
  } = planInsertion(content, afterLine, lines, opts);
  const firstLine = index + 1;
  const lastLine = index + inserted.length;
  if (inserted.length === 0) {
    return {
      from: 0, insert: '', firstLine, lastLine,
    };
  }
  if (index >= rows.length) {
    return {
      from: text.length, insert: `\n${inserted.join('\n')}`, firstLine, lastLine,
    };
  }
  let from = 0;
  for (let i = 0; i < index; i += 1) from += rows[i].length + 1;
  return {
    from, insert: `${inserted.join('\n')}\n`, firstLine, lastLine,
  };
}

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

function lastRealLine(text) {
  const rows = text.split('\n');
  return rows[rows.length - 1] === '' ? rows.length - 1 : rows.length;
}

function lineOf(text, offset) {
  let line = 1;
  for (let i = 0; i < offset && i < text.length; i += 1) if (text[i] === '\n') line += 1;
  return line;
}

// The last top-level statement of a Python module is `while True:` /
// `while 1:` (strings and comments blanked).
const PY_ENDLESS_RE = /^while\s+(True|1)\s*:/;
// The last statement of Java's main never lets a following line run.
const JAVA_ENDLESS_RES = [
  /^while\s*\(\s*true\s*\)/,
  /^for\s*\(\s*;\s*;\s*\)/,
  /^return\b/,
  /^do\b[\s\S]*\bwhile\s*\(\s*true\s*\)\s*;\s*$/,
];
const JAVA_CONTINUATION_RE = /^(else|catch|finally)\b/;

// Offset of the last statement that starts at the top level of the block
// body code[open+1 … close-1], or -1. `}` ends a statement unless `else`,
// `catch`, `finally` or a do-loop's `while` continues it; `;` inside
// parentheses (a for header) ends nothing.
function lastStatementStart(code, open, close) {
  let depth = 0;
  let paren = 0;
  let pending = true;
  let afterBrace = false;
  let current = -1;
  for (let i = open + 1; i < close; i += 1) {
    const ch = code[i];
    if (pending && !/\s/.test(ch)) {
      const rest = code.slice(i, i + 12);
      const continues = afterBrace && (JAVA_CONTINUATION_RE.test(rest)
        || (/^while\b/.test(rest) && current >= 0 && /^do\b/.test(code.slice(current, current + 3))));
      if (!continues) current = i;
      pending = false;
      afterBrace = false;
    }
    if (ch === '(') paren += 1;
    else if (ch === ')') paren -= 1;
    else if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0 && paren === 0) {
        pending = true;
        afterBrace = true;
      }
    } else if (ch === ';' && depth === 0 && paren === 0) {
      pending = true;
      afterBrace = false;
    }
  }
  return current;
}

/**
 * Where „at the end of main" is (review R-O2).
 *   Python: after the last line of main.py, at the top level — or before a
 *     trailing top-level `while True:` / `while 1:`.
 *   Java: inside `main()`, below its last line and indented like its body —
 *     or before a trailing `while (true)`, `for (;;)`, `do … while (true);`
 *     or `return`.
 * Returns `{afterLine, indent}`; for a Java `main` whose closing brace
 * shares its line with code, `{split: {at, bodyIndent, closeIndent}}` (the
 * body is opened up at that brace, see insertAtTarget); for Java with no
 * findable, closed `main`, `{notFound: true}`.
 */
export function mainBodyEnd(content, language) {
  const text = typeof content === 'string' ? content : '';
  const rows = text.split('\n');
  if (language !== 'java') {
    const codeRows = codeOnlyText(text, 'python').split('\n');
    let lastTop = -1;
    codeRows.forEach((row, i) => {
      if (row.trim() !== '' && !/^\s/.test(row)) lastTop = i;
    });
    if (lastTop >= 0 && PY_ENDLESS_RE.test(codeRows[lastTop])) return { afterLine: lastTop, indent: '' };
    return { afterLine: lastRealLine(text), indent: '' };
  }
  const code = codeOnlyText(text, 'java');
  const decl = /\bstatic\s+void\s+main\s*\(/.exec(code);
  if (!decl) return { notFound: true };
  const open = code.indexOf('{', decl.index + decl[0].length);
  if (open < 0) return { notFound: true };
  let depth = 0;
  let close = -1;
  for (let i = open; i < code.length; i += 1) {
    if (code[i] === '{') depth += 1;
    else if (code[i] === '}') {
      depth -= 1;
      if (depth === 0) {
        close = i;
        break;
      }
    }
  }
  if (close < 0) return { notFound: true };
  const unit = detectIndentUnit(text, 'java') || CODE_INDENT_UNIT;
  const openLine = lineOf(text, open);
  const closeLine = lineOf(text, close);
  const closeRow = rows[closeLine - 1];
  const closeCol = close - (text.lastIndexOf('\n', close - 1) + 1);
  if (closeLine === openLine || closeRow.slice(0, closeCol).trim() !== '') {
    const declIndent = leadingWhitespace(rows[openLine - 1]);
    const bodyIndent = closeLine === openLine ? declIndent + unit : leadingWhitespace(closeRow);
    return { split: { at: close, bodyIndent, closeIndent: declIndent } };
  }
  const last = lastStatementStart(code, open, close);
  if (last >= 0) {
    const stmt = code.slice(last, close).trim();
    if (JAVA_ENDLESS_RES.some((re) => re.test(stmt))) {
      const line = lineOf(text, last);
      return { afterLine: line - 1, indent: leadingWhitespace(rows[line - 1]) };
    }
  }
  const closeIndent = leadingWhitespace(closeRow);
  let bodyIndent = null;
  for (let l = closeLine - 1; l > openLine; l -= 1) {
    if (rows[l - 1].trim() === '') continue;
    const o = leadingWhitespace(rows[l - 1]);
    if (o.length > closeIndent.length && o.startsWith(closeIndent)) bodyIndent = o;
    break;
  }
  return { afterLine: closeLine - 1, indent: bodyIndent ?? closeIndent + unit };
}

/**
 * Apply an insertion target (insertionTarget / mainBodyEnd) to `content`:
 * `{content, firstLine, lastLine}`. A `split` target opens a one-line body
 * at its closing brace: the lines go on their own lines, the brace on the
 * next, indented like the declaration.
 */
export function insertAtTarget(content, target, lines, language) {
  const text = typeof content === 'string' ? content : '';
  const body = Array.isArray(lines) ? lines : [];
  if (target && target.split) {
    const { at, bodyIndent, closeIndent } = target.split;
    const before = text.slice(0, at).replace(/[ \t]+$/, '');
    const firstLine = before.split('\n').length + 1;
    const next = `${before}\n${body.map((l) => `${bodyIndent}${l}`).join('\n')}\n${closeIndent}${text.slice(at)}`;
    return { content: next, firstLine, lastLine: firstLine + body.length - 1 };
  }
  const afterLine = target ? target.afterLine : 0;
  return insertLinesAt(text, afterLine, body, {
    indent: target && typeof target.indent === 'string' ? target.indent : undefined,
    language,
  });
}

/**
 * The insertion point for `files`: the last cursor `{file, line}` when that
 * file exists and has that line (below it, indented like it), else the end of
 * the entry file's main (mainBodyEnd: before a trailing endless loop or
 * return; `split` for a one-line Java main; `notFound` when Java has no main —
 * the caller writes nothing and says so). Apply it with insertAtTarget.
 * @returns {{file: string, afterLine?: number, indent?: string, split?: object,
 *   notFound?: true, fromCursor: boolean}}
 */
export function insertionTarget(files, language, cursor) {
  const project = files && typeof files === 'object' ? files : {};
  if (cursor && typeof cursor.file === 'string' && Number.isInteger(cursor.line)
      && typeof project[cursor.file] === 'string') {
    const lines = project[cursor.file].split('\n').length;
    if (cursor.line >= 1 && cursor.line <= lines) {
      return { file: cursor.file, afterLine: cursor.line, fromCursor: true };
    }
  }
  const entry = ENTRY_FILE[language];
  const at = mainBodyEnd(project[entry] || '', language);
  if (at.notFound) return { file: entry, notFound: true, fromCursor: false };
  if (at.split) return { file: entry, split: at.split, fromCursor: false };
  return { file: entry, afterLine: at.afterLine, indent: at.indent, fromCursor: false };
}
