#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The one gate of every test that needs the REAL AprilTag detector.

Not a test module. A test calls ``require_pupil()`` first; it answers the
``pupil_apriltags`` module. Without that module the test is SKIPPED, unless
``EDUBOTICS_REQUIRE_PUPIL=1`` turns the skip into a failure (the
``EDUBOTICS_REQUIRE_JAVAC`` rule): CI sets it, so a lost detector fails the
run instead of passing it silently. A module that is present but does not
load counts as missing.
"""

from __future__ import annotations

import os

import pytest

REQUIRE_ENV = 'EDUBOTICS_REQUIRE_PUPIL'


def require_pupil():
    """The ``pupil_apriltags`` module, or a skip (a failure under
    ``EDUBOTICS_REQUIRE_PUPIL=1``) when it cannot be imported."""
    try:
        import pupil_apriltags
    except Exception as exc:  # noqa: BLE001 — a broken build is as missing as an absent one
        reason = f'pupil_apriltags cannot be imported ({type(exc).__name__}: {exc})'
        if os.environ.get(REQUIRE_ENV) == '1':
            pytest.fail(f'{REQUIRE_ENV}=1 but {reason}')
        pytest.skip(f'{reason}; set {REQUIRE_ENV}=1 to fail instead')
    return pupil_apriltags
