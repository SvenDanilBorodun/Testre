#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""pytest setup of the server suite: a safe destructor for pupil_apriltags' Detector, in tests only.

pupil_apriltags (every release up to 1.0.4.post11, the newest) frees a Detector in the wrong
order: ``Detector.__del__`` destroys the tag families BEFORE ``apriltag_detector_destroy``, which
then reads the freed families (``apriltag_detector_clear_families`` -> ``quick_decode_uninit``).
That use-after-free crashes the interpreter at random (upstream issues #34, #46, #52; the fix PRs
#65 and #76 are not merged). CI's python-tests job hit it (exit 139, ``bindings.py`` line 398 in
``__del__``) once the real-detector tests ran there.

The node never frees its detector while it runs: it builds one Perception for its lifetime
(``physical_ai_server.py::_build_perception``), and the calibration service uses that one. The
tests, however, build and drop many. Here, and only here, a Detector is freed in the order the
AprilTag C examples use and the fix PRs propose: the detector first, then its families, each once.
"""

from __future__ import annotations


def _free_detector_first(self) -> None:
    """``Detector.__del__`` in the safe order; idempotent, never raises."""
    ptr = getattr(self, 'tag_detector_ptr', None)
    families = getattr(self, 'tag_families', None) or {}
    libc = getattr(self, 'libc', None)
    if ptr is None or libc is None:
        return
    self.tag_detector_ptr = None
    self.tag_families = {}
    try:
        libc.apriltag_detector_destroy.restype = None
        libc.apriltag_detector_destroy(ptr)
        for family, tf in families.items():
            destroy = getattr(libc, f'{family}_destroy')
            destroy.restype = None
            destroy(tf)
    except Exception:  # noqa: BLE001 — a destructor must not raise
        pass


def _install_safe_detector_destructor() -> None:
    try:
        import pupil_apriltags
    except Exception:  # noqa: BLE001 — absent or broken: nothing to patch (pupil_required decides)
        return
    pupil_apriltags.Detector.__del__ = _free_detector_first


_install_safe_detector_destructor()
