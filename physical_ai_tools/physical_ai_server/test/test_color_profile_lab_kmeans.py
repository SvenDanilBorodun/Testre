"""Regression test for the percentile-HSV → LAB-cluster rewrite (audit
§1.7a). The pre-rewrite implementation silently broke for red because
HSV hue wraps at 180; LAB has no wrap so the per-channel std is
well-defined. This test renders four canonical cubes (rot/grün/blau/
gelb) on a contrasting background and asserts that the captured LAB
cluster centroids are well-separated.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture
def calib_dir(monkeypatch, tmp_path):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    from importlib import reload
    from physical_ai_server.workflow import color_profile as cp
    reload(cp)
    return tmp_path


def _cube_image(rgb: tuple[int, int, int], background: tuple[int, int, int] = (200, 200, 200)) -> np.ndarray:
    """Render a 64x64 cube of the given sRGB colour centered in a 320x240 frame."""
    bgr_bg = np.full((240, 320, 3), background[::-1], dtype=np.uint8)
    bgr_bg[88:152, 128:192] = rgb[::-1]
    return bgr_bg


def test_capture_red_works_without_hue_wrap(calib_dir):
    """Red was the canary for the percentile-HSV bug. With LAB the
    capture must succeed and the centroid's a* component must be
    clearly positive (red is +a in LAB)."""
    from physical_ai_server.workflow.color_profile import ColorProfileManager
    mgr = ColorProfileManager()
    bgr = _cube_image((220, 30, 30))
    ok, msg, center, std = mgr.capture('rot', bgr)
    assert ok is True, msg
    assert len(center) == 3
    # OpenCV LAB: a* in 0..255 with offset 128. Red has a* > 128.
    assert center[1] > 140, f'Red a* expected > 140, got {center[1]}'


def test_capture_all_four_colors_produce_distinct_centroids(calib_dir):
    """The four canonical colours must end up at clearly separated LAB
    centroids — pairwise euclidean distance > 30 in 0..255 LAB units."""
    from physical_ai_server.workflow.color_profile import ColorProfileManager
    mgr = ColorProfileManager()
    captures = {
        'rot':    _cube_image((220, 30, 30)),
        'gruen':  _cube_image((30, 200, 30)),
        'blau':   _cube_image((30, 30, 220)),
        'gelb':   _cube_image((230, 220, 30)),
    }
    centers = {}
    for name, frame in captures.items():
        ok, msg, center, _std = mgr.capture(name, frame)
        assert ok is True, f'capture({name}) failed: {msg}'
        centers[name] = np.array(center)

    names = list(centers.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            d = float(np.linalg.norm(centers[names[i]] - centers[names[j]]))
            assert d > 30.0, (
                f'Centroids for {names[i]} and {names[j]} too close: {d:.1f}'
            )


def test_has_all_colors_after_full_capture_set(calib_dir):
    from physical_ai_server.workflow.color_profile import ColorProfileManager
    mgr = ColorProfileManager()
    for name, rgb in (
        ('rot', (220, 30, 30)),
        ('gruen', (30, 200, 30)),
        ('blau', (30, 30, 220)),
        ('gelb', (230, 220, 30)),
    ):
        ok, msg, _c, _s = mgr.capture(name, _cube_image(rgb))
        assert ok, msg
    assert mgr.has_all_colors() is True


def test_persisted_profile_round_trips(calib_dir):
    """Capture → fresh manager picks up the YAML on construction."""
    from physical_ai_server.workflow.color_profile import ColorProfileManager
    mgr = ColorProfileManager()
    mgr.capture('rot', _cube_image((220, 30, 30)))
    mgr2 = ColorProfileManager()
    profile = mgr2.lab_profile('rot')
    assert profile is not None
    assert 'center' in profile and 'std' in profile and 'threshold' in profile
    assert profile['center'].shape == (3,)
