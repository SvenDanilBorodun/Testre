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
// placed on new lines below the last cursor line — or, when the student never
// clicked into the editor, at the end of main.

import { describe, it, expect } from 'vitest';
import {
  SNIPPET_MIME,
  insertLinesAt,
  insertionEdit,
  insertionTarget,
  mainBodyEnd,
  minimalChange,
  snippetLines,
  stepsToCode,
} from '../codeInsert';
import { STARTER_FILES } from '../codeProject';

const STEPS = [
  { type: 'replay', name: 'Winken' },
  { type: 'move_to', name: 'Ablage' },
  { type: 'close_gripper' },
  { type: 'move_to', name: 'Kiste' },
  { type: 'open_gripper' },
];

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

describe('insertLinesAt', () => {
  it('inserts below the anchor line with its indentation', () => {
    const src = 'import robot\nfor i in range(3):\n    robot.home()\nprint(1)\n';
    const r = insertLinesAt(src, 3, ['robot.lift()', 'robot.beep()']);
    expect(r.content).toBe('import robot\nfor i in range(3):\n    robot.home()\n    robot.lift()\n    robot.beep()\nprint(1)\n');
    expect([r.firstLine, r.lastLine]).toEqual([4, 5]);
  });

  it('adds one level after a line opening a block (Python `:`, Java `{`)', () => {
    expect(insertLinesAt('if x:\n    pass\n', 1, ['a()']).content).toBe('if x:\n    a()\n    pass\n');
    expect(insertLinesAt('if x:  # kommentar\n', 1, ['a()']).content).toBe('if x:  # kommentar\n    a()\n');
    expect(insertLinesAt('  void f() {\n  }\n', 1, ['a();'], { language: 'java' }).content)
      .toBe('  void f() {\n      a();\n  }\n');
  });

  it('takes an empty anchor line’s indentation from the nearest line above', () => {
    expect(insertLinesAt('def f():\n    x = 1\n\n', 3, ['y()']).content).toBe('def f():\n    x = 1\n\n    y()\n');
  });

  it('keeps tabs as tabs, clamps the line, honours an explicit indent', () => {
    expect(insertLinesAt('if x:\n\tpass\n', 2, ['a()']).content).toBe('if x:\n\tpass\n\ta()\n');
    expect(insertLinesAt('a\n', 99, ['b']).content).toBe('a\nb\n');
    expect(insertLinesAt('a\n', 0, ['b']).content).toBe('b\na\n');
    expect(insertLinesAt('    a\n', 1, ['b'], { indent: '' }).content).toBe('    a\nb\n');
  });
});

describe('mainBodyEnd', () => {
  it('Python: the end of main.py, top level', () => {
    const src = 'import robot\nfor i in range(3):\n    robot.home()\n';
    expect(mainBodyEnd(src, 'python')).toEqual({ afterLine: 3, indent: '' });
    expect(insertLinesAt(src, 3, ['robot.beep()'], mainBodyEnd(src, 'python')).content)
      .toBe('import robot\nfor i in range(3):\n    robot.home()\nrobot.beep()\n');
    expect(mainBodyEnd('', 'python')).toEqual({ afterLine: 0, indent: '' });
  });

  it('Java: before main()’s closing brace, one level in — braces in strings and comments skipped', () => {
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
    expect(mainBodyEnd(src, 'java')).toEqual({ afterLine: 5, indent: '        ' });
  });

  it('Java starter: lands inside main', () => {
    const src = STARTER_FILES.java['Main.java'];
    const at = mainBodyEnd(src, 'java');
    const out = insertLinesAt(src, at.afterLine, ['Robot.beep();'], at).content;
    expect(out).toContain('        Robot.log("Hallo Roboter!");\n        Robot.beep();\n    }\n}');
  });

  it('Java without a main: the end of the file', () => {
    expect(mainBodyEnd('class A {}\n', 'java')).toEqual({ afterLine: 1, indent: '' });
  });
});

describe('insertionTarget — the cursor, else the end of main', () => {
  const files = { 'main.py': 'import robot\nrobot.home()\n', 'hilfe.py': 'def f():\n    pass\n' };

  it('uses the last cursor when its file and line exist', () => {
    expect(insertionTarget(files, 'python', { file: 'hilfe.py', line: 2 }))
      .toEqual({ file: 'hilfe.py', afterLine: 2, fromCursor: true });
  });

  it('falls back to the entry file’s main end when the cursor is unknown or stale', () => {
    const expected = { file: 'main.py', afterLine: 2, indent: '', fromCursor: false };
    expect(insertionTarget(files, 'python', null)).toEqual(expected);
    expect(insertionTarget(files, 'python', { file: 'weg.py', line: 1 })).toEqual(expected);
    expect(insertionTarget(files, 'python', { file: 'main.py', line: 99 })).toEqual(expected);
  });
});

describe('insertionEdit — the same insertion as ONE editor change', () => {
  it('yields exactly insertLinesAt’s content when applied', () => {
    const cases = [
      ['import robot\nfor i in range(3):\n    robot.home()\nprint(1)\n', 3, {}],
      ['if x:\n    pass\n', 1, {}],
      ['a\n', 99, {}],
      ['a\n', 0, {}],
      ['a', 1, {}],
      ['', 0, {}],
      ['    a\n', 1, { indent: '' }],
      ['  void f() {\n  }\n', 1, { language: 'java' }],
    ];
    for (const [content, after, opts] of cases) {
      const edit = insertionEdit(content, after, ['x()', 'y()'], opts);
      const applied = content.slice(0, edit.from) + edit.insert + content.slice(edit.from);
      expect(applied).toBe(insertLinesAt(content, after, ['x()', 'y()'], opts).content);
      const ref = insertLinesAt(content, after, ['x()', 'y()'], opts);
      expect([edit.firstLine, edit.lastLine]).toEqual([ref.firstLine, ref.lastLine]);
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
