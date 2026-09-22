/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The ONE id shape a code program adds: `<file>:L<line>`. It rides the
// EXISTING wire — `WorkflowSetBreakpoints.srv`'s free-form `string[]` and
// `WorkflowStatus.current_block_id` — so nothing on either side was widened;
// an old client's only consumer of `current_block_id`,
// `WorkspaceSvg.highlightBlock`, is a silent no-op on an unknown id.
//
// Because the same two fields now carry TWO id spaces, the shape is what tells
// them apart, in both directions: the code editor ignores a Blockly id and
// RunControls' Blockly highlight ignores a code id. Blockly's own `genUid`
// alphabet contains `:` (the `[VAR:]`-frame note in CLAUDE.md is the scar), so
// „contains a colon" would be a wrong test — the full shape is the gate.
//
// Pure on purpose: CodeWorkspace, RunControls and DebugPanel all import it and
// none of them may pull CodeMirror in (package.json's `no-restricted-imports`
// excludes exactly the two editor components).

/** The pinned shape. A path carries no `:`, a line is one or more digits. */
export const CODE_BREAKPOINT_ID_RE = /^[^:]+:L\d+$/;

/** `('main.py', 12)` → `'main.py:L12'`. */
export function codeBreakpointId(path, line) {
  return `${path}:L${line}`;
}

/** True for an id this module owns — false for every Blockly id and non-string. */
export function isCodeBreakpointId(id) {
  return typeof id === 'string' && CODE_BREAKPOINT_ID_RE.test(id);
}

/**
 * `'main.py:L12'` → `{path: 'main.py', line: 12}`, else `null`.
 *
 * The regex accepts `\d+`, so the digits alone cannot be the whole gate: a
 * `:L0` — from a hand-built `/workflow/start` payload or a future server — must
 * not become line 0, which `Text.line(0)` throws on. A document line is 1-based.
 */
export function parseCodeBreakpointId(id) {
  if (!isCodeBreakpointId(id)) return null;
  const cut = id.lastIndexOf(':L');
  const line = Number(id.slice(cut + 2));
  if (!Number.isInteger(line) || line < 1) return null;
  return { path: id.slice(0, cut), line };
}

/** The lines of `path` among `ids`, ascending and de-duplicated. */
export function breakpointLinesForFile(ids, path) {
  if (!Array.isArray(ids)) return [];
  const lines = new Set();
  for (const id of ids) {
    const parsed = parseCodeBreakpointId(id);
    if (parsed && parsed.path === path) lines.add(parsed.line);
  }
  return Array.from(lines).sort((a, b) => a - b);
}
