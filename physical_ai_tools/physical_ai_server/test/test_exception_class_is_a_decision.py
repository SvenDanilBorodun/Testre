#!/usr/bin/env python3
"""Where the exception CLASS is a decision, both directions are asserted.

``GraspSkip`` IS a ``WorkflowError``, so ``pytest.raises(WorkflowError)`` cannot
tell the two apart — and the class IS the behaviour: a ``GraspSkip`` is
swallowed by the „Solange sichtbar" loop and the run continues; a base
``WorkflowError`` ends it. Flipping ``motion._refuse_greifziel`` from one to the
other survived the ENTIRE suite (1482 passed / 4 skipped, identical to baseline)
while being hit NINE times by a test that was aimed straight at it.

A BLANKET rule was measured and refused, with the numbers that refuse it:

    raises(WorkflowError) sites in the whole suite            : 93
    …with an explicit `not isinstance(exc.value, GraspSkip)`  :  5
    …in the 8 files that mention GraspSkip at all             : 47

A blanket gate demands ~40 new assertions of which most are meaningless — floor
refusals, zone refusals and jog paths cannot raise a ``GraspSkip`` at all. So
this is the data-driven version instead: ONE table, in ONE place, naming the
production symbols whose class is a decision rather than an accident. A fifth
entry joins in one line. Same pattern as ``enum_parity.NAME_ALIASES`` and
``compose_env_parity.INTENTIONAL``, which this repo already runs.
"""

from __future__ import annotations

import threading

import pytest

from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers.motion import GraspSkip, WorkflowError
from physical_ai_server.workflow.perception import Detection


# symbol -> the class its FAILING path must raise.
#   'WorkflowError' — a PROGRAM error: no loop pass and no retry can fix it, so
#                     it must END the run rather than be swallowed.
#   'both'          — the symbol SPLITS on world state and both branches matter.
CLASS_IS_A_DECISION = {
    'motion._refuse_greifziel': 'WorkflowError',
    'motion._no_greifziel_error': 'both',
    'motion.close_on_object': 'WorkflowError',
    'motion._greifziel_xyz_roll': 'WorkflowError',
}


class _Ctx:
    def __init__(self, last_find_failure=None):
        self.motion_lock = threading.RLock()
        self.logs: list[str] = []
        self.published: list = []
        self.should_stop = lambda: False
        self.last_find_failure = last_find_failure

    def log(self, m):
        self.logs.append(m)

    def publisher(self, chunk):
        self.published.append(list(chunk))


def _greifziel(tag_id=22):
    return Detection(centroid_px=(1, 1), bbox_px=(0, 0, 1, 1), confidence=1.0,
                     label=f'tag{tag_id}', aruco_id=tag_id,
                     world_xyz_m=(0.18, 0.0, 0.015))


def test_the_table_names_only_symbols_that_exist():
    """A stale entry is worse than no entry: it claims a decision is guarded."""
    import physical_ai_server.workflow.handlers.motion as _m
    for dotted in CLASS_IS_A_DECISION:
        mod, _, name = dotted.partition('.')
        assert mod == 'motion', dotted
        assert getattr(_m, name, None) is not None, (
            f'{dotted} is in CLASS_IS_A_DECISION but no longer exists')


def test_refuse_greifziel_is_a_program_error_not_a_skip():
    """C-7. A Greifziel dragged into a DESTINATION socket („aufnehmen", „bewege
    zu", „ablegen bei") is a wiring mistake — no pass can turn it into a
    destination. As a ``GraspSkip`` the loop swallowed it and the arm descended
    on the cube three times with an open gripper, run green."""
    with pytest.raises(WorkflowError) as exc:
        motion._refuse_greifziel(_greifziel(), 'aufnehmen')
    assert not isinstance(exc.value, GraspSkip)


def test_close_on_object_refuses_a_non_greifziel_as_a_program_error():
    """A value that is NOT a Greifziel (a number, a text, a position dict — the
    check:'Greifziel' socket accepts any `variables_get`, which has output:null)
    used to fall through to the profile's HARDEST close: measured with a
    variable holding 42.0, commanded −0.5 / 0.0 / 0.0 with an EMPTY log on all
    three arms."""
    with pytest.raises(WorkflowError) as exc:
        motion.close_on_object(_Ctx(), {'ziel': 42.0})
    assert not isinstance(exc.value, GraspSkip)


def test_greifziel_xyz_roll_refuses_a_wrong_value_as_a_program_error():
    with pytest.raises(WorkflowError) as exc:
        motion._greifziel_xyz_roll(_Ctx(), 'Kiste', 'senke auf')
    assert not isinstance(exc.value, GraspSkip)


def test_no_greifziel_error_splits_on_the_recorded_reason():
    """BOTH directions, because this one is deliberately not constant.

    A RECORDED reason is a per-instance WORLD failure („außerhalb des
    Greifbereichs") that another loop pass on another object can succeed at →
    ``GraspSkip``. NO recorded reason means „finde …" never ran, i.e. the socket
    is empty in the PROGRAM → a base ``WorkflowError`` that ends the run instead
    of being swallowed for MAX_LOOP_ITERATIONS passes."""
    empty = motion._no_greifziel_error(_Ctx())
    assert isinstance(empty, WorkflowError) and not isinstance(empty, GraspSkip)

    recorded = motion._no_greifziel_error(
        _Ctx(last_find_failure='„Würfel" liegt außerhalb des Greifbereichs'))
    assert isinstance(recorded, GraspSkip), (
        'a per-instance world failure must stay loop-swallowable')
    assert 'Greifbereich' in str(recorded)


def test_every_table_entry_has_a_test_here():
    """The table is the index; this is what stops a fifth entry being added
    without its assertions."""
    import inspect
    src = inspect.getsource(__import__(__name__, fromlist=['x']))
    for dotted in CLASS_IS_A_DECISION:
        name = dotted.split('.', 1)[1].lstrip('_')
        assert src.count(name) >= 2, (
            f'{dotted} is in the table but nothing here exercises it')
