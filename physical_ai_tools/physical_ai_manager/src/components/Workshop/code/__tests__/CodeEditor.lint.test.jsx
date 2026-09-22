/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// A16, the half a pure test cannot see: the DEBOUNCE and the CURSOR-LINE rule
// as the real `@codemirror/lint` plugin applies them inside a real EditorView.
// One test, three ordered assertions, so it genuinely fails before the change
// on (i): an editor with no linter at all passes (ii) trivially.
//
// Exercised through `buildParseLint` (the mechanism) because the ship list is
// EMPTY today (§3.11's exit, docs/KNOWN-ISSUES.md); the last test pins that
// `parseLintExtensions` — what the editor installs — is inert for both
// languages while it stays so.
//
// The CodeMirror packages are dynamic imports here for the same reason as in
// parseMarkers.test.js: the entry-bundle eslint rule excludes exactly the two
// shipped editor components, and a test file never reaches a bundle.
//
// `@codemirror/lint` dispatches its diagnostics from a `.then` after the delay
// timer fires, so the timers are advanced with the ASYNC form, which also
// drains that microtask.

import { CODE_LINT_IDLE_MS } from '../parseMarkers';
import { buildParseLint, parseLintExtensions } from '../CodeEditor';

let EditorView;
let EditorState;
let EditorSelection;
let python;
let forEachDiagnostic;

beforeAll(async () => {
  const [view, state, lang, lint] = await Promise.all([
    import('@codemirror/view'),
    import('@codemirror/state'),
    import('@codemirror/lang-python'),
    import('@codemirror/lint'),
  ]);
  EditorView = view.EditorView;
  EditorState = state.EditorState;
  EditorSelection = state.EditorSelection;
  python = lang.python;
  forEachDiagnostic = lint.forEachDiagnostic;
});

const GOOD = [
  'import robot',                     // 1
  '',                                 // 2
  'robot.home()',                     // 3
  'anzahl = 0',                       // 4
  'for i in range(3):',               // 5
  '    robot.log("Runde")',           // 6
  '    anzahl = anzahl + 1',          // 7
  '',                                 // 8
  'robot.open_gripper()',             // 9
  'robot.home()',                     // 10
].join('\n');

function diagnosticLines(view) {
  const lines = [];
  forEachDiagnostic(view.state, (d, from) => {
    lines.push(view.state.doc.lineAt(from).number);
  });
  return lines.sort((a, b) => a - b);
}

function lineRange(view, n) {
  const line = view.state.doc.line(n);
  return { from: line.from, to: line.to };
}

function mount(host, extensions) {
  return new EditorView({
    state: EditorState.create({ doc: GOOD, extensions: [python(), extensions] }),
    parent: host,
  });
}

describe('CodeEditor — buildParseLint in a real EditorView', () => {
  let host;
  let view;

  beforeEach(() => {
    vi.useFakeTimers();
    host = document.createElement('div');
    document.body.appendChild(host);
    view = mount(host, buildParseLint('python'));
  });

  afterEach(() => {
    view.destroy();
    host.remove();
    vi.useRealTimers();
  });

  test('debounce, then the cursor-line rule, then leaving the line reveals it', async () => {
    // (i) A defect on line 3, cursor parked on line 9: nothing immediately,
    //     exactly one diagnostic on line 3 once the idle window has passed.
    const l3 = lineRange(view, 3);
    view.dispatch({
      changes: { from: l3.from, to: l3.to, insert: 'robot.home())' },
      selection: EditorSelection.cursor(lineRange(view, 9).to),
    });
    expect(diagnosticLines(view)).toEqual([]);
    await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS + 50);
    expect(diagnosticLines(view)).toEqual([3]);

    // (ii) A half-typed line where the cursor IS: never marked, however long
    //      the student pauses; line 3's marker stays.
    const l9 = lineRange(view, 9);
    view.dispatch({
      changes: { from: l9.from, to: l9.to, insert: 'if ' },
      selection: EditorSelection.cursor(l9.from + 3),
    });
    await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS + 50);
    expect(diagnosticLines(view)).toEqual([3]);
    await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS * 10);
    expect(diagnosticLines(view)).toEqual([3]);

    // (iii) Moving the cursor to line 1 — a selection change, no document
    //       change — is what reveals line 9 (the needsRefresh path).
    view.dispatch({ selection: EditorSelection.cursor(0) });
    await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS + 50);
    expect(diagnosticLines(view)).toEqual([3, 9]);
  });

  test('the diagnostics carry the German notice and the warning severity', async () => {
    const l3 = lineRange(view, 3);
    view.dispatch({
      changes: { from: l3.from, to: l3.to, insert: 'robot.home())' },
      selection: EditorSelection.cursor(lineRange(view, 9).to),
    });
    await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS + 50);
    const seen = [];
    forEachDiagnostic(view.state, (d) => seen.push(d));
    expect(seen).toHaveLength(1);
    expect(seen[0].severity).toBe('warning');
    expect(seen[0].message).toContain('noch nicht lesen');
    expect(seen[0].message).not.toMatch(/Fehler/);
  });
});

describe('CodeEditor — parseLintExtensions is the ship gate', () => {
  test('installs nothing for both languages while CODE_LINT_LANGUAGES is empty', async () => {
    vi.useFakeTimers();
    const host = document.createElement('div');
    document.body.appendChild(host);
    try {
      for (const language of ['python', 'java']) {
        expect(parseLintExtensions(language)).toEqual([]);
      }
      const view = mount(host, parseLintExtensions('python'));
      const l3 = lineRange(view, 3);
      view.dispatch({
        changes: { from: l3.from, to: l3.to, insert: 'robot.home())' },
        selection: EditorSelection.cursor(lineRange(view, 9).to),
      });
      await vi.advanceTimersByTimeAsync(CODE_LINT_IDLE_MS * 10);
      expect(diagnosticLines(view)).toEqual([]);
      view.destroy();
    } finally {
      host.remove();
      vi.useRealTimers();
    }
  });
});
