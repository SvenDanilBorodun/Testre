#!/usr/bin/env python3
"""The five checked-in artifacts ARE the renderers' output, byte for byte.

``robot_api.render_all()`` maps a repo-relative path to the content the table
renders; this file diffs each against the tree and refuses a drift. To
regenerate after an intended table change::

    EDUBOTICS_REGEN_STUBS=1 pytest test/test_robot_api_generated.py

(the ``EDUBOTICS_REGEN_GOLDEN`` precedent of ``test_dof_golden.py``; the name
lives only in this test file, which the env-forwarding guard does not scan).

Beyond byte-equality, two structural properties of the rendered Python stub
are load-bearing for A7.1 and are asserted over the RENDERED text, so a
template edit cannot lose them silently:

- every ``sendall(`` in the stub is preceded, in its own function body, by a
  ``_rate_floor(`` call — the runner-side rate floor is structural;
- no function outside the ``_Rpc`` class touches the socket.

Both fences are proven to bite: the test deletes the floor line from a copy
of the rendered stub and asserts the checker refuses it.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from physical_ai_server.workflow import robot_api


_REPO = Path(__file__).resolve().parents[3]
_REGEN = os.environ.get('EDUBOTICS_REGEN_STUBS', '').strip() not in ('', '0')
_RENDERED = robot_api.render_all()


@pytest.fixture(scope='module', autouse=True)
def _regenerate_when_asked():
    if _REGEN:
        for rel, content in _RENDERED.items():
            path = _REPO / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')


# ── byte-equality ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('rel', sorted(_RENDERED))
def test_generated_file_is_byte_equal_to_the_renderer(rel):
    path = _REPO / rel
    assert path.exists(), (
        f'{rel} missing — run EDUBOTICS_REGEN_STUBS=1 pytest '
        'test/test_robot_api_generated.py once and commit the result')
    assert path.read_bytes() == _RENDERED[rel].encode('utf-8'), (
        f'{rel} drifted from robot_api.py — regenerate (see module docstring)')


def test_the_five_paths_are_the_five_of_the_spec():
    assert set(_RENDERED) == {
        'robotis_ai_setup/docker/code_runner/runner/lib/robot.py',
        'robotis_ai_setup/docker/code_runner/runner/java/edubotics/Robot.java',
        'robotis_ai_setup/docker/code_runner/runner/java/edubotics/Greifobjekt.java',
        'robotis_ai_setup/docker/code_runner/runner/java/edubotics/RpcClient.java',
        'physical_ai_tools/physical_ai_manager/src/components/Workshop/code/robot_api.json',
    }


def test_rendering_is_deterministic():
    assert robot_api.render_all() == _RENDERED


def test_no_edubotics_token_in_any_generated_file():
    token = 'EDUBOTICS' + '_'
    for rel, content in _RENDERED.items():
        assert token not in content, rel


# ── the rate-floor fence over the rendered Python stub ────────────────────

_STUB = _RENDERED[robot_api.GENERATED_PATHS['python_stub']]


def _calls_in_order(fn: ast.FunctionDef) -> list[tuple[int, str]]:
    """(position, callee) for every call in the function, in source order;
    callee is the attribute name for ``x.y(...)`` or the bare name."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            out.append(((node.lineno, node.col_offset), node.func.attr))
        elif isinstance(node.func, ast.Name):
            out.append(((node.lineno, node.col_offset), node.func.id))
    return sorted(out)


def _rate_floor_precedes_every_send(source: str) -> list[str]:
    """Names of functions in which a ``sendall``/``write_frame`` call is NOT
    preceded by a ``_rate_floor`` call in the same body (empty = the fence holds)."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = _calls_in_order(node)
        for pos, callee in calls:
            if callee in ('sendall', 'write_frame'):
                if not any(c == '_rate_floor' and p < pos for p, c in calls):
                    offenders.append(node.name)
    return offenders


_SOCKET_OPS = frozenset({'sendall', 'send', 'recv', 'recv_into', 'connect', 'socket'})


def _functions_touching_the_socket_outside_rpc(source: str) -> list[str]:
    tree = ast.parse(source)
    inside_rpc: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == '_Rpc':
            for sub in ast.walk(node):
                inside_rpc.add(id(sub))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or id(node) in inside_rpc:
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in _SOCKET_OPS):
                offenders.append(node.name)
                break
    return offenders


def test_rendered_python_stub_rate_floor_precedes_every_send():
    assert _rate_floor_precedes_every_send(_STUB) == []
    # …and the stub does send somewhere (the fence is not vacuous).
    assert 'sendall(' in _STUB


def test_the_rate_floor_fence_bites_when_the_floor_line_is_deleted():
    lines = _STUB.split('\n')
    without = [ln for ln in lines if '_rate_floor(kind)' not in ln]
    assert len(without) == len(lines) - 1, 'expected exactly one floor call site'
    assert _rate_floor_precedes_every_send('\n'.join(without)) == ['call']


def test_no_function_outside_rpc_touches_the_socket():
    assert _functions_touching_the_socket_outside_rpc(_STUB) == []


def test_the_socket_fence_bites_on_a_module_level_sender():
    tampered = _STUB + '\ndef leak(sock, data):\n    sock.sendall(data)\n'
    assert _functions_touching_the_socket_outside_rpc(tampered) == ['leak']


def test_every_api_method_in_the_stub_goes_through_rpc_call():
    tree = ast.parse(_STUB)
    defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for call in robot_api.ROBOT_API + robot_api.CODE_ONLY_METHODS:
        fn = defs[call.name]
        args = [a.arg for a in fn.args.args]
        assert args == [p.name for p in call.params], call.name
        callees = [c for _p, c in _calls_in_order(fn)]
        assert 'call' in callees, call.name


def test_stub_encoder_is_ensure_ascii_false_and_compact():
    """R2-3: the frame bytes must be the bytes the project cap counted."""
    found = []
    for node in ast.walk(ast.parse(_STUB)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'dumps'):
            kw = {k.arg: k.value for k in node.keywords}
            assert isinstance(kw.get('ensure_ascii'), ast.Constant) and \
                kw['ensure_ascii'].value is False
            sep = kw.get('separators')
            assert isinstance(sep, ast.Tuple) and \
                [e.value for e in sep.elts] == [',', ':']
            found.append(node)
    assert len(found) == 1


def test_register_object_row_renders_seven_positionals_in_both_stubs():
    row = robot_api.INTERNAL_METHODS_BY_NAME['register_object']
    n = len(row.params)
    assert n == 7
    # Python: the list literal Greifobjekt.__init_subclass__ hands to _rpc.call.
    tree = ast.parse(_STUB)
    cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == 'Greifobjekt')
    init = next(x for x in cls.body if isinstance(x, ast.FunctionDef)
                and x.name == '__init_subclass__')
    sends = [c for c in ast.walk(init) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute) and c.func.attr == 'call'
             and isinstance(c.args[0], ast.Constant) and c.args[0].value == 'register_object']
    assert len(sends) == 1
    positionals = sends[0].args[1]
    assert isinstance(positionals, ast.List) and len(positionals.elts) == n
    assert sends[0].args[2].value == row.budget
    # Java: the Object[] literal Greifobjekt's private constructor hands over.
    java = _RENDERED[robot_api.GENERATED_PATHS['java_greifobjekt']]
    m = re.search(r'RpcClient\.call\("register_object", new Object\[\] \{([^}]*)\}, "(\w+)"\)', java)
    assert m, 'register_object call not found in Greifobjekt.java'
    java_args = [a.strip() for a in m.group(1).split(',')]
    assert len(java_args) == n
    assert m.group(2) == row.budget
    # Same ORDER by name in both: the Python list names the row's fields and the
    # Java list the camelCased row's fields (name/label come from the label).
    py_names = [ast.unparse(e) for e in positionals.elts]
    assert py_names == ['name', 'label', 'list(cls.tag_ids)', 'cls.hoehe_m',
                        'cls.greiftiefe_m', 'cls.greifer_schliessen_rad',
                        'cls.anfahrhoehe_m']
    assert java_args == ['this.name', 'label', 'tagIds', 'hoeheM', 'greiftiefeM',
                         'greiferSchliessenRad', 'anfahrhoeheM']


def test_java_rpc_client_carries_the_limits_of_the_table():
    java = _RENDERED[robot_api.GENERATED_PATHS['java_rpc_client']]
    L = robot_api.RPC_LIMITS
    for name, value in (('MAX_CALLS_PER_S', L.MAX_CALLS_PER_S), ('BURST', L.BURST),
                        ('PERCEPTION_MAX_PER_S', L.PERCEPTION_MAX_PER_S),
                        ('PERCEPTION_BURST', L.PERCEPTION_BURST),
                        ('MAX_FRAME_BYTES', L.MAX_FRAME_BYTES)):
        m = re.search(rf'static final (?:double|int) {name} = ([0-9.]+);', java)
        assert m, name
        assert float(m.group(1)) == float(value), name


def test_java_robot_has_every_row_and_the_object_overloads():
    java = _RENDERED[robot_api.GENERATED_PATHS['java_robot']]
    for call in robot_api.ROBOT_API:
        assert re.search(rf'public static \S+ {call.java_name}\(', java), call.java_name
    # `obj` takes a String or a Greifobjekt; `target` a double[] or a String.
    assert 'grasp(String obj)' in java and 'grasp(Greifobjekt obj)' in java
    assert 'moveTo(double[] target)' in java and 'moveTo(String target)' in java
    # A trailing default renders an extra overload.
    assert 'replay(String name, double speed)' in java and 'replay(String name)' in java


def test_zeige_renders_in_both_stubs_and_the_asset_tag_in_neither():
    """2026-09-27 (O3): `zeige` is public in the Python stub and in Robot.java
    with one overload per Java value type; the asset tags are JSON-only, so no
    stub carries the word."""
    tree = ast.parse(_STUB)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'zeige')
    assert [a.arg for a in fn.args.args] == ['name', 'wert']
    assert 'asset' not in _STUB
    java = _RENDERED[robot_api.GENERATED_PATHS['java_robot']]
    for jtype in ('int', 'long', 'double', 'boolean', 'char', 'String', 'double[]',
                  'int[]', 'Greifziel', 'Object'):
        assert f'public static void zeige(String name, {jtype} wert)' in java, jtype
    assert java.count('public static void zeige(') == 10
    for rel in (robot_api.GENERATED_PATHS['java_robot'],
                robot_api.GENERATED_PATHS['java_greifobjekt'],
                robot_api.GENERATED_PATHS['java_rpc_client']):
        assert 'asset' not in _RENDERED[rel], rel


def test_the_python_zeige_value_is_bounded_and_json_safe(tmp_path, monkeypatch):
    """`_shown` renders any value into a bounded JSON-safe shape without a
    second json.dumps in the stub (the encoder fence above pins exactly one):
    non-finite floats and huge ints become text, containers are capped, a
    hostile __repr__ cannot break the call, and the rendered frame stays far
    below MAX_FRAME_BYTES whatever the student hands over."""
    monkeypatch.delenv('CODE_RPC_SOCKET', raising=False)
    robot = _load_stub(tmp_path)
    sent = []
    monkeypatch.setattr(robot._rpc, 'call', lambda m, a, k: sent.append((m, a, k)))

    class Boese:
        def __repr__(self):
            raise RuntimeError('nope')

    import json as _json
    import math as _math
    robot.zeige('a', 3)
    robot.zeige('b', _math.nan)
    robot.zeige('c', 10 ** 5000)
    robot.zeige('d', [[['x' * 5000] * 80] * 80] * 80)
    robot.zeige('e', Boese())
    robot.zeige('f', {'x': (1, 2.5, None, True)})
    robot.zeige('g', robot.Greifziel(4))
    assert [m for m, _a, _k in sent] == ['zeige'] * 7
    assert all(k == 'call' for _m, _a, k in sent)
    values = {a[0]: a[1] for _m, a, _k in sent}
    assert values['a'] == 3
    assert values['b'] == 'nan'
    assert isinstance(values['c'], str)
    assert values['e'] == '<?>'
    assert values['f'] == {'x': [1, 2.5, None, True]}
    assert values['g'] == 'Greifziel(4)'
    for _m, a, _k in sent:
        body = _json.dumps({'id': 1, 'm': 'zeige', 'a': a}, ensure_ascii=False,
                           allow_nan=False).encode('utf-8')
        assert len(body) < robot_api.RPC_LIMITS.MAX_FRAME_BYTES // 4, len(body)


# ── the stub as a Python module ───────────────────────────────────────────

def _load_stub(tmp_path: Path):
    path = tmp_path / 'robot.py'
    path.write_text(_STUB, encoding='utf-8')
    spec = importlib.util.spec_from_file_location('robot_stub_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stub_imports_without_a_socket_and_refuses_calls_in_german(tmp_path, monkeypatch):
    monkeypatch.delenv('CODE_RPC_SOCKET', raising=False)
    monkeypatch.delenv('CODE_RUN_TOKEN', raising=False)
    robot = _load_stub(tmp_path)
    with pytest.raises(robot.RobotError) as exc:
        robot.home()
    assert 'Roboter' in str(exc.value)
    with pytest.raises(robot.RobotError):
        robot.move_above('not a Greifziel')
    with pytest.raises(robot.RobotError):
        robot.sees(42)


def test_stub_connects_to_a_missing_socket_path_with_a_german_error(tmp_path, monkeypatch):
    monkeypatch.setenv('CODE_RPC_SOCKET', str(tmp_path / 'nirgends.sock'))
    monkeypatch.setenv('CODE_RUN_TOKEN', '0' * 32)
    robot = _load_stub(tmp_path)
    with pytest.raises(robot.RobotError) as exc:
        robot.home()
    assert 'Roboter' in str(exc.value)


def test_greifobjekt_subclass_registers_at_definition_time(tmp_path, monkeypatch):
    """A subclass sends the seven positionals in the row's order the moment it
    is defined; with no robot the definition itself fails loud in German."""
    monkeypatch.delenv('CODE_RPC_SOCKET', raising=False)
    monkeypatch.delenv('CODE_RUN_TOKEN', raising=False)
    robot = _load_stub(tmp_path)
    sent = []

    def fake_call(method, args, kind):
        sent.append((method, list(args), kind))
        return None

    monkeypatch.setattr(robot._rpc, 'call', fake_call)

    class Banane(robot.Greifobjekt):
        tag_ids = [30, 31]
        hoehe_m = 0.040
        greiftiefe_m = 0.015

    assert sent == [('register_object',
                     ['banane', 'Banane', [30, 31], 0.040, 0.015, None, None],
                     'perception')]
    assert robot._type_name(Banane) == 'banane'

    monkeypatch.setattr(robot._rpc, 'call', robot._Rpc.call.__get__(robot._rpc))
    with pytest.raises(robot.RobotError):
        class Kiwi(robot.Greifobjekt):  # noqa: F841 — the definition is the call
            tag_ids = [40]
            hoehe_m = 0.03
            greiftiefe_m = 0.01


def test_stub_compiles_under_the_runner_python_floor():
    """B12: the runner is Python ≥ 3.12; the stub must at least compile on the
    interpreter running this suite (3.12 in the lerobot env, 3.14 on the host)."""
    assert sys.version_info >= (3, 12)
    compile(_STUB, 'robot.py', 'exec')
    proc = subprocess.run([sys.executable, '-c', 'import ast, sys; ast.parse(sys.stdin.read())'],
                          input=_STUB, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
