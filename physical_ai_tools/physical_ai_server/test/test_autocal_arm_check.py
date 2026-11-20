#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""``autocal.arm_check`` (spec 40 S4, WP-10): the gates, the droop warning
(decisions D33, D42: never a refusal), the identifiability of the solve, the
field the software unprojects, and the purity of the package.

A kit is simulated by ``autocal_sim`` and handed to ``arm_check.run`` exactly
as the service would hand it: the rested pose records and the full-frame
looks. A "quiet" kit (no axis misalignment, no encoder non-linearity, the
camera at the contract pose, no decentering, the tower at its place) isolates
the gate a case is about.
"""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import platform
import sys

import numpy as np
import pytest

import autocal_sim as S
from physical_ai_server.workflow import scene_fixture as sf
from physical_ai_server.workflow.autocal import arm_check as ac, chain, lm, mat_check as mc, store, texts_de as t
from physical_ai_server.workflow.autocal.camera_model import LENS_FIELD_MIN_RAD

QUIET = replace(S.Options(), skew_deg=0.0, inl_deg=0.0, cam_lens_m=0.0, cam_rot_deg=0.0, decenter=0.0,
                tower_place=False)
KINDS = ('v1', 'omx')


def _kit(kind, opt=QUIET, seed=3, **kw):
    rng = np.random.default_rng([seed, 0])
    return S.sample_kit(kind, rng, opt, **kw), rng


def _run(kind, kit, rng, opt=QUIET):
    inp = S.arm_check_inputs(kit, rng, opt)
    return ac.run(inp.records, inp.looks, profile_id=S.PROFILE[kind], camera_profile_id='tower_v1',
                  created_utc='2026-11-20T09:00:00Z')


def _across(kind):
    return mc.contract_camera(chain.BASE_AXIS_X[kind])[0][0]


# ── S4's numbers, written as literals ────────────────────────────────────────
_GATES = (   # (constant, S4's value, unit the literal is written in)
    ('CAPTURE_JOINT_TOL_RAD', 0.15, 'rad'), ('CAPTURE_MIN_TARGET_TAGS', 6, ''), ('MIN_POSES', 14, ''),
    ('MIN_FIELD_TAGS', 10, ''), ('MIN_TAG_FRAMES', 5, ''), ('RMS_MAX_PX', 0.6, 'px'), ('HOLDOUT_MAX_PX', 1.0, 'px'),
    ('PP_MAX_PX', 60.0, 'px'), ('ASPECT_TOL', 0.01, ''), ('K_MAX', 0.25, ''), ('OFFSET_MAX_RAD', 3.0, 'deg'),
    ('TILT_MAX_RAD', 1.5, 'deg'), ('MOUNT_MAX_M', 0.005, 'm'), ('MOUNT_MAX_RAD', 5.0, 'deg'),
    ('MAT_SHIFT_MAX_M', 0.020, 'm'), ('MAT_YAW_MAX_RAD', 4.0, 'deg'), ('SCALE_TOL', 0.02, ''), ('SLOPE_MAX', 0.02, ''),
    ('LENS_AXIAL_MAX_M', 0.025, 'm'), ('AXIS_MAX_RAD', 3.0, 'deg'), ('DROOP_ENVELOPE_MAX_RAD', 0.10, 'rad'),
    ('HOLDOUT_EVERY', 5, ''), ('PIXEL_SIGMA', 0.15, 'px'),
)
_LAYOUT = (('I_SCALE', 34), ('I_HEIGHT', 35), ('N_PARAMS', 36), ('I_J2B', 18), ('I_D1', 12), ('I_D5', 16),
           ('I_TA', 29), ('I_TB', 30), ('I_DX', 31), ('I_DY', 32), ('I_DPSI', 33))


@pytest.mark.parametrize('name,value,unit', _GATES)
def test_the_gates_are_s4_s_numbers(name, value, unit):
    got = getattr(ac, name)
    assert (math.degrees(got) if unit == 'deg' else got) == pytest.approx(value, abs=1e-12)


def test_the_parameter_layout():
    for name, index in _LAYOUT:
        assert getattr(ac, name) == index, name
    assert ac.PARAM_NAMES[ac.I_J2B] == 'j2b' and ac.PARAM_NAMES[ac.I_HEIGHT] == 'mat_height'


# ── a good arm check ─────────────────────────────────────────────────────────
@pytest.mark.parametrize('kind', KINDS)
def test_a_quiet_kit_is_stored_with_every_key_the_store_needs(kind, tmp_path):
    kit, rng = _kit(kind)
    r = _run(kind, kit, rng)
    assert r.ok and r.code == 'ok' and r.message_de == t.OK_DE
    cal = r.calibration
    assert set(store.REQUIRED_KEYS) <= set(cal)
    assert cal['target_removed'] is False and cal['droop_warning'] is None and cal['notes'] == []
    assert cal['joint_offsets_rad'][0] == 0.0 and cal['joint_offsets_rad'][4] == 0.0
    assert cal['axis_tilts_rad'][1] == 0.0                                   # j2b: always 0
    assert len(cal['droop_envelope_rad']) == 5 and len(cal['tower_pose']) == 3
    assert len(cal['fixture_xy_mat']) == 8 and len(cal['fixture_px']) == 8
    assert cal['quality']['poses_used'] == 20 and cal['quality']['rms_px'] <= ac.RMS_MAX_PX
    st = store.Store(str(tmp_path))
    st.write(cal)
    assert st.read(S.PROFILE[kind], (1920, 1080)).status == 'ok'
    # the stored camera, put back into W by the stored mat pose, is the solved lens
    T = mc.w_from_m(chain.BASE_AXIS_X[kind], cal['mat_pose'])
    R = chain.rodrigues(cal['camera_pose_mat']['rvec'])
    lens_m = -R.T @ np.asarray(cal['camera_pose_mat']['t'])
    assert np.linalg.norm(T[:3, :3] @ lens_m + T[:3, 3] - np.asarray(cal['lens_w'])) < 1e-6


@pytest.mark.parametrize('kind', KINDS)
def test_thirteen_captured_poses_are_too_few_and_nothing_is_stored(kind):
    kit, rng = _kit(kind)
    inp = S.arm_check_inputs(kit, rng, QUIET)
    records = [r if i < 13 else r._replace(target=None) for i, r in enumerate(inp.records)]
    r = ac.run(records, inp.looks, profile_id=S.PROFILE[kind])
    assert not r.ok and r.code == 'too_few_poses' and r.calibration is None
    assert '(13 von 20)' in r.message_de


# ── the lens gates (the amendment for D33) ───────────────────────────────────
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('k1,k2,ok', ((-0.24, -0.24, False), (-0.19, -0.10, True), (0.0, 0.26, False)))
def test_the_lens_shape_gate(kind, k1, k2, ok):
    """k1 = k2 = −0.24 (|k| inside 0.25, valid field 0.754 rad) is refused,
    (−0.19, −0.10) (0.904 rad; revision 1's fixed 1.2 rad refused it) is not,
    k2 = 0.26 is refused."""
    kit, rng = _kit(kind)
    kit.cam.k1, kit.cam.k2 = k1, k2
    r = _run(kind, kit, rng)
    assert r.ok is ok, (r.code, r.details.get('valid_theta'))
    if not ok:
        assert r.code == 'lens' and r.message_de == t.GATE_LENS_DE and r.offers_handheld


@pytest.mark.parametrize('kind', KINDS)
def test_a_solved_lens_12_mm_along_the_axis_passes(kind):
    kit, rng = _kit(kind)
    kit.cam = kit.cam.moved(dL=-mc.contract_axis() * 0.012)
    r = _run(kind, kit, rng)
    assert r.ok, r.code
    assert 8.0 < r.details['lens_along_m'] * 1000 < 18.0 and r.details['lens_across_m'] * 1000 < 3.0


@pytest.mark.parametrize('kind', KINDS)
def test_a_solved_lens_12_mm_across_the_axis_fails_with_the_tower_sentence(kind):
    kit, rng = _kit(kind)
    kit.cam = kit.cam.moved(dL=_across(kind) * 0.012)
    r = _run(kind, kit, rng)
    assert r.code == 'tower' and r.message_de == t.GATE_TOWER_DE and not r.offers_handheld


@pytest.mark.parametrize('kind', KINDS)
def test_a_solved_lens_30_mm_along_the_axis_fails_with_the_handheld_sentence(kind):
    kit, rng = _kit(kind)
    kit.cam = kit.cam.moved(dL=-mc.contract_axis() * 0.030)
    r = _run(kind, kit, rng)
    assert r.code == 'lens' and r.message_de == t.GATE_LENS_DE and r.offers_handheld
    assert r.details['lens_along_m'] > ac.LENS_AXIAL_MAX_M


@pytest.mark.parametrize('kind', KINDS)
def test_an_optical_axis_4_degrees_off_names_the_tower(kind):
    kit, rng = _kit(kind)
    kit.cam = kit.cam.moved(dR=chain.rodrigues(_across(kind) * math.radians(4.0)))
    r = _run(kind, kit, rng)
    assert r.code == 'tower' and math.degrees(r.details['axis_rad']) > 3.0


@pytest.mark.parametrize('kind', KINDS)
def test_a_joint_axis_tilt_of_2_degrees_is_refused(kind):
    """``|tilt| ≤ 1.5°``: an axis tilt of 2° is refused with the joint sentence."""
    kit, rng = _kit(kind, tilts=np.radians([0.0, 0.0, 2.0, 0.0, 0.0, 0.0]))
    r = _run(kind, kit, rng)
    assert r.code == 'joint' and r.message_de == t.GATE_JOINT_DE
    assert abs(math.degrees(r.details['p'][ac.S_TILT][2]) - 2.0) < 0.2


@pytest.mark.parametrize('kind', KINDS)
def test_a_lens_12_mm_across_from_the_pose_carried_lens_but_6_mm_from_lens_f_names_the_tower(kind):
    """A15 (revision 4): judged against ``lens_w(base_axis_x, tower_pose)`` (revision 3
    judged against ``LENS_F`` and passed it)."""
    kit, rng = _kit(kind)
    v = _across(kind)
    kit.cam = kit.cam.moved(dL=-v * 0.006)
    kit.fixture_w[:, :2] += v[:2] / np.linalg.norm(v[:2]) * 0.006
    r = _run(kind, kit, rng)
    assert r.code == 'tower' and 11.0 < r.details['lens_across_m'] * 1000 < 13.5
    off = kit.cam.lens - (np.asarray(sf.LENS_F) + (chain.BASE_AXIS_X[kind], 0.0, 0.0))
    assert mc.split_along_across(off)[1] < sf.LENS_FOLLOW_MAX_M


@pytest.mark.parametrize('kind', KINDS)
def test_the_lens_reading_is_taken_in_the_arm_s_frame_whatever_the_mat_s_slope(kind):
    """The tower, its tags and ``LENS_F`` are rigid to the arm's base, which W
    holds turned by the mat slope (S6.1). A mat tilted by 0.01 (inside the
    0.02 gate) under a tower built exactly: read in W the lens would sit
    0.575 m × 0.01 ≈ 5.8 mm across; read in the arm's frame it sits where the
    tower carries it."""
    kit, rng = _kit(kind)
    kit.mat['slope'] = np.array([0.0, 0.01])
    r = _run(kind, kit, rng)
    assert r.ok, r.code
    assert abs(r.calibration['tilt'][1] - 0.01) < 0.001
    assert r.details['lens_across_m'] * 1000 < 1.5


# ── the droop warning (D33, D42) ─────────────────────────────────────────────
NO_DROOP = replace(QUIET, droop_scale=0.0)


def _sag_kit(kind, big, small=0.03, at=7):
    kit, rng = _kit(kind, NO_DROOP)
    kit.extra_sag = {i: np.array([0.0, -small, 0.0, 0.0, 0.0]) for i in range(20)}
    kit.extra_sag[at] = np.array([0.0, -big, 0.0, 0.0, 0.0])
    return kit, rng


@pytest.mark.parametrize('kind', KINDS)
def test_a_joint_2_error_of_0_12_rad_is_stored_with_the_droop_warning(kind):
    kit, rng = _sag_kit(kind, 0.12)
    r = _run(kind, kit, rng, NO_DROOP)
    assert r.ok and r.calibration is not None                   # the droop alone never refuses
    assert r.droop_warning == {'joint': 2, 'deg': 7}
    assert r.calibration['droop_warning'] == {'joint': 2, 'deg': 7}
    assert t.droop_warning_de(2, 7) in r.warnings_de
    assert '(Abweichung 7° an Gelenk 2)' in r.warnings_de[0]
    assert abs(r.calibration['droop_envelope_rad'][1] - 0.12) < 0.003


@pytest.mark.parametrize('kind', KINDS)
def test_a_joint_2_error_of_0_09_rad_gives_no_droop_warning(kind):
    kit, rng = _sag_kit(kind, 0.09)
    r = _run(kind, kit, rng, NO_DROOP)
    assert r.ok and r.droop_warning is None and r.calibration['droop_warning'] is None
    assert not any(w.startswith('Achtung: Der Arm erreicht') for w in r.warnings_de)


@pytest.mark.parametrize('kind', KINDS)
def test_a_pose_that_came_to_rest_without_being_captured_counts_in_the_envelope(kind):
    """0.20 rad at one pose: not captured (beyond 0.15 rad), yet its record
    carries the error; the run warns (11°) and stores — no too-few-poses."""
    kit, rng = _sag_kit(kind, 0.20)
    inp = S.arm_check_inputs(kit, rng, NO_DROOP)
    assert inp.records[7].target is None and sum(r.target is not None for r in inp.records) == 19
    r = ac.run(inp.records, inp.looks, profile_id=S.PROFILE[kind])
    assert r.ok and r.code == 'ok'
    assert r.droop_warning == {'joint': 2, 'deg': 11}


def test_the_droop_limit_is_a_warning_at_0_10_rad():
    assert ac.DROOP_ENVELOPE_MAX_RAD == 0.10
    env = ac.droop_envelope([ac.PoseRecord(0, (0, 0.5, 0, 0, 0), (0, 0.38, 0, 0, 0), None),
                             ac.PoseRecord(1, (0, 0.5, 0, 0, 0), (0, 0.47, 0.01, 0, 0), {})])
    assert np.allclose(env, (0.0, 0.12, 0.01, 0.0, 0.0))


@pytest.mark.parametrize('kind', KINDS)
def test_an_envelope_above_the_design_envelope_stores_the_note_droop_above_design(kind):
    """Revision 4: a kit whose envelope exceeds ``TOWER_DROOP_DESIGN_ENV_RAD``
    on any joint stores the note and carries its sentence — never a refusal."""
    design = sf.TOWER_DROOP_DESIGN_ENV_RAD[kind]
    kit, rng = _kit(kind, NO_DROOP)
    kit.extra_sag = {i: np.array([0.0, 0.0, -(design[1] + 0.005), 0.0, 0.0]) for i in range(20)}
    r = _run(kind, kit, rng, NO_DROOP)
    assert r.ok and r.notes == ('droop_above_design',) and r.calibration['notes'] == ['droop_above_design']
    assert t.DROOP_ABOVE_DESIGN_DE in r.warnings_de and r.droop_warning is None
    kit, rng = _kit(kind, NO_DROOP)
    r = _run(kind, kit, rng, NO_DROOP)
    assert r.ok and r.notes == () and r.warnings_de == ()


# ── identifiability (P-R7) ───────────────────────────────────────────────────
def _sigmas(model, p, free=None, prior=None):
    free = model.free if free is None else free
    s = lm.sigmas(model.jacobian(p, free, prior))
    out = np.full(ac.N_PARAMS, np.nan)
    out[np.flatnonzero(free)] = s
    return out


@pytest.fixture(scope='module', params=KINDS)
def solved(request):
    kind = request.param
    rng = np.random.default_rng([1, 0])
    kit = S.sample_kit(kind, rng, S.Options())
    inp = S.arm_check_inputs(kit, rng)
    model = ac.build_model(ac.prepare(kind, inp.records, inp.looks))
    return model, model.solve().p


def test_the_spec_s_32_parameters_are_identified(solved):
    model, p = solved
    assert len(ac.PARAM_NAMES) == ac.N_PARAMS
    assert int(model.free.sum()) == 32
    assert not model.free[[ac.I_D1, ac.I_D5, ac.I_J2B, ac.I_HEIGHT]].any()
    tilts = [17, 19, 20, 21, 22]
    no_tilt_prior = model.prior_sigma.copy()
    no_tilt_prior[ac.S_TILT] = np.inf
    for prior in (None, no_tilt_prior):      # and the same with the five tilt priors removed
        s = _sigmas(model, p, prior=prior)
        assert np.degrees(s[13:16]).max() < 0.05, np.degrees(s[13:16])           # d2 … d4
        assert np.degrees(s[tilts]).max() < 0.05, np.degrees(s[tilts])          # five tilts
        assert math.degrees(s[ac.I_DPSI]) < 0.03                                 # mat yaw
        assert s[[ac.I_DX, ac.I_DY]].max() * 1000 < 0.15                         # mat position
        assert s[ac.I_SCALE] * 100 < 0.1                                         # print scale
        assert s[ac.I_F] < 2.0                                                   # focal length


def test_the_five_tilt_priors_removed_change_nothing(solved):
    model, p = solved
    no_tilt_prior = model.prior_sigma.copy()
    no_tilt_prior[ac.S_TILT] = np.inf
    a, b = _sigmas(model, p), _sigmas(model, p, prior=no_tilt_prior)
    tilts = [17, 19, 20, 21, 22]
    assert np.degrees(b[tilts]).max() < 0.05
    assert np.allclose(a[tilts], b[tilts], rtol=0.05)


def test_j2b_freed_is_the_joint_1_zero_offset_against_the_mat_yaw(solved):
    model, p = solved
    free = model.free.copy()
    free[ac.I_J2B] = True
    prior = model.prior_sigma.copy()
    prior[ac.I_J2B] = np.inf
    s = _sigmas(model, p, free, prior)
    assert math.degrees(s[ac.I_J2B]) > 100.0 and math.degrees(s[ac.I_DPSI]) > 100.0


def test_the_mat_height_freed_is_unidentified(solved):
    model, p = solved
    free = model.free.copy()
    free[ac.I_HEIGHT] = True
    assert _sigmas(model, p, free)[ac.I_HEIGHT] > 1.0


def test_d5_freed_is_held_only_by_the_clip_s_seating_prior(solved):
    model, p = solved
    free = model.free.copy()
    free[ac.I_D5] = True
    assert math.degrees(_sigmas(model, p, free)[ac.I_D5]) > 0.1


# ── without a handheld lens the solve is the parent's; with one the lens is held (D89-1) ──
# What ArmCheckModel without a handheld lens solved on the quiet kit's fixed-seed inputs at the parent 4277005e,
# before a handheld lens was held fixed (scratch/build-WP-15F/noprior_digest.py): the sha256 of the solution and
# its 1σ (float64, in order) and the values (12 significant digits). The bits are pinned on the stack they were
# taken on; float64 last bits are not portable across BLAS, SIMD and NumPy builds (NumPy 2.4.6 on the same Mac
# lands 1.4e-9 away), so on any stack the values must hold to 1e-7 (one ulp in the observations moves them by at
# most 2e-9).
_PARENT_STACK = ('darwin', 'arm64', '1.26.4')
_PARENT_NO_LENS = {
    'v1': (
        'ecc4489369b2bd4e7732215a8deebba4d2dbc924d83889c9fb1192d26c43928d',
        np.array([
            977.831735795, 1.00192963125, 942.350056805, 520.077623268, -0.00434249604701,
            0.0872875520663, 1.67071015183, -2.40261770765, 0.390584717645, 0.0583070495299,
            0.0604611235287, 0.585867540272, 0, -0.000844152338066, 0.00411604824933, 0.0164289782718,
            0, -0.00207561134347, 0, -0.00119623923912, -0.00429865953053, 0.000218051505083,
            -0.000734336221398, -0.00400990175973, 0.00129766702711, 0.00422528264405,
            -5.94056695941e-05, 0.000193915975489, 0.00051602898239, -0.0027336047074,
            -0.00178821376362, -0.00075329763814, -6.7494794502e-05, 0.016479107226, 1.00512752228,
            0.00045]),
        np.array([
            0.555941358205, 8.879184596e-05, 0.411230775321, 0.516943018085, 0.00105933338358,
            0.00188749965018, 0.000271155649307, 0.000380557302328, 0.0007507544431, 0.000241461871609,
            0.000300959553912, 0.000190159421054, 0.000224991037244, 0.000277085778956,
            0.000341462232228, 0.000215785209623, 0.000121006521004, 0.000244070554332,
            0.000307822320445, 0.000330677609024, 0.000425971335784, 0.000406179639231,
            0.000298313975862, 2.64222286725e-05, 1.0855490512e-05, 2.26475289543e-05,
            0.000223315376437, 0.000191290430432, 4.05152386768e-05, 4.91143279089e-05,
            0.000175832487903, 0.000350649154531])),
    'omx': (
        '292bd071cdd8c0fa5946a6ec8daf97d6f8e19c15ab25004fcb8b81cedd64a086',
        np.array([
            982.264797375, 1.00188533572, 943.757510627, 516.604371332, -0.00221145235762,
            0.0843738980011, 1.64483388834, -2.42209049851, 0.399138996522, 0.0541373777049,
            0.0517302471602, 0.591526296241, 0, -0.00119466333928, -0.000273059165817, 0.0152890433834,
            0, 0.000771949488652, 0, -3.47022625531e-05, 0.00176475142408, -0.00117027261661,
            -7.94400569428e-07, -0.00802551290319, -0.00030831542689, -0.00589105351078,
            0.000990167354472, 0.000294942628255, 0.00022279988727, -0.00176415885549,
            -0.00180832460211, -0.00133522765598, 0.00121991051654, -0.00536900358337, 1.0025546247,
            0.00045]),
        np.array([
            0.665625551479, 0.000113574340287, 0.685659571965, 0.712721573892, 0.00188893547066,
            0.00559977215993, 0.000323264666627, 0.000543001342189, 0.00110792982933, 0.000403532234468,
            0.000417720360439, 0.000256218238102, 0.000194617919956, 0.000331547645482,
            0.000329318842815, 0.000198543413072, 9.93538528779e-05, 0.000269232202172,
            0.000228408781666, 0.000295341886559, 0.000339903121702, 0.000343970856495,
            0.000268088467472, 2.47988321744e-05, 1.01878993545e-05, 1.6496159226e-05,
            0.000377015592403, 0.00028990572831, 4.16691558653e-05, 5.57578686033e-05,
            0.000204773559057, 0.000340159509323])),
}


@pytest.mark.parametrize('kind', KINDS)
def test_without_a_handheld_lens_the_arm_check_solves_exactly_as_before(kind):
    """An arm check without a pending handheld lens solves as at the parent: the same parameters and 1σ, bit for
    bit on the stack the digest was taken on."""
    kit, rng = _kit(kind)
    inp = S.arm_check_inputs(kit, rng, QUIET)
    sol = ac.build_model(ac.prepare(kind, inp.records, inp.looks)).solve()
    sig = lm.sigmas(sol.jacobian)
    digest, p, s = _PARENT_NO_LENS[kind]
    np.testing.assert_allclose(sol.p, p, rtol=1e-9, atol=1e-7)
    np.testing.assert_allclose(sig, s, rtol=1e-7, atol=0.0)
    if (sys.platform, platform.machine(), np.__version__) == _PARENT_STACK:
        assert hashlib.sha256(np.concatenate([sol.p, sig]).tobytes()).hexdigest() == digest


@pytest.mark.parametrize('kind', KINDS)
def test_a_handheld_lens_is_held_fixed_and_stored_as_it_is(kind):
    """FAILS FIRST (S4, conductor D89-1): with a pending handheld lens the six lens parameters are not free —
    here one 0.8 px off the kit's true focal length, which the arm check's own data would pull back — and the
    stored lens is that lens to the bit; everything else is solved as without one."""
    kit, rng = _kit(kind)
    c = kit.cam
    lens = {'kb': {'f': c.f + 0.8, 'aspect': c.a, 'cx': c.cx - 0.6, 'cy': c.cy + 0.4, 'k1': c.k1, 'k2': c.k2}}
    inp = S.arm_check_inputs(kit, rng, QUIET)
    r = ac.run(inp.records, inp.looks, profile_id=S.PROFILE[kind], lens_prior=lens)
    assert r.ok, r.code
    assert r.calibration['kb'] == lens['kb']
    model = ac.build_model(ac.prepare(kind, inp.records, inp.looks), lens_prior=lens)
    assert not model.free[:6].any() and int(model.free.sum()) == 32 - 6


# ── the field the software unprojects ────────────────────────────────────────
def _field_angle(kind, X):
    R, t = mc.contract_camera(chain.BASE_AXIS_X[kind])
    Xc = np.asarray(X, dtype=np.float64).reshape(-1, 3) @ R.T + t
    return np.arctan2(np.hypot(Xc[:, 0], Xc[:, 1]), Xc[:, 2])


@pytest.mark.parametrize('kind', KINDS)
def test_every_point_unprojected_on_purpose_lies_inside_the_lens_gate_s_field(kind):
    """Field-tag and fixture-tag corners, the zone at 0 … 0.10 m, the target's
    corners in the 20 arm-check poses: within ``LENS_FIELD_MIN_RAD / 1.1`` of
    the contract camera's axis (probe P-R19 at the revision-1 lens: largest
    0.692 rad). At placement B the V1's target at pose AC12 read 0.734 rad;
    D84 moved AC12 inward (joint 3 by −5°): now 0.695 rad, the V1's largest
    (field tags 0.620, zone 0.612, fixture 0.429); the OMX's largest is its
    target at AC20, 0.591 rad."""
    spec = sf.ARMS[kind]
    ax = chain.BASE_AXIS_X[kind]
    field = mc.field_model_corners(spec, sorted(spec['mat_tags_f'])).reshape(-1, 3) + (ax, 0.0, sf.MAT_THICKNESS_M)
    fixture = np.hstack([np.asarray(sf.fixture_corners_w(ax)), np.full((8, 1), sf.FIXTURE_TAG_Z_M)])
    zone = []
    for r in np.linspace(spec['zone_inner_m'], spec['zone_outer_m'], 12):
        for az in np.radians(np.linspace(*spec['zone_sector_deg'], 35)):
            if sf.in_zone(spec, r * math.cos(az), r * math.sin(az)):
                for z in (0.0, 0.05, 0.10):
                    zone.append((ax + r * math.cos(az), r * math.sin(az), z))
    model = ac._Nominal(kind)
    p = np.zeros(ac.N_PARAMS)
    p[ac.I_SCALE], p[ac.I_HEIGHT] = 1.0, sf.MAT_THICKNESS_M
    target = model.target_points_w(p, np.asarray(spec['arm_check_poses_rad']))
    worst = {name: float(_field_angle(kind, X).max())
             for name, X in (('field', field), ('fixture', fixture), ('zone', np.array(zone)), ('target', target))}
    assert max(worst.values()) < LENS_FIELD_MIN_RAD / 1.1, worst


# ── purity ───────────────────────────────────────────────────────────────────
_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'workflow' / 'autocal'
_ALLOWED = {
    'physical_ai_server.workflow.ik_solver', 'physical_ai_server.workflow.v1_ik',
    'physical_ai_server.workflow.scene_fixture',
}


def test_autocal_imports_only_numpy_the_stdlib_and_the_two_solvers_constants():
    """``scene_fixture`` (the stdlib-only contract the gates are written in) and the
    package's own modules besides; never ROS, OpenCV or the node."""
    stdlib = set(sys.stdlib_module_names)
    for path in sorted(_PKG.glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ''
                names = [base + '.' + a.name if base == 'physical_ai_server.workflow' else base for a in node.names]
            for name in names:
                top = name.split('.')[0]
                ok = (top in stdlib or top == 'numpy' or name in _ALLOWED
                      or name.startswith('physical_ai_server.workflow.autocal'))
                assert ok, (path.name, name)
