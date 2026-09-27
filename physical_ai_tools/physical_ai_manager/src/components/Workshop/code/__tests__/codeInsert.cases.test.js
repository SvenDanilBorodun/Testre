/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Every insertion case of review round 2 (mi2, mi3 — the reviewer's list and
// the owner's) as a GOLDEN FIXTURE: this test computes what „Einfügen" writes
// into each program and compares it with fixtures/code-insert-cases.json; the
// fixture is what robotis_ai_setup/tests/test_code_insert_cases.py PARSES
// with CPython 3.12 and COMPILES with javac 21 — and runs, checking the
// inserted `robot.replay("Winken")` is reached. A change to the insertion
// therefore fails here until the fixture is regenerated, and the regenerated
// programs are judged by the real tools:
//
//   EDUBOTICS_REGEN_CODE_CASES=1 npx vitest run src/components/Workshop/code/__tests__/codeInsert.cases.test.js
//
// `reach: 'run'` — the marker must actually run (main runs, and the lines are
// in the body that runs last or in main); `reach: 'static'` — only that no
// statement before it in its block makes it unreachable (an insertion into a
// function the case never calls).

import fs from 'fs';
import path from 'path';
import { describe, it, expect } from 'vitest';
import {
  insertAtTarget, insertionTarget, snippetLines,
} from '../codeInsert';

const FIXTURE = path.join(__dirname, 'fixtures', 'code-insert-cases.json');
const REGEN = !['', '0', undefined].includes(process.env.EDUBOTICS_REGEN_CODE_CASES);

const J = (body, head = 'import edubotics.Robot;\npublic class Main {\n    public static void main(String[] args) {\n') => (
  `${head}${body}    }\n}\n`
);

// [name, input, cursorLine | null, reach]
const PYTHON = [
  ['two_space_func_call', 'import robot\n\ndef main():\n  for i in range(3):\n    robot.home()\n  robot.log("x")\n\nmain()\n', null, 'run'],
  ['trailing_while_in_main_guard', 'import robot\n\ndef main():\n    robot.home()\n\nif __name__ == "__main__":\n    while True:\n        main()\n', null, 'run'],
  ['trailing_while_in_func', 'import robot\n\ndef main():\n    robot.home()\n    while True:\n        robot.log("x")\n\nmain()\n', null, 'run'],
  ['guard_calls_main_with_loop', 'import robot\n\ndef main():\n    robot.home()\n    while True:\n        robot.log("x")\n\nif __name__ == \'__main__\':\n    main()\n', null, 'run'],
  ['func_trailing_return', 'import robot\n\ndef main():\n    robot.home()\n    return\n\nmain()\n', null, 'run'],
  ['top_while_true', 'import robot\nrobot.home()\nwhile True:\n    robot.log("x")\n', null, 'run'],
  ['top_while_one_liner', 'import robot\nrobot.home()\nwhile True: pass\n', null, 'run'],
  ['top_while_else', 'import robot\nwhile True:\n    break\nelse:\n    robot.home()\n', null, 'run'],
  ['no_trailing_newline', 'import robot\nrobot.home()', null, 'run'],
  ['crlf', 'import robot\r\nrobot.home()\r\nwhile True:\r\n    robot.log("x")\r\n', null, 'run'],
  ['tabs_body', 'import robot\nif True:\n\trobot.home()\nwhile True:\n\trobot.log("x")\n', null, 'run'],
  ['docstring_end', 'import robot\n"""\nwhile True:\n"""\n', null, 'run'],
  ['last_line_in_multiline_call', 'import robot\nrobot.log(\n"x"\n)\n', null, 'run'],
  ['while_1', 'import robot\nwhile 1:\n    robot.home()\n', null, 'run'],
  ['empty', '', null, 'run'],
  ['cursor_multiline_if', 'import robot\nif (1 and\n        2):\n    robot.home()\n', 2, 'run'],
  ['cursor_in_list', 'import robot\npunkte = [\n    1,\n    2,\n]\n', 3, 'run'],
  ['cursor_decorator', 'import robot\nimport functools\n@functools.cache\ndef f():\n    return 1\n', 3, 'static'],
  ['cursor_opener_comment', 'import robot\nfor i in range(2):  # loop:\n    robot.home()\n', 2, 'run'],
  ['cursor_opener_no_body_2sp', 'import robot\ndef f():\n  x = 1\n  if x:\n    robot.home()\nf()\n', 4, 'run'],
  ['cursor_else', 'import robot\nif 0:\n  robot.home()\nelse:\n  robot.log("y")\n', 4, 'run'],
  ['cursor_dict_brace', 'import robot\nd = {\n    "a": 1,\n}\n', 2, 'run'],
  ['cursor_backslash', 'import robot\nx = 1 + \\\n    2\n', 2, 'run'],
  ['cursor_docstring_mid', 'import robot\ndef f():\n    """Doc\n    more"""\n    return 1\n', 3, 'static'],
  ['cursor_opener_tab_body', 'import robot\nif True:\n\trobot.home()\n', 2, 'run'],
  ['cursor_2sp_opener_bodyless_file4', 'import robot\ndef f():\n    pass\nfor i in range(2):\n', 4, 'run'],
  ['cursor_class_line', 'import robot\nclass A:\n    x = 1\n', 2, 'run'],
  ['cursor_try', 'import robot\ntry:\n    robot.home()\nexcept Exception:\n    pass\nfinally:\n    pass\n', 6, 'run'],
  ['cursor_on_return', 'import robot\ndef f():\n    robot.home()\n    return 1\nf()\n', 4, 'run'],
  ['cursor_on_raise', 'import robot\ndef f():\n    robot.home()\n    raise ValueError(1)\ntry:\n    f()\nexcept ValueError:\n    pass\n', 4, 'run'],
  ['cursor_on_break', 'import robot\nfor i in range(3):\n    robot.home()\n    break\n', 4, 'run'],
  ['cursor_on_continue', 'import robot\nfor i in range(3):\n    robot.home()\n    continue\n', 4, 'run'],
  ['cursor_on_endless_oneliner', 'import robot\nrobot.home()\nwhile True: robot.log("x")\n', 3, 'run'],
  ['cursor_on_semicolon_return', 'import robot\ndef f():\n    robot.home(); return 1\nf()\n', 3, 'run'],
];

const JAVA = [
  ['plain', 'import edubotics.Robot;\n\npublic class Main {\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['trailing_while', J('        Robot.home();\n        while (true) {\n            Robot.log("x");\n        }\n'), null, 'run'],
  ['trailing_while_nospace', J('        while(true){\n            Robot.log("x");\n        }\n'), null, 'run'],
  ['trailing_for', J('        for (;;) { Robot.home(); }\n'), null, 'run'],
  ['trailing_do', J('        do {\n            Robot.home();\n        } while (true);\n'), null, 'run'],
  ['trailing_return', J('        Robot.home();\n        return;\n'), null, 'run'],
  ['trailing_labeled_while', J('        outer:\n        while (true) {\n            Robot.home();\n        }\n'), null, 'run'],
  ['trailing_labeled_while_sameline', J('        outer: while (true) {\n            Robot.home();\n        }\n'), null, 'run'],
  ['trailing_throw', J('        Robot.home();\n        throw new RuntimeException("x");\n'), null, 'run'],
  ['return_nested_not_trailing', J('        if (args.length > 5) {\n            return;\n        }\n        Robot.home();\n'), null, 'run'],
  ['one_line_main', 'import edubotics.Robot;\npublic class Main {\n    public static void main(String[] args) { Robot.home(); }\n}\n', null, 'run'],
  ['one_line_main_while', 'import edubotics.Robot;\npublic class Main {\n    public static void main(String[] args) { while (true) { Robot.home(); } }\n}\n', null, 'run'],
  ['main_in_comment_first', 'import edubotics.Robot;\n// public static void main(String[] a) { }\npublic class Main {\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['main_in_string_first', 'import edubotics.Robot;\npublic class Main {\n    static String s = "static void main( {";\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['overloaded_main_first', 'import edubotics.Robot;\npublic class Main {\n    static void main(int x) {\n        Robot.home();\n    }\n    public static void main(String[] args) {\n        main(1);\n    }\n}\n', null, 'run'],
  ['nested_class_main_first', 'import edubotics.Robot;\npublic class Main {\n    static class Inner {\n        static void main(String[] a) { }\n    }\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['static_public_order', 'import edubotics.Robot;\npublic class Main {\n    static public void main(String[] args) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['final_main', 'import edubotics.Robot;\npublic class Main {\n    public static final void main(String[] args) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['generic_main', 'import edubotics.Robot;\npublic class Main {\n    public static <T> void main(String[] args) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['annotated_param', 'import edubotics.Robot;\npublic class Main {\n    public static void main(final String... args) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['c_style_array_param', 'import edubotics.Robot;\npublic class Main {\n    public static void main(String args[]) {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['text_block', J('        String t = """\n            }\n            """;\n        Robot.log(t);\n'), null, 'run'],
  ['char_brace', J("        char c = '}';\n        Robot.home();\n"), null, 'run'],
  ['lambda_last', J('        Runnable r = () -> { Robot.home(); };\n'), null, 'run'],
  ['allman', 'import edubotics.Robot;\npublic class Main\n{\n    public static void main(String[] args)\n    {\n        Robot.home();\n    }\n}\n', null, 'run'],
  ['close_same_line', 'import edubotics.Robot;\npublic class Main {\n    public static void main(String[] args) {\n        Robot.home(); }\n}\n', null, 'run'],
  ['crlf', 'import edubotics.Robot;\r\npublic class Main {\r\n    public static void main(String[] args) {\r\n        Robot.home();\r\n    }\r\n}\r\n', null, 'run'],
  ['if_else_return', J('        if (args.length == 0) { return; } else { return; }\n'), null, 'run'],
  ['while_constant', J('        while (1 == 1) { Robot.home(); }\n'), null, 'run'],
  ['final_constant_loop', J('        final boolean immer = true;\n        while (immer) { Robot.home(); }\n'), null, 'run'],
  ['finite_loop_on_variable', J('        boolean laeuft = args.length > 5;\n        while (laeuft) { laeuft = false; }\n'), null, 'run'],
  ['finite_for', J('        for (int i = 0; i < 3; i++) {\n            Robot.home();\n        }\n'), null, 'run'],
  ['endless_with_break', J('        while (true) {\n            Robot.home();\n            break;\n        }\n'), null, 'run'],
  ['trailing_switch', J('        switch (args.length) {\n            case 0:\n                return;\n            default:\n                return;\n        }\n'), null, 'run'],
  ['try_while', J('        try {\n            while (true) { Robot.home(); }\n        } finally {\n            Robot.log("x");\n        }\n'), null, 'run'],
  ['no_main', 'import edubotics.Robot;\npublic class Main {\n}\n', null, 'hint'],
  ['cursor_import_line', 'import edubotics.Robot;\n\npublic class Main {\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 1, 'hint'],
  ['cursor_class_line', 'import edubotics.Robot;\n\npublic class Main {\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 3, 'hint'],
  ['cursor_field_line', 'import edubotics.Robot;\npublic class Main {\n    static int zaehler = 0;\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 3, 'hint'],
  ['cursor_between_methods', 'import edubotics.Robot;\npublic class Main {\n    static void hilfe() {\n    }\n\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 5, 'hint'],
  ['cursor_return', J('        Robot.home();\n        return;\n'), 5, 'run'],
  ['cursor_on_throw', J('        Robot.home();\n        throw new RuntimeException("x");\n'), 5, 'run'],
  ['cursor_on_break_in_loop', J('        for (int i = 0; i < 3; i++) {\n            Robot.home();\n            break;\n        }\n'), 6, 'run'],
  ['cursor_on_continue', J('        for (int i = 0; i < 3; i++) {\n            Robot.home();\n            continue;\n        }\n'), 6, 'run'],
  ['cursor_in_call_args', J('        Robot.log(\n            "x");\n'), 4, 'run'],
  ['cursor_on_endless_loop_header', J('        while (true) {\n            Robot.home();\n        }\n'), 4, 'run'],
  ['cursor_on_endless_loop_close', J('        Robot.home();\n        while (true) {\n            Robot.log("x");\n        }\n'), 8, 'run'],
  ['cursor_on_main_close', J('        Robot.home();\n'), 5, 'run'],
  ['cursor_one_line_main', 'import edubotics.Robot;\npublic class Main {\n    public static void main(String[] args) { Robot.home(); }\n}\n', 3, 'run'],
];

function compute() {
  const cases = [];
  for (const [language, table, file] of [['python', PYTHON, 'main.py'], ['java', JAVA, 'Main.java']]) {
    const lines = snippetLines({ kind: 'recording', name: 'Winken' }, language);
    for (const [name, input, line, reach] of table) {
      const cursor = line === null ? null : { file, line };
      const target = insertionTarget({ [file]: input }, language, cursor);
      const out = target.notFound ? null : insertAtTarget(input, target, lines, language).content;
      cases.push({
        name, language, line, reach, input, output: out, hint: target.notFound ? target.hint : null,
      });
    }
  }
  return {
    _generated_by: 'physical_ai_manager/src/components/Workshop/code/__tests__/codeInsert.cases.test.js '
      + '(EDUBOTICS_REGEN_CODE_CASES=1 to regenerate)',
    _checked_by: 'robotis_ai_setup/tests/test_code_insert_cases.py (CPython ast/compile + javac 21)',
    marker: { python: 'robot.replay("Winken")', java: 'Robot.replay("Winken");' },
    cases,
  };
}

describe('the insertion cases of review round 2 (mi2, mi3)', () => {
  it('match the fixture the compile-and-run test judges', () => {
    const now = compute();
    if (REGEN) {
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, `${JSON.stringify(now, null, 1)}\n`);
    }
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(now).toEqual(saved);
  });

  it('place a hint only where no statement may stand, and nowhere else', () => {
    const { cases } = compute();
    const hinted = cases.filter((c) => c.output === null).map((c) => c.name);
    expect(hinted).toEqual(cases.filter((c) => c.reach === 'hint').map((c) => c.name));
    for (const c of cases.filter((x) => x.output !== null)) {
      const marker = c.language === 'java' ? 'Robot.replay("Winken");' : 'robot.replay("Winken")';
      expect(c.output.split(/\r?\n/).filter((l) => l.trim() === marker)).toHaveLength(1);
      // Nothing but the inserted line (and line breaks around a split) changed.
      expect(c.output.replace(/\s+/g, '').replace(marker, '')).toBe(c.input.replace(/\s+/g, ''));
    }
  });
});
