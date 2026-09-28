/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The editor indents by the program's BLOCK STRUCTURE, against a REAL
// CodeMirror view and its real commands: the stock ones — Enter
// (`insertNewlineAndIndent`), Tab (`indentMore`), Backspace
// (`deleteCharBackward`) — and the keys a student presses, run through the
// view's own keymap (`runScopeHandlers`).
//
//   * review round 2 (MA1 / R2-O2): inside a 2-space, a 4-space and a tab
//     body, Enter/Tab/Backspace continue the file's own indentation; a new
//     file gets 4 spaces; „Einfügen" lands where Enter would.
//   * review round 3 (MB1): the unit alone was fooled — aligned multi-line
//     literals made a 2-space program read as 5-space, a pasted 4-space
//     snippet or one hand-typed 2-space block flipped the whole file. Now only
//     statement starts vote, Enter continues the block's OWN sibling
//     indentation, and Tab/Backspace go to the next/previous level of the
//     block stack — never to a column no block uses. Each case below is a
//     program that compiled before the keystroke; what the keystrokes made
//     must compile too.
//   * review round 4 (MC1): an EMPTY row inside a block (or a comment at
//     column 0) chose nothing — Enter there continues the block, where it
//     used to write column 0 and an IndentationError. (R4-O1): a paste goes
//     in exactly as copied; only the lines Vormachen itself just copied land
//     like „Einfügen", below the cursor line, or not at all with the reason.
//
// Every Python program typed here is also written to fixtures/
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
import { render, act, fireEvent } from '@testing-library/react';
import toast from 'react-hot-toast';
import CodeEditor from '../CodeEditor';
import {
  CODE_INDENT_UNIT, detectIndentUnit, insertAtTarget, insertionTargetAt, SNIPPET_MIME,
} from '../codeInsert';
import { CODE_DE } from '../codeMessagesDe';
import { forgetVormachenCopy, rememberVormachenCopy } from '../vormachenClipboard';

vi.mock('react-hot-toast', () => {
  const t = Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() });
  return { __esModule: true, default: t };
});

const FIXTURE = path.join(__dirname, 'fixtures', 'code-editor-indent-cases.json');
const REGEN = !['', '0', undefined].includes(process.env.EDUBOTICS_REGEN_CODE_CASES);
const MARK = 'robot.replay("Winken")';

let EditorView;
let EditorSelection;
let runScopeHandlers;
let insertNewlineAndIndent;
let indentMore;
let deleteCharBackward;
let indentUnitFacet;
let startCompletion;
let completionStatus;

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
  const [view, state, commands, language, autocomplete] = await Promise.all([
    import('@codemirror/view'),
    import('@codemirror/state'),
    import('@codemirror/commands'),
    import('@codemirror/language'),
    import('@codemirror/autocomplete'),
  ]);
  startCompletion = autocomplete.startCompletion;
  completionStatus = autocomplete.completionStatus;
  EditorView = view.EditorView;
  runScopeHandlers = view.runScopeHandlers;
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
function mount(value, language = 'python') {
  const file = language === 'java' ? 'Main.java' : 'main.py';
  const utils = render(<CodeEditor language={language} path={file} value={value} onChange={() => {}} />);
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
// A key as the student presses it: through the view's own keymap.
const press = (view, key, shift = false) => act(() => {
  const handled = runScopeHandlers(view, new KeyboardEvent('keydown', { key, shiftKey: shift }), 'editor');
  expect(handled).toBe(true);
});
// A paste as the browser delivers it: a `paste` event on the content, which
// CodeMirror's own handler (and the editor's Vormachen handler) read.
// (fireEvent wraps itself in act.)
const paste = (view, text) => {
  fireEvent.paste(view.contentDOM, {
    clipboardData: { types: ['text/plain'], getData: (t) => (t === 'text/plain' ? text : '') },
  });
};
const lineText = (view, line) => view.state.doc.line(line).text;
const caretCol = (view) => {
  const head = view.state.selection.main.head;
  return head - view.state.doc.lineAt(head).from;
};

const STYLES = [['zwei', '  '], ['vier', '    '], ['tab', '\t']];
const program = (u) => `import robot\n\ndef f():\n${u}for i in range(3):\n${u}${u}robot.home()\n${u}robot.log("x")\n\nf()\n`;

const produced = [];
// `runs`: how many times CPython must run the marker (review round 5).
function record(name, output, reach = 'run', runs = undefined) {
  produced.push({
    name,
    language: 'python',
    line: null,
    reach,
    input: null,
    output,
    hint: null,
    ...(runs !== undefined ? { runs } : {}),
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

  test('Enter inside a body keeps the body’s indentation — the stock command and the key', () => {
    const { view } = mount(program(u));
    put(view, endOf(view, 5));
    run(view, insertNewlineAndIndent);
    expect(lineText(view, 6)).toBe(u + u);
    type(view, MARK);
    record(`enter_in_body_${style}`, view.state.doc.toString());
    const second = mount(program(u)).view;
    put(second, endOf(second, 5));
    press(second, 'Enter');
    expect(lineText(second, 6)).toBe(u + u);
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
    const second = mount(program(u)).view;
    put(second, endOf(second, 5));
    press(second, 'Enter');
    press(second, 'Backspace');
    expect(lineText(second, 6)).toBe(u);
  });

  test('Tab indents a line by one of the file’s units — the stock command and the key', () => {
    const src = `import robot\n\ndef f():\n${u}if True:\n${u}${MARK}\n\nf()\n`;
    const { view } = mount(src);
    put(view, view.state.doc.line(5).from + u.length);
    run(view, indentMore);
    expect(lineText(view, 5)).toBe(`${u}${u}${MARK}`);
    record(`tab_one_unit_${style}`, view.state.doc.toString());
    const second = mount(src).view;
    put(second, second.state.doc.line(5).from + u.length);
    press(second, 'Tab');
    expect(lineText(second, 5)).toBe(`${u}${u}${MARK}`);
  });

  test('„Einfügen" below the opener’s line lands where Enter does', () => {
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

  test('a new file: `if True:` then Enter — through the key too (3-A)', () => {
    const { view } = mount('import robot\n');
    put(view, view.state.doc.length);
    type(view, 'if True:');
    press(view, 'Enter');
    expect(caretCol(view)).toBe(4);
    type(view, MARK);
    record('new_file_enter_key_after_opener', view.state.doc.toString());
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

// Review round 3, MB1: 3-A's programs (probes/detect_out.json, indent_out.json)
// and 3-B's, each compiling before the keystroke.
describe('continuation lines never vote (review round 3, MB1)', () => {
  // [program, the line Enter is pressed at the end of]
  const DETECT = {
    two_continuations_one_block: ['import robot\nziele = ["A",\n         "B",\n         "C"]\nwerte = [1,\n         2]\nif True:\n  robot.home()\n', 8],
    points_literals: ['import robot\na = [[0.1, 0.2],\n     [0.3, 0.4]]\nb = [[0.1, 0.2],\n     [0.3, 0.4]]\nc = [[0.1, 0.2],\n     [0.3, 0.4]]\nif True:\n  robot.home()\n  robot.log("x")\n', 10],
    dict_hanging: ['import robot\nd = {\n    "a": 1,\n}\nif True:\n  robot.home()\n', 6],
    backslash_and_docstring: ['import robot\nx = 1 + \\\n      2\ndef f():\n  """Doku\n        mehr"""\n  robot.home()\nf()\n', 7],
  };

  test.each(Object.entries(DETECT))('%s reads as the 2 spaces its blocks use', (name, [src, line]) => {
    expect(detectIndentUnit(src, 'python')).toBe('  ');
    const { view } = mount(src);
    expect(view.state.facet(indentUnitFacet)).toBe('  ');
    put(view, endOf(view, line));
    run(view, insertNewlineAndIndent);
    expect(caretCol(view)).toBe(2);
    type(view, MARK);
    record(`detect_${name}`, view.state.doc.toString());
  });

  test('Java: chained-call continuations do not vote either', () => {
    const src = 'import edubotics.Robot;\npublic class Main {\n  public static void main(String[] args) {\n    String s = String.join(",",\n            "a",\n            "b");\n    int x = Math.max(1,\n            2);\n    Robot.home();\n  }\n}\n';
    expect(detectIndentUnit(src, 'java')).toBe('  ');
  });

  test('aligned literals: Enter in the 2-space block (3-A indent_out, case 4)', () => {
    const src = 'import robot\na = [[0.1, 0.2],\n     [0.3, 0.4]]\nb = [[0.1, 0.2],\n     [0.3, 0.4]]\nc = [[0.1, 0.2],\n     [0.3, 0.4]]\nif True:\n  robot.home()\n';
    const { view } = mount(src);
    put(view, endOf(view, 9));
    run(view, insertNewlineAndIndent);
    type(view, MARK);
    expect(lineText(view, 10)).toBe(`  ${MARK}`);
    record('aligned_literals_enter', view.state.doc.toString());
  });
});

describe('a block continues ITS OWN indentation, whatever the others use (review round 3, MB1)', () => {
  test('one hand-typed 2-space block in a 4-space file: Enter in the 4-space body (3-A case 2)', () => {
    const { view } = mount('import robot\ndef f():\n    robot.home()\n\nf()\n');
    put(view, view.state.doc.length);
    type(view, 'if True:\n  robot.log("a")\n');
    put(view, endOf(view, 3));
    run(view, insertNewlineAndIndent);
    type(view, MARK);
    expect(lineText(view, 4)).toBe(`    ${MARK}`);
    record('hand_typed_block_enter_in_four', view.state.doc.toString());
  });

  test('a 4-space snippet pasted into a 2-space file: Enter in the old block (3-A case 3)', () => {
    const { view } = mount('import robot\nif True:\n  robot.home()\n');
    put(view, view.state.doc.length);
    type(view, 'def g():\n    if True:\n        for i in range(2):\n            robot.beep()\n');
    put(view, endOf(view, 3));
    run(view, insertNewlineAndIndent);
    type(view, MARK);
    expect(lineText(view, 4)).toBe(`  ${MARK}`);
    record('pasted_snippet_enter_in_two', view.state.doc.toString());
  });

  test('Backspace lands on a level, never between two (2 outside, 4 inside)', () => {
    const src = 'import robot\nif True:\n  robot.home()\n  for i in range(1):\n      robot.log("a")\n';
    const { view } = mount(src);
    put(view, endOf(view, 5));
    press(view, 'Enter');
    expect(caretCol(view)).toBe(6);
    press(view, 'Backspace');
    expect(caretCol(view)).toBe(2);
    type(view, MARK);
    record('backspace_to_the_outer_level', view.state.doc.toString());
  });

  test('Tab after a Backspace returns to the block’s own level (4 in a file whose majority is 2)', () => {
    const src = 'import robot\ndef f():\n    robot.home()\n    robot.log("a")\nif True:\n  robot.log("b")\nif True:\n  robot.log("c")\nf()\n';
    const { view } = mount(src);
    expect(view.state.facet(indentUnitFacet)).toBe('  ');
    put(view, endOf(view, 3));
    press(view, 'Enter');
    press(view, 'Backspace');
    expect(caretCol(view)).toBe(0);
    press(view, 'Tab');
    expect(caretCol(view)).toBe(4);
    type(view, MARK);
    record('tab_back_to_the_block_level', view.state.doc.toString());
    press(view, 'Tab', true);
    expect(lineText(view, 4)).toBe(MARK);
  });

  test('a tab block in a spaces file keeps its tabs on Enter', () => {
    const src = 'import robot\ndef f():\n    robot.home()\ndef g():\n    robot.log("b")\nif True:\n\trobot.log("a")\nf()\n';
    const { view } = mount(src);
    expect(view.state.facet(indentUnitFacet)).toBe('    ');
    put(view, endOf(view, 7));
    press(view, 'Enter');
    type(view, MARK);
    expect(lineText(view, 8)).toBe(`\t${MARK}`);
    record('tab_block_in_space_file', view.state.doc.toString());
  });

  test('a clause word still lines up with its compound (the editor’s own rule)', () => {
    const { view } = mount('import robot\nx = 0\n');
    put(view, view.state.doc.length);
    type(view, 'if x:');
    press(view, 'Enter');
    type(view, 'robot.home()');
    press(view, 'Enter');
    type(view, 'else:');
    expect(lineText(view, 5)).toBe('else:');
    press(view, 'Enter');
    type(view, MARK);
    record('else_lines_up', view.state.doc.toString());
  });

  test('„Einfügen" agrees with Enter in the mixed file', () => {
    const src = 'import robot\ndef f():\n    robot.home()\n\nf()\nif True:\n  robot.log("a")\n';
    for (const [line, indent] of [[3, '    '], [7, '  ']]) {
      const target = insertionTargetAt(src, 'python', line);
      const { view } = mount(src);
      put(view, endOf(view, line));
      run(view, insertNewlineAndIndent);
      expect([line, target.indent]).toEqual([line, lineText(view, line + 1)]);
      expect(target.indent).toBe(indent);
    }
  });
});

// Review round 4, MC1: 4-A's P1…P37 and stock_out.json — each program
// compiled before the keystroke. [program, the empty (or column-0 comment)
// line Enter is pressed at the end of, the column Enter must give].
const EMPTY_ROWS = {
  stock_program: ['import robot\ndef main():\n    robot.home()\n\n    robot.log(1)\nmain()\n', 4, 4],
  mid_function: ['import robot\n\ndef main():\n    robot.home()\n\n    robot.log("x")\n\nmain()\n', 5, 4],
  mid_main_guard: ['import robot\n\nif __name__ == "__main__":\n    robot.home()\n\n    robot.log("x")\n', 5, 4],
  mid_for_body: ['import robot\nfor i in range(1):\n    robot.home()\n\n    robot.log("x")\n', 4, 4],
  col0_comment: ['import robot\ndef main():\n    robot.home()\n# Kommentar\n    robot.log("x")\nmain()\n', 4, 4],
  tab_body: ['import robot\nif True:\n\trobot.home()\n\n\trobot.log("x")\n', 4, 1],
  two_space_body: ['import robot\ndef main():\n  robot.home()\n\n  robot.log("x")\nmain()\n', 4, 2],
  crlf_body: ['import robot\r\ndef main():\r\n    robot.home()\r\n\r\n    robot.log("x")\r\nmain()\r\n', 4, 4],
  if_body_then_more: ['import robot\nif True:\n    robot.home()\n\n    robot.log("x")\nrobot.log("y")\n', 4, 4],
  nested_body: ['import robot\ndef main():\n    for i in range(1):\n        robot.home()\n\n        robot.log("x")\nmain()\n', 5, 8],
  before_else: ['import robot\nif True:\n    robot.home()\n\nelse:\n    pass\n', 4, 4],
};

describe('an EMPTY row inside a block chose nothing: Enter continues the block (review round 4, MC1)', () => {
  test.each(Object.entries(EMPTY_ROWS))('%s — the key, then typing, compiles', (name, [src, line, col]) => {
    const { view } = mount(src);
    put(view, endOf(view, line));
    press(view, 'Enter');
    expect(caretCol(view)).toBe(col);
    // The row the student stood on stays as it was.
    expect(lineText(view, line)).toBe(src.replace(/\r/g, '').split('\n')[line - 1]);
    type(view, MARK);
    record(`empty_row_enter_${name}`, view.state.doc.toString());
  });

  test.each(Object.entries(EMPTY_ROWS))('%s — the stock command, and „Einfügen" agrees', (name, [src, line, col]) => {
    const { view } = mount(src);
    put(view, endOf(view, line));
    run(view, insertNewlineAndIndent);
    expect(caretCol(view)).toBe(col);
    const target = insertionTargetAt(src.replace(/\r/g, ''), 'python', line);
    expect([name, target.indent]).toEqual([name, lineText(view, line + 1)]);
  });

  test('a deliberate step back is explicit whitespace: it is kept', () => {
    const src = 'import robot\ndef main():\n    for i in range(1):\n        robot.home()\n    \n    robot.log("x")\nmain()\n';
    const { view } = mount(src);
    put(view, endOf(view, 5));
    press(view, 'Enter');
    expect(caretCol(view)).toBe(4);
    type(view, MARK);
    record('explicit_step_back_kept', view.state.doc.toString());
  });

  test('at the end of the file nothing follows: column 0, after the block', () => {
    const src = 'import robot\nn = 0\nwhile n < 3:\n    n += 1\n\n';
    const { view } = mount(src);
    put(view, endOf(view, 5));
    press(view, 'Enter');
    expect(caretCol(view)).toBe(0);
    type(view, MARK);
    record('empty_row_end_of_file', view.state.doc.toString());
  });

  test('Enter on a whitespace-only line leaves no trailing whitespace behind (nc5)', () => {
    const src = 'import robot\ndef main():\n    robot.home()\n    \n    robot.log("x")\nmain()\n';
    const { view } = mount(src);
    put(view, endOf(view, 4));
    press(view, 'Enter');
    expect(lineText(view, 4)).toBe('');
    expect(lineText(view, 5)).toBe('    ');
    type(view, MARK);
    record('whitespace_line_enter_strips', view.state.doc.toString());
  });

  test('an open completion list still takes Enter as „accept" (the completion keymap wins)', async () => {
    const { view } = mount('import robot\nrobot.ho');
    put(view, view.state.doc.length);
    act(() => { startCompletion(view); });
    for (let i = 0; i < 50 && completionStatus(view.state) !== 'active'; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((r) => { setTimeout(r, 20); }); });
    }
    expect(completionStatus(view.state)).toBe('active');
    // Past the completion's own interaction delay (75 ms), as a student is.
    await act(async () => { await new Promise((r) => { setTimeout(r, 120); }); });
    press(view, 'Enter');
    expect(view.state.doc.toString()).toBe('import robot\nrobot.home()');
  });
});

describe('a paste goes in exactly as copied (owner decision R4-O1)', () => {
  beforeEach(() => {
    forgetVormachenCopy();
    toast.error.mockClear();
  });

  test('a multi-line string keeps its inner indentation byte for byte (4-A E6)', () => {
    const src = 'import robot\ndef main():\n    robot.home()\n    \n';
    const { view } = mount(src);
    put(view, endOf(view, 4));
    const text = 'hilfe = """Zeile 1\n  eingerueckt\nZeile 3"""';
    paste(view, text);
    expect(view.state.doc.toString()).toBe(`import robot\ndef main():\n    robot.home()\n    ${text}\n`);
  });

  test('a snippet keeps its own structure (4-A E7, E7b)', () => {
    const src = 'import robot\ndef main():\n    robot.home()\n    \n';
    for (const text of ['robot.log("a")\n    robot.log("b")', '    for i in range(2):\n        robot.log("a")\n']) {
      const { view, unmount } = mount(src);
      put(view, endOf(view, 4));
      paste(view, text);
      expect(view.state.doc.toString()).toBe(`import robot\ndef main():\n    robot.home()\n    ${text}\n`);
      unmount();
    }
  });

  test('Vormachen’s own copied lines land like „Einfügen": below the cursor line, never joined', () => {
    const copied = rememberVormachenCopy('python', ['robot.move_to("Ablage")', MARK]);
    const src = 'import robot\n\ndef main():\n    robot.home()\n\n    robot.log(1)\n\nmain()\n';
    for (const [where, line] of [['end of a body line', 4], ['the empty row in the body', 5]]) {
      const { view, unmount } = mount(src);
      put(view, endOf(view, line));
      paste(view, copied);
      const rows = view.state.doc.toString().split('\n');
      expect([where, rows[line - 1], rows[line], rows[line + 1]])
        .toEqual([where, src.split('\n')[line - 1], '    robot.move_to("Ablage")', `    ${MARK}`]);
      // The caret at the end of the last pasted line.
      expect(view.state.doc.lineAt(view.state.selection.main.head).number).toBe(line + 2);
      expect(toast.error).not.toHaveBeenCalled();
      record(`vormachen_paste_${line}`, view.state.doc.toString());
      unmount();
    }
  });

  test('… as the Windows clipboard hands it back (CRLF) too', () => {
    rememberVormachenCopy('python', [MARK]);
    const { view } = mount('import robot\nif True:\n    robot.home()\n');
    put(view, endOf(view, 3));
    paste(view, `${MARK}\r\n`);
    expect(view.state.doc.toString()).toBe(`import robot\nif True:\n    robot.home()\n    ${MARK}\n`);
  });

  test('… and at a spot where a line cannot stand, nothing is pasted and the reason is said', () => {
    const copied = rememberVormachenCopy('python', [MARK]);
    const src = 'import robot\nimport sys\nrobot.home()\n';
    const { view } = mount(src);
    put(view, endOf(view, 1));
    paste(view, copied);
    expect(view.state.doc.toString()).toBe(src);
    expect(toast.error).toHaveBeenCalledWith(CODE_DE.INSERT_LEADING_BLOCK_HINT);
  });

  test('an edited copy, or one for the other language, is ordinary text: verbatim', () => {
    const copied = rememberVormachenCopy('python', ['robot.move_to("Ablage")', MARK]);
    const src = 'import robot\ndef main():\n    robot.home()\n    \n';
    const edited = copied.replace('Ablage', 'Kiste');
    const { view } = mount(src);
    put(view, endOf(view, 4));
    paste(view, edited);
    expect(view.state.doc.toString()).toBe(`import robot\ndef main():\n    robot.home()\n    ${edited}\n`);
    rememberVormachenCopy('java', ['Robot.home();']);
    const java = mount('class Main {}\n').view;
    put(java, 0);
    paste(java, copied);
    expect(java.state.doc.toString()).toBe(`${copied}class Main {}\n`);
  });
});

// Review round 5, MD1: the empty row after a file's single final line break
// is a row of its own. Enter there gives column 0 — and so do „Einfügen", a
// Vormachen paste and a drop on that row: the line runs ONCE after the last
// block (it used to land INSIDE it: three times in the loop, never in the
// def or the else).
const EOF_ROWS = {
  after_for: ['import robot\nfor i in range(3):\n    robot.home()\n', 4],
  after_def_body: ['import robot\ndef main():\n    robot.home()\n', 4],
  after_if_else: ['import robot\nif True:\n    robot.home()\nelse:\n    robot.log("x")\n', 6],
  after_while: ['import robot\nn = 0\nwhile n < 3:\n    n += 1\n', 5],
  after_try_except: ['import robot\ntry:\n    robot.home()\nexcept Exception:\n    pass\n', 6],
  crlf_after_for: ['import robot\r\nfor i in range(3):\r\n    robot.home()\r\n', 4],
  two_space_for: ['import robot\nfor i in range(3):\n  robot.home()\n', 4],
  tab_for: ['import robot\nfor i in range(3):\n\trobot.home()\n', 4],
};

describe('the empty row after the final line break (review round 5, MD1)', () => {
  beforeEach(() => {
    forgetVormachenCopy();
    toast.error.mockClear();
  });

  test.each(Object.entries(EOF_ROWS))('%s — Enter, „Einfügen", a Vormachen paste and a drop agree', (name, [src, line]) => {
    const flat = src.replace(/\r/g, '');
    const expected = `${flat}\n${MARK}`;
    // Enter, then typing.
    const enter = mount(src).view;
    expect(enter.state.doc.lines).toBe(line);
    put(enter, enter.state.doc.length);
    press(enter, 'Enter');
    expect(caretCol(enter)).toBe(0);
    type(enter, MARK);
    expect(enter.state.doc.toString()).toBe(expected);
    record(`eof_row_enter_${name}`, enter.state.doc.toString(), 'run', 1);
    // „Einfügen" with the cursor on that row.
    const target = insertionTargetAt(src, 'python', line);
    expect(target).toMatchObject({ mode: 'after', indent: '' });
    expect(insertAtTarget(src, target, [MARK], 'python').content.replace(/\r/g, '')).toBe(expected);
    // Vormachen's copy, pasted with the cursor on that row.
    const copied = rememberVormachenCopy('python', [MARK]);
    const pasted = mount(src).view;
    put(pasted, pasted.state.doc.length);
    paste(pasted, copied);
    expect(pasted.state.doc.toString()).toBe(expected);
    record(`eof_row_vormachen_paste_${name}`, pasted.state.doc.toString(), 'run', 1);
    // A Sammlung row dropped on that row.
    const dropped = mount(src).view;
    dropped.posAtCoords = () => dropped.state.doc.length;
    const data = { [SNIPPET_MIME]: JSON.stringify({ kind: 'recording', name: 'Winken' }) };
    fireEvent.drop(dropped.contentDOM, { dataTransfer: { types: Object.keys(data), getData: (t) => data[t] || '' } });
    expect(dropped.state.doc.toString()).toBe(expected);
    record(`eof_row_drop_${name}`, dropped.state.doc.toString(), 'run', 1);
    expect(toast.error).not.toHaveBeenCalled();
  });
});

describe('whitespace is a step back only when the next statement stands there (review round 5, md4)', () => {
  test('4 spaces between two 8-space statements: Enter continues the 8-space body', () => {
    const src = 'import robot\ndef main():\n    if True:\n        robot.home()\n    \n        robot.log(1)\nmain()\n';
    const { view } = mount(src);
    put(view, endOf(view, 5));
    press(view, 'Enter');
    expect(caretCol(view)).toBe(8);
    type(view, MARK);
    record('ws_row_shallower_than_next_enter', view.state.doc.toString(), 'run', 1);
    // „Einfügen" on that row agrees.
    expect(insertionTargetAt(src, 'python', 5)).toMatchObject({ mode: 'after', indent: '        ' });
  });

  test('Tab on the empty row before an `else:` goes where Enter does — the if-body', () => {
    const src = 'import robot\ndef main():\n    if True:\n        robot.home()\n\n    else:\n        robot.log(1)\nmain()\n';
    const { view } = mount(src);
    put(view, view.state.doc.line(5).from);
    press(view, 'Tab');
    expect(caretCol(view)).toBe(8);
    type(view, MARK);
    record('tab_empty_row_before_else', view.state.doc.toString(), 'run', 1);
    const second = mount(src).view;
    put(second, endOf(second, 4));
    press(second, 'Enter');
    expect(caretCol(second)).toBe(8);
  });
});

describe('a Sammlung row dropped where no line may stand (review round 4, mc9 — X2)', () => {
  test('writes nothing and says why, in German', () => {
    toast.error.mockClear();
    const src = 'import robot\nimport sys\nrobot.home()\n';
    const { view } = mount(src);
    view.posAtCoords = () => view.state.doc.line(1).from;
    const data = { [SNIPPET_MIME]: JSON.stringify({ kind: 'recording', name: 'Winken' }) };
    fireEvent.drop(view.contentDOM, { dataTransfer: { types: Object.keys(data), getData: (t) => data[t] || '' } });
    expect(view.state.doc.toString()).toBe(src);
    expect(toast.error).toHaveBeenCalledWith(CODE_DE.INSERT_LEADING_BLOCK_HINT);
  });
});

describe('Java: Enter continues the block’s sibling indentation (review round 3, 3-B N1)', () => {
  test('a 2-space loop body in a 4-space starter stays at its own column', () => {
    const src = 'import edubotics.Robot;\n\npublic class Main {\n    public static void main(String[] args) {\n        Robot.home();\n        for (int i = 0; i < 3; i++) {\n          Robot.log("x");\n        }\n    }\n}\n';
    const { view } = mount(src, 'java');
    put(view, endOf(view, 7));
    run(view, insertNewlineAndIndent);
    expect(caretCol(view)).toBe(10);
  });

  test('a closing brace typed on its own line still dedents (the editor’s own rule)', () => {
    const src = 'import edubotics.Robot;\npublic class Main {\n    public static void main(String[] args) {\n        for (int i = 0; i < 3; i++) {\n        }\n    }\n}\n';
    const { view } = mount(src, 'java');
    put(view, endOf(view, 4));
    run(view, insertNewlineAndIndent);
    expect(caretCol(view)).toBe(12);
    type(view, 'Robot.home();');
    run(view, insertNewlineAndIndent);
    type(view, '}');
    expect(lineText(view, 6)).toBe('        }');
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
    // … + review round 5: three per EOF row (Enter, a paste, a drop) and two md4 cases.
    expect(doc.cases.length).toBe(STYLES.length * 6 + 14 + Object.keys(EMPTY_ROWS).length + 5
      + Object.keys(EOF_ROWS).length * 3 + 2);
    if (REGEN) {
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, `${JSON.stringify(doc, null, 1)}\n`);
    }
    expect(doc).toEqual(JSON.parse(fs.readFileSync(FIXTURE, 'utf8')));
  });
});
