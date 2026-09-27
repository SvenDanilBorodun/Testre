/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Code insertion (owner decision O6): the steps a Vormachen round or a Sammlung
// row stands for, spelled in the program's language from robot_api.json, and
// placed by the program's STRUCTURE — the statement on the cursor's line, else
// the end of the body that runs last (review round 2, mi2/mi3). Every program
// these produce is also PARSED by CPython 3.12 and COMPILED by javac 21
// (codeInsert.cases.test.js writes them to a fixture that
// robotis_ai_setup/tests/test_code_insert_cases.py compiles and runs).

import { describe, it, expect } from 'vitest';
import {
  CODE_INDENT_UNIT,
  SNIPPET_MIME,
  detectIndentUnit,
  fileIndentUnit,
  insertAtTarget,
  insertionChange,
  insertionTarget,
  insertionTargetAt,
  mainTarget,
  minimalChange,
  snippetLines,
  stepsToCode,
} from '../codeInsert';
import { STARTER_FILES } from '../codeProject';
import { CODE_DE } from '../codeMessagesDe';

const STEPS = [
  { type: 'replay', name: 'Winken' },
  { type: 'move_to', name: 'Ablage' },
  { type: 'close_gripper' },
  { type: 'move_to', name: 'Kiste' },
  { type: 'open_gripper' },
];

// The lines inserted with the cursor on `line`, and at the end of main.
const at = (content, line, lines, language = 'python') => insertAtTarget(
  content, insertionTargetAt(content, language, line), lines, language,
);
const atMain = (content, lines, language = 'python') => insertAtTarget(
  content, mainTarget(content, language), lines, language,
);

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

describe('the cursor’s statement decides', () => {
  it('inserts below the anchor line with its indentation', () => {
    const src = 'import robot\nfor i in range(3):\n    robot.home()\nprint(1)\n';
    const r = at(src, 3, ['robot.lift()', 'robot.beep()']);
    expect(r.content).toBe('import robot\nfor i in range(3):\n    robot.home()\n    robot.lift()\n    robot.beep()\nprint(1)\n');
    expect([r.firstLine, r.lastLine]).toEqual([4, 5]);
  });

  it('adds one level after a line opening a block (Python `:`, Java `{`)', () => {
    expect(at('if x:\n    pass\n', 1, ['a()']).content).toBe('if x:\n    a()\n    pass\n');
    expect(at('if x:  # kommentar\n', 1, ['a()']).content).toBe('if x:  # kommentar\n    a()\n');
    // No body yet: the anchor's own indentation plus the FILE's unit (2 here).
    expect(at('  void f() {\n  }\n', 1, ['a();'], 'java').content)
      .toBe('  void f() {\n    a();\n  }\n');
  });

  it('takes an empty anchor line’s indentation from the statement above it', () => {
    expect(at('def f():\n    x = 1\n\n', 3, ['y()']).content).toBe('def f():\n    x = 1\n\n    y()\n');
  });

  it('keeps tabs as tabs and clamps the line to the file', () => {
    expect(at('if x:\n\tpass\n', 2, ['a()']).content).toBe('if x:\n\tpass\n\ta()\n');
    expect(at('a\n', 99, ['b']).content).toBe('a\nb\n');
    expect(at('a', 1, ['b']).content).toBe('a\nb');
  });

  it('a statement that spans lines is moved past as a whole (mi3)', () => {
    // Inside brackets, a `\` continuation, a docstring: the statement's end.
    expect(at('d = {\n    "a": 1,\n}\n', 1, ['b()']).content).toBe('d = {\n    "a": 1,\n}\nb()\n');
    expect(at('x = 1 + \\\n    2\n', 1, ['b()']).content).toBe('x = 1 + \\\n    2\nb()\n');
    expect(at('def f():\n    """Doc\n    mehr"""\n    return 1\n', 2, ['b()']).content)
      .toBe('def f():\n    """Doc\n    mehr"""\n    b()\n    return 1\n');
    // A multi-line header ends in `:` — its body gets the lines.
    expect(at('if (a and\n        b):\n    c()\n', 1, ['x()']).content)
      .toBe('if (a and\n        b):\n    x()\n    c()\n');
    // A decorator belongs to its def.
    expect(at('@cache\ndef f():\n    return 1\n', 1, ['x()']).content)
      .toBe('@cache\ndef f():\n    x()\n    return 1\n');
  });

  it('never below a statement that lets no next line run: before it (mi3)', () => {
    expect(at('def f():\n    a()\n    return 1\n', 3, ['x()']).content)
      .toBe('def f():\n    a()\n    x()\n    return 1\n');
    expect(at('for i in y:\n    break\n', 2, ['x()']).content).toBe('for i in y:\n    x()\n    break\n');
    expect(at('while True: pass\n', 1, ['x()']).content).toBe('x()\nwhile True: pass\n');
    const java = 'class Main {\n  static void main(String[] a) {\n    Robot.home();\n    return;\n  }\n}\n';
    expect(at(java, 4, ['Robot.beep();'], 'java').content)
      .toBe('class Main {\n  static void main(String[] a) {\n    Robot.home();\n    Robot.beep();\n    return;\n  }\n}\n');
  });

  it('Java: a cursor in a call’s arguments moves to the statement’s end; on a closing brace, into the block', () => {
    const src = 'class Main {\n  static void main(String[] a) {\n    Robot.log(\n      "x");\n  }\n}\n';
    expect(at(src, 3, ['Robot.beep();'], 'java').content)
      .toBe('class Main {\n  static void main(String[] a) {\n    Robot.log(\n      "x");\n    Robot.beep();\n  }\n}\n');
    expect(at(src, 5, ['Robot.beep();'], 'java').content)
      .toBe('class Main {\n  static void main(String[] a) {\n    Robot.log(\n      "x");\n    Robot.beep();\n  }\n}\n');
  });

  it('Java: no statement outside a method — an import, class or field line is a German hint (mi3)', () => {
    const src = 'import edubotics.Robot;\n\npublic class Main {\n  static int x = 1;\n  public static void main(String[] a) {\n  }\n}\n';
    for (const line of [1, 3, 4]) {
      expect(insertionTargetAt(src, 'java', line)).toEqual({ notFound: true, hint: CODE_DE.NOT_IN_METHOD_HINT });
    }
    expect(CODE_DE.NOT_IN_METHOD_HINT).toMatch(/Methode/);
  });
});

describe('the end of main', () => {
  it('Python: the end of main.py, top level', () => {
    const src = 'import robot\nfor i in range(3):\n    robot.home()\n';
    expect(atMain(src, ['robot.beep()']).content)
      .toBe('import robot\nfor i in range(3):\n    robot.home()\nrobot.beep()\n');
    expect(atMain('', ['robot.beep()']).content).toBe('robot.beep()\n');
  });

  it('Java: inside main, after its last statement — braces in strings and comments skipped', () => {
    const src = [
      'public class Main {',
      '    public static void main(String[] args) {',
      '        String s = "}";',
      '        // }',
      '        Robot.home();',
      '    }',
      '    static void hilfe() {',
      '    }',
      '}',
      '',
    ].join('\n');
    const out = atMain(src, ['Robot.beep();'], 'java');
    expect(out.content.split('\n').slice(4, 7)).toEqual(['        Robot.home();', '        Robot.beep();', '    }']);
    expect([out.firstLine, out.lastLine]).toEqual([6, 6]);
  });

  it('Java starter: lands inside main', () => {
    const src = STARTER_FILES.java['Main.java'];
    const out = atMain(src, ['Robot.beep();'], 'java').content;
    expect(out).toContain('        Robot.log("Hallo Roboter!");\n        Robot.beep();\n    }\n}');
  });

  it('Java without a main, or with unbalanced braces: nothing, and a German reason', () => {
    expect(mainTarget('class A {}\n', 'java')).toEqual({ notFound: true, hint: CODE_DE.NO_MAIN_HINT });
    expect(mainTarget('public class Main {\n  static void main(String[] a) {\n', 'java'))
      .toEqual({ notFound: true, hint: CODE_DE.NO_SAFE_PLACE_HINT });
    expect(insertAtTarget('class A {}\n', mainTarget('class A {}\n', 'java'), ['x();'], 'java').content)
      .toBe('class A {}\n');
  });

  it('Python: an unterminated string or bracket at the end is no safe place', () => {
    expect(mainTarget('x = (\n', 'python')).toEqual({ notFound: true, hint: CODE_DE.NO_SAFE_PLACE_HINT });
    expect(mainTarget('"""\noffen\n', 'python')).toEqual({ notFound: true, hint: CODE_DE.NO_SAFE_PLACE_HINT });
  });
});

describe('insertionTarget — the cursor, else the end of main', () => {
  const files = { 'main.py': 'import robot\nrobot.home()\n', 'hilfe.py': 'def f():\n    pass\n' };

  it('uses the last cursor when its file and line exist', () => {
    const t = insertionTarget(files, 'python', { file: 'hilfe.py', line: 2 });
    expect(t).toMatchObject({ file: 'hilfe.py', fromCursor: true, mode: 'after', indent: '    ' });
    expect(insertAtTarget(files['hilfe.py'], t, ['x()'], 'python').content).toBe('def f():\n    pass\n    x()\n');
  });

  it('falls back to the entry file’s main end when the cursor is unknown or stale', () => {
    for (const cursor of [null, { file: 'weg.py', line: 1 }, { file: 'main.py', line: 99 }]) {
      const t = insertionTarget(files, 'python', cursor);
      expect(t).toMatchObject({ file: 'main.py', fromCursor: false });
      expect(insertAtTarget(files['main.py'], t, ['x()'], 'python').content)
        .toBe('import robot\nrobot.home()\nx()\n');
    }
  });

  it('passes a missing main on instead of guessing a place — and a class-level cursor too', () => {
    const t = insertionTarget({ 'Main.java': 'class A {}\n' }, 'java', null);
    expect(t).toMatchObject({ file: 'Main.java', notFound: true, fromCursor: false, hint: CODE_DE.NO_MAIN_HINT });
    // A cursor on a class line is no place for a statement either (mi3).
    expect(insertionTarget({ 'Main.java': 'class A {}\n' }, 'java', { file: 'Main.java', line: 1 }))
      .toMatchObject({ fromCursor: true, notFound: true, hint: CODE_DE.NOT_IN_METHOD_HINT });
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
      ['  void f() {\n  }\n', 1, 'java'],
      ['class M {\n  static void main(String[] a) { Robot.home(); }\n}\n', 2, 'java'],
    ];
    for (const [content, line, language] of cases) {
      const target = insertionTargetAt(content, language, line);
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

describe('indentation is the file’s own (review M2, round 2 R2-O2)', () => {
  it('a file with no indented line uses 4 spaces (PEP 8, the Java starter)', () => {
    expect(CODE_INDENT_UNIT).toBe('    ');
    expect(fileIndentUnit('a()\nb()\n', 'python')).toBe('    ');
    expect(fileIndentUnit('', 'python')).toBe('    ');
    expect(STARTER_FILES.java['Main.java']).toContain('\n    public static void main');
    expect(fileIndentUnit(STARTER_FILES.java['Main.java'], 'java')).toBe('    ');
  });

  it('after a block opener: the indentation of the body line below it', () => {
    const two = 'import robot\n\nfor i in range(3):\n  robot.replay("Winken")\nrobot.home()\n';
    expect(at(two, 3, ['robot.beep()']).content)
      .toBe('import robot\n\nfor i in range(3):\n  robot.beep()\n  robot.replay("Winken")\nrobot.home()\n');
    const tab = 'for i in range(3):\n\trobot.home()\n';
    expect(at(tab, 1, ['a()']).content).toBe('for i in range(3):\n\ta()\n\trobot.home()\n');
    const four = 'while x:\n    a()\n';
    expect(at(four, 1, ['b()']).content).toBe('while x:\n    b()\n    a()\n');
  });

  it('an opener with no body yet: its own indentation plus the unit the file uses', () => {
    expect(at('def f():\n  return 1\nif x:\n', 3, ['a()']).content)
      .toBe('def f():\n  return 1\nif x:\n  a()\n');
    expect(at('def f():\n\treturn 1\nif x:\n', 3, ['a()']).content)
      .toBe('def f():\n\treturn 1\nif x:\n\ta()\n');
    expect(at('if x:\n', 1, ['a()']).content).toBe('if x:\n    a()\n');
  });

  it('inside a body the anchor’s own indentation (2 spaces stays 2)', () => {
    const two = 'for i in range(3):\n  robot.home()\n';
    expect(at(two, 2, ['robot.beep()']).content).toBe('for i in range(3):\n  robot.home()\n  robot.beep()\n');
  });

  it('detectIndentUnit reads the step the file uses; the editor reads the same function', () => {
    expect(detectIndentUnit('if a:\n  if b:\n    c()\n')).toBe('  ');
    expect(detectIndentUnit('if a:\n\tb()\n')).toBe('\t');
    expect(detectIndentUnit('a()\nb()\n')).toBeNull();
    expect(detectIndentUnit('x = """\n      text\n"""\n', 'python')).toBeNull();
    expect(fileIndentUnit('if a:\n  b()\n', 'python')).toBe('  ');
  });

  it('Java main with a 2-space body: the body’s own indentation', () => {
    const src = 'public class Main {\n  public static void main(String[] a) {\n    Robot.home();\n  }\n}\n';
    expect(atMain(src, ['Robot.beep();'], 'java').content)
      .toBe('public class Main {\n  public static void main(String[] a) {\n    Robot.home();\n    Robot.beep();\n  }\n}\n');
    const empty = 'public class Main {\n  public static void main(String[] a) {\n  }\n}\n';
    expect(atMain(empty, ['Robot.beep();'], 'java').content)
      .toBe('public class Main {\n  public static void main(String[] a) {\n    Robot.beep();\n  }\n}\n');
  });
});

describe('the end of main stops before what never lets the next line run (review R-O2, round 2 mi2)', () => {
  it('Python: before a trailing top-level `while True:`; a finite end keeps the end of the file', () => {
    const src = 'import robot\nrobot.home()\nwhile True:\n    robot.beep()\n\n# Ende\n';
    expect(atMain(src, ['x()']).content)
      .toBe('import robot\nrobot.home()\nx()\nwhile True:\n    robot.beep()\n\n# Ende\n');
    expect(atMain('while 1:\n    pass\n', ['x()']).content).toBe('x()\nwhile 1:\n    pass\n');
    const notLast = 'while True:\n    break\nrobot.home()\n';
    expect(atMain(notLast, ['x()']).content).toBe('while True:\n    break\nrobot.home()\nx()\n');
  });

  it('Python: into the body that runs last — the __main__ block, the function a final call runs', () => {
    const guard = 'def main():\n    a()\n\nif __name__ == "__main__":\n    while True:\n        main()\n';
    expect(atMain(guard, ['x()']).content)
      .toBe('def main():\n    a()\n\nif __name__ == "__main__":\n    x()\n    while True:\n        main()\n');
    const func = 'def main():\n    a()\n    while True:\n        b()\n\nmain()\n';
    expect(atMain(func, ['x()']).content)
      .toBe('def main():\n    a()\n    x()\n    while True:\n        b()\n\nmain()\n');
    // A function with an ordinary end: the lines go at its end, still run last.
    const plain = 'def main():\n  a()\n\nmain()\n';
    expect(atMain(plain, ['x()']).content).toBe('def main():\n  a()\n  x()\n\nmain()\n');
  });

  const java = (body) => ['public class Main {', '    public static void main(String[] args) {',
    ...body, '    }', '}', ''].join('\n');
  const lastLines = (content, n = 3) => content.split('\n').slice(2, 2 + n);

  it('Java: before a trailing while (true) / for (;;) / return / do … while (true) / throw', () => {
    expect(lastLines(atMain(java(['        Robot.home();', '        while (true) {', '            Robot.beep();', '        }']), ['X();'], 'java').content))
      .toEqual(['        Robot.home();', '        X();', '        while (true) {']);
    expect(lastLines(atMain(java(['        Robot.home();', '        for (;;) Robot.beep();']), ['X();'], 'java').content))
      .toEqual(['        Robot.home();', '        X();', '        for (;;) Robot.beep();']);
    expect(lastLines(atMain(java(['        Robot.home();', '        return;']), ['X();'], 'java').content))
      .toEqual(['        Robot.home();', '        X();', '        return;']);
    expect(lastLines(atMain(java(['        do {', '            Robot.beep();', '        } while (true);']), ['X();'], 'java').content, 2))
      .toEqual(['        X();', '        do {']);
    expect(lastLines(atMain(java(['        throw new RuntimeException("x");']), ['X();'], 'java').content, 2))
      .toEqual(['        X();', '        throw new RuntimeException("x");']);
  });

  it('Java: an ordinary last statement (an if/else, a finite loop on a variable) keeps the end of main', () => {
    const src = java(['        int i = 0;', '        if (i > 1) {', '            a();', '        } else {', '            b();', '        }',
      '        while (i < 3) { i++; }']);
    expect(atMain(src, ['X();'], 'java').content.split('\n').slice(8, 10))
      .toEqual(['        while (i < 3) { i++; }', '        X();']);
  });

  it('Java: a main whose braces sit on ONE line is opened up, inside the class', () => {
    const src = 'public class Main {\n    public static void main(String[] a) { Robot.home(); }\n}\n';
    const out = atMain(src, ['Robot.beep();'], 'java');
    expect(out.content).toBe(
      'public class Main {\n    public static void main(String[] a) { Robot.home();\n'
      + '        Robot.beep();\n    }\n}\n',
    );
    expect([out.firstLine, out.lastLine]).toEqual([3, 3]);
  });
});

describe('a CRLF file gets CRLF line breaks (review round 2, ni2)', () => {
  it('both at the end of main and below a cursor', () => {
    expect(atMain('import robot\r\nrobot.home()\r\n', ['x()']).content).toBe('import robot\r\nrobot.home()\r\nx()\r\n');
    expect(at('if x:\r\n    a()\r\n', 1, ['b()']).content).toBe('if x:\r\n    b()\r\n    a()\r\n');
    const java = 'class Main {\r\n  static void main(String[] a) {\r\n    Robot.home();\r\n  }\r\n}\r\n';
    expect(atMain(java, ['X();'], 'java').content)
      .toBe('class Main {\r\n  static void main(String[] a) {\r\n    Robot.home();\r\n    X();\r\n  }\r\n}\r\n');
  });
});
