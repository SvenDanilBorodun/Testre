"""Perception fail-loud + scene-frame freshness gate.

A missing AprilTag detector or a stale/frozen scene frame must raise a precise
German WorkflowError at the block — not silently return an empty list
(indistinguishable from "nothing on the table"). The legacy colour/COCO/marker
detection blocks were removed (P4); AprilTag named-object detection is the only
perception path, so these gates cover the marker detector + the shared
scene-frame freshness check.
"""

from __future__ import annotations

import numpy as np
import pytest

from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.handlers.perception_blocks import (
    _ensure_perception,
    _require_marker_detector,
    _scene_frame,
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
    def __init__(self, apriltag=True):
        self._at = apriltag

    def apriltag_available(self):
        return self._at

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


def test_marker_detector_missing_raises():
    ctx = _Ctx(frame=_img(), age=0.1, perception=_Perc(apriltag=False))
    with pytest.raises(WorkflowError) as e:
        _require_marker_detector(ctx)
    assert 'Marker-Erkennung' in str(e.value)


def test_perception_uninitialised_raises():
    ctx = _Ctx(frame=_img(), age=0.1, perception=None)
    with pytest.raises(WorkflowError) as e:
        _ensure_perception(ctx)
    assert 'Wahrnehmung' in str(e.value)
