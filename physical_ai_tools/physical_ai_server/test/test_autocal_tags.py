#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""``autocal.tags`` (spec 40 S4, A13, A14): the corner order, the split of the
ids, the median over frames, the tag centre."""

from __future__ import annotations

import numpy as np
import pytest

from physical_ai_server.workflow import scene_fixture as sf
from physical_ai_server.workflow.autocal import tags


def _quad(cx=500.0, cy=300.0, size=40.0, turn=0.2, persp=0.15):
    """A perspective-distorted tag quad in the model's corner order."""
    h = size / 2
    base = np.array([(h, -h), (h, h), (-h, h), (-h, -h)])
    c, s = np.cos(turn), np.sin(turn)
    q = base @ np.array([[c, -s], [s, c]]).T
    q[:, 0] *= 1.0 + persp * q[:, 1] / size
    return q + (cx, cy)


@pytest.mark.parametrize('shift', (1, 2, 3))
def test_match_corners_restores_a_detection_rotated_by_one_two_or_three_places(shift):
    model = _quad()
    detected = np.roll(model, shift, axis=0)
    assert not np.allclose(detected, model)
    assert np.allclose(tags.match_corners(detected, model), model)


def test_match_corners_keeps_an_ordered_detection_and_ignores_a_whole_tag_offset():
    model = _quad()
    nominal = _quad(cx=530.0, cy=280.0, turn=0.25)      # a camera a few mm off: 30 px away, slightly turned
    for k in range(4):
        assert np.allclose(tags.match_corners(np.roll(model, k, axis=0), nominal), model)


def test_classify_splits_the_reserved_ranges_by_arm():
    omx_field = list(sf.ARM_OMX['mat_tags_f'])[:3]
    v1_field = list(sf.ARM_V1['mat_tags_f'])[:2]
    ids = omx_field + v1_field + [480, 481, 520, 500, 3, 399, 586, 1000]
    g = tags.classify(ids, 'omx')
    assert g['field'] == sorted(omx_field)
    assert g['foreign_mat'] == sorted(v1_field)
    assert g['fixture'] == [480, 481]
    assert g['target'] == [520]
    assert g['foreign_target'] == [500]
    assert g['object'] == [3, 399, 1000]
    g1 = tags.classify(ids, 'v1')
    assert g1['field'] == sorted(v1_field) and g1['target'] == [500] and g1['foreign_target'] == [520]


def test_median_corners_is_per_tag_and_per_corner():
    a = _quad()
    frames = [{7: a + 1.0}, {7: a - 1.0, 9: a}, {7: tags.TagObs(a + 0.25, 50.0)}]
    med = tags.median_corners(frames)
    assert med[7][1] == 3 and med[9][1] == 1
    assert np.allclose(med[7][0], a + 0.25)
    assert np.allclose(med[9][0], a)


def test_centre_from_corners_is_where_the_diagonals_cross():
    q = _quad(persp=0.4)
    c = tags.centre_from_corners(q)
    # the intersection lies on both diagonals
    for i, j in ((0, 2), (1, 3)):
        d = q[j] - q[i]
        w = np.array(c) - q[i]
        assert abs(d[0] * w[1] - d[1] * w[0]) < 1e-9
    # and is not the corners' mean under perspective
    assert np.linalg.norm(np.array(c) - q.mean(axis=0)) > 0.05
    assert tags.centre_from_corners([[0, 0], [1, 0], [2, 0], [3, 0]]) is None
    assert tags.centre_from_corners([[0, 0], [1, 1], [float('nan'), 0], [0, 1]]) is None
