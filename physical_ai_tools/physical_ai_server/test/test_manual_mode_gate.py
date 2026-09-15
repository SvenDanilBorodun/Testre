"""Batch 2b — the on_manual relaxation of ``_assert_no_other_active``.

Extracts the REAL method by ``ast`` (physical_ai_server.py imports rclpy and
cannot be imported in CI) and execs it onto a stub ``self`` — the deps-free
pattern from ``test_workshop_capture_pose.py``. Verifies:

* a 'manual' request RELAXES against on_manual (jog/record/replay/capture coexist)
  yet still refuses on collision / recording / inference / training / calibration
  / workflow;
* every NON-manual request refuses when on_manual is set;
* D8 — a 'leader_teach' request is refused by every owner (itself included),
  every non-capture mode refuses while on_leader_teach is set, and 'capture'
  relaxes against on_manual AND on_leader_teach.
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
    on_leader_teach=False,
)

# The owner flags in the order the gate checks them, with the sentence each one
# refuses a 'leader_teach' request with (the D8 matrix, §5.3).
_OWNER_SENTENCES = [
    ('_collision_active', 'Eine Kollision wird gerade behoben — bitte zuerst die '
                          'Schritte im Hinweisfenster abschließen.'),
    ('on_recording', 'Aufnahme läuft gerade — bitte zuerst stoppen.'),
    ('on_inference', 'Inferenz läuft gerade — bitte zuerst stoppen.'),
    ('is_training', 'Training läuft gerade — bitte abwarten oder abbrechen.'),
    ('on_calibration', 'Kalibrierung läuft gerade — bitte zuerst beenden.'),
    ('on_workflow', 'Ein Workflow läuft gerade — bitte zuerst stoppen.'),
    ('on_manual', 'Handbetrieb ist aktiv — bitte zuerst den Handbetrieb beenden.'),
    ('on_leader_teach', 'Eine Leader-Aufnahme läuft gerade — bitte zuerst beenden.'),
]


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


# --- D8: leader_teach + capture --------------------------------------------

@pytest.mark.parametrize('flag,sentence', _OWNER_SENTENCES)
def test_leader_teach_refused_by_every_owner(flag, sentence):
    # Every owner refuses a leader take — itself included (a second tab).
    ok, msg = _assert(_node(**{flag: True}), 'leader_teach')
    assert ok is False
    assert msg == sentence


@pytest.mark.parametrize('mode', ['manual', 'recording', 'inference', 'workflow',
                                  'calibration', 'training', 'leader_teach'])
def test_non_capture_modes_refused_while_leader_teach(mode):
    ok, msg = _assert(_node(on_leader_teach=True), mode)
    assert ok is False
    assert 'Leader-Aufnahme' in msg


def test_manual_refused_while_leader_teach():
    ok, msg = _assert(_node(on_leader_teach=True), 'manual')
    assert (ok, msg) == (False, 'Eine Leader-Aufnahme läuft gerade — bitte zuerst beenden.')


@pytest.mark.parametrize('flag', ['on_manual', 'on_leader_teach'])
def test_capture_relaxes_against_manual_and_leader_teach(flag):
    ok, msg = _assert(_node(**{flag: True}), 'capture')
    assert ok is True and msg == ''
    ok, msg = _assert(_node(on_manual=True, on_leader_teach=True), 'capture')
    assert ok is True and msg == ''


@pytest.mark.parametrize('flag,sentence', _OWNER_SENTENCES[:6])
def test_capture_still_refuses_on_other_owners(flag, sentence):
    ok, msg = _assert(_node(**{flag: True}), 'capture')
    assert (ok, msg) == (False, sentence)


@pytest.mark.parametrize('mode', ['recording', 'inference', 'workflow', 'calibration'])
def test_other_modes_refuse_while_leader_teach_even_vs_their_own_flag(mode):
    # workflow relaxes vs on_workflow and calibration vs on_calibration, but
    # neither relaxes against a leader take.
    own = {'workflow': 'on_workflow', 'calibration': 'on_calibration'}.get(mode)
    flags = {'on_leader_teach': True}
    if own:
        flags[own] = True
    ok, msg = _assert(_node(**flags), mode)
    assert ok is False
    assert 'Leader-Aufnahme' in msg


def test_missing_on_leader_teach_attr_defaults_false():
    st = types.SimpleNamespace(
        _collision_active=False, on_recording=False, on_inference=False,
        is_training=False, on_calibration=False, on_workflow=False,
        on_manual=False,
    )
    for mode in ('leader_teach', 'capture', 'manual', 'workflow'):
        ok, msg = _assert(st, mode)
        assert ok is True and msg == '', mode


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
