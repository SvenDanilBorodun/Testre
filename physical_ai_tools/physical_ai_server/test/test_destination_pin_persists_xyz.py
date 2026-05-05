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


def test_pinned_xyz_lands_in_destinations():
    ctx = _StubCtx(z_table=0.05)
    destination_pin(ctx, {'name': 'A', 'x': '0.234', 'y': '-0.012', 'z': '0.045'})
    assert ctx.destinations['A'] == {
        'x': pytest.approx(0.234),
        'y': pytest.approx(-0.012),
        'z': pytest.approx(0.045),
        'label': 'A',
    }


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


def test_missing_name_raises():
    ctx = _StubCtx()
    with pytest.raises(WorkflowError):
        destination_pin(ctx, {'name': '', 'x': 0.1, 'y': 0.2, 'z': 0.3})
