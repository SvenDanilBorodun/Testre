#!/usr/bin/env python3
"""``workflow/robot_api.py`` is THE table: one row per handler-table key.

Three surfaces read it — the server-side RPC validator, the generated Python
and Java stubs the student imports, and the editor's ``robot_api.json`` — so
every fence here is about the table saying nothing the handlers do not do:

- the bijection with ``STATEMENT_HANDLERS`` | ``VALUE_EVALUATORS`` (a handler
  added without a row, or a row naming no handler, fails here);
- every ``arg_key`` is a key its handler actually READS (the fence that stops a
  ``counter_add(name, n)``-style lie: ``counters.add`` is hardcoded ``+1``);
- every ``doc_de`` is German by the predicate the repo already lints with;
- the seven interpreter-native block types have NO row (they are language
  constructs in a text program, not calls).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import inspect
import json
import re
from pathlib import Path

from physical_ai_server.workflow import robot_api
from physical_ai_server.workflow.handlers import STATEMENT_HANDLERS, VALUE_EVALUATORS
from physical_ai_server.workflow.interpreter import HAT_BLOCK_TYPES


_REPO = Path(__file__).resolve().parents[3]
_RUNNER_TREE = _REPO / 'robotis_ai_setup' / 'docker' / 'code_runner'

# P18: the seven block types the interpreter dispatches natively. A text
# program spells them as language constructs (while True / while not … /
# while robot.sees(…)) or has no meaning for them (hats, broadcast).
_INTERPRETER_NATIVE = frozenset({
    'edubotics_broadcast',
    'edubotics_forever',
    'edubotics_wait_until',
    'edubotics_when_broadcast',
    'edubotics_when_counter_gt',
    'edubotics_when_object_seen',
    'edubotics_while_visible',
})

# §3.4: names = pythonCodeGen.js's 32 minus `broadcast`, plus `ziel`.
_PYTHON_SURFACE = frozenset({
    'home', 'open_gripper', 'close_gripper', 'move_to', 'pickup', 'drop_at',
    'wait', 'replay', 'move_above', 'descend_to', 'close_on_object', 'lift',
    'grasp', 'mark_done', 'pin', 'pin_current', 'log', 'beep', 'speak', 'tone',
    'toast', 'counter_reset', 'counter_add', 'ziel', 'sees', 'count',
    'wait_until_seen', 'wait_until_held', 'find', 'object_position',
    'is_holding', 'counter_get',
})


def _load_gdl():
    """The shipped lint, loaded by path (the test_feetech_bus precedent)."""
    path = _REPO / '.github' / 'scripts' / 'german_detail_lint.py'
    spec = importlib.util.spec_from_file_location('gdl', path)
    gdl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gdl)
    return gdl


def _is_german(gdl, s: str) -> bool:
    """The predicate the acceptance names (there is NO ``is_german`` in the
    script, P30): a German marker AND no transliteration."""
    return bool(gdl.GERMAN_CHARS.search(s) or gdl.GERMAN_WORDS.search(s)) \
        and not gdl.TRANSLITERATIONS.search(s)


# ── the arg-key reader ────────────────────────────────────────────────────

def _function_def(module_tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in module_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _keys_read(module_tree: ast.Module, fn: ast.FunctionDef, args_name: str,
               seen: set[str]) -> set[str]:
    """Every ``args.get('k')`` / ``args['k']`` key the function reads, following
    module-level helpers the function hands ``args`` to (``counters._name(args)``,
    ``motion._move_tempo(args)``)."""
    if fn.name in seen:
        return set()
    seen.add(fn.name)
    keys: set[str] = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get'
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == args_name
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == args_name
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            helper = _function_def(module_tree, node.func.id)
            if helper is None:
                continue
            for pos, arg in enumerate(node.args):
                if isinstance(arg, ast.Name) and arg.id == args_name \
                        and pos < len(helper.args.args):
                    keys |= _keys_read(module_tree, helper,
                                       helper.args.args[pos].arg, seen)
    return keys


def _handler_for(block_type: str):
    return STATEMENT_HANDLERS.get(block_type) or VALUE_EVALUATORS.get(block_type)


def _keys_read_by_handler(handler) -> set[str]:
    module = inspect.getmodule(handler)
    tree = ast.parse(inspect.getsource(module))
    fn = _function_def(tree, handler.__name__)
    assert fn is not None, f'{handler.__name__} not found at module level'
    assert fn.args.args and len(fn.args.args) >= 2, handler.__name__
    return _keys_read(tree, fn, fn.args.args[1].arg, set())


def _unread_arg_keys(call: robot_api.ApiCall) -> set[str]:
    """The row's ``arg_key``s its handler never reads (empty = the row is honest)."""
    return {p.arg_key for p in call.params} - _keys_read_by_handler(_handler_for(call.block_type))


# ── the table ─────────────────────────────────────────────────────────────

def test_bijection_with_handler_tables():
    table_types = {c.block_type for c in robot_api.ROBOT_API}
    assert table_types == set(STATEMENT_HANDLERS) | set(VALUE_EVALUATORS)
    assert len(robot_api.ROBOT_API) == 32


def test_every_block_type_and_name_appears_exactly_once():
    types = [c.block_type for c in robot_api.ROBOT_API]
    assert len(types) == len(set(types))
    names = [c.name for c in robot_api.ROBOT_API]
    assert len(names) == len(set(names))
    java = [c.java_name for c in robot_api.ROBOT_API]
    assert len(java) == len(set(java))
    assert set(robot_api.ROBOT_API_BY_NAME) == set(names)
    assert set(robot_api.ROBOT_API_BY_BLOCK_TYPE) == set(types)


def test_the_table_column_agrees_with_the_dispatch_table():
    for call in robot_api.ROBOT_API:
        if call.block_type in STATEMENT_HANDLERS:
            assert call.table == 'statement', call.name
            assert call.returns == 'none', call.name
        else:
            assert call.table == 'value', call.name
            assert call.returns != 'none', call.name


def test_interpreter_native_types_have_no_row():
    assert HAT_BLOCK_TYPES <= _INTERPRETER_NATIVE
    assert not ({c.block_type for c in robot_api.ROBOT_API} & _INTERPRETER_NATIVE)


def test_python_surface_is_the_32_names_of_the_spec():
    assert {c.name for c in robot_api.ROBOT_API} == _PYTHON_SURFACE


def test_every_arg_key_is_read_by_its_handler():
    offenders = {c.name: _unread_arg_keys(c) for c in robot_api.ROBOT_API
                 if _unread_arg_keys(c)}
    assert offenders == {}


def test_the_arg_key_fence_catches_a_parameter_the_handler_never_reads():
    """The fence must FAIL on a lie: ``counter_add(name, n)`` — ``counters.add``
    is hardcoded ``+1`` and reads no ``n``."""
    honest = robot_api.ROBOT_API_BY_NAME['counter_add']
    assert [p.arg_key for p in honest.params] == ['name']
    lie = dataclasses.replace(
        honest,
        params=honest.params + (robot_api.ApiParam('n', 'int', 'n', lo=1, hi=10),),
    )
    assert _unread_arg_keys(lie) == {'n'}


def test_the_arg_key_reader_follows_helpers():
    """``counters.add`` reads ``name`` only through ``_name(args)``; ``motion.lift``
    reads ``geschwindigkeit`` only through ``_move_tempo(args)``. Both must be
    visible, or the fence would refuse every honest row on those modules."""
    assert 'name' in _keys_read_by_handler(STATEMENT_HANDLERS['edubotics_counter_add'])
    assert 'geschwindigkeit' in _keys_read_by_handler(STATEMENT_HANDLERS['edubotics_lift'])


def test_the_three_point_takers_carry_the_arg_keys_the_handlers_read():
    """P32: ``move_to`` and ``drop_at`` read ``args['destination']``, only
    ``pickup`` reads ``args['target']`` — same student-facing name, three rows."""
    for name, key in (('move_to', 'destination'), ('drop_at', 'destination'),
                      ('pickup', 'target')):
        call = robot_api.ROBOT_API_BY_NAME[name]
        assert [p.name for p in call.params] == ['target'], name
        assert call.params[0].arg_key == key, name
        assert call.params[0].kind == 'target', name


def test_every_param_kind_is_a_declared_kind_and_every_row_is_well_formed():
    for call in robot_api.ROBOT_API + robot_api.INTERNAL_METHODS:
        assert call.budget in ('call', 'perception'), call.name
        assert call.returns in robot_api.RETURN_KINDS, call.name
        seen_default = False
        for p in call.params:
            assert p.kind in robot_api.KINDS, (call.name, p.name)
            assert re.match(r'^[a-z][a-z0-9_]*$', p.name), (call.name, p.name)
            if p.lo is not None and p.hi is not None:
                assert p.lo < p.hi, (call.name, p.name)
            if p.default is not robot_api.REQUIRED:
                seen_default = True
            else:
                assert not seen_default, (
                    f'{call.name}: a required parameter after a defaulted one')


def test_perception_budget_is_the_b2_set():
    perception = {c.name for c in robot_api.ROBOT_API + robot_api.INTERNAL_METHODS
                  if c.budget == 'perception'}
    assert perception == {'sees', 'count', 'find', 'wait_until_seen',
                          'object_position', 'register_object'}


def test_doc_de_is_german_by_the_shipped_predicate():
    gdl = _load_gdl()
    for call in robot_api.ROBOT_API + robot_api.INTERNAL_METHODS:
        assert call.doc_de.strip(), call.name
        assert _is_german(gdl, call.doc_de), (call.name, call.doc_de)
    # …and every German constant the module carries beside the table.
    for name, value in vars(robot_api).items():
        if name.endswith('_DE') and isinstance(value, str):
            assert _is_german(gdl, value), (name, value)


def test_internal_methods_carry_the_five_names_and_register_object_has_seven_params():
    assert [c.name for c in robot_api.INTERNAL_METHODS] == [
        '__hello', '__paused', '__line', '__exit', 'register_object']
    row = robot_api.INTERNAL_METHODS_BY_NAME['register_object']
    assert [p.name for p in row.params] == [
        'name', 'label', 'tag_ids', 'hoehe_m', 'greiftiefe_m',
        'greifer_schliessen_rad', 'anfahrhoehe_m']
    assert [p.kind for p in row.params] == [
        'obj', 'str', 'tagids', 'float', 'float', 'float', 'float']
    assert row.params[0].max_len == 24
    assert row.params[1].max_len == 40
    assert (row.params[3].lo, row.params[3].hi, row.params[3].lo_open) == (0.0, 0.30, True)
    assert (row.params[4].lo, row.params[4].hi) == (0.0, 0.30)
    assert (row.params[5].lo, row.params[5].hi, row.params[5].nullable) == (-1.75, 1.75, True)
    assert (row.params[6].lo, row.params[6].hi, row.params[6].lo_open,
            row.params[6].nullable) == (0.0, 0.30, True, True)
    assert row.budget == 'perception'
    for c in robot_api.INTERNAL_METHODS:
        assert c.block_type is None and c.table == 'internal', c.name


def test_rpc_limits_are_the_numbers_of_the_spec():
    L = robot_api.RPC_LIMITS
    assert L.MAX_CALLS_PER_S == 200.0
    assert L.BURST == 50
    assert L.PERCEPTION_MAX_PER_S == 20.0
    assert L.PERCEPTION_BURST == 5
    assert L.MAX_FRAME_BYTES == 65536
    assert L.CONTROL_MAX_FRAME_BYTES == 196608
    assert robot_api.MAX_CODE_FILES == 32
    assert robot_api.MAX_CODE_FILE_BYTES == 65536
    assert robot_api.MAX_CODE_PROJECT_BYTES == 131072
    assert L.CONTROL_MAX_FRAME_BYTES >= robot_api.MAX_CODE_PROJECT_BYTES + 8192


def test_code_path_re_is_the_spec_pattern():
    rx = re.compile(robot_api.CODE_PATH_RE)
    assert rx.match('main.py') and rx.match('Main.java')
    assert rx.match('utils/helfer.py') and rx.match('a/b/c/d.java')
    assert not rx.match('a/b/c/d/e.py')          # at most three directories
    assert not rx.match('../main.py')
    assert not rx.match('/main.py')
    assert not rx.match('main.js')
    assert not rx.match('1abc.py')
    assert not rx.match('a' * 41 + '.py')
    assert len(robot_api.CODE_PATH_RE) > 0


def test_export_json_limits_is_rpc_limits_union_the_project_caps():
    doc = json.loads(robot_api.export_json())
    expected = dict(dataclasses.asdict(robot_api.RPC_LIMITS))
    expected.update(robot_api.CODE_PROJECT_LIMITS)
    assert doc['limits'] == expected
    assert 'MAX_FRAME_BYTES' in doc['limits'] and 'CONTROL_MAX_FRAME_BYTES' in doc['limits']
    assert {m['name'] for m in doc['methods']} == _PYTHON_SURFACE
    assert [m['name'] for m in doc['internal']] == [c.name for c in robot_api.INTERNAL_METHODS]
    for m in doc['methods'] + doc['internal']:
        assert m['doc_de']
        for p in m['params']:
            assert set(p) >= {'name', 'kind', 'required'}


def test_suggest_is_difflib_over_the_python_names():
    assert robot_api.suggest('hoome') == 'home'
    assert robot_api.suggest('open_griper') == 'open_gripper'
    assert robot_api.suggest('xyzzy_nothing_like_it') is None
    assert robot_api.suggest('') is None


def test_no_edubotics_token_in_robot_api_or_the_runner_tree():
    """§3.1: the server package is scanned by ``env-forwarding-guard`` (comments
    included); the runner service carries no ``environment:`` block at all."""
    token = 'EDUBOTICS' + '_'
    assert token not in Path(robot_api.__file__).read_text(encoding='utf-8')
    if _RUNNER_TREE.exists():
        for path in _RUNNER_TREE.rglob('*'):
            if path.is_file():
                assert token not in path.read_text(encoding='utf-8', errors='replace'), path


def test_java_names_are_camel_case_and_never_collide_with_object_methods():
    """``Robot.wait(double)`` would fight ``Object.wait(long)`` in overload
    resolution (``Robot.wait(1)`` picks the instance method and fails to
    compile), so the Java surface must not reuse a ``java.lang.Object`` name."""
    object_methods = {'wait', 'notify', 'notifyAll', 'equals', 'hashCode',
                      'toString', 'getClass', 'clone', 'finalize'}
    for call in robot_api.ROBOT_API:
        assert re.match(r'^[a-z][A-Za-z0-9]*$', call.java_name), call.java_name
        assert call.java_name not in object_methods, call.java_name
