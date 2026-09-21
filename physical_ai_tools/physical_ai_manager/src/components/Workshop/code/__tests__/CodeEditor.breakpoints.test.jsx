/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The editor half of the Python debugger, against a REAL EditorView in jsdom
// (the same shape as CodeEditor.lint.test.jsx): the breakpoint gutter, the
// press that toggles one, and the run-line highlight the server drives through
// `current_block_id`.
//
// The Java asymmetry is structural, not a flag the editor reads: `CodeWorkspace`
// hands `onToggleBreakpoint` only for Python, and without it the gutter
// extension is never installed — so there is no gutter to press and none to
// mis-read. One test pins exactly that.
//
// jsdom has no layout: every rectangle is 0×0, so CodeMirror's own
// height-to-line mapping answers „line 1" for any Y. That is why the editor
// resolves a gutter press from the PRESSED element's `data-line` first and
// only falls back to the height mapping — and it is why these assertions can
// press one line and read back that line. The hidden spacer element the gutter
// appends for its base width is filtered out by its inline `visibility: hidden`.

import React from 'react';
import { render, act } from '@testing-library/react';
import CodeEditor from '../CodeEditor';

const PY = Array.from({ length: 20 }, (_, i) => `zeile_${i + 1} = ${i + 1}`).join('\n');
const JAVA = 'class Main {\n  public static void main(String[] args) {\n    int x = 1;\n  }\n}\n';

/*
 * CodeMirror's gutter and content lines carry no ARIA role and no text a
 * Testing-Library query can reach — a gutter element for an unset breakpoint
 * is deliberately EMPTY. Every direct node access in this file is confined to
 * these five readers.
 */
/* eslint-disable testing-library/no-container, testing-library/no-node-access */
const hasGutter = (container) => !!container.querySelector('.cm-edubotics-bp-gutter');

function gutterLines(container) {
  const g = container.querySelector('.cm-edubotics-bp-gutter');
  if (!g) return [];
  return Array.from(g.querySelectorAll('.cm-gutterElement'))
    .filter((el) => el.style.visibility !== 'hidden');
}

const markerIn = (el) => el.querySelector('[data-line]');
const hasSetDot = (el) => !!el.querySelector('.cm-edubotics-bp-set');
const contentLines = (container) => Array.from(container.querySelectorAll('.cm-line'));
/* eslint-enable testing-library/no-container, testing-library/no-node-access */

const BASE = {
  language: 'python',
  path: 'main.py',
  value: PY,
  onChange: () => {},
  breakpointLines: [],
  onToggleBreakpoint: null,
  highlightLine: null,
  highlightKind: null,
};

function mount(props) {
  const utils = render(<CodeEditor {...BASE} {...props} />);
  return {
    ...utils,
    show: (next) => utils.rerender(<CodeEditor {...BASE} {...props} {...next} />),
  };
}

describe('CodeEditor — the breakpoint gutter', () => {
  test('a Python file gets the gutter, and a set breakpoint is drawn on its own line', () => {
    const { container } = mount({ breakpointLines: [12], onToggleBreakpoint: () => {} });
    const lines = gutterLines(container);
    expect(lines.length).toBeGreaterThanOrEqual(12);
    // EVERY line is a press target (that is how an unset breakpoint is set);
    // only the set one carries the dot.
    expect(lines.every(markerIn)).toBe(true);
    const marked = lines.filter(hasSetDot);
    expect(marked).toHaveLength(1);
    expect(lines.indexOf(marked[0])).toBe(11); // line 12, 0-based
  });

  test('a press on a gutter line toggles THAT line, by its document line number', () => {
    const onToggleBreakpoint = vi.fn();
    const { container } = mount({ onToggleBreakpoint });
    const lines = gutterLines(container);
    act(() => {
      markerIn(lines[11]).dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    });
    expect(onToggleBreakpoint).toHaveBeenCalledTimes(1);
    expect(onToggleBreakpoint).toHaveBeenCalledWith(12);
  });

  test('the set of breakpoints is live — a rerender moves the dot', () => {
    const { container, show } = mount({ breakpointLines: [12], onToggleBreakpoint: () => {} });
    const at = () => gutterLines(container).findIndex(hasSetDot);
    expect(at()).toBe(11);
    show({ breakpointLines: [3] });
    expect(at()).toBe(2);
  });

  test('a Java file has no gutter at all — the asymmetry is structural', () => {
    const { container } = mount({ language: 'java', path: 'Main.java', value: JAVA });
    expect(hasGutter(container)).toBe(false);
  });
});

describe('CodeEditor — the run-line highlight', () => {
  const marked = (container) => contentLines(container)
    .filter((el) => el.classList.contains('cm-edubotics-run-line'));

  test('the paused line carries the highlight class, and only that line', () => {
    const { container } = mount({ highlightLine: 7, highlightKind: 'paused' });
    expect(marked(container)).toHaveLength(1);
    expect(marked(container)[0].classList.contains('cm-edubotics-run-paused')).toBe(true);
    expect(contentLines(container).indexOf(marked(container)[0])).toBe(6); // line 7, 0-based
  });

  test('the kind rides the class, so a paused line is distinguishable from a running one', () => {
    const { container, show } = mount({ highlightLine: 4, highlightKind: 'paused' });
    expect(marked(container)[0].classList.contains('cm-edubotics-run-paused')).toBe(true);
    show({ highlightKind: 'running' });
    expect(marked(container)[0].classList.contains('cm-edubotics-run-running')).toBe(true);
    expect(marked(container)[0].classList.contains('cm-edubotics-run-paused')).toBe(false);
  });

  test('no highlight, and a line beyond the document, leave every line alone', () => {
    const { container, show } = mount({ highlightLine: null, highlightKind: null });
    expect(marked(container)).toHaveLength(0);
    // A `current_block_id` naming a line the open file does not have (the
    // student edited it while the run was live) must not throw out of
    // `Text.line`, which would take the whole editor down.
    show({ highlightLine: 999, highlightKind: 'running' });
    expect(marked(container)).toHaveLength(0);
  });
});
