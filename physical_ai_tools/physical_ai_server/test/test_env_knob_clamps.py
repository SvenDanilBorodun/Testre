#!/usr/bin/env python3
"""Env knobs that bound how long the arm is OWNED, and one that crashed at import.

``EDUBOTICS_TAG_YAW_FRAMES`` is the one this file exists for, and it is not a
nit. ``_sample_tag_yaw`` runs ``2 * N`` detect attempts at ≈ 0.0752 s each
(N = 7 → 0.510 s, N = 200 → 15.03 s, N = 8000 → ≈ 601 s, doubled again by
``GRASP_RETRY + 1``), and its only caller is ``grasp_object``, which holds
``ctx.motion_lock`` for the WHOLE grasp. Since that lock lost its bound, „every
holder is bounded" is the argument that replaces it — so an UNCAPPED frame count
is a hole in the argument, not a nit.

Its second fault crashed the module: ``_safe_float`` catches TypeError and
ValueError, but the surrounding ``int()`` does not, so ``=inf`` raised
OverflowError and ``=nan`` raised ValueError AT IMPORT, taking
``handlers/__init__``'s dispatch tables down with ``perception_blocks`` — the
exact cascade ``_safe_float``'s own docstring says it exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from physical_ai_server.workflow import calibration_manager as CM
from physical_ai_server.workflow.handlers import perception_blocks as PB


def test_the_tag_yaw_frame_ceiling_is_thirty():
    """A LITERAL pin, read from the SOURCE: the module attribute is env-derived,
    so ``assert PB._TAG_YAW_FRAMES_MAX == 30`` passes with the env var set."""
    src = Path(PB.__file__).read_text(encoding='utf-8')
    m = re.search(r'^_TAG_YAW_FRAMES_MAX = (\d+)$', src, re.M)
    assert m, '_TAG_YAW_FRAMES_MAX moved or stopped being a plain literal'
    assert m.group(1) == '30', (
        '30 frames ≈ 1.8 s of sampling under the motion lock, already 4× the '
        'shipped default of 7')


@pytest.mark.parametrize('raw,expected,warns', [
    ('7', 7, False),        # the shipped default, unchanged
    ('30', 30, False),      # EXACTLY the ceiling is accepted
    ('31', 30, True),       # one over clamps
    ('8000', 30, True),     # the ~601 s case
    ('1', 1, False),        # exactly the floor
    ('0', 1, True),
    ('-5', 1, True),
    ('inf', 7, True),       # used to raise OverflowError at IMPORT
    ('nan', 7, True),       # used to raise ValueError at IMPORT
    ('1e9', 30, True),      # ~2.4 years of sampling
    ('nonsense', 7, False),  # _safe_float's own default path, unchanged
])
def test_the_tag_yaw_frame_count_is_clamped_at_both_ends(raw, expected, warns,
                                                         monkeypatch, capsys):
    monkeypatch.setenv('EDUBOTICS_TAG_YAW_FRAMES', raw)
    assert PB._clamped_tag_yaw_frames() == expected
    said = capsys.readouterr().out
    assert bool('[WARNUNG]' in said) is warns, (
        f'{raw!r} → {expected}: an out-of-range knob falls back LOUDLY, '
        f'mirroring the EDUBOTICS_EDU6_* knobs. said={said!r}')


def test_the_shipped_default_is_seven_frames():
    """The other half of the two-sided pin: the DEFAULT, not just the ceiling."""
    src = Path(PB.__file__).read_text(encoding='utf-8')
    assert re.search(r"_safe_float\('EDUBOTICS_TAG_YAW_FRAMES', 7\.0\)", src)


# ══════════════════════════════════════════════════════════════════════════
# G-4 — one floor, one number, one German word
# ══════════════════════════════════════════════════════════════════════════

def test_the_node_verify_gate_interpolates_the_managers_floor():
    """``physical_ai_server.py::_verify_solve`` said „Mindestens ZWEI Prüfpunkte"
    while ``calibration_manager.fit_xy_correction`` refuses below THREE. At
    n = 2 the node's gate passed and the manager's „drei" refusal was surfaced
    verbatim, so the stale text actually fired at n ∈ {0, 1} — telling a student
    two points are enough while the layer below refuses them. React never lets
    them press „Korrektur berechnen" below 4, so the whole gate is
    rosbridge-only; nothing pinned either string.

    *Kills:* the literal coming back, in either message."""
    node_src = (Path(CM.__file__).parent.parent / 'physical_ai_server.py'
                ).read_text(encoding='utf-8')
    assert 'Mindestens zwei Prüfpunkte' not in node_src
    assert re.search(r"f'Mindestens \{_VERIFY_MIN_POINTS\} Prüfpunkte", node_src), (
        'the floor must be INTERPOLATED from calibration_manager, not restated')
    assert re.search(r'if n < _VERIFY_MIN_POINTS:', node_src)


def test_both_layers_use_the_students_own_word():
    """The step is called „Genauigkeit prüfen", so „Prüfpunkte" is the student's
    own word. „Referenzpunkte" was a second German word for one thing.

    (The BEHAVIOURAL half is
    ``test_rs_sub_perception_calib::test_two_points_are_refused``, which has the
    CALIB_DIR fixture this file deliberately does not; here it is a source
    fence, because what has to be pinned is that neither layer restates the
    other's vocabulary.)"""
    import ast
    src = Path(CM.__file__).read_text(encoding='utf-8')
    # STRING LITERALS only: the comment explaining the change naturally names
    # the word it replaced, and a raw text scan would forbid documenting it.
    literals = [n.value for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    offenders = [lit for lit in literals if 'Referenzpunkte' in lit]
    assert offenders == [], (
        'two German words for one thing — the student pressed „Genauigkeit '
        f'prüfen", so their word is „Prüfpunkte": {offenders}')
    assert re.search(r"f'Mindestens \{VERIFY_MIN_POINTS\} Prüfpunkte", src)


def test_the_verify_floor_is_three():
    """LITERAL pin. Two point pairs give 4 equations for a 4-DOF similarity, so
    the residual is 0.000000 mm for ANY input — a quality number that cannot
    fail, in every draw measured."""
    src = Path(CM.__file__).read_text(encoding='utf-8')
    m = re.search(r'^VERIFY_MIN_POINTS = (\d+)$', src, re.M)
    assert m and m.group(1) == '3'


# ══════════════════════════════════════════════════════════════════════════
# B-4 — the wizard's first-paint tap count, fenced against the SERVER's floor
# ══════════════════════════════════════════════════════════════════════════

def test_the_react_tap_fallback_equals_the_server_requirement():
    """``TableTouchStep``'s first-paint value, before ``frames_required``
    arrives over the wire.

    A LITERAL pin on the JS side (`expect(TAP_COUNT_FALLBACK).toBe(4)`) would
    create a THIRD opinion about a number the SERVER owns
    (``calibration_manager.TABLE_TOUCH_POINTS_REQUIRED``) — which is exactly how
    the „mindestens 3" mismatch shipped in the first place. One assertion, both
    directions, one owner: this kills the JS-side mutation 4 → 3 (green across
    all 1071 vitest tests today) AND the reverse drift, a server bump to 5 with
    the wizard left at 4.

    Skips when the React tree is absent, like ``test_model_root_agreement``."""
    root = (Path(__file__).resolve().parents[3] / 'physical_ai_tools'
            / 'physical_ai_manager' / 'src')
    if not root.is_dir():
        pytest.skip('React source tree not present')
    jsx = (root / 'components' / 'Workshop' / 'TableTouchStep.jsx'
           ).read_text(encoding='utf-8')
    m = re.search(r'TAP_COUNT_FALLBACK\s*=\s*(\d+)', jsx)
    assert m, 'TAP_COUNT_FALLBACK moved or was renamed'
    assert int(m.group(1)) == CM.TABLE_TOUCH_POINTS_REQUIRED, (
        f'the wizard first-paints {m.group(1)} taps while the server requires '
        f'{CM.TABLE_TOUCH_POINTS_REQUIRED}')
