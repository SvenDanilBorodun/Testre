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
        f'mirroring the EDUBOTICS_FEETECH_* knobs. said={said!r}')


def test_the_shipped_default_is_seven_frames():
    """The other half of the two-sided pin: the DEFAULT, not just the ceiling."""
    src = Path(PB.__file__).read_text(encoding='utf-8')
    assert re.search(r"_safe_float\('EDUBOTICS_TAG_YAW_FRAMES', 7\.0\)", src)
