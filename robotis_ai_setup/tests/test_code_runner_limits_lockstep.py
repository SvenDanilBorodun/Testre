"""Lockstep between the code runner and the server package it talks to
(deps-free, AST-only — the server package needs numpy/ROS, the runner ships
in another image; neither side can import the other).

What is fenced:

  * every number in ``robot_api.RpcLimits`` — ``MAX_CALLS_PER_S``, ``BURST``,
    ``PERCEPTION_MAX_PER_S``, ``PERCEPTION_BURST``, ``MAX_FRAME_BYTES`` AND
    ``CONTROL_MAX_FRAME_BYTES`` — against ``runner_limits.py``;
  * the code-project caps, ``RUN_TOKEN_RE`` and ``PAUSED_MAX_LOCALS`` (the
    supervisor re-validates the ``start`` envelope; the hook bounds the
    locals it sends) against the same file;
  * the two stdout bounds against ``code_rpc.py``;
  * the frame encoder: the supervisor's ``encode_frame_body`` is the SAME
    ``json.dumps(obj, ensure_ascii=False, separators=(',', ':'))`` call as
    ``code_rpc.encode_frame_body``'s, keyword for keyword — a project whose
    bytes the cap counted must be the bytes on the wire (R2-3);
  * the exit report: the keys ``student_main.classify_exception`` returns
    are exactly ``code_rpc._EXIT_INFO_KEYS``, and every ``kind`` it assigns
    is in ``code_errors_de.ERROR_KINDS`` (plus ``'ok'``);
  * the control vocabulary: every ``ev`` ``code_program.py`` sends is one the
    supervisor dispatches, and every ``ev`` it awaits is one the supervisor
    emits.
"""

import ast
import pathlib
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner'
_WORKFLOW = _REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'workflow'
_LIMITS = _RUNNER / 'runner_limits.py'
_SUPERVISOR = _RUNNER / 'supervisor.py'
_STUDENT_MAIN = _RUNNER / 'student_main.py'
_STUB = _RUNNER / 'lib' / 'robot.py'
_ROBOT_API = _WORKFLOW / 'robot_api.py'
_CODE_RPC = _WORKFLOW / 'code_rpc.py'
_CODE_PROGRAM = _WORKFLOW / 'code_program.py'
_CODE_ERRORS = _WORKFLOW / 'code_errors_de.py'


def _tree(path):
    return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def _module_constants(tree):
    """``NAME = <literal>`` at module level → value (tuples/dicts included)."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return out


def _class_defaults(tree, cls):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {item.target.id: ast.literal_eval(item.value) for item in node.body
                    if isinstance(item, ast.AnnAssign) and item.value is not None}
    raise AssertionError(f'{cls} not found')


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found')


def _dumps_call(fn):
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == 'json.dumps']
    assert len(calls) == 1, ast.unparse(fn)
    return calls[0]


def _string_constants_in(node):
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
            and isinstance(n.value, str)}


def _assigned_value(tree, name):
    """The AST node assigned to a module-level ``NAME = …`` (any shape)."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name:
            return node.value
    raise AssertionError(f'{name} not found')


class RpcLimitsTwin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for path in (_LIMITS, _ROBOT_API, _CODE_RPC):
            assert path.is_file(), path
        cls.runner = _module_constants(_tree(_LIMITS))
        cls.api_tree = _tree(_ROBOT_API)
        cls.api = _module_constants(cls.api_tree)
        cls.rpc = _module_constants(_tree(_CODE_RPC))

    def test_every_RpcLimits_number_is_the_same_in_runner_limits(self):
        limits = _class_defaults(self.api_tree, 'RpcLimits')
        self.assertEqual(set(limits), {'MAX_CALLS_PER_S', 'BURST', 'PERCEPTION_MAX_PER_S',
                                       'PERCEPTION_BURST', 'MAX_FRAME_BYTES',
                                       'CONTROL_MAX_FRAME_BYTES'})
        for name, value in limits.items():
            self.assertIn(name, self.runner, name)
            self.assertEqual(self.runner[name], value, name)
            self.assertIs(type(self.runner[name]), type(value), name)

    def test_the_project_caps_token_and_locals_bound_are_twins(self):
        for name in ('MAX_CODE_FILES', 'MAX_CODE_FILE_BYTES', 'MAX_CODE_PROJECT_BYTES',
                     'CODE_PATH_RE', 'RUN_TOKEN_RE', 'PAUSED_MAX_LOCALS'):
            self.assertIn(name, self.api, name)
            self.assertEqual(self.runner.get(name), self.api[name], name)

    def test_the_stdout_bounds_are_twins_of_code_rpc(self):
        for name in ('STDOUT_MAX_LINE_BYTES', 'STDOUT_MAX_LINES_PER_S'):
            self.assertIn(name, self.rpc, name)
            self.assertEqual(self.runner.get(name), self.rpc[name], name)

    def test_the_generated_stub_carries_the_same_numbers(self):
        stub = _module_constants(_tree(_STUB))
        for name in ('MAX_CALLS_PER_S', 'BURST', 'PERCEPTION_MAX_PER_S',
                     'PERCEPTION_BURST', 'MAX_FRAME_BYTES'):
            self.assertEqual(stub.get(name), self.runner[name], name)


class FrameEncoderTwin(unittest.TestCase):
    def test_the_supervisor_encoder_is_the_servers_call_keyword_for_keyword(self):
        ours = _dumps_call(_function(_tree(_SUPERVISOR), 'encode_frame_body'))
        theirs = _dumps_call(_function(_tree(_CODE_RPC), 'encode_frame_body'))
        self.assertEqual(ast.dump(ours), ast.dump(theirs))
        keywords = {kw.arg: ast.unparse(kw.value) for kw in ours.keywords}
        self.assertEqual(keywords, {'ensure_ascii': 'False', 'separators': "(',', ':')"})

    def test_the_stub_encodes_its_requests_the_same_way(self):
        stub = _tree(_STUB)
        call = None
        for node in ast.walk(stub):
            if isinstance(node, ast.FunctionDef) and node.name == 'call':
                call = _dumps_call(node)
        self.assertIsNotNone(call)
        keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        self.assertEqual(keywords, {'ensure_ascii': 'False', 'separators': "(',', ':')"})


class ExitReportTwin(unittest.TestCase):
    def test_classify_exception_returns_exactly_the_servers_exit_info_keys(self):
        # `_EXIT_INFO_KEYS = {'kind': (str, 32), …}` — the values name types,
        # so only the KEYS are literal; read them off the dict node.
        table = _assigned_value(_tree(_CODE_RPC), '_EXIT_INFO_KEYS')
        self.assertIsInstance(table, ast.Dict)
        server_keys = {k.value for k in table.keys}
        fn = _function(_tree(_STUDENT_MAIN), 'classify_exception')
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)
                   and isinstance(n.value, ast.Dict)]
        self.assertEqual(len(returns), 1)
        keys = {k.value for k in returns[0].value.keys}
        self.assertEqual(keys, server_keys)

    def test_every_kind_the_launcher_assigns_is_a_server_error_kind(self):
        # `ERROR_KINDS = frozenset({...})` — a call around a set literal.
        node = _assigned_value(_tree(_CODE_ERRORS), 'ERROR_KINDS')
        self.assertIsInstance(node, ast.Call)
        error_kinds = _string_constants_in(node.args[0])
        self.assertGreaterEqual(len(error_kinds), 12)
        fn = _function(_tree(_STUDENT_MAIN), 'classify_exception')
        kinds = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                targets = [ast.unparse(t) for t in node.targets]
                if targets == ['kind'] and isinstance(node.value, ast.Constant):
                    kinds.add(node.value.value)
                if targets == ['(kind, name)'] and isinstance(node.value, ast.Tuple):
                    kinds.add(node.value.elts[0].value)
        self.assertTrue(kinds)
        self.assertLessEqual(kinds, error_kinds, kinds - error_kinds)
        for expected in ('syntax', 'indentation', 'name', 'robot_method', 'import', 'type',
                         'zero_division', 'index', 'recursion', 'memory', 'robot', 'other'):
            self.assertIn(expected, kinds)


class ControlVocabulary(unittest.TestCase):
    def setUp(self):
        self.program = _tree(_CODE_PROGRAM)
        self.supervisor = _tree(_SUPERVISOR)

    def _ev_values_in_dicts(self, tree):
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == 'ev' \
                            and isinstance(value, ast.Constant):
                        out.add(value.value)
        return out

    def test_every_event_the_server_sends_is_one_the_supervisor_dispatches(self):
        sent = self._ev_values_in_dicts(self.program)
        # The exact vocabulary, so a fourth frame has to be declared here. It
        # read {'start', 'kill'} while `breakpoints` was DOCUMENTED and never
        # sent — the supervisor's branch for it, `Run.set_breakpoints` and the
        # hook's `restart_events()` were all unreachable from the product.
        self.assertEqual(sent, {'start', 'kill', 'breakpoints'})
        handled = set()
        for node in ast.walk(self.supervisor):
            if isinstance(node, ast.Compare) and ast.unparse(node.left) in ('ev', "frame.get('ev')"):
                for comparator in node.comparators:
                    handled |= _string_constants_in(comparator)
        self.assertLessEqual(sent, handled, sent - handled)

    def test_every_event_the_server_awaits_is_one_the_supervisor_emits(self):
        awaited = set()
        for node in ast.walk(self.program):
            if isinstance(node, ast.Set):
                awaited |= _string_constants_in(node)
            if isinstance(node, ast.Compare) and ast.unparse(node.left) in (
                    'ev', "frame.get('ev')", "started.get('ev')"):
                for comparator in node.comparators:
                    awaited |= _string_constants_in(comparator)
        for needed in ('started', 'busy', 'killed', 'exited', 'compile_error', 'stdout'):
            self.assertIn(needed, awaited, f'code_program no longer awaits {needed!r}')
        emitted = self._ev_values_in_dicts(self.supervisor)
        for node in ast.walk(self.supervisor):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == 'self._emit_lines':
                emitted |= _string_constants_in(node)
        self.assertLessEqual(awaited, emitted, awaited - emitted)

    def test_the_exit_report_rides_the_data_socket_as_the_server_reads_it(self):
        # The launcher calls __exit with ONE dict; the server takes args[0].
        fn = _function(_tree(_STUDENT_MAIN), 'main')
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == 'robot._rpc.call'
                 and n.args and isinstance(n.args[0], ast.Constant)
                 and n.args[0].value == '__exit']
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0].args[1]), '[info]')


if __name__ == '__main__':
    unittest.main()
