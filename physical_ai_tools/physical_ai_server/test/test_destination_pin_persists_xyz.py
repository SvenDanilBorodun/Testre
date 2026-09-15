"""Audit §1.4 — destination_pin reads X/Y/Z from block fields and
fails loud when they are missing (sentinel '—').

The v1 handler defaulted x=0, y=0, z=ctx.z_table which silently
overwrote whatever world coords the click-to-pin flow had stashed in
``ctx.destinations``. The fix moved the coordinates into the block
itself (read-only label fields) and made the handler reject the
sentinel.
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.handlers.destinations import (
    UNPINNED_SENTINEL,
    destination_pin,
)
from physical_ai_server.workflow.handlers.motion import WorkflowError


class _StubCtx:
    def __init__(self, z_table: float | None = None):
        self.destinations: dict = {}
        self.z_table = z_table
        self.log = lambda msg: None


def test_pinned_xy_lands_in_destinations_and_z_follows_the_live_table():
    """X and Y come from the block; Z does NOT.

    The block's Z field was read off the table plane at click time and baked into
    the saved workflow, while „Tisch vermessen" re-draws that plane every lesson
    (EDUBOTICS_FORCE_RECALIBRATION ships 1) — so the stored 0.045 is a cached
    answer to a question the rig can answer freshly. Here the ctx's measured
    table sits at 0.050, and that is what the run descends to."""
    ctx = _StubCtx(z_table=0.05)
    destination_pin(ctx, {'name': 'A', 'x': '0.234', 'y': '-0.012', 'z': '0.045'})
    assert ctx.destinations['A'] == {
        'x': pytest.approx(0.234),
        'y': pytest.approx(-0.012),
        'z': pytest.approx(0.050),
        'label': 'A',
        'plane_tracked': True,
    }


def test_the_baked_z_survives_when_the_rig_has_no_table_height_at_all():
    """An uncalibrated rig must keep the only height anybody ever gave the pin.
    There is NO z_table = 0.0 fallback here — nothing is invented."""
    ctx = _StubCtx(z_table=None)
    destination_pin(ctx, {'name': 'A', 'x': '0.234', 'y': '-0.012', 'z': '0.045'})
    assert ctx.destinations['A']['z'] == pytest.approx(0.045)


def test_unpinned_sentinel_raises():
    ctx = _StubCtx(z_table=0.05)
    with pytest.raises(WorkflowError) as exc:
        destination_pin(ctx, {
            'name': 'A',
            'x': UNPINNED_SENTINEL,
            'y': UNPINNED_SENTINEL,
            'z': UNPINNED_SENTINEL,
        })
    assert 'gepinnt' in str(exc.value).lower() or 'pin' in str(exc.value).lower()


def test_missing_xyz_raises():
    ctx = _StubCtx(z_table=0.05)
    with pytest.raises(WorkflowError):
        destination_pin(ctx, {'name': 'A'})


def test_invalid_coordinate_raises():
    ctx = _StubCtx(z_table=0.05)
    with pytest.raises(WorkflowError) as exc:
        destination_pin(ctx, {'name': 'A', 'x': 'not-a-number', 'y': 0.0, 'z': 0.0})
    assert 'ungültig' in str(exc.value).lower() or 'invalid' in str(exc.value).lower()


def test_an_overflowing_coordinate_raises_the_german_error_not_overflow():
    ctx = _StubCtx(z_table=0.05)
    with pytest.raises(WorkflowError) as exc:
        destination_pin(ctx, {'name': 'A', 'x': 10 ** 400, 'y': 0.0, 'z': 0.0})
    assert str(exc.value) == 'Ziel "A" hat ungültige Koordinaten.'


def test_missing_name_raises():
    ctx = _StubCtx()
    with pytest.raises(WorkflowError):
        destination_pin(ctx, {'name': '', 'x': 0.1, 'y': 0.2, 'z': 0.3})
