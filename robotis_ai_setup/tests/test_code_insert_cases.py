"""What the code editor writes into a student's program is judged by the REAL
tools (review round 2, mi2/mi3 and MA1): every program in the React fixtures
is parsed and compiled by CPython and compiled by javac, and the inserted
marker line is checked to be reachable — statically for every case (nothing
before it in its block that never lets the next line run: javac's own
„unreachable statement" rule for Java), and by RUNNING the program for every
case whose marker must run (the end of main: the body that runs last).

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

def _never_falls_through(stmt) -> bool:
    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return True
    if isinstance(stmt, ast.While):
        test = stmt.test
        const = isinstance(test, ast.Constant) and bool(test.value)
        has_break = any(isinstance(n, ast.Break) for s in stmt.body for n in ast.walk(s))
        return const and not has_break
    if isinstance(stmt, ast.If):
        return bool(stmt.orelse) and _body_ends(stmt.body) and _body_ends(stmt.orelse)
    if isinstance(stmt, ast.Try):
        if stmt.finalbody and _body_ends(stmt.finalbody):
            return True
        return _body_ends(stmt.body) and all(_body_ends(h.body) for h in stmt.handlers)
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
                    return walk(sub)
            for handler in getattr(stmt, 'handlers', None) or []:
                if handler.lineno <= line <= (handler.end_lineno or handler.lineno):
                    return walk(handler.body)
            return False
        return False
    return walk(tree.body)


class _Reached(Exception):
    pass


class _OutOfSteps(Exception):
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


class PythonCasesCompile(unittest.TestCase):
    def test_the_fixtures_exist_and_carry_cases(self):
        self.assertTrue(_INSERT_CASES.is_file(), _INSERT_CASES)
        self.assertTrue(_INDENT_CASES.is_file(), _INDENT_CASES)
        self.assertGreaterEqual(len(_python_cases()), 40)

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
                if case['output'] is None:
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
