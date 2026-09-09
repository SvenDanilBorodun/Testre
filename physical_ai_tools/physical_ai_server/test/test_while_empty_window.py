"""SEPARABLE CHANGE — the „Solange sichtbar" empty window: 2 looks → 3.

Deliberately its own file, so it can be committed or reverted on its own. The whole
change is TWO things: the ``WHILE_EMPTY_FRAMES`` default in
``workflow/interpreter.py`` and this file. It is INDEPENDENT of the reclaim
redesign in ``test_while_visible_loop.py`` — that one decides WHICH objects a loop
grasps, this one decides HOW LONG the loop waits before calling it a day.

WHY. With the reclaim rebuilt around position, „put the cube back and watch it get
picked up again" works whenever the student gets the object onto the table before
the empty window closes. Measured over 40 realistic one-cube student timings:
21/40 at two looks, 38/40 at three — the remaining failures are simply a student
slower than the loop, not a rule refusing to fire.

WHAT IT COSTS, and it is paid by everyone: +7.7 s before EVERY „Solange sichtbar"
loop reports „nichts mehr sichtbar — fertig", on every program and every arm,
including the ones where nobody intends to put anything back.
``EDUBOTICS_WHILE_EMPTY_FRAMES=2`` is the one-variable rollback.

The harness (synthetic overhead camera, real IK solver, real catalog) is shared
with test_while_visible_loop.py rather than copied — cross-test-module imports are
an established pattern in this suite (see test_degraded_boot_recovery.py).
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from physical_ai_server.workflow import interpreter as interp_mod
from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.interpreter import Interpreter
from test_while_visible_loop import _Ctx, _StubPerception, _det, _while_block


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Same speed harness as test_while_visible_loop's, MINUS its
    ``WHILE_EMPTY_FRAMES`` pin — that pin is exactly what these tests measure."""
    state = {'t': 0.0}

    def _mono():
        state['t'] += 1000.0
        return state['t']

    monkeypatch.setattr(trajectory_builder, 'time',
                        types.SimpleNamespace(monotonic=_mono, sleep=lambda _s: None))
    monkeypatch.setattr(interp_mod, 'WHILE_EMPTY_SECONDS', 0.0)
    monkeypatch.setattr(interp_mod, 'WHILE_SETTLE_S', 0.0)
    yield


def test_the_shipped_empty_window_is_three_looks():
    """A source fence, because every other test in the suite monkeypatches this
    constant — so the SHIPPED default is not observable from the attribute."""
    src = Path(interp_mod.__file__).read_text(encoding='utf-8')
    m = re.search(
        r"WHILE_EMPTY_FRAMES\s*=\s*_env_int\(\s*'EDUBOTICS_WHILE_EMPTY_FRAMES'\s*,\s*(\d+)\s*\)",
        src)
    assert m is not None, 'the WHILE_EMPTY_FRAMES default moved or was renamed'
    assert int(m.group(1)) == 3


def test_two_empty_looks_no_longer_end_the_loop(monkeypatch):
    """The behavioural half: a student who is two looks slow keeps the loop.

    At two frames the loop terminates on the second empty look and the object
    that arrives on the third is never grasped — which is the whole 21/40.
    """
    monkeypatch.setattr(interp_mod, 'WHILE_EMPTY_FRAMES', 3)
    ctx = _Ctx(_StubPerception(
        lambda call: [] if call <= 2 else [_det(20, (0.18, 0.0))]))
    Interpreter([])._exec_while_visible(_while_block(), ctx, lambda *a: None)
    assert ctx.claimed_tags == {20}


def test_two_empty_looks_would_have_ended_it_before(monkeypatch):
    """The counter-measurement: the same scene under the OLD default loses it.

    Pinned so the +7.7 s is never paid for nothing — if this ever starts passing,
    the empty window stopped being what ends the loop and the trade-off above has
    to be re-argued.
    """
    monkeypatch.setattr(interp_mod, 'WHILE_EMPTY_FRAMES', 2)
    ctx = _Ctx(_StubPerception(
        lambda call: [] if call <= 2 else [_det(20, (0.18, 0.0))]))
    Interpreter([])._exec_while_visible(_while_block(), ctx, lambda *a: None)
    assert ctx.claimed_tags == set()
