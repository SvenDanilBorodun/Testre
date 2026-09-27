/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The editor indents like the FILE (review round 2, MA1 / owner decision
// R2-O2), against a REAL CodeMirror view and its real commands — Enter
// (`insertNewlineAndIndent`), Tab (`indentMore`), Backspace
// (`deleteCharBackward`) — inside a 2-space, a 4-space and a tab body; a new
// file gets 4 spaces; and „Einfügen" lands where Enter would.
//
// Every program typed here is also written to fixtures/
// code-editor-indent-cases.json, which robotis_ai_setup/tests/
// test_code_insert_cases.py compiles with CPython (3.12 in CI) and runs to its
// `robot.replay("Winken")` marker — so „it compiles" is the interpreter's
// word, not this file's. Regenerate with
//
//   EDUBOTICS_REGEN_CODE_CASES=1 npx vitest run src/components/Workshop/code/__tests__/CodeEditor.indent.test.jsx
//
// The CodeMirror packages are dynamic imports for the reason
// CodeEditor.lint.test.jsx gives (the entry-bundle eslint rule).

import fs from 'fs';
import path from 'path';
import React from 'react';
import { render, act } from '@testing-library/react';
import CodeEditor from '../CodeEditor';
import { CODE_INDENT_UNIT, insertAtTarget, insertionTargetAt } from '../codeInsert';

const FIXTURE = path.join(__dirname, 'fixtures', 'code-editor-indent-cases.json');
const REGEN = !['', '0', undefined].includes(process.env.EDUBOTICS_REGEN_CODE_CASES);
const MARK = 'robot.replay("Winken")';

let EditorView;
let EditorSelection;
let insertNewlineAndIndent;
let indentMore;
let deleteCharBackward;
let indentUnitFacet;

const hadRangeRects = typeof Range !== 'undefined' && typeof Range.prototype.getClientRects === 'function';
beforeAll(async () => {
  if (typeof Range !== 'undefined' && !hadRangeRects) {
    Range.prototype.getClientRects = function getClientRects() { return []; };
    Range.prototype.getBoundingClientRect = function getBoundingClientRect() {
      return {
        x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0,
      };
    };
  }
  const [view, state, commands, language] = await Promise.all([
    import('@codemirror/view'),
    import('@codemirror/state'),
    import('@codemirror/commands'),
    import('@codemirror/language'),
  ]);
  EditorView = view.EditorView;
  EditorSelection = state.EditorSelection;
  insertNewlineAndIndent = commands.insertNewlineAndIndent;
  indentMore = commands.indentMore;
  deleteCharBackward = commands.deleteCharBackward;
  indentUnitFacet = language.indentUnit;
});
afterAll(() => {
  if (typeof Range !== 'undefined' && !hadRangeRects) {
    delete Range.prototype.getClientRects;
    delete Range.prototype.getBoundingClientRect;
  }
});

/* eslint-disable testing-library/no-container, testing-library/no-node-access */
function mount(value) {
  const utils = render(<CodeEditor language="python" path="main.py" value={value} onChange={() => {}} />);
  const view = EditorView.findFromDOM(utils.container.querySelector('.cm-editor'));
  return { ...utils, view };
}
/* eslint-enable testing-library/no-container, testing-library/no-node-access */

const endOf = (view, line) => view.state.doc.line(line).to;
const put = (view, pos) => act(() => { view.dispatch({ selection: EditorSelection.cursor(pos) }); });
const run = (view, command) => act(() => { command(view); });
const type = (view, text) => act(() => {
  view.dispatch(view.state.replaceSelection(text), { userEvent: 'input.type' });
});
const lineText = (view, line) => view.state.doc.line(line).text;
const caretCol = (view) => {
  const head = view.state.selection.main.head;
  return head - view.state.doc.lineAt(head).from;
};

const STYLES = [['zwei', '  '], ['vier', '    '], ['tab', '\t']];
const program = (u) => `import robot\n\ndef f():\n${u}for i in range(3):\n${u}${u}robot.home()\n${u}robot.log("x")\n\nf()\n`;

const produced = [];
function record(name, output, reach = 'run') {
  produced.push({
    name, language: 'python', line: null, reach, input: null, output, hint: null,
  });
}

describe.each(STYLES)('a %s body', (style, u) => {
  test('the editor takes the file’s own unit', () => {
    const { view } = mount(program(u));
    expect(view.state.facet(indentUnitFacet)).toBe(u);
  });

  test('Enter after a block opener indents one of the file’s units deeper', () => {
    const { view } = mount(program(u));
    put(view, endOf(view, 4));
    run(view, insertNewlineAndIndent);
    expect(lineText(view, 5)).toBe(u + u);
    type(view, MARK);
    record(`enter_after_opener_${style}`, view.state.doc.toString());
  });

  test('Enter inside a body keeps the body’s indentation', () => {
    const { view } = mount(program(u));
    put(view, endOf(view, 5));
    run(view, insertNewlineAndIndent);
    expect(lineText(view, 6)).toBe(u + u);
    type(view, MARK);
    record(`enter_in_body_${style}`, view.state.doc.toString());
  });

  test('Backspace in the indentation removes ONE of the file’s units, never all', () => {
    const { view } = mount(program(u));
    put(view, endOf(view, 5));
    run(view, insertNewlineAndIndent);
    run(view, deleteCharBackward);
    expect(lineText(view, 6)).toBe(u);
    expect(caretCol(view)).toBe(u.length);
    type(view, MARK);
    record(`backspace_one_unit_${style}`, view.state.doc.toString());
  });

  test('Tab indents a line by one of the file’s units', () => {
    const src = `import robot\n\ndef f():\n${u}if True:\n${u}${MARK}\n\nf()\n`;
    const { view } = mount(src);
    put(view, view.state.doc.line(5).from + u.length);
    run(view, indentMore);
    expect(lineText(view, 5)).toBe(`${u}${u}${MARK}`);
    record(`tab_one_unit_${style}`, view.state.doc.toString());
  });

  test('„Einfügen" on the opener’s line lands where Enter does', () => {
    const src = program(u);
    const target = insertionTargetAt(src, 'python', 4);
    const { view } = mount(src);
    put(view, endOf(view, 4));
    run(view, insertNewlineAndIndent);
    expect(target.indent).toBe(lineText(view, 5));
    record(`einfuegen_after_opener_${style}`, insertAtTarget(src, target, [MARK], 'python').content);
    // An opener with no body yet: the file's unit on both sides too.
    const bodyless = `import robot\ndef g():\n${u}pass\nif True:\n`;
    const t2 = insertionTargetAt(bodyless, 'python', 4);
    const second = mount(bodyless).view;
    put(second, endOf(second, 4));
    run(second, insertNewlineAndIndent);
    expect(t2.indent).toBe(lineText(second, 5));
    expect(t2.indent).toBe(u);
    record(`einfuegen_bodyless_opener_${style}`, insertAtTarget(bodyless, t2, [MARK], 'python').content);
  });
});

describe('a file without indentation yet', () => {
  test('a new file gets 4 spaces, and Enter after an opener gives them', () => {
    const { view } = mount('');
    expect(view.state.facet(indentUnitFacet)).toBe(CODE_INDENT_UNIT);
    type(view, 'import robot\ndef f():');
    run(view, insertNewlineAndIndent);
    expect(caretCol(view)).toBe(4);
    type(view, MARK);
    run(view, insertNewlineAndIndent);
    run(view, deleteCharBackward);
    expect(caretCol(view)).toBe(0);
    type(view, 'f()\n');
    record('new_file_four_spaces', view.state.doc.toString());
  });

  test('the first indented line the student types sets the unit', () => {
    const { view } = mount('import robot\nif True:\n');
    expect(view.state.facet(indentUnitFacet)).toBe(CODE_INDENT_UNIT);
    put(view, view.state.doc.line(3).from);
    type(view, '  robot.home()');
    expect(view.state.facet(indentUnitFacet)).toBe('  ');
    run(view, insertNewlineAndIndent);
    expect(lineText(view, 4)).toBe('  ');
    type(view, MARK);
    record('first_indented_line_sets_two', view.state.doc.toString());
  });

  test('an external replacement (a version restore) brings its own unit', () => {
    const { view, rerender } = mount('import robot\nx = 1\n');
    rerender(<CodeEditor language="python" path="main.py" value={program('  ')} onChange={() => {}} />);
    expect(view.state.facet(indentUnitFacet)).toBe('  ');
  });
});

describe('the fixture the Python test compiles', () => {
  test('matches what the editor produced above', () => {
    const doc = {
      _generated_by: 'physical_ai_manager/src/components/Workshop/code/__tests__/CodeEditor.indent.test.jsx '
        + '(EDUBOTICS_REGEN_CODE_CASES=1 to regenerate)',
      _checked_by: 'robotis_ai_setup/tests/test_code_insert_cases.py (CPython ast/compile + a run)',
      marker: { python: MARK },
      cases: produced.slice().sort((a, b) => a.name.localeCompare(b.name)),
    };
    expect(doc.cases.length).toBe(STYLES.length * 6 + 2);
    if (REGEN) {
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, `${JSON.stringify(doc, null, 1)}\n`);
    }
    expect(doc).toEqual(JSON.parse(fs.readFileSync(FIXTURE, 'utf8')));
  });
});
