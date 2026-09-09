#!/usr/bin/env python3
"""ONE German sentence for the destination alphabet, ONE server-side gate.

Audit `docs/plans/audit-G10-G12.md` §7.4–7.6 + §8.1. Two independent findings,
one fix each:

* §7.6 / §8.1 — of the three SERVICE writers into ``_persisted_destinations``,
  only ``capture_pose_callback`` validated anything. ``mark_destination_callback``
  stored ``request.label`` verbatim and handed ``corr_x`` / ``corr_y`` to
  ``set_destination`` with no finiteness check, twenty lines of the same feature
  away. The fix is ONE check inside ``WorkflowManager.set_destination`` — the
  place all three writers and all three block handlers pass through, "the only
  place that cannot be forgotten again".
* §7.4 / §7.5 — the shared sentence promised „höchstens 40 Zeichen" while the
  student's Blockly field stops at 24, and a THIRD validator
  (``CameraFeedOverlay``, the PRIMARY way a destination is created) still emitted
  the bare „Ungültiger Ziel-Name." the rest of the feature had replaced.

Every expected value below is a LITERAL (test-quality rule B-0): never the
symbol under test on both sides of the assertion.
"""

from __future__ import annotations

import math

import pytest

from physical_ai_server.workflow.handlers import destinations as dst
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.workflow_manager import WorkflowManager


def _wfm() -> WorkflowManager:
    return WorkflowManager(publisher=lambda _p: None, load_destinations=lambda: {})


# ── the shared sentence ─────────────────────────────────────────────────────

def test_the_alphabet_sentence_names_the_alphabet_and_no_character_count():
    """§7.4: the editor caps at 24, the camera prompt and the regex at 40, so a
    number in this sentence is wrong on at least one surface that shows it."""
    sentence = dst._NAME_ALPHABET_DE
    assert 'Buchstaben' in sentence
    assert 'Ziffern' in sentence
    assert 'Bindestrich' in sentence
    assert '40' not in sentence
    assert '24' not in sentence
    assert 'Zeichen' not in sentence


@pytest.mark.parametrize('name', ['A!', 'A/B', '日本', '😀', 'A\nB', 'A]B', 'x' * 41,
                                  '', '   ', None, 7])
def test_the_pure_name_predicate_refuses_and_names_both(name):
    message = dst.destination_name_error_de(name)
    assert message is not None, name
    assert message.startswith('Ungültiger Ziel-Name: ')
    assert 'Buchstaben' in message and 'Bindestrich' in message
    # The echo must never carry a log sentinel or a line break back out.
    assert '[' not in message and ']' not in message
    assert '\n' not in message


@pytest.mark.parametrize('name', ['A', 'A B-c_Ä', 'Ablage über Tisch', 'x' * 40,
                                  'ÄÖÜäöüß', 'Ziel_1'])
def test_the_pure_name_predicate_accepts_the_alphabet(name):
    assert dst.destination_name_error_de(name) is None, name


def test_the_pure_coordinate_predicate_refuses_only_non_finite():
    assert dst.destination_coordinate_error_de('A', 0.1, 0.2, 0.3) is None
    for bad in (float('nan'), float('inf'), float('-inf')):
        message = dst.destination_coordinate_error_de('A', bad, 0.0, 0.0)
        assert message is not None
        assert 'Ziel „A"' in message
        assert 'Koordinaten' in message


def test_the_block_handlers_still_raise_the_same_text_as_the_predicate():
    """The three raisers must not drift into three sentences."""
    class _Ctx:
        destinations: dict = {}
        z_table = None
        log = staticmethod(lambda _m: None)

    with pytest.raises(WorkflowError) as exc:
        dst.destination_pin(_Ctx(), {'name': 'A!', 'x': 0.1, 'y': 0.0, 'z': 0.0})
    assert str(exc.value) == dst.destination_name_error_de('A!')


# ── the ONE server-side gate ────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['A!', 'A/B', '日本', 'A\nB', 'A]B', 'x' * 41])
def test_set_destination_refuses_a_name_outside_the_alphabet(name):
    """§7.6: this is what ``mark_destination_callback`` had no fence against."""
    manager = _wfm()
    with pytest.raises(ValueError) as exc:
        manager.set_destination(name, 0.2, 0.0, 0.01)
    assert 'Ungültiger Ziel-Name' in str(exc.value)
    assert 'Bindestrich' in str(exc.value)
    assert manager.get_destinations() == {}


@pytest.mark.parametrize('coords', [
    (float('nan'), 0.0, 0.0),
    (0.0, float('inf'), 0.0),
    (0.0, 0.0, float('-inf')),
    (float('1e400'), 0.0, 0.0),
])
def test_set_destination_refuses_non_finite_coordinates(coords):
    """§8.1: `corr_x`/`corr_y` reached this dict with no finiteness check."""
    manager = _wfm()
    with pytest.raises(ValueError) as exc:
        manager.set_destination('A', *coords)
    assert 'Koordinaten' in str(exc.value)
    assert manager.get_destinations() == {}


def test_set_destination_refuses_a_coordinate_that_is_not_a_number_at_all():
    manager = _wfm()
    with pytest.raises(ValueError):
        manager.set_destination('A', 'nicht-eine-zahl', 0.0, 0.0)
    assert manager.get_destinations() == {}


def test_set_destination_still_stores_a_good_pin_unchanged():
    manager = _wfm()
    manager.set_destination('Ablage', 0.234, -0.012, 0.045)
    stored = manager.get_destinations()['Ablage']
    assert stored['x'] == pytest.approx(0.234)
    assert stored['y'] == pytest.approx(-0.012)
    assert stored['z'] == pytest.approx(0.045)
    assert stored['label'] == 'Ablage'


def test_an_empty_name_is_still_the_silent_no_op_it_always_was():
    """`mark_destination_callback` guards on `request.label` before calling, and
    an empty name has no student-visible click behind it — keep the historical
    no-op rather than inventing a refusal for a case nobody can reach."""
    manager = _wfm()
    manager.set_destination('', 0.2, 0.0, 0.0)
    assert manager.get_destinations() == {}


def test_a_whitespace_only_name_is_refused_loudly_not_stored_blank():
    """The camera prompt's own regex ACCEPTS '   ' (space is in the alphabet)
    and `mark_destination_callback` passed `request.label` verbatim, so a blank
    key landed in the store under a green „gespeichert.". Unlike the empty
    string this IS reachable from a student click, so it refuses rather than
    no-ops."""
    manager = _wfm()
    with pytest.raises(ValueError) as exc:
        manager.set_destination('   ', 0.2, 0.0, 0.0)
    assert 'Ungültiger Ziel-Name' in str(exc.value)
    assert manager.get_destinations() == {}


def test_a_padded_name_is_stored_under_the_key_the_block_uses():
    """React's `nameValidator` trims, so the Blockly block carries 'A'. The
    service used to store ' A ' — a key `destination_ref` could never match."""
    manager = _wfm()
    manager.set_destination('  Ablage  ', 0.2, 0.0, 0.0)
    assert list(manager.get_destinations()) == ['Ablage']
    assert manager.get_destinations()['Ablage']['label'] == 'Ablage'


def test_math_is_imported_for_the_non_finite_literals_above():
    assert math.isnan(float('nan'))
