"""Batch 2b — the on_manual relaxation of ``_assert_no_other_active``.

Extracts the REAL method by ``ast`` (physical_ai_server.py imports rclpy and
cannot be imported in CI) and execs it onto a stub ``self`` — the deps-free
pattern from ``test_workshop_capture_pose.py``. Verifies:

* a 'manual' request RELAXES against on_manual (jog/record/replay/capture coexist)
  yet still refuses on collision / recording / inference / training / calibration
  / workflow;
* every NON-manual request refuses when on_manual is set.
"""

from __future__ import annotations

import ast
import textwrap
import types
from pathlib import Path

import pytest

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)


def _load_method(name):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            ns: dict = {}
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'method {name} not found')


_assert = _load_method('_assert_no_other_active')

_ALL_FLAGS = dict(
    _collision_active=False,
    on_recording=False,
    on_inference=False,
    is_training=False,
    on_calibration=False,
    on_workflow=False,
    on_manual=False,
)


def _node(**overrides):
    st = types.SimpleNamespace(**{**_ALL_FLAGS, **overrides})
    return st


def test_idle_allows_every_mode():
    for mode in ('manual', 'recording', 'inference', 'workflow', 'calibration'):
        ok, msg = _assert(_node(), mode)
        assert ok is True and msg == '', mode


# --- manual RELAXES against on_manual --------------------------------------

def test_manual_request_relaxes_against_on_manual():
    ok, msg = _assert(_node(on_manual=True), 'manual')
    assert ok is True and msg == ''


def test_manual_request_still_refuses_on_other_owners():
    for flag in ('_collision_active', 'on_recording', 'on_inference',
                 'is_training', 'on_calibration', 'on_workflow'):
        ok, msg = _assert(_node(**{flag: True}), 'manual')
        assert ok is False, flag
        assert msg  # German, non-empty


# --- non-manual modes refuse while on_manual is set ------------------------

@pytest.mark.parametrize('mode', ['recording', 'inference', 'workflow',
                                  'calibration', 'training'])
def test_non_manual_refused_while_manual_active(mode):
    ok, msg = _assert(_node(on_manual=True), mode)
    assert ok is False
    assert 'Handbetrieb' in msg


def test_workflow_still_relaxes_against_on_workflow():
    # The pre-existing relaxation (a workflow request skips the on_workflow
    # refusal) must be untouched by the new on_manual clause.
    ok, msg = _assert(_node(on_workflow=True), 'workflow')
    assert ok is True and msg == ''


def test_missing_on_manual_attr_defaults_false():
    # getattr-guard: a stub self without on_manual must not raise.
    st = types.SimpleNamespace(
        _collision_active=False, on_recording=False, on_inference=False,
        is_training=False, on_calibration=False, on_workflow=False,
    )
    ok, msg = _assert(st, 'recording')
    assert ok is True and msg == ''


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
