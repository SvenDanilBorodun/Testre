"""The two exit-report launchers are twins (area C, F2; deps-free).

A Python program ends through ``runner/student_main.py``, a Java program
through ``runner/java/edubotics/Launcher.java``. Both report the program's
fault to the robot as the internal ``__exit`` call with the SAME dict
(``{kind, file, line, exc_type, name, detail_line}``,
``code_rpc._EXIT_INFO_KEYS``), using only the existing wire kinds
(``code_errors_de.ERROR_KINDS``) — so a runner and a server of different
tags still agree. The German sentence is the server's; the one sentence each
launcher owns, the refusal of a keyboard read, is byte-equal in both.

Read from the sources (no JDK on a test host); the image's build runs
``SmokeMain``, which asserts ``Launcher.classify`` itself.
"""

import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner'
_LAUNCHER = _RUNNER / 'java' / 'edubotics' / 'Launcher.java'
_SMOKE = _RUNNER / 'java' / 'edubotics' / 'SmokeMain.java'
_STUDENT_MAIN = _RUNNER / 'student_main.py'
_SUPERVISOR = _RUNNER / 'supervisor.py'
_WORKFLOW = (_REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
             / 'workflow')
_CODE_ERRORS = _WORKFLOW / 'code_errors_de.py'
_CODE_RPC = _WORKFLOW / 'code_rpc.py'

# The spec's table (area C): Java fault → existing wire kind.
_JAVA_KINDS = {
    'RpcClient.RobotError': 'robot',
    'ArithmeticException': 'zero_division',
    'IndexOutOfBoundsException': 'index',
    'java.util.NoSuchElementException': 'index',
    'StackOverflowError': 'recursion',
    'OutOfMemoryError': 'memory',
    'ClassCastException': 'type',
    'IllegalArgumentException': 'type',
}


def _assigned(path, name):
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f'{name} not assigned in {path.name}')


def _error_kinds():
    node = _assigned(_CODE_ERRORS, 'ERROR_KINDS')
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
            and isinstance(n.value, str)}


def _java_string(src, name):
    m = re.search(r'static final String ' + name + r'\s*=\s*((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+);',
                  src)
    assert m, name
    return ''.join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))


def _classify_body(src):
    start = src.index('static Map<String, Object> classify(')
    return src[start:src.index('\n    }\n', start)]


class LauncherClassification(unittest.TestCase):
    def setUp(self):
        self.src = _LAUNCHER.read_text(encoding='utf-8')
        self.body = _classify_body(self.src)

    def test_every_java_fault_maps_to_its_wire_kind(self):
        pairs = re.findall(r'instanceof ([\w.]+)\)?[^{]*?\{\s*kind = "(\w+)";', self.body,
                           re.S)
        chains = re.findall(r'if \(([^{]*)\)\s*\{\s*kind = "(\w+)";', self.body, re.S)
        found = {}
        for cond, kind in chains:
            for cls in re.findall(r'instanceof ([\w.]+)', cond):
                found[cls] = kind
        self.assertTrue(pairs or chains)
        self.assertEqual(found, _JAVA_KINDS)
        self.assertIn('String kind = "other";', self.body)

    def test_both_launchers_use_only_the_existing_wire_kinds(self):
        kinds = _error_kinds()
        java = set(re.findall(r'kind = "(\w+)"', self.body))
        self.assertLessEqual(java, kinds, java - kinds)
        fn = next(n for n in ast.walk(ast.parse(_STUDENT_MAIN.read_text(encoding='utf-8')))
                  if isinstance(n, ast.FunctionDef) and n.name == 'classify_exception')
        py = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)
              and isinstance(n.value, str) and n.value in kinds | {'ok'}}
        self.assertLessEqual(py, kinds, py - kinds)

    def test_the_exit_dict_carries_exactly_the_servers_keys(self):
        table = _assigned(_CODE_RPC, '_EXIT_INFO_KEYS')
        server_keys = {k.value for k in table.keys}
        java_keys = set(re.findall(r'info\.put\("(\w+)"', self.body))
        self.assertEqual(java_keys, server_keys)

    def test_the_keyboard_refusal_is_byte_equal_in_both_launchers(self):
        node = _assigned(_STUDENT_MAIN, 'INPUT_REFUSED_DE')
        python = ''.join(n.value for n in ast.walk(node) if isinstance(n, ast.Constant))
        java = _java_string(self.src, 'INPUT_DE')
        self.assertEqual(java, python)
        self.assertTrue(python.startswith('Eingaben über die Tastatur gibt es hier nicht'))

    def test_the_launcher_reports_and_never_prints_a_stack_trace(self):
        self.assertNotIn('printStackTrace', self.src)
        self.assertIn('RpcClient.call("__exit", new Object[] {info}, "call")', self.src)
        self.assertIn('System.exit(1);', self.src)
        self.assertIn('System.setIn(', self.src)
        # The frames the student never wrote are skipped.
        for prefix in ('edubotics.', 'java.', 'jdk.', 'sun.'):
            self.assertIn(f'"{prefix}"', self.src)

    def test_the_smoke_main_asserts_the_classification(self):
        smoke = _SMOKE.read_text(encoding='utf-8')
        self.assertIn('Launcher.classify(', smoke)
        self.assertIn('Launcher.projectFile(', smoke)


class SupervisorStartsTheLauncher(unittest.TestCase):
    def setUp(self):
        self.src = _SUPERVISOR.read_text(encoding='utf-8')

    def _function(self, name):
        for node in ast.walk(ast.parse(self.src)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.unparse(node)
        raise AssertionError(name)

    def test_java_runs_through_the_launcher_with_the_sources(self):
        spawn = self._function('_spawn_java')
        self.assertIn("'edubotics.Launcher', 'Main', *self._java_sources()", spawn)

    def test_javac_keeps_the_debug_names(self):
        # -g: the JVM's own null message names the variable ("s"), which the
        # [TECHNIK] line shows.
        self.assertIn("'-g'", self._function('_compile_java'))


if __name__ == '__main__':
    unittest.main()


class TypeNameTwins(unittest.TestCase):
    """robot_api.type_name_for (rendered into the Python stub) and
    Greifobjekt.typeNameFor (Java) agree on SmokeMain's table: the image build
    runs the Java half, this runs the Python rule over the same rows."""

    def test_the_smoke_table_is_the_python_rule(self):
        import importlib.util
        smoke = _SMOKE.read_text(encoding='utf-8')
        block = smoke[smoke.index('String[][] typeNames = {'):]
        block = block[:block.index('};')]
        rows = re.findall(r'\{"((?:[^"\\]|\\.)*)", "((?:[^"\\]|\\.)*)"\}', block)
        self.assertGreaterEqual(len(rows), 10)
        # The stub's copy IS robot_api.type_name_for's source (rendered from it,
        # byte-equality fenced by test_robot_api_generated.py).
        spec = importlib.util.spec_from_file_location('robot_stub_twin', _RUNNER / 'lib' / 'robot.py')
        stub = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stub)
        for label, want in rows:
            label = label.encode('ascii', 'backslashreplace').decode('unicode_escape') \
                if '\\u' in label else label
            self.assertEqual(stub._type_name_for(label), want, label)
