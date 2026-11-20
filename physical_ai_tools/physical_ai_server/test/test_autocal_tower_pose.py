#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower pose the arm check measures from fixture tags 480/481 (spec 40 S4,
revision 4, D65 (1); ``56`` N-1) and its ``fixture_layout`` gate (revision 5,
``58`` M-3 — the gate's blind direction, accepted by the owner, D79).

The arm check fixes the joint-1 zero offset at 0, so the calibrated arm and
the tower are turned against each other by that offset; the tags, seen by the
same camera in the same solve, place the tower in the calibrated arm's own
frame. Sign: a positive yaw of the tower is a turn about +z.
"""

from __future__ import annotations

from dataclasses import replace
import math

import numpy as np
import pytest

import autocal_sim as S
from physical_ai_server.workflow import scene_fixture as sf
from physical_ai_server.workflow.autocal import arm_check as ac, chain, texts_de as t

# A kit without the scatter that is not under test (pixel noise 0.0015 px, no link, backlash or curl error),
# so the tower pose and its fit read the geometry the spec's numbers are (``scratch/r7`` q21).
QUIET = replace(S.Options(), skew_deg=0.0, inl_deg=0.0, cam_lens_m=0.0, cam_rot_deg=0.0, decenter=0.0,
                tower_place=False, link_m=0.0, noise=0.01)
KINDS = ('v1', 'omx')


def _arm_turned_by(kind, deg):
    """The joint-1 zero offset that turns the real arm by ``deg`` about +z against
    its encoders (the OMX's joint 1 turns about +z, the V1's about −z)."""
    d1 = math.radians(deg) * (1.0 if kind == 'omx' else -1.0)
    return np.array([d1, 0.0, 0.0, 0.0, 0.0])


def _run(kind, *, dzero=None, fixture_shift=None, seed=3, exact=False):
    """One arm check of a quiet kit. ``exact`` also removes what the fit reads
    on top of the tags' geometry — the encoder ticks, the droop, the mat's slope
    (each moves the solved camera a little, and the fixture corners it
    back-projects by 0.05–0.1 mm) — so the RMS is the geometry's alone."""
    rng = np.random.default_rng([seed, 0])
    opt = replace(QUIET, droop_scale=0.0) if exact else QUIET
    kit = S.sample_kit(kind, rng, opt, dzero=np.zeros(5) if dzero is None else dzero)
    kit.backlash = 0.0
    kit.mat['curl'] = 0.0
    kit.mat['h'] = sf.MAT_THICKNESS_M
    if exact:
        kit.mat['slope'] = np.zeros(2)
    if fixture_shift is not None:
        kit.fixture_w[:, :2] += fixture_shift
    tick = S.TICK
    try:
        if exact:
            S.TICK = 1e-12
        inp = S.arm_check_inputs(kit, rng, opt)
    finally:
        S.TICK = tick
    return ac.run(inp.records, inp.looks, profile_id=S.PROFILE[kind], camera_profile_id='tower_v1')


def _box_corners_2d(boxes, name):
    for n, c, rows, half in boxes:
        if n == name:
            R = np.asarray(rows, dtype=np.float64)
            signs = np.array([[(i >> 2) & 1, (i >> 1) & 1, i & 1] for i in range(8)]) * 2 - 1
            return (np.asarray(c) + (signs * np.asarray(half)) @ R.T)[:, :2]
    raise KeyError(name)


def _moved(kind, pose_a, pose_b, name='column'):
    ax = chain.BASE_AXIS_X[kind]
    a = _box_corners_2d(sf.tower_obbs_w(ax, pose_a), name)
    b = _box_corners_2d(sf.tower_obbs_w(ax, pose_b), name)
    return float(np.max(np.linalg.norm(a - b, axis=1)))


@pytest.mark.parametrize('kind', KINDS)
def test_a_joint_1_zero_offset_of_1_5_degrees_turns_the_tower_against_the_arm(kind):
    """The arm turned −1.5° against its encoders: the tower stands +1.5° in the
    calibrated arm's frame (± 0.05°), and the column placed with it moves
    5.5 mm ± 0.3 (2 · 0.2086 m · sin 0.75°)."""
    r = _run(kind, dzero=_arm_turned_by(kind, -1.5))
    assert r.ok, r.code
    psi, tx, ty = r.calibration['tower_pose']
    assert abs(math.degrees(psi) - 1.5) < 0.05, math.degrees(psi)
    ax = chain.BASE_AXIS_X[kind]
    centre = {n: np.asarray(c[:2]) for n, c, _, _ in sf.tower_obbs_w(ax, (psi, tx, ty))}['column']
    contract = {n: np.asarray(c[:2]) for n, c, _, _ in sf.tower_obbs_w(ax, None)}['column']
    assert abs(np.linalg.norm(centre - contract) * 1000 - 5.5) < 0.3
    # the other way round
    r2 = _run(kind, dzero=_arm_turned_by(kind, 1.5))
    assert abs(math.degrees(r2.calibration['tower_pose'][0]) + 1.5) < 0.05


@pytest.mark.parametrize('kind', KINDS)
def test_arm_check_json_carries_the_tower_pose_its_fit_and_the_per_joint_droop_envelope(kind):
    r = _run(kind)
    cal = r.calibration
    assert len(cal['tower_pose']) == 3 and all(math.isfinite(v) for v in cal['tower_pose'])
    assert 0.0 <= cal['tower_fit_rms_m'] < 0.0002
    assert len(cal['droop_envelope_rad']) == 5 and cal['droop_envelope_rad'][1] > 0.0


# ── fixture_layout: what the gate sees and what it cannot see ───────────────
def _directions():
    c480 = np.asarray(sf.FIXTURE_TAGS_F[480])
    c481 = np.asarray(sf.FIXTURE_TAGS_F[481])
    along = (c481 - c480) / np.linalg.norm(c481 - c480)
    perp = np.array([-along[1], along[0]])
    return {'along': along, 'diagonal': (along + perp) / math.sqrt(2.0), 'perpendicular': perp}


def _shift_481(v, mm):
    s = np.zeros((8, 2))
    s[4:] = v * mm / 1000.0
    return s


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('exact', (True, False))
@pytest.mark.parametrize('direction,rms_mm', (('along', 1.50), ('diagonal', 1.16)))
def test_one_fixture_tag_3_mm_off_along_or_diagonally_is_refused_and_nothing_is_stored(kind, exact, direction,
                                                                                       rms_mm):
    """``q21``: 3 mm along the line between the tags → RMS 1.50 mm, diagonally →
    1.16 mm, both above ``TOWER_FIT_MAX_M``. ``exact``: the geometry's RMS to
    0.01 mm; through the full chain (encoder ticks, droop, slope) within 0.15 mm."""
    r = _run(kind, fixture_shift=_shift_481(_directions()[direction], 3.0), exact=exact)
    assert not r.ok and r.code == 'fixture_layout' and r.calibration is None
    assert r.message_de == t.FIXTURE_LAYOUT_DE
    assert abs(r.details['tower_pose'][3] * 1000 - rms_mm) < (0.01 if exact else 0.15)


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('exact', (True, False))
def test_one_fixture_tag_3_mm_off_at_right_angles_is_not_refused_and_moves_the_column(kind, exact):
    """The gate's blind direction (``58`` M-3), pinned so that nobody believes
    the gate guards it — nothing else does since D79 (the owner accepts it, §6
    revision 6 R33): RMS 0.645 mm, passed; the placed column moves 2.88 mm."""
    ref = _run(kind, exact=exact)
    r = _run(kind, fixture_shift=_shift_481(_directions()['perpendicular'], 3.0), exact=exact)
    assert r.ok, r.code
    assert abs(r.calibration['tower_fit_rms_m'] * 1000 - 0.645) < (0.01 if exact else 0.15)
    assert abs(_moved(kind, ref.calibration['tower_pose'], r.calibration['tower_pose']) * 1000 - 2.88) < 0.05


@pytest.mark.parametrize('kind', KINDS)
def test_both_tags_within_their_pockets_are_stored(kind):
    """Both tags within their pockets' ±0.2 mm (F8): stored, RMS ≤ 0.6 mm."""
    rng = np.random.default_rng(21)
    for _ in range(4):
        shift = np.repeat(rng.uniform(-0.0002, 0.0002, (2, 2)), 4, axis=0)
        r = _run(kind, fixture_shift=shift)
        assert r.ok, r.code
        assert r.calibration['tower_fit_rms_m'] <= 0.0006
