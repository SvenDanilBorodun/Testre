"""What the code editor writes into a student's program is judged by the REAL
tools (review round 2, mi2/mi3 and MA1; round 3, R3-O4 and MB1): every
program in the React fixtures is parsed and compiled by CPython and compiled
by javac, and the inserted marker line is checked to be reachable —
statically (nothing before it in its block that never lets the next line
run: javac's own „unreachable statement" rule for Java) and by RUNNING the
program to it. A case either inserted exactly that (reach 'run') or
inserted NOTHING and carries its German reason (reach 'hint'): never a
broken insertion, never a dead one.

The fixtures are written by the vitest files that compute them
(`codeInsert.cases.test.js`, `CodeEditor.indent.test.jsx`), which compare
the React code against them — so a change to the insertion or to the
editor's indentation fails there until the fixture is regenerated, and the
regenerated programs are judged here.

Python is judged by the interpreter running this suite (CI: 3.12, the
runner's version). Java needs a JDK ≥ 17 (text blocks): `$EDUBOTICS_JAVAC`,
then `$JAVA_HOME_21_X64` (GitHub's runners), then `javac` on PATH; without
one the Java half is SKIPPED, and `EDUBOTICS_REQUIRE_JAVAC=1` makes that a
failure. Deliberately stdlib-only.
"""

import ast
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FIXTURES = (_REPO_ROOT / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'components'
             / 'Workshop' / 'code' / '__tests__' / 'fixtures')
_INSERT_CASES = _FIXTURES / 'code-insert-cases.json'
_INDENT_CASES = _FIXTURES / 'code-editor-indent-cases.json'

_FAKE_ROBOT_JAVA = '''package edubotics;

public final class Robot {
    private Robot() {
    }

    public static void home() {
    }

    public static void log(Object... a) {
    }

    public static void moveTo(Object... a) {
    }

    public static void openGripper() {
    }

    public static void closeGripper() {
    }

    public static void beep() {
    }

    public static void replay(String name) {
        if ("Winken".equals(name)) {
            System.out.println("MARKE ERREICHT");
            System.exit(0);
        }
    }
}
'''


def _load(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


# ── Python: static reachability (the rule the insertion itself follows) ─────

_FOLD_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_FOLD_CMPOPS = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
    ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b,
}


class _NotConstant(Exception):
    pass


def _fold(node):
    """The value of an expression made only of literals and operators (the
    interpreter's own semantics), else _NotConstant."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp):
        v = _fold(node.operand)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
    if isinstance(node, ast.BinOp) and type(node.op) in _FOLD_BINOPS:
        return _FOLD_BINOPS[type(node.op)](_fold(node.left), _fold(node.right))
    if isinstance(node, ast.BoolOp):
        values = [_fold(v) for v in node.values]
        out = values[0]
        for v in values[1:]:
            out = (out and v) if isinstance(node.op, ast.And) else (out or v)
        return out
    if isinstance(node, ast.Compare) and all(type(o) in _FOLD_CMPOPS for o in node.ops):
        left = _fold(node.left)
        for op, right_node in zip(node.ops, node.comparators):
            right = _fold(right_node)
            if not _FOLD_CMPOPS[type(op)](left, right):
                return False
            left = right
        return True
    raise _NotConstant()


def _constant_true(test) -> bool:
    try:
        return bool(_fold(test))
    except Exception:  # noqa: BLE001 — anything not a folded literal is not constant
        return False


def _breaks_out(loop) -> bool:
    """A `break` in `loop`'s body that leaves THIS loop (not an inner loop's,
    and not one inside a nested def/class/lambda; an inner loop's `else:`
    breaks the outer one)."""
    stack = list(loop.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Break):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            stack.extend(node.orelse)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def _is_exit_call(stmt) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    func = stmt.value.func
    if isinstance(func, ast.Name):
        return func.id in ('exit', 'quit')
    return (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and (func.value.id, func.attr) in (('sys', 'exit'), ('os', '_exit')))


def _endless_loop(stmt) -> bool:
    return isinstance(stmt, ast.While) and _constant_true(stmt.test) and not _breaks_out(stmt)


def _never_falls_through(stmt) -> bool:
    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return True
    if _is_exit_call(stmt):
        return True
    if isinstance(stmt, (ast.While, ast.For, ast.AsyncFor)):
        if _endless_loop(stmt):
            return True
        return bool(stmt.orelse) and not _breaks_out(stmt) and _body_ends(stmt.orelse)
    if isinstance(stmt, ast.If):
        return bool(stmt.orelse) and _body_ends(stmt.body) and _body_ends(stmt.orelse)
    if isinstance(stmt, (ast.Try, getattr(ast, 'TryStar', ast.Try))):
        if stmt.finalbody and _body_ends(stmt.finalbody):
            return True
        body_ends = _body_ends(stmt.body) or bool(stmt.orelse and _body_ends(stmt.orelse))
        return bool(stmt.handlers) and body_ends and all(_body_ends(h.body) for h in stmt.handlers)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return _body_ends(stmt.body)
    return False


def _body_ends(body) -> bool:
    return any(_never_falls_through(s) for s in body)


def _statically_reachable(tree, line) -> bool:
    def walk(body):
        for i, stmt in enumerate(body):
            if not stmt.lineno <= line <= (stmt.end_lineno or stmt.lineno):
                continue
            if any(_never_falls_through(p) for p in body[:i]):
                return False
            if stmt.lineno == line and isinstance(stmt, ast.Expr):
                return True
            for field in ('body', 'orelse', 'finalbody'):
                sub = getattr(stmt, field, None)
                if isinstance(sub, list) and any(
                        s.lineno <= line <= (s.end_lineno or s.lineno) for s in sub):
                    # The `else:` of an endless loop never runs.
                    if field == 'orelse' and _endless_loop(stmt):
                        return False
                    return walk(sub)
            for handler in getattr(stmt, 'handlers', None) or []:
                if handler.lineno <= line <= (handler.end_lineno or handler.lineno):
                    return walk(handler.body)
            for case in getattr(stmt, 'cases', None) or []:
                if case.body and case.body[0].lineno <= line <= (case.body[-1].end_lineno or line):
                    return walk(case.body)
            return False
        return False
    return walk(tree.body)


# BaseException: the program's own `except Exception:` (a try body is a
# valid spot) must not swallow the verdict.
class _Reached(BaseException):
    pass


class _OutOfSteps(BaseException):
    pass


def _runs_to_the_marker(src: str, steps: int = 50_000) -> bool:
    """Run `src` with a fake `robot` whose replay("Winken") raises `_Reached`
    and a line budget (a program that loops before the marker never gets
    there)."""
    robot = types.ModuleType('robot')

    def replay(name, speed=1.0):
        if name == 'Winken':
            raise _Reached()
    robot.replay = replay
    for n in ('home', 'log', 'move_to', 'open_gripper', 'close_gripper', 'beep'):
        setattr(robot, n, lambda *a, **k: None)
    saved = sys.modules.get('robot')
    sys.modules['robot'] = robot
    left = [steps]

    def tracer(frame, event, arg):
        # Only the program's own lines count: a library it calls (asyncio.run
        # runs thousands of lines) is not the program looping.
        if frame.f_code.co_filename == 'main.py':
            left[0] -= 1
            if left[0] <= 0:
                raise _OutOfSteps()
        return tracer
    sys.settrace(tracer)
    try:
        exec(compile(src, 'main.py', 'exec'), {'__name__': '__main__', 'robot': robot})  # noqa: S102
    except _Reached:
        return True
    except _OutOfSteps:
        return False
    finally:
        sys.settrace(None)
        if saved is None:
            sys.modules.pop('robot', None)
        else:
            sys.modules['robot'] = saved
    return False


def _python_cases():
    out = []
    for path in (_INSERT_CASES, _INDENT_CASES):
        doc = _load(path)
        marker = doc['marker']['python']
        out += [(path.name, c, marker) for c in doc['cases'] if c['language'] == 'python']
    return out


class TheJudgeHasTeeth(unittest.TestCase):
    """The judge itself refuses what an insertion must never produce: a
    marker below something that never lets it run, and one that never runs
    — so a fixture it passes is evidence, not a formality."""

    _M = 'robot.replay("Winken")'

    def _line(self, src):
        return [i + 1 for i, r in enumerate(src.split('\n')) if r.strip() == self._M][0]

    def test_a_dead_marker_is_statically_unreachable(self):
        dead = [
            f'def f():\n    return 1\n    {self._M}\nf()\n',
            f'import sys\nsys.exit(0)\n{self._M}\n',
            f'while not False:\n    pass\n{self._M}\n',
            f'while 1 == 1:\n    pass\n{self._M}\n',
            f'def f():\n    if x:\n        return\n    else:\n        raise ValueError()\n    {self._M}\n',
            f'def f():\n    try:\n        a()\n    finally:\n        return\n    {self._M}\n',
            f'while True:\n    pass\nelse:\n    {self._M}\n',
            f'for i in []:\n    pass\nelse:\n    exit()\n{self._M}\n',
        ]
        for src in dead:
            with self.subTest(src=src):
                self.assertFalse(_statically_reachable(ast.parse(src), self._line(src)))

    def test_a_reachable_marker_stays_reachable(self):
        live = [
            f'while True:\n    for i in []:\n        break\n    break\n{self._M}\n',
            f'while True:\n    if x:\n        break\n{self._M}\n',
            f'def f():\n    if x:\n        return\n    {self._M}\n',
        ]
        for src in live:
            with self.subTest(src=src):
                self.assertTrue(_statically_reachable(ast.parse(src), self._line(src)))

    def test_a_marker_that_never_runs_is_caught_by_running(self):
        self.assertFalse(_runs_to_the_marker(f'def f():\n    {self._M}\n'))
        self.assertFalse(_runs_to_the_marker(f'while True:\n    pass\n{self._M}\n'))
        self.assertTrue(_runs_to_the_marker(f'try:\n    {self._M}\nexcept Exception:\n    pass\n'))


class PythonCasesCompile(unittest.TestCase):
    def test_the_fixtures_exist_and_carry_cases(self):
        self.assertTrue(_INSERT_CASES.is_file(), _INSERT_CASES)
        self.assertTrue(_INDENT_CASES.is_file(), _INDENT_CASES)
        self.assertGreaterEqual(len(_python_cases()), 40)

    def test_a_case_either_inserted_or_says_why_never_both(self):
        for fixture, case, _marker in _python_cases():
            with self.subTest(fixture=fixture, case=case['name']):
                self.assertIn(case['reach'], ('run', 'hint'))
                if case['reach'] == 'hint':
                    self.assertIsNone(case['output'])
                    self.assertTrue(case['hint'])
                else:
                    self.assertIsNotNone(case['output'])

    def test_every_python_program_parses_and_its_marker_is_reachable(self):
        for fixture, case, marker in _python_cases():
            if case['output'] is None:
                self.assertTrue(case['hint'], (fixture, case['name']))
                continue
            with self.subTest(fixture=fixture, case=case['name']):
                src = case['output']
                compile(src, 'main.py', 'exec')
                tree = ast.parse(src)
                rows = src.replace('\r\n', '\n').split('\n')
                lines = [i + 1 for i, r in enumerate(rows) if r.strip() == marker]
                self.assertEqual(len(lines), 1, 'exactly one marker line')
                self.assertTrue(_statically_reachable(tree, lines[0]),
                                f'{case["name"]}: the marker is unreachable')
                if case['reach'] == 'run':
                    self.assertTrue(_runs_to_the_marker(src),
                                    f'{case["name"]}: the program never runs the marker')


def _find_javac():
    candidates = [os.environ.get('EDUBOTICS_JAVAC')]
    home = os.environ.get('JAVA_HOME_21_X64')
    if home:
        candidates.append(os.path.join(home, 'bin', 'javac'))
    candidates.append(shutil.which('javac'))
    for javac in candidates:
        if not javac or not os.path.isfile(javac):
            continue
        java = os.path.join(os.path.dirname(javac), 'java')
        try:
            out = subprocess.run([javac, '-version'], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        text = (out.stdout + out.stderr).strip()
        try:
            major = int(text.split()[1].split('.')[0])
        except (IndexError, ValueError):
            continue
        if out.returncode == 0 and major >= 17 and os.path.isfile(java):
            return javac, java
    return None


_JAVAC = _find_javac()


@unittest.skipUnless(_JAVAC or os.environ.get('EDUBOTICS_REQUIRE_JAVAC') == '1',
                     'no JDK >= 17 (set EDUBOTICS_JAVAC, or EDUBOTICS_REQUIRE_JAVAC=1 to fail)')
class JavaCasesCompile(unittest.TestCase):
    def test_every_java_program_compiles_and_runs_to_its_marker(self):
        self.assertIsNotNone(_JAVAC, 'EDUBOTICS_REQUIRE_JAVAC=1 but no JDK >= 17 was found')
        javac, java = _JAVAC
        doc = _load(_INSERT_CASES)
        cases = [c for c in doc['cases'] if c['language'] == 'java']
        self.assertGreaterEqual(len(cases), 40)
        with tempfile.TemporaryDirectory(prefix='edu-java-cases-') as tmp:
            lib = os.path.join(tmp, 'lib')
            src = os.path.join(tmp, 'edubotics')
            os.makedirs(src)
            with open(os.path.join(src, 'Robot.java'), 'w', encoding='utf-8') as handle:
                handle.write(_FAKE_ROBOT_JAVA)
            subprocess.run([javac, '-d', lib, os.path.join(src, 'Robot.java')],
                           check=True, capture_output=True, timeout=120)
            for case in cases:
                self.assertIn(case['reach'], ('run', 'hint'), case['name'])
                if case['output'] is None:
                    self.assertEqual(case['reach'], 'hint', case['name'])
                    self.assertTrue(case['hint'], case['name'])
                    continue
                with self.subTest(case=case['name']):
                    folder = os.path.join(tmp, case['name'])
                    os.makedirs(folder)
                    with open(os.path.join(folder, 'Main.java'), 'w', encoding='utf-8',
                              newline='') as handle:
                        handle.write(case['output'])
                    built = subprocess.run(
                        [javac, '-Xlint:none', '-encoding', 'UTF-8', '-cp', lib, '-d', folder,
                         os.path.join(folder, 'Main.java')],
                        capture_output=True, text=True, timeout=120)
                    self.assertEqual(built.returncode, 0,
                                     f'{case["name"]} does not compile:\n{built.stderr}')
                    if case['reach'] == 'run':
                        ran = subprocess.run([java, '-cp', lib + os.pathsep + folder, 'Main'],
                                             capture_output=True, text=True, timeout=30)
                        self.assertIn('MARKE ERREICHT', ran.stdout,
                                      f'{case["name"]} never runs the marker')


if __name__ == '__main__':
    unittest.main()
