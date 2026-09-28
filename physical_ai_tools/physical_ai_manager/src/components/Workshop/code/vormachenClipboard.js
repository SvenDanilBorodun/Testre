/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The lines Vormachen's „Als Programm einfügen" put on the clipboard because
// the student had not clicked into the code yet (owner decision R3-O4).
//
// Owner decision R4-O1: whatever a student pastes goes into the editor
// EXACTLY as copied — except this one text. The lines Vormachen itself just
// copied are remembered HERE, in memory (never in browser storage), and a
// paste of exactly that text behaves like „Einfügen": whole lines directly
// below the cursor line, with the block's indentation, through the same check
// (codeInsert.insertionTargetAt) — or nothing and the German reason. It never
// joins an existing line. An edited copy is ordinary text and pastes verbatim.
//
// The copied text ends with a line break (review round 4, mc2), so a paste
// somewhere this editor cannot see — another program, a text field — never
// runs two statements together on one line.
//
// PURE and tiny: TeachOverlay writes it (entry bundle), the lazy CodeEditor
// reads it; no CodeMirror here.

let remembered = null;

// The clipboard may hand back Windows line breaks (CRLF) for what was written
// with LF; the comparison is on the lines, not on the break characters.
function normalized(text) {
  return typeof text === 'string' ? text.replace(/\r\n?/g, '\n') : '';
}

/** The text put on the clipboard for `lines`: one per line, ending with a line break. */
export function vormachenClipboardText(lines) {
  const body = (Array.isArray(lines) ? lines : []).filter((l) => typeof l === 'string');
  return body.length === 0 ? '' : `${body.join('\n')}\n`;
}

/**
 * Remember the lines just copied for `language` ('python' | 'java'); returns
 * the clipboard text. Call it only once the clipboard write succeeded.
 */
export function rememberVormachenCopy(language, lines) {
  const text = vormachenClipboardText(lines);
  remembered = text ? { language, text, lines: text.slice(0, -1).split('\n') } : null;
  return text;
}

/** Forget the remembered copy (tests; a copy that failed). */
export function forgetVormachenCopy() {
  remembered = null;
}

/**
 * The lines to insert when `text` is pasted into a `language` program: the
 * remembered Vormachen lines when `text` is EXACTLY what Vormachen copied for
 * that language (line breaks aside), else null — an ordinary paste.
 */
export function vormachenPasteLines(text, language) {
  if (!remembered || remembered.language !== language) return null;
  return normalized(text) === remembered.text ? remembered.lines.slice() : null;
}
