#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""``autocal.store`` (spec 40 S4): the stored arm check — round trip, the
version counter, and a file made under another contract reads as absent."""

from __future__ import annotations

import json
import os

import pytest

from physical_ai_server.workflow import scene_fixture as sf
from physical_ai_server.workflow.autocal import store


def _cal(**over):
    data = {
        'v': 1, 'fixture_id': sf.FIXTURE_ID, 'contract_hash': sf.contract_hash(), 'profile_id': 'v1_studio',
        'camera_profile_id': 'tower_v1', 'image_size': [1920, 1080], 'created_utc': '2026-11-20T08:00:00Z',
        'kb': {'f': 1031.2, 'aspect': 1.001, 'cx': 961.0, 'cy': 538.2, 'k1': 0.05, 'k2': -0.04},
        'print_scale': 1.0012, 'joint_offsets_rad': [0.0, 0.004, -0.002, 0.001, 0.0],
        'axis_tilts_rad': [0.001, 0.0, -0.002, 0.0005, 0.003, -0.001], 'tilt': [0.002, -0.001],
        'mat_pose': [0.0011, -0.0004, 0.003], 'camera_pose_mat': {'rvec': [2.1, 0.3, -0.2], 't': [0.01, 0.2, 0.55]},
        'lens_w': [-0.1012, 0.0991, 0.5761], 'fixture_xy_mat': [[0.0, 0.0]] * 8, 'fixture_px': [[900.0, 400.0]] * 8,
        'mount': {'rvec': [0.0, 0.1, 0.0], 't': [0.0, -0.095, -0.11]}, 'droop_envelope_rad': [0.001, 0.02, 0.01, 0.004, 0.001],
        'tower_pose': [0.004, 0.0005, -0.0003], 'tower_fit_rms_m': 0.0002, 'target_removed': False,
        'droop_warning': None, 'notes': [],
        'quality': {'poses_used': 20, 'rms_px': 0.21, 'holdout_rms_px': 0.4, 'holdout_p95_mm': 0.4,
                    'field_tags': 14, 'sharpness': 80.0},
    }
    data.update(over)
    return data


def test_round_trip(tmp_path):
    s = store.Store(str(tmp_path))
    assert s.read('v1_studio').status == 'none'
    s.write(_cal())
    r = s.read('v1_studio', (1920, 1080))
    assert r.status == 'ok' and r.data == _cal()
    assert s.path == os.path.join(str(tmp_path), 'calibration', 'arm_check.json')
    assert [p for p in os.listdir(os.path.dirname(s.path)) if p.endswith('.tmp')] == []


def test_every_write_bumps_the_version(tmp_path):
    s = store.Store(str(tmp_path))
    v0 = store.version()
    s.write(_cal())
    assert store.version() == v0 + 1
    assert s.set_target_removed('v1_studio', True)
    assert store.version() == v0 + 2
    assert s.read('v1_studio').data['target_removed'] is True
    # another instance on the same directory shares the counter
    store.Store(str(tmp_path)).write(_cal())
    assert store.version() == v0 + 3


@pytest.mark.parametrize('field,value', [
    ('contract_hash', '0' * 64),
    ('fixture_id', 'tower_v0'),
    ('profile_id', 'omx_full'),
    ('image_size', [640, 480]),
])
def test_another_contract_profile_or_image_size_reads_as_absent(tmp_path, field, value):
    s = store.Store(str(tmp_path))
    s.write(_cal(**{field: value}))
    assert s.read('v1_studio', (1920, 1080)).status == 'contract_changed'
    assert s.read('v1_studio', (1920, 1080)).data is None


def test_a_broken_or_incomplete_file_reads_as_none(tmp_path):
    s = store.Store(str(tmp_path))
    os.makedirs(os.path.dirname(s.path))
    with open(s.path, 'w', encoding='utf-8') as fh:
        fh.write('{not json')
    assert s.read('v1_studio').status == 'none'
    data = _cal()
    del data['tower_pose']
    with open(s.path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    assert s.read('v1_studio').status == 'none'


def test_an_incomplete_or_non_finite_calibration_is_never_written(tmp_path):
    s = store.Store(str(tmp_path))
    v0 = store.version()
    data = _cal()
    del data['lens_w']
    with pytest.raises(ValueError):
        s.write(data)
    with pytest.raises(ValueError):
        s.write(_cal(print_scale=float('nan')))
    assert not os.path.exists(s.path)
    assert store.version() == v0


def test_set_target_removed_needs_a_valid_arm_check(tmp_path):
    s = store.Store(str(tmp_path))
    assert s.set_target_removed('v1_studio', True) is False
    s.write(_cal(contract_hash='1' * 64))
    assert s.set_target_removed('v1_studio', True) is False


def test_the_last_mat_check_is_kept_beside_it(tmp_path):
    s = store.Store(str(tmp_path))
    assert s.read_mat_check_last() is None
    s.write_mat_check_last({'ok': True, 'code': 'ok', 'notes': ['mat_moved']})
    assert s.read_mat_check_last() == {'ok': True, 'code': 'ok', 'notes': ['mat_moved']}
