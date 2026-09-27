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
//     indented like it (one level deeper after a line that opens a block — `:`
//     or `{`). A student who never clicked into the editor gets the end of main:
//     the end of main.py at the top level, or just inside `main()`'s closing
//     brace in Java (a brace scan that skips strings and comments).
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
const DEFAULT_INDENT_UNIT = '    ';

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

// Where the lines go and how they are indented; shared by the string form
// (insertLinesAt) and the editor-change form (insertionEdit), so the two can
// never disagree.
function planInsertion(content, afterLine, lines, opts) {
  const text = typeof content === 'string' ? content : '';
  const rows = text.split('\n');
  const body = Array.isArray(lines) ? lines : [];
  const at = Math.min(Math.max(Number.isInteger(afterLine) ? afterLine : 0, 0), rows.length);
  let indent = typeof opts.indent === 'string' ? opts.indent : null;
  if (indent === null) {
    indent = '';
    for (let i = at - 1; i >= 0; i -= 1) {
      if (rows[i].trim() === '') continue;
      const own = leadingWhitespace(rows[i]);
      const code = codePart(rows[i], opts.language || 'python');
      const unit = own.includes('\t') ? '\t' : DEFAULT_INDENT_UNIT;
      indent = /[:{]$/.test(code) ? own + unit : own;
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

/**
 * Where „at the end of main" is. Python: after the last line of main.py, at
 * the top level. Java: just before `main()`'s closing brace, one level inside
 * it — or the end of the file when there is no `static void main(` whose body
 * spans lines.
 * @returns {{afterLine: number, indent: string}}
 */
export function mainBodyEnd(content, language) {
  const text = typeof content === 'string' ? content : '';
  const end = { afterLine: lastRealLine(text), indent: '' };
  if (language !== 'java') return end;
  const code = codeOnlyText(text, 'java');
  const decl = /\bstatic\s+void\s+main\s*\(/.exec(code);
  if (!decl) return end;
  const open = code.indexOf('{', decl.index + decl[0].length);
  if (open < 0) return end;
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
  if (close < 0) return end;
  const openLine = lineOf(text, open);
  const closeLine = lineOf(text, close);
  if (closeLine <= openLine) return end;
  const closeRow = text.split('\n')[closeLine - 1];
  const own = leadingWhitespace(closeRow);
  return { afterLine: closeLine - 1, indent: own + (own.includes('\t') ? '\t' : DEFAULT_INDENT_UNIT) };
}

/**
 * The insertion point for `files`: the last cursor `{file, line}` when that
 * file exists and has that line (below it, indented like it), else the end of
 * the entry file's main.
 * @returns {{file: string, afterLine: number, indent?: string, fromCursor: boolean}}
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
  return { file: entry, afterLine: at.afterLine, indent: at.indent, fromCursor: false };
}
