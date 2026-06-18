"""Detection fail-loud + scene-frame freshness gate (P4).

A missing object/marker model, an uncalibrated colour, or a stale/frozen scene
frame must raise a precise German WorkflowError at the block — not silently
return an empty list (indistinguishable from "nothing on the table").
"""

from __future__ import annotations

import numpy as np
import pytest

from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.handlers.perception_blocks import (
    _scene_frame,
    detect_color,
    detect_marker,
    detect_object,
)


class _Ctx:
    def __init__(self, frame=None, age=None, perception=None):
        self._frame = frame
        self._age = age
        self.perception = perception

    def get_scene_frame(self):
        return self._frame

    def get_scene_frame_age(self):
        return self._age


class _Perc:
    def __init__(self, yolox=True, apriltag=True, colors=()):
        self._yolox = yolox
        self._at = apriltag
        self._colors = set(colors)

    def yolox_available(self):
        return self._yolox

    def apriltag_available(self):
        return self._at

    def has_color(self, c):
        return c in self._colors

    def detect(self, *a, **k):
        return []


def _img():
    return np.zeros((10, 10, 3), dtype=np.uint8)


def test_scene_frame_none_raises():
    with pytest.raises(WorkflowError):
        _scene_frame(_Ctx(frame=None))


def test_scene_frame_stale_raises():
    with pytest.raises(WorkflowError) as e:
        _scene_frame(_Ctx(frame=_img(), age=5.0))
    assert 'aktuelles' in str(e.value)


def test_scene_frame_fresh_ok():
    assert _scene_frame(_Ctx(frame=_img(), age=0.1)) is not None


def test_scene_frame_unknown_age_does_not_block():
    # age None (unknown) must not falsely reject a present frame.
    assert _scene_frame(_Ctx(frame=_img(), age=None)) is not None


def test_object_detector_missing_raises():
    ctx = _Ctx(frame=_img(), age=0.1, perception=_Perc(yolox=False))
    with pytest.raises(WorkflowError) as e:
        detect_object(ctx, {'class': 'banana'})
    assert 'Objekt-Erkennung' in str(e.value)


def test_marker_detector_missing_raises():
    ctx = _Ctx(frame=_img(), age=0.1, perception=_Perc(apriltag=False))
    with pytest.raises(WorkflowError) as e:
        detect_marker(ctx, {'marker_id': 1})
    assert 'Marker-Erkennung' in str(e.value)


def test_uncalibrated_color_raises():
    ctx = _Ctx(frame=_img(), age=0.1, perception=_Perc(colors=()))
    with pytest.raises(WorkflowError) as e:
        detect_color(ctx, {'color': 'rot'})
    assert 'kalibriert' in str(e.value)
