/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Code insertion: the steps a Vormachen round or a Sammlung row stands for,
// spelled in the program's language from robot_api.json — and put where the
// STUDENT chose (owner decision R3-O4): directly below the cursor's line,
// after this module CHECKED that a line can stand there and run. Without a
// cursor, and at a spot that fails the check, nothing is written and a short
// German reason says why; the line is never moved anywhere else. The program
// structure also decides the indentation, the same function the editor's
// Enter uses (review round 3, MB1). Every program these produce is also
// PARSED by CPython 3.12 and COMPILED by javac 21 (codeInsert.cases.test.js
// writes them to a fixture that robotis_ai_setup/tests/
// test_code_insert_cases.py compiles and runs).

import { describe, it, expect } from 'vitest';
import {
  CODE_INDENT_UNIT,
  SNIPPET_MIME,
  detectIndentUnit,
  fileIndentUnit,
  indentStepAt,
  insertAtTarget,
  insertionChange,
  insertionTarget,
  insertionTargetAt,
  minimalChange,
  newlineIndentAt,
  snippetLines,
  stepsToCode,
} from '../codeInsert';
import { STARTER_FILES } from '../codeProject';
import { CODE_DE, formatCode } from '../codeMessagesDe';

const STEPS = [
  { type: 'replay', name: 'Winken' },
  { type: 'move_to', name: 'Ablage' },
  { type: 'close_gripper' },
  { type: 'move_to', name: 'Kiste' },
  { type: 'open_gripper' },
];

// The lines inserted with the cursor on `line` (the refusal leaves the text).
const at = (content, line, lines, language = 'python') => insertAtTarget(
  content, insertionTargetAt(content, language, line), lines, language,
);
const hintAt = (content, line, language = 'python') => insertionTargetAt(content, language, line).hint;
// A line may stand below `line` and run there.
const allowedAt = (content, line, language = 'python') => (
  insertionTargetAt(content, language, line).mode === 'after'
);
const NEVER = (what) => formatCode(CODE_DE.INSERT_NEVER_RUNS_HINT, what);

describe('stepsToCode', () => {
  it('spells Python calls from robot_api.json', () => {
    expect(stepsToCode(STEPS, 'python')).toEqual([
      'robot.replay("Winken")',
      'robot.move_to("Ablage")',
      'robot.close_gripper()',
      'robot.move_to("Kiste")',
      'robot.open_gripper()',
    ]);
  });

  it('spells Java calls with the Java names and a semicolon', () => {
    expect(stepsToCode(STEPS, 'java')).toEqual([
      'Robot.replay("Winken");',
      'Robot.moveTo("Ablage");',
      'Robot.closeGripper();',
      'Robot.moveTo("Kiste");',
      'Robot.openGripper();',
    ]);
  });

  it('drops an unknown step and a name no code can carry', () => {
    expect(stepsToCode([{ type: 'fly' }, { type: 'replay', name: 'a"b' }, { type: 'replay' }], 'python'))
      .toEqual([]);
    expect(stepsToCode(null, 'python')).toEqual([]);
  });
});

describe('snippetLines — a Sammlung row as one line of code', () => {
  it('recording → replay, Ziel and Position → move_to, anything else → nothing', () => {
    expect(snippetLines({ kind: 'recording', name: 'Winken' }, 'python')).toEqual(['robot.replay("Winken")']);
    expect(snippetLines({ kind: 'pin', name: 'Ablage' }, 'java')).toEqual(['Robot.moveTo("Ablage");']);
    expect(snippetLines({ kind: 'pose', name: 'P1' }, 'python')).toEqual(['robot.move_to("P1")']);
    expect(snippetLines({ kind: 'variable', name: 'x' }, 'python')).toEqual([]);
    expect(snippetLines(null, 'python')).toEqual([]);
  });
});

describe('directly below the line the student chose (owner decision R3-O4)', () => {
  it('goes on the next line with the block’s own indentation', () => {
    const src = 'import robot\nfor i in range(3):\n    robot.home()\nprint(1)\n';
    const r = at(src, 3, ['robot.lift()', 'robot.beep()']);
    expect(r.content).toBe('import robot\nfor i in range(3):\n    robot.home()\n    robot.lift()\n    robot.beep()\nprint(1)\n');
    expect([r.firstLine, r.lastLine]).toEqual([4, 5]);
  });

  it('below a block opener: its body’s first line (Python `:`, Java `{`)', () => {
    expect(at('if x:\n    pass\n', 1, ['a()']).content).toBe('if x:\n    a()\n    pass\n');
    expect(at('if x:  # kommentar\n    pass\n', 1, ['a()']).content).toBe('if x:  # kommentar\n    a()\n    pass\n');
    const java = 'class Main {\n  static void main(String[] a) {\n    Robot.home();\n  }\n}\n';
    expect(at(java, 2, ['Robot.beep();'], 'java').content)
      .toBe('class Main {\n  static void main(String[] a) {\n    Robot.beep();\n    Robot.home();\n  }\n}\n');
  });

  it('a bodyless opener: its own indentation plus the unit of the block it sits in (MB1)', () => {
    // Top level: the file's unit (2 here); inside a 4-space block: 4.
    expect(at('def f():\n  return 1\nif x:\n', 3, ['a()']).content).toBe('def f():\n  return 1\nif x:\n  a()\n');
    expect(at('if x:\n', 1, ['a()']).content).toBe('if x:\n    a()\n');
    expect(at('def g():\n  pass\ndef f():\n    y = 1\n    if y:\n', 5, ['a()']).content)
      .toBe('def g():\n  pass\ndef f():\n    y = 1\n    if y:\n        a()\n');
  });

  it('a blank or comment line keeps its own level when a statement may take it', () => {
    expect(at('def f():\n    x = 1\n    \n    y = 2\nf()\n', 3, ['z()']).content)
      .toBe('def f():\n    x = 1\n    \n    z()\n    y = 2\nf()\n');
    expect(at('def f():\n    x = 1\n\nf()\n', 3, ['y()']).content).toBe('def f():\n    x = 1\n\ny()\nf()\n');
  });

  it('keeps tabs as tabs, clamps the line to the file, and needs no final newline', () => {
    expect(at('if x:\n\tpass\n', 2, ['a()']).content).toBe('if x:\n\tpass\n\ta()\n');
    expect(at('a\n', 99, ['b']).content).toBe('a\nb\n');
    expect(at('a', 1, ['b']).content).toBe('a\nb');
    expect(at('', 1, ['b']).content).toBe('b\n');
  });

  it('a refusal writes nothing — never a line moved somewhere else', () => {
    const src = 'import robot\ndef f():\n    robot.home()\n    return 1\nf()\n';
    const target = insertionTargetAt(src, 'python', 4);
    expect(target).toEqual({ notFound: true, hint: NEVER('„return“') });
    expect(insertAtTarget(src, target, ['x()'], 'python')).toEqual({ content: src, firstLine: 0, lastLine: -1 });
  });
});

describe('the spots a line may not take, each with its reason', () => {
  it('above or inside the file’s leading block (mb5): the starter’s comments, a docstring, __future__', () => {
    const starter = STARTER_FILES.python['main.py'];
    expect(hintAt(starter, 1)).toBe(CODE_DE.INSERT_LEADING_BLOCK_HINT);
    expect(hintAt(starter, 2)).toBe(CODE_DE.INSERT_LEADING_BLOCK_HINT);
    expect(at(starter, 3, ['robot.beep()']).content.split('\n')[3]).toBe('robot.beep()');
    expect(hintAt('"""Doku."""\nfrom __future__ import annotations\nimport robot\n', 1))
      .toBe(CODE_DE.INSERT_LEADING_BLOCK_HINT);
    expect(hintAt('import robot\nimport sys\nrobot.home()\n', 1)).toBe(CODE_DE.INSERT_LEADING_BLOCK_HINT);
  });

  it('right above a def’s or class’s docstring, right below a decorator', () => {
    expect(hintAt('def f():\n    """Doku."""\n    return 1\n', 1)).toBe(CODE_DE.INSERT_DOCSTRING_HINT);
    expect(hintAt('class A:\n    """Doku."""\n', 1)).toBe(CODE_DE.INSERT_DOCSTRING_HINT);
    expect(hintAt('import functools\n@functools.cache\ndef f():\n    return 1\n', 2)).toBe(CODE_DE.INSERT_DECORATOR_HINT);
  });

  it('a match/case line (mb4) and a switch/case line (mb3)', () => {
    const py = 'x = 1\nmatch x:\n    case 1:\n        pass\n';
    expect(hintAt(py, 2)).toBe(CODE_DE.INSERT_MATCH_HINT);
    expect(hintAt(py, 3)).toBe(CODE_DE.INSERT_MATCH_HINT);
    const java = (body) => `class Main {\n  static void main(String[] a) {\n    int x = 1;\n${body}  }\n}\n`;
    const colon = java('    switch (x) {\n      case 1:\n        Robot.home();\n        break;\n      default:\n        Robot.beep();\n    }\n');
    for (const line of [4, 5, 8]) expect(hintAt(colon, line, 'java')).toBe(CODE_DE.INSERT_SWITCH_HINT);
    // A statement line inside a case group is a spot like any other.
    expect(at(colon, 6, ['Robot.log("x");'], 'java').content.split('\n')[6]).toBe('        Robot.log("x");');
    const arrow = java('    switch (x) {\n      case 1 -> Robot.home();\n      default -> Robot.beep();\n    }\n');
    for (const line of [4, 5, 6]) expect(hintAt(arrow, line, 'java')).toBe(CODE_DE.INSERT_SWITCH_HINT);
  });

  it('inside a multi-line expression, a `\\` continuation or a string', () => {
    for (const [src, line] of [['d = {\n    "a": 1,\n}\n', 1], ['x = 1 + \\\n    2\n', 1],
      ['def f():\n    """Doc\n    mehr"""\n    return 1\n', 2], ['if (a and\n        b):\n    c()\n', 1]]) {
      expect(hintAt(src, line)).toBe(CODE_DE.INSERT_INSIDE_EXPRESSION_HINT);
    }
    const java = 'class Main {\n  static void main(String[] a) {\n    Robot.log(\n      "x");\n  }\n}\n';
    expect(hintAt(java, 3, 'java')).toBe(CODE_DE.INSERT_INSIDE_EXPRESSION_HINT);
    // Its LAST line is a spot: the line goes below the whole statement.
    expect(at(java, 4, ['Robot.beep();'], 'java').content.split('\n')[4]).toBe('    Robot.beep();');
  });

  it('Java outside a method body: an import, class, field or closing-brace line — and a local or anonymous class member line (MB2g)', () => {
    const src = 'import edubotics.Robot;\n\npublic class Main {\n  static int x = 1;\n  public static void main(String[] a) {\n  }\n}\n';
    for (const line of [1, 3, 4, 6, 7]) expect(hintAt(src, line, 'java')).toBe(CODE_DE.NOT_IN_METHOD_HINT);
    const local = 'class Main {\n  static void main(String[] a) {\n    class Hilfe {\n      int x = 1;\n      void f() { }\n    }\n    new Hilfe().f();\n  }\n}\n';
    expect(hintAt(local, 4, 'java')).toBe(CODE_DE.NOT_IN_METHOD_HINT);
    const anon = 'class Main {\n  static void main(String[] a) {\n    Runnable r = new Runnable() {\n      int x = 1;\n      public void run() { }\n    };\n  }\n}\n';
    expect(hintAt(anon, 4, 'java')).toBe(CODE_DE.NOT_IN_METHOD_HINT);
  });

  it('nb1: a generic anonymous class is a class body, and „unclosed" is said only when something is', () => {
    const generic = 'class Main {\n  static void main(String[] a) {\n    java.util.Comparator<Integer> c = new java.util.Comparator<Integer>() {\n      int z = 0;\n      public int compare(Integer x, Integer y) { return x - y; }\n    };\n  }\n}\n';
    expect(hintAt(generic, 4, 'java')).toBe(CODE_DE.NOT_IN_METHOD_HINT);
    expect(hintAt('class Main {\n  static void main(String[] a) {\n    Robot.home();\n', 3, 'java'))
      .toBe(CODE_DE.NO_SAFE_PLACE_HINT);
    expect(hintAt('x = (\n', 1)).toBe(CODE_DE.NO_SAFE_PLACE_HINT);
    expect(hintAt('import robot\n"""\noffen\n', 1)).toBe(CODE_DE.NO_SAFE_PLACE_HINT);
    expect(CODE_DE.NO_SAFE_PLACE_HINT).toMatch(/Klammer oder ein Text/);
    expect(CODE_DE.INSERT_UNREADABLE_HINT).not.toMatch(/Klammer/);
  });

  it('a spot that can never run: after return/raise/break/continue/exit (mb6)', () => {
    expect(hintAt('def f():\n    a()\n    return 1\n', 3)).toBe(NEVER('„return“'));
    expect(hintAt('for i in y:\n    break\n', 2)).toBe(NEVER('„break“'));
    expect(hintAt('for i in y:\n    continue\n', 2)).toBe(NEVER('„continue“'));
    expect(hintAt('def f():\n    raise ValueError(1)\n', 2)).toBe(NEVER('„raise“'));
    for (const call of ['sys.exit(main())', 'exit()', 'quit()', 'os._exit(0)']) {
      expect(hintAt(`import sys, os\n${call}\n`, 2)).toBe(NEVER('ein Programmende (exit)'));
    }
    const java = (s) => `class Main {\n  static void main(String[] a) {\n    ${s}\n  }\n}\n`;
    expect(hintAt(java('return;'), 3, 'java')).toBe(NEVER('„return“'));
    expect(hintAt(java('throw new RuntimeException();'), 3, 'java')).toBe(NEVER('„throw“'));
    expect(hintAt(java('System.exit(0);'), 3, 'java')).toBe(NEVER('ein Programmende (exit)'));
  });

  it('after an endless loop — a provably constant condition, no break out (mb6, mb7, mb2)', () => {
    for (const cond of ['True', '1', 'not False', '1 == 1', '(True)', '2 > 1 and True']) {
      expect(hintAt(`import robot\nwhile ${cond}:\n    robot.home()\n\n`, 4)).toBe(NEVER('eine Endlosschleife'));
    }
    // A break out of it, or a condition that can change: the loop ends.
    expect(at('while True:\n    break\n\n', 3, ['x()']).content).toBe('while True:\n    break\n\nx()\n');
    expect(at('n = 0\nwhile n < 3:\n    n += 1\n\n', 4, ['x()']).content).toBe('n = 0\nwhile n < 3:\n    n += 1\n\nx()\n');
    const java = (body, extra = '') => `interface K {\n  boolean LAUF = true;\n}\nclass Main implements K {\n${extra}  static void main(String[] a) {\n${body}  }\n}\n`;
    const after = (body, extra) => {
      const src = java(body, extra);
      const rows = src.split('\n');
      return insertionTargetAt(src, 'java', rows.lastIndexOf('    }') + 1);
    };
    expect(after('    while (LAUF) {\n    }\n').hint).toBe(NEVER('eine Endlosschleife'));
    expect(after('    while (MAX > 0) {\n    }\n', '  static final int MAX = 3;\n').hint).toBe(NEVER('eine Endlosschleife'));
    expect(after('    for (int i = 0; i < MAX; i++) {\n    }\n', '  static final int MAX = 3;\n').notFound).toBeUndefined();
    expect(after('    for (int i = 0; i < a.length; i++) {\n    }\n').notFound).toBeUndefined();
    expect(after('    int n = 0;\n    while (n < MAX) {\n      n++;\n    }\n', '  static final int MAX = 3;\n').notFound).toBeUndefined();
    expect(after('    while (Konstanten.LAUF) {\n    }\n').hint).toBe(CODE_DE.INSERT_UNSURE_HINT);
  });

  it('after an if/else or try whose every way out leaves (mb6)', () => {
    expect(hintAt('def f():\n    if a:\n        return\n    else:\n        return\n    # danach\n', 6))
      .toBe(NEVER('ein if/else, das in jedem Zweig endet'));
    expect(hintAt('def f():\n    try:\n        a()\n    finally:\n        return\n    # danach\n', 6))
      .toBe(NEVER('ein try, das in jedem Zweig endet'));
    // Inside the else-body, before its return, the line runs.
    expect(at('def f():\n    if a:\n        return\n    else:\n        b()\n        return\n', 5, ['x()']).content)
      .toBe('def f():\n    if a:\n        return\n    else:\n        b()\n        x()\n        return\n');
  });

  it('no line between a block and its else/elif/except/finally, none where the indentation cannot fit', () => {
    // Explicit whitespace at the clause's own level is a deliberate step
    // back: a line there would cut the compound in two.
    expect(hintAt('def f():\n    if a:\n        b()\n    \n    else:\n        c()\n', 4))
      .toBe(CODE_DE.INSERT_CLAUSE_HINT);
    // Explicit whitespace shallower than the next statement of its block.
    expect(hintAt('def f():\n    for i in x:\n        a()\n    \n        b()\n', 4))
      .toBe(CODE_DE.INSERT_INDENT_HINT);
    expect(hintAt('if True:\n    a()\n  b()\n', 2)).toBe(CODE_DE.INSERT_INDENT_BROKEN_HINT);
  });

  it('an EMPTY row chose nothing: it takes the level the next statement needs (review round 4, MC1)', () => {
    // Between two statements of a body: the body (this used to be column 0,
    // and refused as „Einrückung passt nicht").
    expect(at('def f():\n    a()\n\n    b()\n', 3, ['x()']).content)
      .toBe('def f():\n    a()\n\n    x()\n    b()\n');
    // A comment at column 0 inside the body chose nothing either.
    expect(at('def f():\n    a()\n# Notiz\n    b()\n', 3, ['x()']).content)
      .toBe('def f():\n    a()\n# Notiz\n    x()\n    b()\n');
    // In front of an else: the block it closes.
    expect(at('if a:\n    b()\n\nelse:\n    c()\n', 3, ['x()']).content)
      .toBe('if a:\n    b()\n\n    x()\nelse:\n    c()\n');
    // Between a body's end and the next statement further out: that one's level.
    expect(at('def f():\n    for i in y:\n        a()\n\n    b()\n', 4, ['x()']).content)
      .toBe('def f():\n    for i in y:\n        a()\n\n    x()\n    b()\n');
    // At the end of the file nothing follows: column 0, after the loop.
    expect(at('while n < 3:\n    n += 1\n\n', 3, ['x()']).content).toBe('while n < 3:\n    n += 1\n\nx()\n');
    // Explicit whitespace stays the student's choice.
    expect(at('def f():\n    for i in y:\n        a()\n    \n    b()\n', 4, ['x()']).content)
      .toBe('def f():\n    for i in y:\n        a()\n    \n    x()\n    b()\n');
    // Java already did this (E12): the sibling level.
    const java = 'class Main {\n  static void main(String[] a) {\n    a();\n\n    b();\n  }\n}\n';
    expect(insertionTargetAt(java, 'java', 4)).toMatchObject({ mode: 'after', indent: '    ' });
  });

  it('a try without a handler whose body never completes (review round 4, mc4)', () => {
    const TRY = NEVER('ein try, das in jedem Zweig endet');
    expect(hintAt('def f():\n    try:\n        return 1\n    finally:\n        pass\n    # danach\n', 6)).toBe(TRY);
    expect(hintAt('try:\n    while True:\n        a()\nfinally:\n    b()\n\n', 6)).toBe(TRY);
    // A handler may catch what the body raises: the line after it runs.
    expect(allowedAt('try:\n    while True:\n        a()\nexcept Exception:\n    pass\n\n', 6)).toBe(true);
    // A body that completes: the finally, then on.
    expect(allowedAt('try:\n    a()\nfinally:\n    b()\n\n', 5)).toBe(true);
  });

  it('the remaining never-runs cases (review round 4, mc6)', () => {
    expect(hintAt('def f():\n    if True:\n        return 1\n    # danach\n', 4))
      .toBe(NEVER('ein if, das immer genommen wird und endet'));
    expect(hintAt('def f():\n    if 0:\n        pass\n    elif 1 == 1:\n        return 2\n    # danach\n', 6))
      .toBe(NEVER('ein if, das immer genommen wird und endet'));
    // A constant FALSE branch is never taken: an else that returns ends it.
    expect(hintAt('def f():\n    if False:\n        pass\n    else:\n        return 2\n    # danach\n', 6))
      .toBe(NEVER('ein if/else, das in jedem Zweig endet'));
    expect(allowedAt('def f():\n    if False:\n        return 1\n    # danach\n', 4)).toBe(true);
    expect(hintAt('def f(x):\n    match x:\n        case 1:\n            return 1\n        case _:\n            return 2\n    # danach\n', 7))
      .toBe(NEVER('ein match, das in jedem Fall endet'));
    // Without a case that matches anything, a match may run no case at all.
    expect(allowedAt('def f(x):\n    match x:\n        case 1:\n            return 1\n    # danach\n', 5)).toBe(true);
    for (const cond of ['"x"', "'nicht leer'", '"a" == "a"', 'not ""']) {
      expect(hintAt(`import robot\nwhile ${cond}:\n    robot.home()\n\n`, 4)).toBe(NEVER('eine Endlosschleife'));
    }
    for (const cond of ['""', '"a" == "b"', 'f"{x}"']) {
      expect(allowedAt(`import robot\nwhile ${cond}:\n    robot.home()\n\n`, 4)).toBe(true);
    }
    expect(hintAt('import os\nos.abort()\n', 2)).toBe(NEVER('ein Programmende (exit)'));
    // `with suppress(…)` may swallow what its body raises: the line after it runs …
    expect(allowedAt('import contextlib\nwith contextlib.suppress(ValueError):\n    raise ValueError()\nx = 1\n', 4))
      .toBe(true);
    // … inside it, after the raise, it never does; nor after any other with.
    expect(hintAt('import contextlib\nwith contextlib.suppress(ValueError):\n    raise ValueError()\nx = 1\n', 3))
      .toBe(NEVER('„raise“'));
    expect(hintAt('import contextlib\nwith contextlib.nullcontext():\n    raise ValueError()\nx = 1\n', 4))
      .toBe(NEVER('„raise“'));
    const java = (s) => `class Main {\n  static void main(String[] a) {\n    ${s}\n  }\n}\n`;
    for (const call of ['Runtime.getRuntime().halt(0);', 'Runtime.getRuntime().exit(0);', 'java.lang.Runtime.getRuntime().halt(1);']) {
      expect(hintAt(java(call), 3, 'java')).toBe(NEVER('ein Programmende (exit)'));
    }
    // A switch opened and closed on one row is a whole statement.
    for (const row of ['int y = switch (a.length) { case 1 -> 2; default -> 3; };',
      'switch (a.length) { case 1 -> b(); default -> c(); }',
      'switch (a.length) { case 1: b(); break; default: break; }']) {
      expect(insertionTargetAt(java(row), 'java', 3)).toMatchObject({ mode: 'after', indent: '    ' });
    }
    expect(hintAt(java('int y = switch (a.length) {\n      default -> 3;\n    };'), 3, 'java')).toBe(CODE_DE.INSERT_SWITCH_HINT);
  });

  it('a Java keyword is never a declaration: `return LAUF;`, `case STUFE:` (review round 4, mc5)', () => {
    const main = (extra, cond) => `class Main implements K {\n${extra}  public static void main(String[] a) {\n    while (${cond}) {\n    }\n  }\n}\n`;
    const rows = (src) => src.split('\n').lastIndexOf('    }') + 1;
    for (const [extra, cond] of [
      ['  static boolean f() { return LAUF; }\n', 'LAUF'],
      ['  static int g(int x) { switch (x) { case STUFE: return 1; default: return 0; } }\n', 'STUFE > 0'],
      ['  static boolean h(Object x) { return (x instanceof LAUF); }\n', 'LAUF'],
      ['  static boolean k() { throw LAUF; }\n', 'LAUF'],
    ]) {
      const src = main(extra, cond);
      expect(insertionTargetAt(src, 'java', rows(src)).hint).toBe(CODE_DE.INSERT_UNSURE_HINT);
    }
    // A real declaration still counts — a primitive, var, a class type, modifiers.
    for (const decl of ['  static boolean LAUF = false;\n', '  static final Boolean LAUF = f();\n',
      '  private static volatile boolean LAUF = true;\n']) {
      const src = main(decl, 'LAUF');
      expect(insertionTargetAt(src, 'java', rows(src)).notFound).toBeUndefined();
    }
  });
});

describe('insertionTarget — the cursor, or nothing', () => {
  const files = { 'main.py': 'import robot\nrobot.home()\n', 'hilfe.py': 'def f():\n    pass\n' };

  it('uses the last cursor when its file and line exist', () => {
    const t = insertionTarget(files, 'python', { file: 'hilfe.py', line: 2 });
    expect(t).toMatchObject({ file: 'hilfe.py', mode: 'after', indent: '    ' });
    expect(insertAtTarget(files['hilfe.py'], t, ['x()'], 'python').content).toBe('def f():\n    pass\n    x()\n');
  });

  it('without a known cursor NOTHING is placed — the student is asked to click first', () => {
    for (const cursor of [null, { file: 'weg.py', line: 1 }, { file: 'main.py', line: 99 }]) {
      expect(insertionTarget(files, 'python', cursor))
        .toEqual({ notFound: true, noCursor: true, hint: CODE_DE.CLICK_FIRST_HINT });
    }
    expect(CODE_DE.CLICK_FIRST_HINT).toBe('Klicke zuerst in deinen Code, wo die Zeile hin soll.');
  });

  it('a cursor on a class line is no place for a statement', () => {
    expect(insertionTarget({ 'Main.java': 'class A {}\n' }, 'java', { file: 'Main.java', line: 1 }))
      .toEqual({ file: 'Main.java', notFound: true, hint: CODE_DE.NOT_IN_METHOD_HINT });
  });
});

describe('insertionChange — the same insertion as ONE editor change', () => {
  it('yields exactly insertAtTarget’s content when applied', () => {
    const cases = [
      ['import robot\nfor i in range(3):\n    robot.home()\nprint(1)\n', 3, 'python'],
      ['if x:\n    pass\n', 1, 'python'],
      ['a\n', 99, 'python'],
      ['a', 1, 'python'],
      ['', 1, 'python'],
      ['class M {\n  void f() {\n  }\n}\n', 2, 'java'],
      ['class M {\r\n  static void main(String[] a) {\r\n    Robot.home();\r\n  }\r\n}\r\n', 3, 'java'],
    ];
    for (const [content, line, language] of cases) {
      const target = insertionTargetAt(content, language, line);
      expect(target.notFound).toBeUndefined();
      const res = insertionChange(content, target, ['x()', 'y()'], language);
      const { from, to, insert } = res.change;
      expect(content.slice(0, from) + insert + content.slice(to)).toBe(res.content);
      const ref = insertAtTarget(content, target, ['x()', 'y()'], language);
      expect([res.firstLine, res.lastLine, res.content]).toEqual([ref.firstLine, ref.lastLine, ref.content]);
    }
  });
});

describe('minimalChange — an external edit as the smallest replacement', () => {
  it('keeps the common prefix and suffix out of the change', () => {
    expect(minimalChange('robot.move_to("Ablage")\nx = 1\n', 'robot.move_to("Tisch")\nx = 1\n'))
      .toEqual({ from: 15, to: 21, insert: 'Tisch' });
    expect(minimalChange('abc', 'abc')).toBeNull();
    expect(minimalChange('', 'x')).toEqual({ from: 0, to: 0, insert: 'x' });
    expect(minimalChange('aaa', 'aa')).toEqual({ from: 2, to: 3, insert: '' });
    expect(minimalChange('abcabc', 'abc')).toEqual({ from: 3, to: 6, insert: '' });
  });

  it('always rebuilds the target text', () => {
    const pairs = [['hallo welt', 'hallo schöne welt'], ['xyz', 'abc'], ['aXa', 'aYa'], ['a\nb\nc', 'a\nb\nb\nc']];
    for (const [a, b] of pairs) {
      const c = minimalChange(a, b);
      expect(a.slice(0, c.from) + c.insert + a.slice(c.to)).toBe(b);
    }
  });
});

describe('SNIPPET_MIME', () => {
  it('is the one drag type the drawer sets and the editor reads', () => {
    expect(SNIPPET_MIME).toBe('application/x-edubotics-snippet');
  });
});

describe('the file’s indentation unit: only statement starts vote (review round 3, MB1)', () => {
  it('a file with no indented block uses 4 spaces (PEP 8, the Java starter)', () => {
    expect(CODE_INDENT_UNIT).toBe('    ');
    expect(fileIndentUnit('a()\nb()\n', 'python')).toBe('    ');
    expect(fileIndentUnit('', 'python')).toBe('    ');
    expect(STARTER_FILES.java['Main.java']).toContain('\n    public static void main');
    expect(fileIndentUnit(STARTER_FILES.java['Main.java'], 'java')).toBe('    ');
  });

  it('the step from an opener to its body’s first statement; continuation lines never vote', () => {
    expect(detectIndentUnit('if a:\n  if b:\n    c()\n')).toBe('  ');
    expect(detectIndentUnit('if a:\n\tb()\n')).toBe('\t');
    expect(detectIndentUnit('a()\nb()\n')).toBeNull();
    expect(detectIndentUnit('x = """\n      text\n"""\n', 'python')).toBeNull();
    expect(detectIndentUnit('x = [1,\n     2]\ny = (3 +\n     4)\nz = 1 + \\\n     5\n', 'python')).toBeNull();
    expect(detectIndentUnit('a = [[1],\n     [2]]\nb = [[1],\n     [2]]\nif a:\n  b()\n', 'python')).toBe('  ');
    expect(detectIndentUnit('int[] a = {\n        1,\n};\nvoid f() {\n  g();\n}\n', 'java')).toBe('  ');
  });

  it('ties go to the smaller step', () => {
    expect(detectIndentUnit('def f():\n    a()\nif b:\n  c()\n')).toBe('  ');
  });
});

describe('newlineIndentAt — what Enter writes (review round 3, MB1)', () => {
  const end = (src, line) => src.split('\n').slice(0, line).join('\n').length;
  it('Python: a block’s own sibling indentation, whatever unit the file votes for', () => {
    const src = 'def f():\n    a()\nif b:\n  c()\nif d:\n  e()\n';
    expect(detectIndentUnit(src)).toBe('  ');
    expect(newlineIndentAt(src, end(src, 2), 'python')).toBe('    ');
    expect(newlineIndentAt(src, end(src, 4), 'python')).toBe('  ');
  });

  it('Python: after an opener its existing body, else the opener’s block unit', () => {
    expect(newlineIndentAt('for i in x:\n   a()\n', end('for i in x:\n   a()\n', 1), 'python')).toBe('   ');
    const nested = 'def f():\n    if a:\n';
    expect(newlineIndentAt(nested, nested.length - 1, 'python')).toBe('        ');
  });

  it('Python: the editor’s own rule inside brackets, strings, after `\\`, before a clause word', () => {
    expect(newlineIndentAt('x = (1,\n', 7, 'python')).toBeNull();
    expect(newlineIndentAt('x = """a\n', 7, 'python')).toBeNull();
    expect(newlineIndentAt('x = 1 + \\\n', 9, 'python')).toBeNull();
    expect(newlineIndentAt('if a:\n    b()\nelse:\n', 14, 'python')).toBeNull();
  });

  it('Python: in front of a statement the statement keeps its own indentation', () => {
    const src = 'def f():\n    a()\n    b()\n';
    expect(newlineIndentAt(src, src.indexOf('    b()'), 'python')).toBe('    ');
  });

  it('Java: a block’s sibling indentation (3-B N1), a closing brace left to the editor', () => {
    const src = 'class M {\n    void f() {\n        for (;;) {\n          g();\n        }\n    }\n}\n';
    const pos = src.indexOf('g();') + 'g();'.length;
    expect(newlineIndentAt(src, pos, 'java')).toBe('          ');
    expect(newlineIndentAt(src, src.indexOf('        }'), 'java')).toBeNull();
  });
});

describe('indentStepAt — Python Tab / Backspace go level to level (review round 3, MB1)', () => {
  const src = 'import robot\nif True:\n  robot.home()\n  for i in range(1):\n      robot.log("a")\n      \n';
  it('Backspace from the inner level lands on the outer one, never between', () => {
    expect(indentStepAt(src, 'python', 6, -1)).toBe('  ');
    expect(indentStepAt('if a:\n  b()\n  \n', 'python', 3, -1)).toBe('');
  });

  it('Tab goes to the next level, a new body gets its block’s unit', () => {
    expect(indentStepAt('def f():\n    a()\n\n', 'python', 3, 1)).toBe('    ');
    expect(indentStepAt('def f():\n    if a:\n    b()\n', 'python', 3, 1)).toBe('        ');
    expect(indentStepAt('x = 1\n', 'java', 1, 1)).toBeNull();
  });
});

describe('a CRLF file gets CRLF line breaks, and every rule reads CRLF (mb8)', () => {
  it('below a cursor, in both languages', () => {
    expect(at('if x:\r\n    a()\r\n', 1, ['b()']).content).toBe('if x:\r\n    b()\r\n    a()\r\n');
    const java = 'class Main {\r\n  static void main(String[] a) {\r\n    Robot.home();\r\n  }\r\n}\r\n';
    expect(at(java, 3, ['X();'], 'java').content)
      .toBe('class Main {\r\n  static void main(String[] a) {\r\n    Robot.home();\r\n    X();\r\n  }\r\n}\r\n');
  });

  it('a trailing comment, a guard, a return and a decorator read the same with CRLF', () => {
    const guard = 'import robot\r\ndef main():\r\n    robot.home()\r\n\r\nif __name__ == "__main__":  # Start\r\n    main()\r\n';
    expect(at(guard, 5, ['x()']).content.split('\r\n')[5]).toBe('    x()');
    expect(hintAt('def f():\r\n    return 1  # fertig\r\n', 2)).toBe(NEVER('„return“'));
    expect(hintAt('import functools\r\n@functools.cache\r\ndef f():\r\n    return 1\r\n', 2))
      .toBe(CODE_DE.INSERT_DECORATOR_HINT);
    const java = 'class Main {\r\n  static void main(String[] a) {\r\n    Robot.home(); // Start\r\n  }\r\n}\r\n';
    expect(at(java, 3, ['X();'], 'java').content.split('\r\n')[3]).toBe('    X();');
  });
});
