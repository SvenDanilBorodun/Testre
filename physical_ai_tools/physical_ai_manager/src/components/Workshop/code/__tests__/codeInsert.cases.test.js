/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Every insertion case as a GOLDEN FIXTURE (owner decision R3-O4: the student
// chooses the spot, the app only checks it). This test computes what
// „Einfügen" does for each program and cursor line and compares it with
// fixtures/code-insert-cases.json; the fixture is what
// robotis_ai_setup/tests/test_code_insert_cases.py PARSES with CPython 3.12
// and COMPILES with javac 21 — and RUNS, checking the inserted
// `robot.replay("Winken")` is reached. A change to the insertion therefore
// fails here until the fixture is regenerated, and the regenerated programs
// are judged by the real tools:
//
//   EDUBOTICS_REGEN_CODE_CASES=1 npx vitest run src/components/Workshop/code/__tests__/codeInsert.cases.test.js
//
// Every case ends ONE of two ways — never a broken insertion, never a dead
// one, never a line moved somewhere the student did not click:
//   'run'  — the marker stands directly below the cursor line, the program
//            compiles, and running it reaches the marker;
//   a hint — nothing is inserted, and the German reason is the one named.
// The cases include every program of the round-3 review (3-A's probe list
// and 3-B's), re-cast with the cursor where each one's question now lies.

import fs from 'fs';
import path from 'path';
import { describe, it, expect } from 'vitest';
import {
  insertAtTarget, insertionTarget, newlineIndentAt, snippetLines,
} from '../codeInsert';
import { CODE_DE, formatCode } from '../codeMessagesDe';
import { STARTER_FILES } from '../codeProject';

const FIXTURE = path.join(__dirname, 'fixtures', 'code-insert-cases.json');
const REGEN = !['', '0', undefined].includes(process.env.EDUBOTICS_REGEN_CODE_CASES);

const NEVER = (what) => formatCode(CODE_DE.INSERT_NEVER_RUNS_HINT, what);
const H = {
  click: CODE_DE.CLICK_FIRST_HINT,
  lead: CODE_DE.INSERT_LEADING_BLOCK_HINT,
  doc: CODE_DE.INSERT_DOCSTRING_HINT,
  deco: CODE_DE.INSERT_DECORATOR_HINT,
  match: CODE_DE.INSERT_MATCH_HINT,
  switch: CODE_DE.INSERT_SWITCH_HINT,
  inside: CODE_DE.INSERT_INSIDE_EXPRESSION_HINT,
  clause: CODE_DE.INSERT_CLAUSE_HINT,
  indent: CODE_DE.INSERT_INDENT_HINT,
  indentBroken: CODE_DE.INSERT_INDENT_BROKEN_HINT,
  unclosed: CODE_DE.NO_SAFE_PLACE_HINT,
  unreadable: CODE_DE.INSERT_UNREADABLE_HINT,
  method: CODE_DE.NOT_IN_METHOD_HINT,
  unsure: CODE_DE.INSERT_UNSURE_HINT,
  return: NEVER('„return“'),
  raise: NEVER('„raise“'),
  throw: NEVER('„throw“'),
  break: NEVER('„break“'),
  continue: NEVER('„continue“'),
  exit: NEVER('ein Programmende (exit)'),
  loop: NEVER('eine Endlosschleife'),
  ifelse: NEVER('ein if/else, das in jedem Zweig endet'),
  ifalways: NEVER('ein if, das immer genommen wird und endet'),
  try: NEVER('ein try, das in jedem Zweig endet'),
  matchEnds: NEVER('ein match, das in jedem Fall endet'),
  switchEnds: NEVER('ein switch, das in jedem Fall endet'),
};

const J = (body, pre = '', post = '') => (
  `import edubotics.Robot;\n${pre}public class Main {\n    public static void main(String[] args) {\n${body}    }\n${post}}\n`
);
const PY_STARTER = STARTER_FILES.python['main.py'];
const JAVA_STARTER = STARTER_FILES.java['Main.java'];

// [name, input, cursorLine | null, 'run' | hint key, runs?] — `runs`: how
// many times the inserted marker must run (CPython counts it, review round
// 5); a case without it only has to reach the marker once.
const PYTHON = [
  // ── valid spots: the line goes directly below, and runs ──
  ['starter_below_import', PY_STARTER, 3, 'run', 1],
  ['starter_blank_line', PY_STARTER, 4, 'run', 1],
  ['starter_last_line', PY_STARTER, 6, 'run', 1],
  // The empty row after the starter's final line break is a row of its own.
  ['starter_eof_row', PY_STARTER, 7, 'run', 1],
  ['plain_statement', 'import robot\nrobot.home()\n', 2, 'run', 1],
  ['no_trailing_newline', 'import robot\nrobot.home()', 2, 'run', 1],
  ['empty_file', '', 1, 'run', 1],
  ['for_body_sibling', 'import robot\nfor i in range(2):\n    robot.home()\n', 3, 'run', 2],
  // Review round 5, MD1: the empty row after a file's final line break is its
  // own row — the line goes below it at column 0, exactly where Enter puts
  // it, and runs ONCE (it used to land in the last block: three times inside
  // the loop, never inside a def or an else).
  ['eof_row_after_for', 'import robot\nfor i in range(3):\n    robot.home()\n', 4, 'run', 1],
  ['eof_row_after_def_body', 'import robot\ndef main():\n    robot.home()\n', 4, 'run', 1],
  ['eof_row_after_if_else', 'import robot\nif True:\n    robot.home()\nelse:\n    robot.log("x")\n', 6, 'run', 1],
  ['eof_row_after_while', 'import robot\nn = 0\nwhile n < 3:\n    n += 1\n', 5, 'run', 1],
  ['eof_row_after_try_except', 'import robot\ntry:\n    robot.home()\nexcept Exception:\n    pass\n', 6, 'run', 1],
  ['eof_row_crlf_after_for', 'import robot\r\nfor i in range(3):\r\n    robot.home()\r\n', 4, 'run', 1],
  ['eof_row_two_space_for', 'import robot\nfor i in range(3):\n  robot.home()\n', 4, 'run', 1],
  ['eof_row_tab_for', 'import robot\nfor i in range(3):\n\trobot.home()\n', 4, 'run', 1],
  ['eof_row_after_main_call', 'import robot\ndef main():\n    robot.home()\nmain()\n', 5, 'run', 1],
  ['blank_file_first_row', '\n\n\n', 1, 'run', 1],
  // Review round 5, md4: whitespace shallower than the next statement is not
  // a step back — the line takes the level that statement needs.
  ['ws_row_shallower_than_next', 'import robot\ndef main():\n    if True:\n        robot.home()\n    \n        robot.log(1)\nmain()\n', 5, 'run', 1],
  // Review round 5, md3: a handler or a suppress catches only what is RAISED
  // — a call that raises first still lets the line after run.
  ['after_with_suppress_call_then_return', 'import robot\nfrom contextlib import suppress\ndef main():\n    with suppress(ValueError):\n        int("x")\n        return\n    robot.log(1)\nmain()\n', 7, 'run', 1],
  ['after_try_call_then_return_except', 'import robot\ndef main():\n    try:\n        int("x")\n        return\n    except ValueError:\n        pass\n    robot.log(1)\nmain()\n', 8, 'run', 1],
  ['after_try_return_call_except', 'import robot\ndef main():\n    try:\n        return int("x")\n    except ValueError:\n        pass\n    robot.log(1)\nmain()\n', 7, 'run', 1],
  ['after_try_sys_exit_except_base', 'import robot\nimport sys\ndef main():\n    try:\n        sys.exit(0)\n    except BaseException:\n        pass\n    robot.log(1)\nmain()\n', 8, 'run', 1],
  ['after_try_sys_exit_except_systemexit', 'import robot\nimport sys\ndef main():\n    try:\n        sys.exit(0)\n    except SystemExit:\n        pass\n    robot.log(1)\nmain()\n', 8, 'run', 1],
  ['after_try_sys_exit_bare_except', 'import robot\nimport sys\ndef main():\n    try:\n        sys.exit(0)\n    except:\n        pass\n    robot.log(1)\nmain()\n', 8, 'run', 1],
  ['for_header_body', 'import robot\nfor i in range(2):\n    robot.home()\n', 2, 'run'],
  ['two_space_inner', 'import robot\n\ndef main():\n  for i in range(3):\n    robot.home()\n  robot.log("x")\n\nmain()\n', 5, 'run'],
  ['two_space_header', 'import robot\n\ndef main():\n  for i in range(3):\n    robot.home()\n  robot.log("x")\n\nmain()\n', 4, 'run'],
  ['two_space_outer', 'import robot\n\ndef main():\n  for i in range(3):\n    robot.home()\n  robot.log("x")\n\nmain()\n', 6, 'run'],
  ['tabs_body', 'import robot\nif True:\n\trobot.home()\n\tif True:\n\t\trobot.log("a")\n', 5, 'run'],
  ['aligned_literals', 'import robot\na = [[0.1, 0.2],\n     [0.3, 0.4]]\nb = [[0.1, 0.2],\n     [0.3, 0.4]]\nif True:\n  robot.home()\n  robot.log("x")\n', 7, 'run'],
  ['aligned_literals_header', 'import robot\na = [[0.1, 0.2],\n     [0.3, 0.4]]\nif True:\n  robot.home()\n', 4, 'run'],
  ['mixed_four_space_body', 'import robot\ndef f():\n    robot.home()\n\nf()\nif True:\n  robot.log("a")\n', 3, 'run'],
  ['mixed_two_space_block', 'import robot\ndef f():\n    robot.home()\n\nf()\nif True:\n  robot.log("a")\n', 7, 'run'],
  ['multiline_statement_end', 'import robot\nrobot.log(\n    "x"\n)\n', 4, 'run'],
  ['multiline_if_end', 'import robot\nif (1 and\n        2):\n    robot.home()\n', 3, 'run'],
  ['else_header', 'import robot\nif 0:\n  robot.home()\nelse:\n  robot.log("y")\n', 4, 'run'],
  ['if_body_before_else', 'import robot\nif 1:\n  robot.home()\nelse:\n  robot.log("y")\n', 3, 'run'],
  ['elif_header', 'import robot\nx = 2\nif x == 1:\n    robot.home()\nelif x == 2:\n    robot.log("a")\n', 5, 'run'],
  ['after_loop_with_break', 'import robot\ndef f():\n    while True:\n        robot.home()\n        break\n    # hier\nf()\n', 6, 'run'],
  ['after_while_true_break_top', 'import robot\nrobot.home()\nn = 0\nwhile True:\n    n += 1\n    if n > 3:\n        break\n\n', 8, 'run'],
  ['main_guard_body', 'import robot\n\ndef main():\n    robot.home()\n\nif __name__ == "__main__":\n    main()\n', 7, 'run'],
  ['main_guard_crlf_comment', 'import robot\r\n\r\ndef main():\r\n    robot.home()\r\n\r\nif __name__ == "__main__":  # Start\r\n    main()\r\n', 6, 'run'],
  ['crlf_body', 'import robot\r\nif True:\r\n    robot.home()\r\n', 3, 'run'],
  ['before_return_in_main', 'import robot, sys\n\ndef main():\n    robot.home()\n    return 0\n\nif __name__ == "__main__":\n    sys.exit(main())\n', 4, 'run'],
  ['blank_line_in_block', 'import robot\ndef f():\n    robot.home()\n    \n    robot.log("x")\nf()\n', 4, 'run'],
  ['comment_line_in_block', 'import robot\ndef f():\n    robot.home()\n    # Kommentar\n    robot.log("x")\nf()\n', 4, 'run'],
  ['class_body', 'import robot\nclass A:\n    x = 1\nrobot.home()\n', 2, 'run'],
  ['try_body', 'import robot\ntry:\n    robot.home()\nexcept Exception:\n    pass\n', 3, 'run'],
  ['with_body', 'import robot\nimport contextlib\nwith contextlib.nullcontext():\n    robot.home()\n', 4, 'run'],
  ['match_case_body', 'import robot\nx = 1\nmatch x:\n    case 1:\n        robot.home()\n    case _:\n        pass\n', 5, 'run'],
  ['async_main_body', 'import asyncio\nimport robot\n\nasync def main():\n    robot.home()\n\nasyncio.run(main())\n', 5, 'run'],
  ['decorated_def_body', 'import robot, functools\n\n@functools.lru_cache\ndef main():\n    robot.home()\n\nmain()\n', 5, 'run'],
  ['only_comments', '# nur ein Kommentar\n# noch einer\n', 2, 'run'],
  ['one_line_guard', 'import robot\n\ndef main():\n    robot.home()\n\nif __name__ == "__main__": main()\n', 6, 'run'],
  ['semicolon_line', 'import robot\n\ndef main():\n    robot.home(); robot.log("a")\n\nmain()\n', 4, 'run'],
  ['loop_else_body', 'import robot\nfor i in range(2):\n    robot.home()\nelse:\n    robot.log("x")\n', 5, 'run'],
  ['after_finite_while', 'import robot\nn = 0\nwhile n < 3:\n    n += 1\n\n', 5, 'run'],
  ['docstring_statement', '"""Mein Programm."""\nimport robot\nrobot.home()\n', 3, 'run'],
  ['future_then_code', '"""Doku."""\nfrom __future__ import annotations\nimport robot\nrobot.home()\n', 3, 'run'],
  // An EMPTY row (or a comment at column 0) inside a block chose nothing: it
  // takes the level the next statement needs (review round 4, MC1; 4-A's
  // P1…P37 and the stock_out program).
  ['empty_row_mid_function', 'import robot\n\ndef main():\n    robot.home()\n\n    robot.log("x")\n\nmain()\n', 5, 'run'],
  ['empty_row_mid_main_guard', 'import robot\n\nif __name__ == "__main__":\n    robot.home()\n\n    robot.log("x")\n', 5, 'run'],
  ['empty_row_mid_for_body', 'import robot\nfor i in range(1):\n    robot.home()\n\n    robot.log("x")\n', 4, 'run'],
  ['col0_comment_mid_function', 'import robot\ndef main():\n    robot.home()\n# Kommentar\n    robot.log("x")\nmain()\n', 4, 'run'],
  ['empty_row_tab_body', 'import robot\nif True:\n\trobot.home()\n\n\trobot.log("x")\n', 4, 'run'],
  ['empty_row_two_space_body', 'import robot\ndef main():\n  robot.home()\n\n  robot.log("x")\nmain()\n', 4, 'run'],
  ['empty_row_crlf_body', 'import robot\r\ndef main():\r\n    robot.home()\r\n\r\n    robot.log("x")\r\nmain()\r\n', 4, 'run'],
  ['empty_row_if_body_then_more', 'import robot\nif True:\n    robot.home()\n\n    robot.log("x")\nrobot.log("y")\n', 4, 'run'],
  ['empty_row_nested_body', 'import robot\ndef main():\n    for i in range(1):\n        robot.home()\n\n        robot.log("x")\nmain()\n', 5, 'run'],
  ['empty_row_stock_program', 'import robot\ndef main():\n    robot.home()\n\n    robot.log(1)\nmain()\n', 4, 'run'],
  ['empty_row_after_inner_block', 'import robot\ndef main():\n    for i in range(1):\n        robot.home()\n\n    robot.log("x")\nmain()\n', 5, 'run'],
  // Two cases that used to be refused, now placed in the block they sit in:
  // the empty row before `else:` belongs to the if-body, and the empty row in
  // the middle of a body is that body.
  ['before_else_clause', 'import robot\nif 1:\n    robot.home()\n\nelse:\n    robot.log("x")\n', 4, 'run'],
  ['dedented_blank_mid_block', 'import robot\ndef f():\n    robot.home()\n\n    robot.log("x")\nf()\n', 4, 'run'],
  ['empty_row_end_of_file', 'import robot\n\ndef main():\n    robot.home()\n\nmain()\n\n', 7, 'run'],
  // A `with suppress(…)` may swallow what its body raises: the line after it
  // runs (review round 4, mc6 — 4-A's P22 asked below the raise, INSIDE the
  // with, where the line truly never runs).
  ['after_with_suppress_raise', 'import robot, contextlib\nwith contextlib.suppress(ValueError):\n    raise ValueError()\nrobot.home()\n', 4, 'run'],
  ['after_if_false_return', 'import robot\ndef f():\n    if False:\n        return 1\n    robot.home()\nf()\n', 5, 'run'],
  ['after_match_without_wildcard', 'import robot\ndef f(x):\n    match x:\n        case 1:\n            return 1\n    robot.home()\nf(2)\n', 6, 'run'],
  ['after_while_empty_string', 'import robot\nwhile "":\n    robot.home()\n\n', 4, 'run'],
  // ── invalid spots: nothing is inserted, and the reason is said ──
  ['no_cursor', 'import robot\nrobot.home()\n', null, 'click'],
  ['starter_comment_line', PY_STARTER, 1, 'lead'],
  ['starter_second_comment', PY_STARTER, 2, 'lead'],
  ['docstring_then_future', '"""Mein Programm."""\nfrom __future__ import annotations\nimport robot\nrobot.home()\n', 1, 'lead'],
  ['comment_then_future', '# Kopf\nfrom __future__ import annotations\nimport robot\nrobot.home()\n', 1, 'lead'],
  ['between_imports', 'import robot\nimport sys\nrobot.home()\n', 1, 'lead'],
  ['def_docstring', 'import robot\ndef f():\n    """Doku."""\n    robot.home()\nf()\n', 2, 'doc'],
  ['class_docstring', 'import robot\nclass A:\n    """Doku."""\n    x = 1\nrobot.home()\n', 2, 'doc'],
  ['decorator_line', 'import robot\nimport functools\n@functools.cache\ndef f():\n    return 1\nf()\n', 3, 'deco'],
  ['match_header', 'import robot\nx = 1\nmatch x:\n    case 1:\n        robot.home()\n', 3, 'match'],
  ['case_header', 'import robot\nx = 1\nmatch x:\n    case 1:\n        robot.home()\n', 4, 'match'],
  ['between_cases', 'import robot\nx = 1\nmatch x:\n    case 1:\n        robot.home()\n    \n    case 2:\n        pass\n', 6, 'match'],
  ['after_last_case_at_case_level', 'import robot\nx = 1\nmatch x:\n    case 1:\n        robot.home()\n    \nrobot.log("a")\n', 6, 'match'],
  ['inside_list', 'import robot\npunkte = [\n    1,\n    2,\n]\n', 3, 'inside'],
  ['list_opener_line', 'import robot\nd = {\n    "a": 1,\n}\n', 2, 'inside'],
  ['backslash_line', 'import robot\nx = 1 + \\\n    2\nrobot.home()\n', 2, 'inside'],
  ['crlf_backslash_line', 'import robot\r\nx = 1 + \\\r\n    2\r\nrobot.home()\r\n', 2, 'inside'],
  ['lambda_multiline', 'import robot\nf = (lambda a:\n     a + 1)\nrobot.home()\n', 2, 'inside'],
  ['docstring_inside', 'import robot\ndef f():\n    """Doc\n    more"""\n    return 1\n', 3, 'inside'],
  ['multiline_if_first_row', 'import robot\nif (1 and\n        2):\n    robot.home()\n', 2, 'inside'],
  ['after_return', 'import robot\ndef f():\n    robot.home()\n    return 1\nf()\n', 4, 'return'],
  ['after_raise', 'import robot\ndef f():\n    robot.home()\n    raise ValueError(1)\ntry:\n    f()\nexcept ValueError:\n    pass\n', 4, 'raise'],
  ['after_break', 'import robot\nfor i in range(3):\n    robot.home()\n    break\n', 4, 'break'],
  ['after_continue', 'import robot\nfor i in range(3):\n    robot.home()\n    continue\n', 4, 'continue'],
  ['after_semicolon_return', 'import robot\ndef f():\n    robot.home(); return 1\nf()\n', 3, 'return'],
  ['after_sys_exit_main', 'import robot, sys\n\ndef main():\n    robot.home()\n    return 0\n\nif __name__ == "__main__":\n    sys.exit(main())\n', 8, 'exit'],
  ['after_exit', 'import robot\nrobot.home()\nexit()\n', 3, 'exit'],
  ['after_quit', 'import robot\nrobot.home()\nquit()\n', 3, 'exit'],
  ['after_os_exit', 'import robot, os\nrobot.home()\nos._exit(0)\n', 3, 'exit'],
  ['after_raise_systemexit', 'import robot\nrobot.home()\nraise SystemExit(0)\n', 3, 'raise'],
  ['after_while_true', 'import robot\nrobot.home()\nwhile True:\n    robot.log("x")\n\n', 5, 'loop'],
  ['after_while_not_false', 'import robot\nrobot.home()\nwhile not False:\n    robot.log("x")\n\n', 5, 'loop'],
  ['after_while_1_eq_1', 'import robot\nrobot.home()\nwhile 1 == 1:\n    robot.log("x")\n\n', 5, 'loop'],
  ['after_while_one_liner', 'import robot\nrobot.home()\nwhile True: robot.log("x")\n', 3, 'loop'],
  ['after_while_true_in_func', 'import robot\ndef main():\n    robot.home()\n    while True:\n        robot.log("x")\n    # danach\nmain()\n', 6, 'loop'],
  ['else_of_endless_loop', 'import robot\nwhile True:\n    robot.home()\nelse:\n    robot.log("x")\n', 5, 'loop'],
  ['after_if_else_returns', 'import robot\n\ndef main():\n    robot.home()\n    if robot.sees("wuerfel"):\n        return\n    else:\n        return\n    # danach\n\nmain()\n', 9, 'ifelse'],
  ['after_try_finally_returns', 'import robot\ndef main():\n    try:\n        robot.home()\n    finally:\n        return\n    # danach\nmain()\n', 7, 'try'],
  ['explicit_level_before_else', 'import robot\ndef f():\n    if 1:\n        robot.home()\n    \n    else:\n        robot.log("x")\nf()\n', 5, 'clause'],
  // Review round 5, md4 (was refused as „Einrückung passt nicht"): 4 spaces
  // between two 8-space statements are no step back — the next statement
  // stands deeper — so the line takes the level that statement needs.
  ['explicit_level_too_shallow', 'import robot\ndef f():\n    for i in range(1):\n        robot.home()\n    \n        robot.log("x")\nf()\n', 5, 'run', 1],
  ['inside_with_suppress_after_raise', 'import robot, contextlib\nwith contextlib.suppress(ValueError):\n    raise ValueError()\nrobot.home()\n', 3, 'raise'],
  ['after_with_nullcontext_raise', 'import robot\nimport contextlib\ndef f():\n    with contextlib.nullcontext():\n        raise ValueError()\n    # danach\ntry:\n    f()\nexcept ValueError:\n    pass\n', 6, 'raise'],
  // review round 4, mc4: a try without a handler whose body never completes.
  ['after_try_finally_body_returns', 'import robot\ndef f():\n    try:\n        return 1\n    finally:\n        pass\n    # danach\nf()\n', 7, 'try'],
  ['after_try_endless_finally', 'import robot\ntry:\n    while True:\n        robot.home()\nfinally:\n    robot.log("x")\n\n', 7, 'try'],
  ['after_try_endless_finally_in_func', 'import robot\ndef main():\n    try:\n        while True:\n            robot.home()\n    finally:\n        robot.log("x")\n    # danach\nmain()\n', 8, 'try'],
  // review round 4, mc6: the remaining never-runs holes.
  ['after_while_string_constant', 'import robot\nwhile "x":\n    robot.home()\n\n', 4, 'loop'],
  ['after_if_true_return', 'import robot\ndef f():\n    if True:\n        return 1\n    # danach\nf()\n', 5, 'ifalways'],
  ['after_match_all_return', 'import robot\ndef f(x):\n    match x:\n        case 1:\n            return 1\n        case _:\n            return 2\n    # danach\nf(1)\n', 8, 'matchEnds'],
  ['after_os_abort', 'import robot, os\nrobot.home()\nos.abort()\n', 3, 'exit'],
  // review round 5, md1 / nd1: nothing between a decorator and its def, and
  // nothing above a def's docstring — a blank or comment row included.
  ['blank_row_after_decorator', 'import robot\ndef deko(f):\n    return f\n@deko\n\ndef main():\n    robot.home()\nmain()\n', 5, 'deco'],
  ['comment_row_after_decorator', 'import robot\ndef deko(f):\n    return f\n@deko\n# Notiz\ndef main():\n    robot.home()\nmain()\n', 5, 'deco'],
  ['blank_row_before_def_docstring', 'import robot\ndef main():\n\n    """Doku."""\n    robot.home()\nmain()\n', 3, 'doc'],
  ['blank_row_before_class_docstring', 'import robot\nclass A:\n\n    """Doku."""\n    x = 1\nrobot.home()\n', 3, 'doc'],
  // review round 5, md3: a suppress swallows only EXCEPTIONS; a handler runs
  // only when something is raised — SystemExit is no Exception.
  ['after_with_suppress_return', 'import robot\nfrom contextlib import suppress\ndef main():\n    with suppress(Exception):\n        return\n    robot.log(1)\nmain()\n', 6, 'return'],
  ['after_with_suppress_continue', 'import robot\nfrom contextlib import suppress\ndef main():\n    for i in range(2):\n        with suppress(Exception):\n            continue\n        robot.log(1)\nmain()\n', 7, 'continue'],
  ['after_with_suppress_break', 'import robot\nfrom contextlib import suppress\ndef main():\n    for i in range(2):\n        with suppress(Exception):\n            break\n        robot.log(1)\nmain()\n', 7, 'break'],
  ['after_try_return_except_pass', 'import robot\ndef main():\n    try:\n        return\n    except Exception:\n        pass\n    robot.log(1)\nmain()\n', 7, 'try'],
  ['after_try_sys_exit_except_exception', 'import robot\nimport sys\ndef main():\n    try:\n        sys.exit(0)\n    except Exception:\n        pass\n    robot.log(1)\nmain()\n', 8, 'try'],
  ['after_try_sys_exit_except_value_error', 'import robot\nimport sys\ndef main():\n    try:\n        sys.exit(0)\n    except (ValueError, KeyError) as e:\n        pass\n    robot.log(1)\nmain()\n', 8, 'try'],
  ['eof_row_after_while_true', 'import robot\nwhile True:\n    robot.home()\n', 4, 'loop'],
  ['inconsistent_file', 'import robot\nif True:\n    robot.home()\n  robot.log("x")\n', 3, 'indentBroken'],
  ['unclosed_bracket', 'import robot\nrobot.home()\nx = (\n', 2, 'unclosed'],
  ['unclosed_triple_string', 'import robot\nrobot.home()\n"""\noffen\n', 2, 'unclosed'],
  ['fstring_multiline', 'import robot\nx = f"{\n1}"\nrobot.home()\n', 2, 'unreadable'],
];

const JAVA = [
  // ── valid spots ──
  ['starter_no_cursor', JAVA_STARTER, null, 'click'],
  ['starter_body', JAVA_STARTER, 7, 'run'],
  ['starter_main_header', JAVA_STARTER, 6, 'run'],
  ['starter_last_statement', JAVA_STARTER, 8, 'run'],
  ['two_space_body', 'import edubotics.Robot;\npublic class Main {\n  public static void main(String[] args) {\n    Robot.home();\n  }\n}\n', 4, 'run'],
  ['mixed_units_loop_body', 'import edubotics.Robot;\n\npublic class Main {\n    public static void main(String[] args) {\n        Robot.home();\n        for (int i = 0; i < 3; i++) {\n          Robot.log("x");\n        }\n    }\n}\n', 7, 'run'],
  ['after_finite_for', J('        for (int i = 0; i < 3; i++) {\n            Robot.home();\n        }\n'), 6, 'run'],
  ['after_for_limit_final', J('        Robot.home();\n        for (int i = 0; i < ANZAHL; i++) {\n            Robot.beep();\n        }\n', '', '    static final int ANZAHL = 3;\n'), 7, 'run'],
  ['after_for_args_length', J('        Robot.home();\n        for (int i = 0; i < args.length; i++) {\n            Robot.beep();\n        }\n'), 7, 'run'],
  ['after_while_counter_max', J('        int n = 0;\n        while (n < MAX) {\n            n++;\n        }\n', '', '    static final int MAX = 3;\n'), 7, 'run'],
  ['after_while_true_break', J('        int n = 0;\n        while (true) {\n            n++;\n            if (n > 3) break;\n        }\n'), 8, 'run'],
  ['after_labelled_break_inner', J('        aussen:\n        while (true) {\n            while (true) {\n                break aussen;\n            }\n        }\n'), 9, 'run'],
  ['after_label_same_line', J('        aussen: for (;;) { for (;;) { break aussen; } }\n'), 4, 'run'],
  ['after_label_break_in_switch', J('        int x = 1;\n        aussen:\n        while (true) {\n            switch (x) {\n                case 1: break aussen;\n                default: break;\n            }\n        }\n'), 11, 'run'],
  ['after_do_while_counter', J('        int i = 0;\n        do {\n            i++;\n        } while (i < 3);\n'), 7, 'run'],
  ['after_switch_no_default', J('        int x = 1;\n        switch (x) {\n            case 1: Robot.home(); break;\n        }\n'), 7, 'run'],
  ['in_case_group', J('        int x = 1;\n        switch (x) {\n            case 1:\n                Robot.home();\n                break;\n            default:\n                Robot.beep();\n        }\n'), 7, 'run'],
  ['after_try_catch', J('        try {\n            Robot.home();\n        } catch (RuntimeException e) {\n            Robot.beep();\n        }\n'), 8, 'run'],
  ['try_block_body', J('        try {\n            Robot.home();\n        } finally {\n            Robot.beep();\n        }\n'), 5, 'run'],
  ['if_block_body', J('        if (args.length == 0) {\n            Robot.home();\n        }\n'), 5, 'run'],
  ['else_block_header', J('        if (args.length > 0) {\n            Robot.home();\n        } else {\n            Robot.beep();\n        }\n'), 6, 'run'],
  ['lambda_body', J('        Runnable r = () -> {\n            Robot.home();\n        };\n        r.run();\n'), 5, 'run'],
  ['anonymous_method_body', J('        Runnable r = new Runnable() {\n            public void run() {\n                Robot.home();\n            }\n        };\n        r.run();\n'), 6, 'run'],
  ['after_anonymous_generic', J('        java.util.Comparator<Integer> c = new java.util.Comparator<Integer>() {\n            public int compare(Integer a, Integer b) { return a - b; }\n        };\n        Robot.home();\n'), 7, 'run'],
  ['after_text_block', J('        String s = """\n            } { main( "\n            """;\n        Robot.home();\n'), 7, 'run'],
  ['after_char_brace', J("        char c = '{';\n        char d = '\\'';\n        Robot.home();\n"), 6, 'run'],
  ['after_local_record', J('        record P(int x) { }\n        Robot.home();\n'), 4, 'run'],
  ['after_switch_expression', J('        int x = 1;\n        int y = switch (x) {\n            case 1 -> { yield 2; }\n            default -> 3;\n        };\n        Robot.home();\n'), 9, 'run'],
  ['labelled_block_body', J('        block: {\n            Robot.home();\n            if (args.length == 0) break block;\n            return;\n        }\n'), 5, 'run'],
  ['after_labelled_block', J('        block: {\n            Robot.home();\n            if (args.length == 0) break block;\n            return;\n        }\n'), 8, 'run'],
  ['crlf_body', J('        Robot.home();\n').replace(/\n/g, '\r\n'), 4, 'run'],
  ['after_comment_line', J('        Robot.home(); // Start\n        // nächster Schritt\n'), 5, 'run'],
  ['record_main', 'import edubotics.Robot;\npublic record Main(int x) {\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 4, 'run'],
  ['enum_main', 'import edubotics.Robot;\npublic enum Main {\n    A, B;\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 5, 'run'],
  ['interface_main', 'import edubotics.Robot;\npublic interface Main {\n    static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 4, 'run'],
  ['after_final_local_non_constant', J('        final boolean lauf = args.length > 5;\n        while (lauf) {\n            Robot.home();\n        }\n'), 7, 'run'],
  // An empty row keeps the sibling level (review round 4: 4-A's E12, J8).
  ['empty_row_mid_body', J('        Robot.home();\n\n        Robot.beep();\n'), 5, 'run'],
  // A switch opened and closed on its row is a whole statement (mc6).
  ['after_one_line_switch_expression', J('        int x = 1;\n        int y = switch (x) { case 1 -> 2; default -> 3; };\n        Robot.home();\n'), 5, 'run'],
  ['after_one_line_switch_statement', J('        int x = 1;\n        switch (x) { case 1 -> Robot.home(); default -> Robot.beep(); }\n'), 5, 'run'],
  ['after_one_line_colon_switch', J('        int x = 1;\n        switch (x) { case 1: Robot.home(); break; default: break; }\n'), 5, 'run'],
  // A constant from another file stays unknown, whatever reads it (mc5).
  ['after_return_constant_elsewhere', J('        int n = 0;\n        while (n < 3) {\n            n++;\n        }\n', '', '    static boolean pruefe() { return LAUF; }\n    static boolean LAUF = false;\n'), 7, 'run'],
  // Review round 5, md2: Java's own arithmetic — `7 / 2` is 3.
  ['after_for_integer_division_limit', J('        for (int i = 0; i < 7 / 2; i++) {\n            Robot.home();\n        }\n'), 6, 'run'],
  // …and a boxed type is never a constant variable (JLS §4.12.4).
  ['after_while_boxed_final_false', J('        while (LAUF) {\n            Robot.home();\n        }\n', '', '    static final Boolean LAUF = false;\n'), 6, 'run'],
  // …and a long constant is long arithmetic: `I + 1` does not overflow.
  ['after_do_long_constant', J('        do {\n            Robot.home();\n        } while (I + 1 < 0);\n', '', '    static final long I = 2147483647;\n'), 6, 'run'],
  // Review round 5, md5: a lambda's parameter shadows the constant field …
  ['lambda_param_shadows_constant', 'import edubotics.Robot;\nimport java.util.function.Consumer;\n\npublic class Main {\n    static final boolean LAUF = true;\n    public static void main(String[] args) {\n        Consumer<Boolean> c = (LAUF) -> {\n            while (LAUF) {\n                Robot.home();\n            }\n        };\n        c.accept(false);\n    }\n}\n', 10, 'run'],
  ['lambda_bare_param_shadows_constant', 'import edubotics.Robot;\nimport java.util.function.Consumer;\n\npublic class Main {\n    static final boolean LAUF = true;\n    public static void main(String[] args) {\n        Consumer<Boolean> c = LAUF -> {\n            while (LAUF) {\n                Robot.home();\n            }\n        };\n        c.accept(false);\n    }\n}\n', 10, 'run'],
  // … and a nested `continue` continues the do-while.
  ['after_do_nested_continue_return', J('        int i = 0;\n        do {\n            i++;\n            if (i < 3) continue;\n            return;\n        } while (i < 2);\n'), 9, 'run'],
  // ── invalid spots ──
  ['no_cursor', J('        Robot.home();\n'), null, 'click'],
  ['starter_comment_line', JAVA_STARTER, 1, 'method'],
  ['import_line', JAVA_STARTER, 3, 'method'],
  ['class_header', JAVA_STARTER, 5, 'method'],
  ['main_closing_brace', JAVA_STARTER, 9, 'method'],
  ['class_closing_brace', JAVA_STARTER, 10, 'method'],
  ['field_line', 'import edubotics.Robot;\npublic class Main {\n    static int zaehler = 0;\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 3, 'method'],
  ['between_methods', 'import edubotics.Robot;\npublic class Main {\n    static void hilfe() {\n    }\n\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 5, 'method'],
  ['one_line_main', 'import edubotics.Robot;\npublic class Main {\n    public static void main(String[] args) { Robot.home(); }\n}\n', 3, 'method'],
  ['enum_body_line', 'import edubotics.Robot;\npublic class Main {\n    enum Farbe { ROT, GRUEN }\n    public static void main(String[] args) {\n        Robot.home();\n    }\n}\n', 3, 'method'],
  ['anonymous_generic_field', J('        java.util.Comparator<Integer> c = new java.util.Comparator<Integer>() {\n            int zaehler = 0;\n\n            public int compare(Integer a, Integer b) { return a - b; }\n        };\n'), 6, 'method'],
  ['anonymous_field', J('        Runnable r = new Runnable() {\n            int zaehler = 0;\n\n            public void run() { Robot.home(); }\n        };\n        r.run();\n'), 5, 'method'],
  ['local_class_member_line', J('        class Hilfe {\n            int x = 1;\n\n            void f() { Robot.home(); }\n        }\n        new Hilfe().f();\n'), 5, 'method'],
  ['switch_header', J('        int x = 1;\n        switch (x) {\n            case 1: Robot.home(); break;\n        }\n'), 5, 'switch'],
  ['case_label_line', J('        int x = 1;\n        switch (x) {\n            case 1:\n                Robot.home();\n                break;\n        }\n'), 6, 'switch'],
  ['arrow_case_line', J('        int x = 1;\n        switch (x) {\n            case 1 -> Robot.home();\n            default -> Robot.beep();\n        }\n'), 7, 'switch'],
  ['default_label_line', J('        int x = 1;\n        switch (x) {\n            case 1: Robot.home(); break;\n            default:\n                Robot.beep();\n        }\n'), 7, 'switch'],
  ['blank_in_arrow_switch', J('        int x = 1;\n        switch (x) {\n            case 1 -> Robot.home();\n\n            default -> Robot.beep();\n        }\n'), 7, 'switch'],
  ['after_return', J('        Robot.home();\n        return;\n'), 5, 'return'],
  ['after_throw', J('        Robot.home();\n        throw new RuntimeException("x");\n'), 5, 'throw'],
  ['after_system_exit', J('        Robot.home();\n        System.exit(0);\n'), 5, 'exit'],
  ['after_runtime_halt', J('        Robot.home();\n        Runtime.getRuntime().halt(0);\n'), 5, 'exit'],
  ['after_runtime_exit', J('        Robot.home();\n        Runtime.getRuntime().exit(0);\n'), 5, 'exit'],
  ['switch_expression_header', J('        int x = 1;\n        int y = switch (x) {\n            case 1 -> 2;\n            default -> 3;\n        };\n'), 5, 'switch'],
  ['after_break_in_loop', J('        for (int i = 0; i < 3; i++) {\n            Robot.home();\n            break;\n        }\n'), 6, 'break'],
  ['after_continue_in_loop', J('        for (int i = 0; i < 3; i++) {\n            Robot.home();\n            continue;\n        }\n'), 6, 'continue'],
  ['after_while_true', J('        Robot.home();\n        while (true) {\n            Robot.log("x");\n        }\n'), 7, 'loop'],
  ['after_for_ever', J('        for (;;) { Robot.home(); }\n'), 4, 'loop'],
  ['after_do_while_true', J('        do {\n            Robot.home();\n        } while (true);\n'), 6, 'loop'],
  ['after_while_constant', J('        while (1 == 1) { Robot.home(); }\n'), 4, 'loop'],
  ['after_while_final_local', J('        final boolean lauf = true;\n        while (lauf) {\n            Robot.home();\n        }\n'), 7, 'loop'],
  ['after_interface_constant', 'import edubotics.Robot;\ninterface Einstellungen {\n    boolean LAUF = true;\n}\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 9, 'loop'],
  ['after_static_final_constant', J('        while (MAX > 0) {\n            Robot.home();\n        }\n', '', '    static final int MAX = 3;\n'), 6, 'loop'],
  ['after_if_else_returns', J('        if (args.length == 0) { return; } else { return; }\n'), 4, 'ifelse'],
  ['after_if_else_if_returns', J('        int x = 1;\n        if (x == 1) return; else if (x == 2) return; else return;\n'), 5, 'ifelse'],
  ['after_try_finally_return', J('        try {\n            Robot.home();\n        } finally {\n            return;\n        }\n'), 8, 'return'],
  ['after_try_catch_both_return', J('        try {\n            Robot.home();\n            return;\n        } catch (RuntimeException e) {\n            return;\n        }\n'), 9, 'try'],
  ['after_switch_all_return', J('        int x = 1;\n        switch (x) {\n            case 1: return;\n            default: return;\n        }\n'), 8, 'switchEnds'],
  ['after_sync_return', J('        synchronized (Main.class) {\n            Robot.home();\n            return;\n        }\n'), 7, 'return'],
  ['in_call_arguments', J('        Robot.log(\n            "x");\n'), 4, 'inside'],
  ['in_multiline_expression', J('        int x = 1 +\n            2;\n        Robot.home();\n'), 4, 'inside'],
  ['in_text_block', J('        String s = """\n            text\n            """;\n        Robot.home();\n'), 5, 'inside'],
  ['in_block_comment', J('        /* ein\n           Kommentar */\n        Robot.home();\n'), 4, 'inside'],
  ['unsure_foreign_constant', J('        while (Konstanten.LAUF) {\n            Robot.home();\n        }\n'), 6, 'unsure'],
  // review round 4, mc5: `return LAUF;` / `case STUFE:` declare nothing — the
  // constant is another file's, so the loop may never end (4-A's K21, K21c;
  // K21b without them stays „unsicher" too).
  ['unsure_constant_read_by_return', 'import edubotics.Robot;\npublic class Main implements Einstellungen {\n    static boolean pruefe() { return LAUF; }\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 7, 'unsure'],
  ['unsure_constant_read_by_case', 'import edubotics.Robot;\npublic class Main implements Einstellungen {\n    static int f(int x) { switch (x) { case STUFE: return 1; default: return 0; } }\n    public static void main(String[] args) {\n        while (STUFE > 0) {\n            Robot.home();\n        }\n    }\n}\n', 7, 'unsure'],
  ['unsure_constant_case_label_same_switch', 'import edubotics.Robot;\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        switch (args.length) {\n            case STUFE:\n                while (STUFE > 0) {\n                    Robot.home();\n                }\n                break;\n        }\n    }\n}\n', 8, 'unsure'],
  // …nor does a same-named variable of ANOTHER method (javac rejected the
  // line as unreachable: the loop reads Einstellungen.LAUF).
  ['unsure_constant_param_elsewhere', 'import edubotics.Robot;\npublic class Main implements Einstellungen {\n    static boolean f(boolean LAUF) { return LAUF; }\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 7, 'unsure'],
  ['unsure_constant_local_elsewhere', 'import edubotics.Robot;\npublic class Main implements Einstellungen {\n    static void g() {\n        boolean LAUF = false;\n    }\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 9, 'unsure'],
  ['unsure_constant_elsewhere', 'import edubotics.Robot;\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 6, 'unsure'],
  // Review round 5, md2: a constant condition in Java's own arithmetic —
  // integer division, int overflow, a long constant, the conditional operator.
  ['after_while_integer_division_const', J('        while (1 / 2 == 0) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_int_overflow_const', J('        while (2147483647 + 1 < 0) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_ternary_const', J('        while (true ? true : false) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_long_constant', J('        while (GROSS + 1 > 0) {\n            Robot.home();\n        }\n', '', '    static final long GROSS = 2147483647;\n'), 6, 'loop'],
  ['after_while_octal_const', J('        while (010 == 8) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_binary_const', J('        while (0b101 == 5) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_int_min_minus_one', J('        while (-2147483648 - 1 > 0) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_long_overflow_const', J('        while (9223372036854775807L + 1 < 0) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  // An overflow is kept through the next operation: (MAX + 1) / 2 is negative.
  ['after_while_overflow_then_divide', J('        while ((2147483647 + 1) / 2 < 0) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_min_divided_by_minus_one', J('        while (-2147483648 / -1 < 0) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_ternary_false_branch', J('        while (1 > 2 ? false : true) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_double_infinity', J('        while (1.0 / 0 > 1e308) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_float_sum', J('        while (0.1f + 0.2f == 0.3f) {\n            Robot.home();\n        }\n'), 6, 'loop'],
  ['after_while_byte_constant', J('        while (K * 2 == 6) {\n            Robot.home();\n        }\n', '', '    static final byte K = 3;\n'), 6, 'loop'],
  ['after_do_int_constant_overflow', J('        do {\n            Robot.home();\n        } while (I + 1 < 0);\n', '', '    static final int I = 2147483647;\n'), 6, 'loop'],
  // Review round 5, MD4: a name means the declaration VISIBLE at the loop —
  // the lookup is cached per position, a local's scope starts at itself, a
  // field's is its class body. Same file (the constant is Einstellungen's) …
  ['cache_two_scopes', 'import edubotics.Robot;\n\ninterface Einstellungen {\n    boolean LAUF = true;\n}\n\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        {\n            boolean LAUF = false;\n            while (LAUF) {\n                Robot.home();\n            }\n        }\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 17, 'loop'],
  ['local_declared_after_loop', 'import edubotics.Robot;\n\ninterface Einstellungen {\n    boolean LAUF = true;\n}\n\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n        boolean LAUF = false;\n    }\n}\n', 11, 'loop'],
  ['field_of_other_class', 'import edubotics.Robot;\n\ninterface Einstellungen {\n    boolean LAUF = true;\n}\n\nclass Helfer {\n    static boolean LAUF = false;\n}\n\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 15, 'loop'],
  // … and review 5-A's own shape: the constant in another file stays unknown.
  ['unsure_cache_two_scopes', 'import edubotics.Robot;\n\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        {\n            boolean LAUF = false;\n            while (LAUF) {\n                Robot.home();\n            }\n        }\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 13, 'unsure'],
  ['unsure_local_declared_after_loop', 'import edubotics.Robot;\n\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n        boolean LAUF = false;\n    }\n}\n', 7, 'unsure'],
  ['unsure_field_of_other_class', 'import edubotics.Robot;\n\nclass Helfer {\n    static boolean LAUF = false;\n}\n\npublic class Main implements Einstellungen {\n    public static void main(String[] args) {\n        while (LAUF) {\n            Robot.home();\n        }\n    }\n}\n', 11, 'unsure'],
  // The empty row after the class's final line break is outside every method.
  ['eof_row_after_class', JAVA_STARTER, 11, 'method'],
  ['unbalanced_braces', 'import edubotics.Robot;\npublic class Main {\n    public static void main(String[] args) {\n        Robot.home();\n', 4, 'unclosed'],
  ['unclosed_string', J('        String s = "offen;\n        Robot.home();\n'), 5, 'unreadable'],
];

// The refusals that claim the line could NEVER run there.
const NEVER_HINT_KEYS = ['return', 'raise', 'throw', 'break', 'continue', 'exit', 'loop', 'ifelse',
  'ifalways', 'try', 'matchEnds', 'switchEnds'];
const NEVER_HINTS = new Set(NEVER_HINT_KEYS.map((k) => H[k]));

// Where row `line` (1-based) of `text` ends, before its line break.
function rowEnd(text, line) {
  const starts = [0];
  for (let i = 0; i < text.length; i += 1) if (text[i] === '\n') starts.push(i + 1);
  const row = Math.min(Math.max(line - 1, 0), starts.length - 1);
  let end = row + 1 < starts.length ? starts[row + 1] - 1 : text.length;
  if (end > starts[row] && text[end - 1] === '\r') end -= 1;
  return end;
}

function compute() {
  const cases = [];
  for (const [language, table, file] of [['python', PYTHON, 'main.py'], ['java', JAVA, 'Main.java']]) {
    const lines = snippetLines({ kind: 'recording', name: 'Winken' }, language);
    for (const [name, input, line, expect, runs] of table) {
      const cursor = line === null ? null : { file, line };
      const target = insertionTarget({ [file]: input }, language, cursor);
      const out = target.notFound ? null : insertAtTarget(input, target, lines, language).content;
      // A refusal that says „this line would never run here" is checked by
      // the real tools too (review round 5): the SHADOW is the line put there
      // anyway, with Enter's indentation — CPython must never run its marker,
      // javac must call it unreachable or never run it.
      let shadow;
      if (target.notFound && NEVER_HINTS.has(target.hint)) {
        const at = rowEnd(input, line);
        const indent = newlineIndentAt(input, at, language);
        if (indent !== null) shadow = insertAtTarget(input, { mode: 'after', at, indent }, lines, language).content;
      }
      cases.push({
        name,
        language,
        line,
        reach: expect === 'run' ? 'run' : 'hint',
        input,
        output: out,
        hint: target.notFound ? target.hint : null,
        ...(runs !== undefined ? { runs } : {}),
        ...(shadow !== undefined ? { shadow } : {}),
      });
    }
  }
  return {
    _generated_by: 'physical_ai_manager/src/components/Workshop/code/__tests__/codeInsert.cases.test.js '
      + '(EDUBOTICS_REGEN_CODE_CASES=1 to regenerate)',
    _checked_by: 'robotis_ai_setup/tests/test_code_insert_cases.py (CPython ast/compile + a run, javac 21 + a run)',
    marker: { python: 'robot.replay("Winken")', java: 'Robot.replay("Winken");' },
    cases,
  };
}

const EXPECTED = new Map([...PYTHON.map((c) => [`python:${c[0]}`, c]), ...JAVA.map((c) => [`java:${c[0]}`, c])]);

describe('insertion at the spot the student chose (owner decision R3-O4)', () => {
  it('match the fixture the compile-and-run test judges', () => {
    const now = compute();
    if (REGEN) {
      fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
      fs.writeFileSync(FIXTURE, `${JSON.stringify(now, null, 1)}\n`);
    }
    const saved = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(now).toEqual(saved);
  });

  it('every case is an insertion directly below the cursor line — or its German reason, and nothing else', () => {
    const { cases } = compute();
    const wrong = [];
    for (const c of cases) {
      const [, , line, want] = EXPECTED.get(`${c.language}:${c.name}`);
      if (want !== 'run') {
        if (c.output !== null || c.hint !== H[want]) wrong.push([c.language, c.name, want, c.hint, c.output]);
        // Every „never runs" refusal carries its shadow for the judge.
        if (NEVER_HINT_KEYS.includes(want) && typeof c.shadow !== 'string') {
          wrong.push([c.language, c.name, 'no shadow']);
        }
        continue;
      }
      if (c.output === null) {
        wrong.push([c.language, c.name, 'run', c.hint]);
        continue;
      }
      const marker = c.language === 'java' ? 'Robot.replay("Winken");' : 'robot.replay("Winken")';
      const rows = c.output.split(/\r?\n/);
      const markerRows = rows.map((r, i) => (r.trim() === marker ? i : -1)).filter((i) => i >= 0);
      // Exactly one marker, on the line right below the cursor's (1-based
      // cursor line = the 0-based index of the row below it) …
      if (markerRows.length !== 1 || markerRows[0] !== (c.input === '' ? 0 : line)) {
        wrong.push([c.language, c.name, 'marker rows', markerRows]);
        continue;
      }
      // … and nothing but that one line was added.
      const without = rows.filter((_, i) => i !== markerRows[0]).join(c.input.includes('\r\n') ? '\r\n' : '\n');
      if (without !== c.input) wrong.push([c.language, c.name, 'changed', c.output]);
    }
    expect(wrong).toEqual([]);
  });
});
