"""AprilTag detector thread-safety (perception.py).

``pupil_apriltags.Detector.detect()`` wraps a C library that is NOT
thread-safe, but ``Perception`` is a single shared instance and the workflow
interpreter spawns one thread per hat block. Two concurrent AprilTag detect
calls (e.g. a ``when_object_seen`` hat racing the main stack) would race the
shared C detector → possible segfault of the whole ROS node.

These tests assert the per-detector lock serializes concurrent AprilTag
detection: a fake detector records whether two ``.detect()`` calls ever overlap.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from physical_ai_server.workflow.perception import Perception


class _OverlapTrackingDetector:
    """Stand-in for ``pupil_apriltags.Detector``. ``detect`` records the peak
    number of threads inside it at once and dwells briefly so an unlocked race
    would actually overlap."""

    def __init__(self) -> None:
        self._counter_lock = threading.Lock()
        self._inside = 0
        self.max_concurrent = 0
        self.calls = 0

    def detect(self, gray):  # noqa: D401 — mirrors pupil_apriltags API
        with self._counter_lock:
            self._inside += 1
            self.calls += 1
            self.max_concurrent = max(self.max_concurrent, self._inside)
        # Dwell so a missing lock would let a second thread enter concurrently.
        time.sleep(0.02)
        with self._counter_lock:
            self._inside -= 1
        return []  # no tags — the marshalling loop is a no-op


def _img():
    return np.zeros((16, 16, 3), dtype=np.uint8)


def test_apriltag_lock_present():
    p = Perception()
    assert hasattr(p, '_apriltag_lock')
    # A real lock exposes acquire/release.
    assert hasattr(p._apriltag_lock, 'acquire')
    assert hasattr(p._apriltag_lock, 'release')


def test_concurrent_detect_marker_is_serialized():
    """Many threads calling detect (apriltag mode) concurrently must NEVER be
    inside the C detector at the same time — the lock serializes them."""
    p = Perception()
    fake = _OverlapTrackingDetector()
    p._apriltag_detector = fake

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()  # release all threads at once → maximal contention
            p.detect(_img(), camera='scene', mode='apriltag', aruco_id=None)
        except BaseException as exc:  # noqa: BLE001 — surface to the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f'worker raised: {errors!r}'
    assert fake.calls == n_threads, 'every call should reach the detector'
    # The whole point: never two threads inside the non-thread-safe detector.
    assert fake.max_concurrent == 1, (
        f'detector was entered by {fake.max_concurrent} threads at once — '
        'the apriltag lock is not serializing concurrent detection'
    )


def test_lock_held_around_detect_call():
    """Direct proof the lock is held while .detect() runs: the detector checks
    that the Perception lock cannot be acquired from another thread mid-detect."""
    p = Perception()
    held_during_detect = {'value': None}

    class _LockProbeDetector:
        def detect(self, gray):
            # Try to acquire the SAME lock from "another" logical path; it must
            # already be held by _detect_apriltag, so a non-blocking acquire
            # fails.
            acquired = p._apriltag_lock.acquire(blocking=False)
            held_during_detect['value'] = not acquired
            if acquired:
                p._apriltag_lock.release()
            return []

    p._apriltag_detector = _LockProbeDetector()
    p.detect(_img(), camera='scene', mode='apriltag', aruco_id=None)
    assert held_during_detect['value'] is True, (
        'the apriltag lock was NOT held during .detect()'
    )


def test_unknown_mode_returns_empty():
    """A non-apriltag mode (the colour/COCO modes were removed) returns [] rather
    than raising."""
    p = Perception()
    assert p.detect(_img(), camera='scene', mode='color') == []
