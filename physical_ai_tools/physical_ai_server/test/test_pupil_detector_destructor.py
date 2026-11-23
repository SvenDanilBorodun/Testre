#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The server suite frees pupil_apriltags detectors in the safe order (see test/conftest.py).

pupil_apriltags' own ``Detector.__del__`` destroys the tag families before the detector, a
use-after-free that crashed CI's python-tests (exit 139). The suite's conftest replaces the
destructor with the detector-first order; these tests prove it is in place and that building and
dropping many real detectors leaves the interpreter alive.
"""

import gc

from pupil_required import require_pupil

import conftest


def test_the_suite_frees_a_detector_detector_first():
    pupil = require_pupil()
    assert pupil.Detector.__del__ is conftest._free_detector_first


def test_many_detectors_are_built_and_freed_without_a_crash():
    pupil = require_pupil()
    for _ in range(64):
        det = pupil.Detector(families='tag36h11', nthreads=2, quad_decimate=1.0)
        assert det.tag_detector_ptr is not None
        det.__del__()                    # the explicit free, then the garbage collector's: idempotent
        assert det.tag_detector_ptr is None and det.tag_families == {}
        del det
    gc.collect()


def test_a_half_built_detector_is_freed_quietly():
    pupil = require_pupil()
    det = pupil.Detector.__new__(pupil.Detector)     # __init__ never ran: no pointer, no libc
    conftest._free_detector_first(det)
    del det
    gc.collect()
