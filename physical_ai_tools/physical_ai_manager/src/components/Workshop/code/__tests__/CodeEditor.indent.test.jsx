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
import { render, act } from '@testing-library/react';
import CodeEditor from '../CodeEditor';
import {
  CODE_INDENT_UNIT, detectIndentUnit, insertAtTarget, insertionTargetAt,
} from '../codeInsert';

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
const paste = (view, text) => act(() => {
  view.dispatch(view.state.replaceSelection(text), { userEvent: 'input.paste' });
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

  test('a flat multi-line paste onto an indented empty line lines up (Vormachen’s clipboard)', () => {
    const { view } = mount('import robot\ndef f():\n    robot.home()\nf()\n');
    put(view, endOf(view, 3));
    press(view, 'Enter');
    paste(view, `robot.move_to("A")\nrobot.close_gripper()\n${MARK}`);
    expect([lineText(view, 4), lineText(view, 5), lineText(view, 6)])
      .toEqual(['    robot.move_to("A")', '    robot.close_gripper()', `    ${MARK}`]);
    record('flat_paste_lines_up', view.state.doc.toString());
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
    expect(doc.cases.length).toBe(STYLES.length * 6 + 15);
    if (REGEN) {
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, `${JSON.stringify(doc, null, 1)}\n`);
    }
    expect(doc).toEqual(JSON.parse(fs.readFileSync(FIXTURE, 'utf8')));
  });
});
