#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The fixture contract (spec 40 §3, revision 4 as revision 6 restores it).

``scene_fixture.py`` carries the contract block byte for byte: its sha256 is
the one the spec states with D84's V1 pose AC12, its values are the block's
values, and the derived rules (the tower placed by a measured pose, the pose
fit, the lens a pose carries, the cut work zone, the contract hash) behave as
§3 says. The React copy ``sceneFixture.json`` and the zone case table
``sceneFixture.cases.json`` are fresh renders of this module
(``tools/export_scene_fixture.py``).
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest

_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_SRC = _PKG / 'workflow' / 'scene_fixture.py'
_REPO = Path(__file__).resolve().parents[3]
_EXPORT = _REPO / 'tools' / 'export_scene_fixture.py'
_FIXTURE_DIR = (_REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'components'
                / 'Workshop' / 'fixture')

# 40 §3 revision 4 (= revision 6, D77) with D84's V1 pose AC12 (joint 3 1.5396 -> 1.4523): sha256 of the block
# between the fences plus its final line break (the convention of scratch/r8/probes/contract_check.py). Revision 4's
# block itself read bc29fce3dc3c341980501b915f3e7b63e422b9912f6b838eea6be7dd4666a46a.
CONTRACT_BLOCK_SHA256 = '9ea025fa8b05f202ce6763dcc64bdaded264f0b0464d1422384d1d19821d5105'
BEGIN = '# ---- BEGIN CONTRACT BLOCK (40 §3; byte for byte, never edit by hand) ----\n'
END = '# ---- END CONTRACT BLOCK ----\n'


def _sf():
    from physical_ai_server.workflow import scene_fixture
    return scene_fixture


def _block_text() -> str:
    text = _SRC.read_text(encoding='utf-8')
    assert text.count(BEGIN) == 1 and text.count(END) == 1
    return text.split(BEGIN, 1)[1].split(END, 1)[0]


def _export_tool():
    spec = importlib.util.spec_from_file_location('_export_scene_fixture_under_test', _EXPORT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════════════════
# (1) the block is the spec's, byte for byte, and the module carries its values
# ══════════════════════════════════════════════════════════════════════════

def test_contract_block_is_the_specs_byte_for_byte():
    block = _block_text()
    assert hashlib.sha256(block.encode('utf-8')).hexdigest() == CONTRACT_BLOCK_SHA256


def test_the_module_carries_exactly_the_blocks_values():
    sf = _sf()
    ns: dict = {}
    exec(compile(_block_text(), str(_SRC), 'exec'), ns)  # noqa: S102 — our own literals
    names = [n for n in ns if not n.startswith('__')]
    assert set(names) == set(sf.CONTRACT_NAMES)
    for name in names:
        assert getattr(sf, name) == ns[name], name


def test_contract_names_are_every_assignment_of_the_block_in_order():
    tree = ast.parse(_block_text())
    assigned = [t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets
                if isinstance(t, ast.Name)]
    assert tuple(assigned) == _sf().CONTRACT_NAMES


def test_revision_4_values_that_revision_6_restored():
    sf = _sf()
    assert sf.TOWER_RESIDUAL_SERVER_M == {'v1': 0.0130, 'omx': 0.0100}
    assert sf.TOWER_RESIDUAL_UNCALIBRATED_M == {'v1': 0.0260, 'omx': 0.0195}
    assert sf.TOWER_RESIDUAL_DRIVER_M == sf.TOWER_RESIDUAL_UNCALIBRATED_M['v1']
    assert sf.TOWER_DROOP_KAPPA == {'v1': 2.6, 'omx': 1.8}
    assert sf.ARM_V1['observe_joints_rad'] == (0.0, 0.1, 2.2, -1.61, 0.0)
    assert sf.ARM_V1['zone_cut_knots'] == ((0.11, 52.0), (0.265, 74.0), (0.297, 83.0), (0.298, 85.0))
    assert sf.ARM_OMX['zone_cut_knots'] == ((0.12, 66.0), (0.1225, 66.0), (0.245, 73.0), (0.25, 74.0))
    assert dict((n, h) for n, _c, _r, h in sf.TOWER_OBB_F)['lens'] == (0.0085, 0.0085, 0.0055)


# the scalar literals of the block, pinned by value too (test_constant_pins reads a pin table like this one)
SCALARS = (
    ('LIFT_M', 0.0072), ('MAT_THICKNESS_M', 0.00045), ('LENS_FOLLOW_MAX_M', 0.010), ('TOWER_LEAN_AT_LENS_M', 0.0175),
    ('TOWER_AXIS_M', 0.0012), ('TOWER_BUILD_M', 0.0005), ('TOWER_TOP_ROTATION_M', 0.0026),
    ('TOWER_SAMPLE_M', 0.0015), ('TOWER_RESIDUAL_DRIVER_M', 0.0260), ('TOWER_FIT_MAX_M', 0.0010),
    ('FIXTURE_TAG_SIZE_M', 0.025), ('FIXTURE_TAG_Z_M', 0.0072), ('MAT_TAG_SIZE_M', 0.03), ('MAT_CARD_M', 0.04),
)


def test_the_scalar_constants_are_the_specs():
    sf = _sf()
    for name, value in SCALARS:
        assert getattr(sf, name) == value, name


def test_the_fingertip_offset_is_the_value_the_build_ships():
    """WP-11 enters WP-M1's number here. Decision D86: the release ships the
    provisional mesh value 0.0026 for the OMX (the owner's RG-0 measurement
    replaces it in a follow-up release); the V1's 0.0 is exact on the meshes."""
    assert _sf().FINGERTIP_BELOW_EE_M == {'omx': 0.0026, 'v1': 0.0}


def test_the_module_imports_the_standard_library_only():
    """Read by object_catalog (stdlib only) and by the setup suite's lockstep test."""
    tree = ast.parse(_SRC.read_text(encoding='utf-8'))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or '').split('.')[0])
    assert mods <= {'__future__', 'hashlib', 'json', 'math'}, mods


# ══════════════════════════════════════════════════════════════════════════
# contract_hash
# ══════════════════════════════════════════════════════════════════════════

CONTRACT_HASH = '0087eb45f8e74b0c831d11123ea3c48ed5e9c2a6a39c30ac6d8ff0fa0c8ce79c'   # D84 (AC12); was 120402f0…


def test_contract_hash_is_stable_and_pinned():
    sf = _sf()
    h = sf.contract_hash()
    assert h == sf.contract_hash()
    assert len(h) == 64 and int(h, 16) >= 0
    assert h == CONTRACT_HASH


def test_contract_hash_moves_with_every_constant(monkeypatch):
    sf = _sf()
    base = sf.contract_hash()
    monkeypatch.setattr(sf, 'FINGERTIP_BELOW_EE_M', {'omx': 0.0031, 'v1': 0.0})
    assert sf.contract_hash() != base
    monkeypatch.undo()
    monkeypatch.setattr(sf, 'ARM_V1', dict(sf.ARM_V1, zone_cut_knots=((0.11, 51.5),)))
    assert sf.contract_hash() != base


# ══════════════════════════════════════════════════════════════════════════
# derived rules
# ══════════════════════════════════════════════════════════════════════════

def test_arm_spec_by_profile():
    sf = _sf()
    assert sf.arm_spec('omx_full') is sf.ARM_OMX
    assert sf.arm_spec('omx_follower') is sf.ARM_OMX
    assert sf.arm_spec('v1_studio') is sf.ARM_V1
    for bad in ('edu6_studio', '', None, 'V1_STUDIO', 7):
        assert sf.arm_spec(bad) is None
    assert sf.arm_key('v1_studio') == 'v1' and sf.arm_key('omx_full') == 'omx'
    assert sf.arm_key('nope') is None


def test_tag_corners_are_in_the_detectors_order():
    sf = _sf()
    assert sf.tag_corners_xy(1.0, 2.0, 0.5) == ((1.25, 1.75), (1.25, 2.25), (0.75, 2.25), (0.75, 1.75))


def test_target_corners():
    sf = _sf()
    assert len(sf.TARGET_CORNERS) == 9
    pitch = sf.TARGET['pitch_m']
    for k, (i, j, corners) in enumerate(sf.TARGET_CORNERS):
        assert (i, j) == divmod(k, 3)
        assert corners == sf.tag_corners_xy((i - 1) * pitch, (j - 1) * pitch, sf.TARGET['tag_size_m'])
    for spec in (sf.ARM_V1, sf.ARM_OMX):
        tags = sf.target_tag_corners(spec)
        assert [t for t, _c in tags] == list(spec['target_tag_ids'])
        assert sum(len(c) for _t, c in tags) == 36


def test_fixture_corners_follow_the_base_axis():
    sf = _sf()
    c0 = sf.fixture_corners_w(0.0)
    assert len(c0) == 8
    assert c0[:4] == sf.tag_corners_xy(-0.09, 0.165, 0.025)
    assert c0[4:] == sf.tag_corners_xy(-0.14, 0.11, 0.025)
    c1 = sf.fixture_corners_w(-0.01125)
    for (x0, y0), (x1, y1) in zip(c0, c1):
        assert x1 == pytest.approx(x0 - 0.01125, abs=1e-15) and y1 == y0


def test_tower_obbs_w_without_a_pose_is_the_contract_place():
    """(R4-1, half) pose None = (0, 0, 0) = TOWER_OBB_F shifted by base_axis_x, to 1e-12."""
    sf = _sf()
    for bax in (0.0, -0.01125):
        placed = sf.tower_obbs_w(bax)
        zero = sf.tower_obbs_w(bax, (0.0, 0.0, 0.0))
        assert [p[0] for p in placed] == [p[0] for p in sf.TOWER_OBB_F]
        for (n, c, r, h), (n0, c0, r0, h0), (nz, cz, rz, hz) in zip(placed, sf.TOWER_OBB_F, zero):
            assert n == n0 == nz and h == h0 == hz
            assert np.allclose(c, (c0[0] + bax, c0[1], c0[2]), atol=1e-12, rtol=0)
            assert np.allclose(r, r0, atol=1e-12, rtol=0) and np.allclose(cz, c, atol=1e-12, rtol=0)
            assert np.allclose(rz, r, atol=1e-12, rtol=0)


def test_tower_obbs_w_turns_the_tower_with_its_pose():
    """(R4-1) a 1.5 deg yaw moves the column centre by 2 * 0.2086 * sin 0.75 deg = 5.46 mm."""
    sf = _sf()
    col0 = dict((p[0], p) for p in sf.tower_obbs_w(0.0))['column']
    col = dict((p[0], p) for p in sf.tower_obbs_w(0.0, (math.radians(1.5), 0.0, 0.0)))['column']
    moved = math.hypot(col[1][0] - col0[1][0], col[1][1] - col0[1][1])
    assert moved * 1000.0 == pytest.approx(5.46, abs=0.01)
    assert col[1][2] == col0[1][2]
    # the axes turn with it, the heights never change, a translation shifts every centre
    head = dict((p[0], p) for p in sf.tower_obbs_w(0.0, (0.3, 0.01, -0.02)))['head']
    head0 = dict((p[0], p) for p in sf.tower_obbs_w(0.0))['head']
    rz = np.array([[math.cos(0.3), -math.sin(0.3), 0.0], [math.sin(0.3), math.cos(0.3), 0.0], [0.0, 0.0, 1.0]])
    assert np.allclose(np.array(head[2]), rz @ np.array(head0[2]), atol=1e-12)
    assert np.allclose(head[1][:2], rz[:2, :2] @ np.array(head0[1][:2]) + (0.01, -0.02), atol=1e-12)
    for bad in ((float('nan'), 0.0, 0.0), (0.0, float('inf'), 0.0), (0.0, 0.0), 'x'):
        with pytest.raises(ValueError):
            sf.tower_obbs_w(0.0, bad)


def test_tower_pose_fit_recovers_a_known_pose_exactly():
    """(R4-2) a known (psi, tx, ty) from the 8 corners to 1e-9, RMS 0."""
    sf = _sf()
    for bax in (0.0, -0.01125):
        nominal = np.array(sf.fixture_corners_w(bax))
        for psi, tx, ty in ((0.0, 0.0, 0.0), (math.radians(1.5), 0.0021, -0.0013), (-0.4, 0.05, 0.02)):
            rz = np.array([[math.cos(psi), -math.sin(psi)], [math.sin(psi), math.cos(psi)]])
            measured = nominal @ rz.T + (tx, ty)
            fpsi, ftx, fty, rms = sf.tower_pose_fit([tuple(p) for p in measured], bax)
            assert abs(fpsi - psi) < 1e-9 and abs(ftx - tx) < 1e-9 and abs(fty - ty) < 1e-9
            assert rms < 1e-12


def test_tower_pose_fit_rms_stays_under_0_3_mm_for_0_2_mm_uniform_noise():
    """(R4-2, restated by R5, 58 m-5) UNIFORM +-0.2 mm per coordinate, seeds 0..99: the fit residual is a projection
    of the noise, so RMS <= sqrt(16 * 0.2^2 / 8) = 0.283 mm for EVERY draw."""
    sf = _sf()
    nominal = np.array(sf.fixture_corners_w(0.0))
    worst = 0.0
    for seed in range(100):
        rng = np.random.default_rng(seed)
        psi, tx, ty = rng.uniform(-0.03, 0.03), rng.uniform(-0.003, 0.003), rng.uniform(-0.003, 0.003)
        rz = np.array([[math.cos(psi), -math.sin(psi)], [math.sin(psi), math.cos(psi)]])
        measured = nominal @ rz.T + (tx, ty) + rng.uniform(-0.0002, 0.0002, size=nominal.shape)
        rms = sf.tower_pose_fit([tuple(p) for p in measured], 0.0)[3]
        worst = max(worst, rms)
        assert rms < 0.0003, (seed, rms)
    assert worst <= math.sqrt(16 * 0.0002 ** 2 / 8) + 1e-15


def test_tower_pose_fit_refuses_what_it_cannot_fit():
    sf = _sf()
    good = sf.fixture_corners_w(0.0)
    for bad in (good[:7], list(good) + [(0.0, 0.0)], [(float('nan'), 0.0)] + list(good[1:]), None):
        with pytest.raises(ValueError):
            sf.tower_pose_fit(bad, 0.0)
    # (x, y, z) corners are accepted (the plane height is not the fit's business)
    assert sf.tower_pose_fit([(x, y, 0.0072) for x, y in good], 0.0)[3] < 1e-12


def test_lens_w_is_carried_by_the_pose():
    sf = _sf()
    assert sf.lens_w(0.0, None) == pytest.approx(sf.LENS_F)
    assert sf.lens_w(-0.01125, None) == pytest.approx((sf.LENS_F[0] - 0.01125, sf.LENS_F[1], sf.LENS_F[2]))
    psi, tx, ty = 0.02, 0.001, -0.002
    x, y, z = sf.lens_w(0.0, (psi, tx, ty))
    assert x == pytest.approx(math.cos(psi) * -0.1 - math.sin(psi) * 0.1 + tx, abs=1e-15)
    assert y == pytest.approx(math.sin(psi) * -0.1 + math.cos(psi) * 0.1 + ty, abs=1e-15)
    assert z == sf.LENS_F[2]


# ══════════════════════════════════════════════════════════════════════════
# (6) the target plate stays well above the table in every pinned pose
# ══════════════════════════════════════════════════════════════════════════

def test_target_plate_is_at_least_5_cm_above_the_table_in_all_40_pinned_poses():
    """r02: lowest corner 127.5 mm (V1) / 126.2 mm (OMX), with the repo's solvers, the contract's mount, the lift."""
    from physical_ai_server.workflow.ik_solver import IKSolver
    from physical_ai_server.workflow.v1_ik import V1IKSolver
    sf = _sf()
    half = sf.TARGET['plate_m'] / 2.0
    plate = np.array([[sx * half, sy * half, 0.0, 1.0] for sx in (-1, 1) for sy in (-1, 1)]).T
    lowest = {}
    for key, spec, solver in (('v1', sf.ARM_V1, V1IKSolver()), ('omx', sf.ARM_OMX, IKSolver())):
        mount = np.array(spec['target_mount_ee'], float)
        zs = []
        for q in spec['arm_check_poses_rad']:
            rot, t = solver.fk(q)
            tee = np.eye(4)
            tee[:3, :3], tee[:3, 3] = rot, t
            zs.append(float((tee @ mount @ plate)[2].min()) + sf.LIFT_M)
        assert len(zs) == 20
        lowest[key] = min(zs)
        assert lowest[key] >= 0.05, key
    assert lowest['v1'] * 1000 == pytest.approx(127.5, abs=0.1)
    assert lowest['omx'] * 1000 == pytest.approx(126.2, abs=0.1)


# ══════════════════════════════════════════════════════════════════════════
# (7) the reserved ids
# ══════════════════════════════════════════════════════════════════════════

def test_the_reserved_range_holds_every_calibration_id_and_no_object_id():
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    sf = _sf()
    lo, hi = sf.RESERVED_TAG_IDS
    assert (lo, hi) == (400, 586)
    ids = set(sf.FIXTURE_TAGS_F)
    for spec in (sf.ARM_V1, sf.ARM_OMX):
        ids |= set(spec['mat_tags_f']) | set(spec['target_tag_ids'])
    assert len(ids) == 2 + 14 + 14 + 9 + 9
    assert all(lo <= t <= hi for t in ids)
    for pid in (None, 'omx_full', 'omx_follower', 'v1_studio'):
        assert not any(lo <= t <= hi for t in fixed_catalog(pid).all_tag_ids()), pid
    assert sf.is_reserved_tag_id(400) and sf.is_reserved_tag_id(586)
    assert not sf.is_reserved_tag_id(399) and not sf.is_reserved_tag_id(587)


# ══════════════════════════════════════════════════════════════════════════
# the cut work zone (R4-8) and the rendered JSON (7)
# ══════════════════════════════════════════════════════════════════════════

def test_zone_az_hi_follows_the_knots():
    sf = _sf()
    v1, omx = sf.ARM_V1, sf.ARM_OMX
    assert sf.zone_az_hi(v1, 0.11) == 52.0
    assert sf.zone_az_hi(v1, 0.1875) == pytest.approx(63.0)
    assert sf.zone_az_hi(v1, 0.2975) == pytest.approx(84.0)
    assert sf.zone_az_hi(v1, 0.298 + 1e-10) == 85.0          # a knot's own radius within 1e-9 m
    assert sf.zone_az_hi(v1, 0.30) == 85.0                    # no cut beyond the last knot: the sector edge
    assert sf.zone_az_hi(omx, 0.12) == 66.0
    assert sf.zone_az_hi(omx, 0.1225) == 66.0
    assert sf.zone_az_hi(omx, 0.25) == 74.0
    assert sf.zone_az_hi(omx, 0.2475) == pytest.approx(73.5)


def test_in_zone_basic():
    sf = _sf()

    def at(spec, r, az):
        return sf.in_zone(spec, r * math.cos(math.radians(az)), r * math.sin(math.radians(az)))

    assert at(sf.ARM_V1, 0.2, 0.0) and at(sf.ARM_V1, 0.2, -85.0) and not at(sf.ARM_V1, 0.2, -85.2)
    assert not at(sf.ARM_V1, 0.2, 80.0)                       # the cut corner
    assert at(sf.ARM_V1, 0.32, 84.9) and not at(sf.ARM_V1, 0.32, 85.1)
    assert not at(sf.ARM_V1, 0.105, 0.0) and not at(sf.ARM_V1, 0.345, 0.0)
    assert at(sf.ARM_OMX, 0.12, -90.0) and not at(sf.ARM_OMX, 0.2, 80.0)
    for bad in ((float('nan'), 0.0), (0.0, float('inf'))):
        assert sf.in_zone(sf.ARM_V1, *bad) is False


def test_in_zone_case_table_is_complete_and_holds():
    """(R4-8) every knot, 0.1 deg inside and outside each cut segment, the sector edges, the radii."""
    sf = _sf()
    cases = json.loads((_FIXTURE_DIR / 'sceneFixture.cases.json').read_text(encoding='utf-8'))
    for key, spec in (('v1', sf.ARM_V1), ('omx', sf.ARM_OMX)):
        rows = cases['in_zone'][key]
        kinds = {r['what'] for r in rows}
        for k in range(len(spec['zone_cut_knots'])):
            assert {f'knot {k}', f'knot {k} +0.1 deg'} <= kinds, (key, k)
        for k in range(len(spec['zone_cut_knots']) - 1):
            assert {f'segment {k} -0.1 deg', f'segment {k} +0.1 deg'} <= kinds, (key, k)
        assert {'sector low -0.1 deg', 'sector low +0.1 deg', 'inner radius', 'outer radius',
                'inside the inner radius', 'outside the outer radius'} <= kinds, key
        for r in rows:
            assert sf.in_zone(spec, r['x_f'], r['y_f']) is r['in_zone'], (key, r)
        for r in cases['zone_az_hi'][key]:
            want = r['az_hi_deg']
            got = sf.zone_az_hi(spec, r['r_m'])
            assert got == pytest.approx(want, abs=1e-9), (key, r)
    assert sum(r['in_zone'] for k in cases['in_zone'] for r in cases['in_zone'][k]) > 0
    assert sum(not r['in_zone'] for k in cases['in_zone'] for r in cases['in_zone'][k]) > 0


def test_the_json_files_equal_a_fresh_render():
    """(7) both React files are byte-identical to what tools/export_scene_fixture.py renders now."""
    tool = _export_tool()
    assert (_FIXTURE_DIR / 'sceneFixture.json').read_text(encoding='utf-8') == tool.render_fixture()
    assert (_FIXTURE_DIR / 'sceneFixture.cases.json').read_text(encoding='utf-8') == tool.render_cases()


def test_the_rendered_json_carries_every_constant_and_the_hash():
    sf = _sf()
    data = json.loads(_export_tool().render_fixture())
    assert data['contract_hash'] == sf.contract_hash()
    assert set(data['constants']) == set(sf.CONTRACT_NAMES)
    assert data['constants']['RESERVED_TAG_IDS'] == [400, 586]
    assert [b[0] for b in data['constants']['TOWER_OBB_F']] == [p[0] for p in sf.TOWER_OBB_F]
    assert data['constants']['FIXTURE_TAGS_F'] == {'480': [-0.09, 0.165], '481': [-0.14, 0.11]}
