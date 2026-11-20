#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""``autocal.mat_check`` (spec 40 S4): every refusal code from a constructed
input, what the flatness and consistency gates see, and the camera-moved rule
(decision C7 as the amendment for D33 and revision 4 word it, A15).

The inputs are rendered by ``autocal_sim`` from a clean kit (flat mat, no
decentering, the contract camera unless a case moves it) whose stored arm
check is the truth itself (``ideal_calibration``), so each case isolates
the rule it names.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

import autocal_sim as S
from physical_ai_server.workflow import scene_fixture as sf
from physical_ai_server.workflow.autocal import chain, mat_check as mc, store, tags

ALL_SEEN = np.zeros(14, dtype=bool)


def _kit(kind, **kw):
    kit = S.clean_kit(kind, cam_lens_m=0.0, cam_rot_deg=0.0, **kw)
    kit.cam.k1 = kit.cam.k2 = 0.0
    kit.cam.f, kit.cam.a, kit.cam.cx, kit.cam.cy = 1037.8, 1.0, 959.5, 539.5
    return kit


def _frames(kit, cam=None, mat=None, seed=5, **kw):
    kw.setdefault('hidden', ALL_SEEN)
    return S.mat_check_frames(kit, kit.cam if cam is None else cam, kit.mat if mat is None else mat,
                              np.random.default_rng(seed), **kw)


def _run(kind, frames, cal=None, *, store_=None, profile='tower_v1', streaming=True):
    return mc.run(frames, store_ or S.FakeStore(cal), sf.ARMS[kind], profile_id=S.PROFILE[kind],
                  profile=profile, camera_streaming=streaming)


def _across(kind):
    """A unit vector across the contract camera's optical axis, horizontal (image x)."""
    return mc.contract_camera(chain.BASE_AXIS_X[kind])[0][0]


@pytest.fixture(params=('v1', 'omx'))
def case(request):
    kit = _kit(request.param)
    return request.param, kit, S.ideal_calibration(kit)


# ── S4's numbers, written as literals ────────────────────────────────────────
_GATES = (
    ('TOO_DARK_GREY', 25.0), ('MIN_FIELD_TAGS', 6), ('WRONG_MAT_TAGS', 3), ('TAG_DROP_PX', 1.5), ('RMS_MAX_PX', 0.6),
    ('FIXTURE_FIT_MAX_M', 0.0005), ('MAT_MOVED_M', 0.0008), ('MAT_MOVED_DEG', 0.15), ('MAT_MISPLACED_M', 0.015),
    ('MAT_MISPLACED_DEG', 3.0), ('CAMERA_MOVED_DEG', 3.0), ('FOLLOW_NOTE_M', 0.005), ('FOLLOW_NOTE_DEG', 0.5),
)


@pytest.mark.parametrize('name,value', _GATES)
def test_the_gates_are_s4_s_numbers(name, value):
    assert getattr(mc, name) == value
    assert sf.LENS_FOLLOW_MAX_M == 0.010
    assert mc.FIXTURE_Z_M == pytest.approx(0.0072 - 0.00045, abs=1e-15)


# ── nothing moved ────────────────────────────────────────────────────────────
def test_nothing_moved_is_ok_without_a_note(case, tmp_path):
    kind, kit, cal = case
    r = _run(kind, _frames(kit), cal)
    assert r.ok and r.code == 'ok' and r.notes == ()
    assert r.message_de == 'Kalibrierung in Ordnung.'
    assert r.rms_px < 0.2 and r.lens_drift_mm < 1.0
    # T_cam→W is the true camera, the mat flat at its thickness
    assert np.linalg.norm(r.T_cam_to_w[:3, 3] - kit.cam.lens) < 0.001
    assert r.tags_seen == 14 and r.sharpness == 80.0
    # kept beside the arm check as mat_check_last.json
    st = store.Store(str(tmp_path))
    st.write_mat_check_last(r.to_json())
    back = st.read_mat_check_last()
    assert back['code'] == 'ok' and back['notes'] == [] and np.allclose(back['T_cam_to_w'], r.T_cam_to_w)


# ── step 1: every refusal in its order ───────────────────────────────────────
def test_every_refusal_code_of_step_1(case, tmp_path):
    kind, kit, cal = case
    good = _frames(kit)
    assert _run(kind, good, cal, profile=None).code == 'profile_unknown'
    assert _run(kind, good, cal, profile='legacy').code == 'camera_not_tower'
    assert _run(kind, good, cal, streaming=False).code == 'camera_missing'
    assert _run(kind, [], cal).code == 'camera_missing'
    small = [tags.Frame(640, 480, fr.mean_grey, fr.tags) for fr in good]
    assert _run(kind, small, cal).code == 'image_size'
    dark = [tags.Frame(fr.width, fr.height, 10.0, fr.tags) for fr in good]
    assert _run(kind, dark, cal).code == 'too_dark'
    other = sf.ARMS['omx' if kind == 'v1' else 'v1']
    foreign = {tid: tags.TagObs(np.array([[100.0, 100.0], [130, 100], [130, 130], [100, 130]]), 80.0)
               for tid in list(other['mat_tags_f'])[:4]}
    wrong = [tags.Frame(fr.width, fr.height, fr.mean_grey,
                        {**foreign, **{t: o for t, o in fr.tags.items() if t in tags.FIXTURE_IDS}}) for fr in good]
    assert _run(kind, wrong, cal).code == 'wrong_mat'
    tid = sf.ARMS[kind]['target_tag_ids'][4]
    r = _run(kind, _frames(kit, extra_tags={tid: np.array([[900.0, 500.0], [930, 500], [930, 530], [900, 530]])}),
             cal)
    assert r.code == 'target_attached' and r.target_seen
    no481 = [tags.Frame(fr.width, fr.height, fr.mean_grey, {t: o for t, o in fr.tags.items() if t != 481})
             for fr in good]
    assert _run(kind, no481, cal).code == 'fixture_hidden'
    hidden = np.ones(14, dtype=bool)
    hidden[:5] = False
    r = _run(kind, _frames(kit, hidden=hidden), cal)
    assert r.code == 'tags_hidden' and r.tags_seen == 5
    assert _run(kind, good, store_=S.FakeStore(None, 'none')).code == 'needs_arm_check/none'
    st = store.Store(str(tmp_path))
    st.write(dict(cal, contract_hash='0' * 64))
    assert _run(kind, good, store_=st).code == 'needs_arm_check/contract_changed'
    assert _run(kind, good, dict(cal, target_removed=False)).code == 'target_not_confirmed'


def test_each_refusal_carries_its_sentence(case):
    kind, kit, cal = case
    r = _run(kind, _frames(kit), cal, profile='legacy')
    assert not r.ok and r.message_de.startswith('Roboter Studio am echten Roboter braucht die Turm-Kamera')
    r = _run(kind, _frames(kit), dict(cal, target_removed=False))
    assert r.message_de == 'Bitte das Kalibrier-Ziel vom Greifer abnehmen und bestätigen.'


# ── step 2: flatness and consistency ─────────────────────────────────────────
def _arc_tag(kind):
    """The arc tag straight ahead (az 0°): the tag the probe P-R8 lifted."""
    return sorted(sf.ARMS[kind]['mat_tags_f'])[6]


def test_an_arc_tag_lifted_4_mm_is_dropped_and_the_check_is_ok(case):
    kind, kit, cal = case
    tid = _arc_tag(kind)
    r = _run(kind, _frames(kit, lift={tid: 0.004}), cal)
    assert r.ok and r.notes == ('tag_dropped',)
    assert r.details['per_tag_px'][tid] > mc.TAG_DROP_PX
    assert r.notes_de == ('Ein Marker der Matte liegt nicht flach und wurde nicht verwendet.',)


def test_an_arc_tag_lifted_2_mm_is_kept(case):
    kind, kit, cal = case
    tid = _arc_tag(kind)
    r = _run(kind, _frames(kit, lift={tid: 0.002}), cal)
    assert r.ok and 'tag_dropped' not in r.notes
    assert 0.9 < r.details['per_tag_px'][tid] < mc.TAG_DROP_PX      # placement B: 1.26 (V1) / 1.20 (OMX) px


def test_two_arc_tags_lifted_4_mm_are_mat_not_flat(case):
    kind, kit, cal = case
    ids = sorted(sf.ARMS[kind]['mat_tags_f'])
    r = _run(kind, _frames(kit, lift={ids[6]: 0.004, ids[3]: 0.004}), cal)
    assert r.code == 'mat_not_flat'
    assert r.message_de == 'Die Matte liegt nicht flach. Bitte glatt auflegen und an den Ecken fixieren.'


def test_a_stored_lens_that_no_longer_fits_is_inconsistent():
    kit = _kit('v1')
    cal = S.ideal_calibration(kit)
    cal['kb'] = dict(cal['kb'], k1=cal['kb']['k1'] + 0.05)
    r = _run('v1', _frames(kit), cal)
    assert r.code == 'inconsistent' and r.rms_px > mc.RMS_MAX_PX


@pytest.mark.xfail(strict=True, reason=(
    'spec 40 WP-10: "the stored k1 off by 0.15 -> inconsistent (probe: 0.85-1.3 px)". At placement B the per-tag '
    'mean residuals reach 3.5 px (V1) / 2.3 px (OMX), so the drop rule of step 2 (one tag above 1.5 px dropped, a '
    'second -> mat_not_flat) answers mat_not_flat before the RMS gate (RMS 1.46 / 1.05 px). The probe measured the '
    'RMS only and never ran the drop rule. Reported, not moved.'))
def test_the_stored_k1_off_by_0_15_is_inconsistent(case):
    kind, kit, cal = case
    cal['kb'] = dict(cal['kb'], k1=cal['kb']['k1'] + 0.15)
    r = _run(kind, _frames(kit), cal)
    assert r.code == 'inconsistent'
    assert 0.8 < r.rms_px < 1.6


# ── step 3: the fixture tags and the mat ─────────────────────────────────────
def test_the_mat_moved_2_mm_is_followed_with_a_note_and_20_mm_is_misplaced(case):
    kind, kit, cal = case
    m = dict(kit.mat, dxy=kit.mat['dxy'] + (0.002, 0.0))
    r = _run(kind, _frames(kit, mat=m), cal)
    assert r.ok and 'mat_moved' in r.notes and 1.5 < r.mat_shift_mm < 2.5
    # G is applied: the camera stays where it is against the arm's base although the mat moved under it
    assert np.linalg.norm(r.T_cam_to_w[:3, 3] - kit.cam.lens) < 0.001
    m = dict(kit.mat, dxy=kit.mat['dxy'] + (0.020, 0.0))
    assert _run(kind, _frames(kit, mat=m), cal).code == 'mat_misplaced'


def test_a_fixture_tag_that_sits_elsewhere_needs_an_arm_check(case):
    kind, kit, cal = case
    shift = np.zeros((8, 2))
    shift[4:, 0] = 0.0015
    r = _run(kind, _frames(kit, fixture_shift=shift), cal)
    assert r.code == 'needs_arm_check/fixture_changed' and r.fixture_fit_mm > 0.5


# ── step 4: the camera moved ─────────────────────────────────────────────────
@pytest.mark.parametrize('mm,code,note', ((4, 'ok', False), (6, 'ok', True), (14, 'tower_displaced', None)))
def test_the_head_moved_across_the_axis(case, mm, code, note):
    kind, kit, cal = case
    r = _run(kind, _frames(kit, cam=kit.cam.moved(dL=_across(kind) * mm / 1000.0)), cal)
    assert r.code == code
    if note is not None:
        assert ('camera_followed' in r.notes) is note
    assert abs(r.lens_drift_mm - mm) < 1.0
    if code == 'tower_displaced':
        assert r.message_de.startswith('Die Kamera sitzt nicht mehr an ihrem Platz am Turm')
        assert '(Abweichung 13,' in r.message_de or '(Abweichung 14,' in r.message_de


def test_the_head_turned_4_degrees_needs_an_arm_check(case):
    kind, kit, cal = case
    turn = chain.rodrigues(np.array([0.0, 0.0, 1.0]) * math.radians(4.0))
    r = _run(kind, _frames(kit, cam=kit.cam.moved(dR=turn)), cal)
    assert r.code == 'needs_arm_check/camera_moved'
    assert r.message_de == 'Die Kamera wurde verdreht. Bitte den Arm-Check starten.'


def test_a_real_focal_length_1_5_percent_above_the_stored_one_is_tower_displaced_v1():
    kit = _kit('v1')
    r = _run('v1', _frames(kit, cam=kit.cam.moved(f_scale=1.015)), S.ideal_calibration(kit))
    assert r.code == 'tower_displaced' and r.rms_px < 0.2


@pytest.mark.xfail(strict=True, reason=(
    'spec 40 WP-10: "a real focal length 1.5 % above the stored one -> tower_displaced with an RMS below 0.2 px '
    '(probe: 12 mm of drift along the axis)". The probe P-R8 ran at the revision-1 lens height (0.68 m); at placement '
    'B (0.575 m) the drift is the lens distance times 1.5 %: 10.6 mm on the V1 (refused) but 9.9 mm on the OMX, under '
    'LENS_FOLLOW_MAX_M (10 mm), so the OMX answers ok. Reported, not moved.'))
def test_a_real_focal_length_1_5_percent_above_the_stored_one_is_tower_displaced_omx():
    kit = _kit('omx')
    r = _run('omx', _frames(kit, cam=kit.cam.moved(f_scale=1.015)), S.ideal_calibration(kit))
    assert r.code == 'tower_displaced' and r.rms_px < 0.2


def test_a_stored_lens_9_mm_along_the_axis_from_lens_f_and_nothing_moved_is_ok_without_a_note(case):
    kind, _, _ = case
    kit = _kit(kind)
    kit.cam = kit.cam.moved(dL=-mc.contract_axis() * 0.009)
    r = _run(kind, _frames(kit), S.ideal_calibration(kit))
    assert r.ok and r.notes == ()
    assert 8.0 < r.lens_along_mm < 10.0 and r.lens_offset_mm < 1.5


def test_a_stored_lens_8_mm_across_and_the_head_4_mm_farther_is_tower_displaced(case):
    kind, _, _ = case
    kit = _kit(kind)
    v = _across(kind)
    kit.cam = kit.cam.moved(dL=v * 0.008)
    cal = S.ideal_calibration(kit)
    r = _run(kind, _frames(kit, cam=kit.cam.moved(dL=v * 0.004)), cal)
    assert r.code == 'tower_displaced'
    assert r.lens_drift_mm < 5.0 and r.lens_offset_mm > 10.0      # 12 mm across from the contract, 4 mm of drift


def test_a_lens_12_mm_across_from_the_pose_carried_lens_but_6_mm_from_lens_f_is_tower_displaced(case):
    """A15 (revision 4): the reading is against ``lens_w(base_axis_x, tower_pose)``, not ``LENS_F``."""
    kind, _, _ = case
    kit = _kit(kind)
    v = _across(kind)
    kit.cam = kit.cam.moved(dL=-v * 0.006)                         # 6 mm from LENS_F
    kit.fixture_w[:, :2] += v[:2] / np.linalg.norm(v[:2]) * 0.006  # the tower 6 mm the other way
    cal = S.ideal_calibration(kit)
    assert np.linalg.norm(np.asarray(sf.lens_w(chain.BASE_AXIS_X[kind], cal['tower_pose'])) -
                          (np.asarray(sf.LENS_F) + (chain.BASE_AXIS_X[kind], 0, 0))) > 0.0055
    r = _run(kind, _frames(kit), cal)
    assert r.code == 'tower_displaced' and 11.0 < r.lens_offset_mm < 13.5 and r.lens_drift_mm < 1.5
    # judged against the contract lens (revision 3) the same lens would have passed
    _, across = mc.split_along_across(kit.cam.lens - (np.asarray(sf.LENS_F) + (chain.BASE_AXIS_X[kind], 0, 0)))
    assert across < sf.LENS_FOLLOW_MAX_M
