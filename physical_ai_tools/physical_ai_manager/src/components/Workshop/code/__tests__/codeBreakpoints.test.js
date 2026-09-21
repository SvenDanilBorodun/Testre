/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The one id shape a code program adds to a wire that already carries Blockly
// block ids: `<file>:L<line>`. It rides `WorkflowSetBreakpoints.srv`'s
// free-form `string[]` and `WorkflowStatus.current_block_id` unchanged, so the
// SHAPE is the whole contract — an id that does not match is a Blockly id and
// must be left alone in both directions (the editor ignores it, the Blockly
// canvas keeps it).
//
// Pure module on purpose: it is imported by CodeWorkspace, RunControls AND
// DebugPanel, none of which may pull CodeMirror in (package.json's
// `no-restricted-imports` excludes exactly the two editor components).

import {
  CODE_BREAKPOINT_ID_RE,
  breakpointLinesForFile,
  codeBreakpointId,
  isCodeBreakpointId,
  parseCodeBreakpointId,
} from '../codeBreakpoints';

describe('codeBreakpointId / parseCodeBreakpointId', () => {
  test('the id is `<file>:L<line>` and every id it builds matches the pinned shape', () => {
    expect(codeBreakpointId('main.py', 12)).toBe('main.py:L12');
    expect(codeBreakpointId('formen/kreis.py', 3)).toBe('formen/kreis.py:L3');
    expect(codeBreakpointId('Main.java', 140)).toBe('Main.java:L140');
    for (const id of ['main.py:L12', 'formen/kreis.py:L3', 'Main.java:L140']) {
      expect(CODE_BREAKPOINT_ID_RE.test(id)).toBe(true);
      expect(isCodeBreakpointId(id)).toBe(true);
    }
  });

  test('a Blockly id is never mistaken for one, in either direction', () => {
    // Blockly's genUid alphabet contains `:` — see the `[VAR:]` frame note in
    // CLAUDE.md — so the refusal has to be the FULL shape, not "contains a colon".
    for (const id of ['b7', 'vorschau-aufnahme-3f9a1c0d', 'q:Lx', 'a:b:L3', ':L3', 'main.py:L', 'main.py:12']) {
      expect(isCodeBreakpointId(id)).toBe(false);
      expect(parseCodeBreakpointId(id)).toBeNull();
    }
    for (const id of [null, undefined, 12, {}]) {
      expect(isCodeBreakpointId(id)).toBe(false);
      expect(parseCodeBreakpointId(id)).toBeNull();
    }
  });

  test('parsing gives back the file and the line as a number', () => {
    expect(parseCodeBreakpointId('main.py:L12')).toEqual({ path: 'main.py', line: 12 });
    expect(parseCodeBreakpointId('formen/kreis.py:L3')).toEqual({ path: 'formen/kreis.py', line: 3 });
  });

  test('line 0 parses as nothing — a document line is 1-based', () => {
    // The regex accepts `\d+`, so the digits alone cannot be the whole gate:
    // a `:L0` from an older or hand-built payload must not become line 0 and
    // then a decoration at `doc.line(0)`, which throws.
    expect(CODE_BREAKPOINT_ID_RE.test('main.py:L0')).toBe(true);
    expect(parseCodeBreakpointId('main.py:L0')).toBeNull();
  });
});

describe('breakpointLinesForFile', () => {
  test('only this file, sorted, de-duplicated', () => {
    const ids = ['hilfe.py:L9', 'main.py:L12', 'main.py:L3', 'main.py:L12', 'b7'];
    expect(breakpointLinesForFile(ids, 'main.py')).toEqual([3, 12]);
    expect(breakpointLinesForFile(ids, 'hilfe.py')).toEqual([9]);
    expect(breakpointLinesForFile(ids, 'formen/kreis.py')).toEqual([]);
  });

  test('a missing or non-array id list is empty, never a throw', () => {
    expect(breakpointLinesForFile(null, 'main.py')).toEqual([]);
    expect(breakpointLinesForFile(undefined, 'main.py')).toEqual([]);
  });
});
