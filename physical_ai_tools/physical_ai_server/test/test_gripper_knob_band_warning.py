#!/usr/bin/env python3
"""The two operator gripper knobs WARN when they sit outside the arm's band —
and are then used anyway.

`docs/KNOWN-ISSUES.md`, „Roboter Studio, 2026-09-09 block-audit round", third
sub-bullet. Both knobs reach a physical decision and both were guarded by
``math.isfinite`` alone:

* ``EDUBOTICS_PICKUP_CLOSE_RAD=5.0`` on an edu6 (band 0.00..1.75) reaches the
  published trajectory's gripper column; ``=-9.0`` likewise.
* ``_held_threshold_rad``'s first branch returns the RAW module constant −0.35
  when the env is non-empty, and on a Feetech rig that sits BELOW the whole
  band — so „Greifer hält etwas?" answers TRUE for every readable angle.

CLAMPING IS THE WRONG ANSWER and these tests pin that too:
``EDUBOTICS_GRASP_HELD_MAX_RAD`` EXISTS to restore an out-of-band constant as
the documented one-variable rollback, so clamping would silently disable the
very rollback it is, and refusing it would too.
"""

from __future__ import annotations

import types

import pytest

from physical_ai_server.workflow.handlers import motion as M


def _ctx(closed: float, opened: float, warned: set | None = None):
    """A ctx carrying only the gripper geometry the helpers read."""
    lines: list[str] = []
    ctx = types.SimpleNamespace(
        gripper_closed_rad=closed,
        gripper_open_rad=opened,
        gripper_knob_warned=set() if warned is None else warned,
        log=lines.append,
        ik=None,
        pickup_close_rad=None,
    )
    ctx.lines = lines
    return ctx


# Both Feetech bands, and the OMX, as LITERALS (robot_profiles owns the values;
# these are the numbers a rig actually runs).
_EDU6 = (0.00, 1.75)
_EDU1 = (0.00, 0.90)
_OMX = (-0.50, 0.80)


# ── EDUBOTICS_PICKUP_CLOSE_RAD ─────────────────────────────────────────────

@pytest.mark.parametrize('band', [_EDU6, _EDU1, _OMX])
@pytest.mark.parametrize('value', [5.0, -9.0])
def test_an_out_of_band_pickup_close_is_used_and_reported(
        monkeypatch, band, value):
    monkeypatch.setenv('EDUBOTICS_PICKUP_CLOSE_RAD', str(value))
    monkeypatch.setattr(M, 'PICKUP_CLOSE_RAD', value)
    ctx = _ctx(*band)
    assert M._pickup_close(ctx) == value, 'the value must NOT be clamped'
    assert len(ctx.lines) == 1, ctx.lines
    line = ctx.lines[0]
    assert line.startswith('[WARNUNG] ')
    assert 'EDUBOTICS_PICKUP_CLOSE_RAD' in line
    assert f'{value:.2f}' in line
    assert f'{band[0]:.2f}' in line and f'{band[1]:.2f}' in line
    assert 'trotzdem benutzt' in line
    assert 'läuft normal weiter' in line


@pytest.mark.parametrize('value', [0.0, 1.0, 1.75])
def test_an_in_band_pickup_close_says_nothing(monkeypatch, value):
    monkeypatch.setenv('EDUBOTICS_PICKUP_CLOSE_RAD', str(value))
    monkeypatch.setattr(M, 'PICKUP_CLOSE_RAD', value)
    ctx = _ctx(*_EDU6)
    assert M._pickup_close(ctx) == value
    assert ctx.lines == []


def test_the_unset_knob_is_silent_and_still_falls_through(monkeypatch):
    """The knob ships UNSET on every profile; compose forwards it as `${…:-}`."""
    monkeypatch.setenv('EDUBOTICS_PICKUP_CLOSE_RAD', '')
    monkeypatch.setattr(M, 'PICKUP_CLOSE_RAD', float('nan'))
    ctx = _ctx(*_EDU6)
    assert M._pickup_close(ctx) == 0.0     # the profile's gripper_closed_rad
    assert ctx.lines == []


# ── EDUBOTICS_GRASP_HELD_MAX_RAD ───────────────────────────────────────────

@pytest.mark.parametrize('band', [_EDU6, _EDU1])
def test_the_shipped_held_default_is_out_of_band_on_a_feetech_rig(
        monkeypatch, band):
    """−0.35 is an OMX-band number. Setting the rollback on a Feetech arm makes
    „Greifer hält etwas?" constant TRUE, and the operator is now told so."""
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '-0.35')
    monkeypatch.setattr(M, 'GRASP_HELD_MAX_RAD', -0.35)
    ctx = _ctx(*band)
    assert M._held_threshold_rad(ctx) == -0.35, 'the rollback must still win'
    assert len(ctx.lines) == 1
    line = ctx.lines[0]
    assert line.startswith('[WARNUNG] ')
    assert 'EDUBOTICS_GRASP_HELD_MAX_RAD' in line
    assert '-0.35' in line
    assert 'Greifer hält etwas?' in line
    assert 'Fehlgriff' in line
    assert 'trotzdem benutzt' in line


def test_the_same_rollback_on_an_omx_rig_is_in_band_and_silent(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '-0.35')
    monkeypatch.setattr(M, 'GRASP_HELD_MAX_RAD', -0.35)
    ctx = _ctx(*_OMX)
    assert M._held_threshold_rad(ctx) == -0.35
    assert ctx.lines == []


def test_an_unset_held_knob_never_warns_and_keeps_the_per_object_path(
        monkeypatch):
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '')
    ctx = _ctx(*_EDU6)
    ctx.last_commanded_close_rad = 1.0
    ctx.grasp_held_margin_rad = 0.12
    assert M._held_threshold_rad(ctx) == pytest.approx(1.12)
    assert ctx.lines == []


# ── the shape of the warning itself ────────────────────────────────────────

def test_a_knob_is_reported_ONCE_PER_RUN_not_once_per_grasp(monkeypatch):
    """A „Solange sichtbar" loop asks on every pass; forty identical lines read
    as forty faults."""
    monkeypatch.setenv('EDUBOTICS_PICKUP_CLOSE_RAD', '5.0')
    monkeypatch.setattr(M, 'PICKUP_CLOSE_RAD', 5.0)
    ctx = _ctx(*_EDU6)
    for _ in range(40):
        M._pickup_close(ctx)
    assert len(ctx.lines) == 1


def test_the_two_knobs_are_reported_independently(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_PICKUP_CLOSE_RAD', '5.0')
    monkeypatch.setattr(M, 'PICKUP_CLOSE_RAD', 5.0)
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '-0.35')
    monkeypatch.setattr(M, 'GRASP_HELD_MAX_RAD', -0.35)
    ctx = _ctx(*_EDU6)
    M._pickup_close(ctx)
    M._held_threshold_rad(ctx)
    assert len(ctx.lines) == 2
    assert 'EDUBOTICS_PICKUP_CLOSE_RAD' in ctx.lines[0]
    assert 'EDUBOTICS_GRASP_HELD_MAX_RAD' in ctx.lines[1]


def test_a_ctx_with_no_warn_set_still_works_and_still_warns():
    """A test double or an older ctx carries no `gripper_knob_warned`. The knob
    must still be reported (and the value still used) rather than crash."""
    ctx = types.SimpleNamespace(gripper_closed_rad=0.0, gripper_open_rad=1.75,
                                log=[].append)
    lines: list[str] = []
    ctx.log = lines.append
    M._warn_gripper_knob_out_of_band(ctx, 'X', 5.0, 'Test')
    assert len(lines) == 1


def test_a_ctx_with_no_log_is_silently_tolerated():
    ctx = types.SimpleNamespace(gripper_closed_rad=0.0, gripper_open_rad=1.75)
    M._warn_gripper_knob_out_of_band(ctx, 'X', 5.0, 'Test')   # must not raise


def test_a_non_finite_value_is_not_reported_as_out_of_band():
    """NaN/inf is a different fault with a different (existing) handler; a band
    sentence about it would name a comparison that was never made."""
    lines: list[str] = []
    ctx = _ctx(*_EDU6)
    ctx.log = lines.append
    for bad in (float('nan'), float('inf'), float('-inf')):
        M._warn_gripper_knob_out_of_band(ctx, 'X', bad, 'Test')
    assert lines == []


def test_the_band_is_orientation_agnostic():
    """The band is min/max of the two profile values, never
    `closed <= v <= open`.

    No SHIPPED profile closes at a numerically higher angle than it opens
    (OMX −0.50/0.80, edu6 0.00/1.75, edu1 0.00/0.90), so a `closed <= v <= open`
    form is indistinguishable on today's fleet — which is exactly why the
    reversed band is asserted explicitly here rather than left to a future arm
    to discover. A one-line helper that silently inverts on the fourth profile
    is the shape this suite keeps finding."""
    ctx = _ctx(-0.50, 0.80)
    M._warn_gripper_knob_out_of_band(ctx, 'X', -0.40, 'Test')
    assert ctx.lines == []
    M._warn_gripper_knob_out_of_band(ctx, 'Y', -0.60, 'Test')
    assert len(ctx.lines) == 1
    assert '-0.50 bis 0.80' in ctx.lines[0]

    # A hypothetical arm whose CLOSED angle is the numerically larger one.
    rev = _ctx(1.75, 0.00)
    M._warn_gripper_knob_out_of_band(rev, 'X', 1.00, 'Test')
    assert rev.lines == [], 'an in-band value on a reversed band must be silent'
    M._warn_gripper_knob_out_of_band(rev, 'Y', 2.00, 'Test')
    assert len(rev.lines) == 1
    assert '0.00 bis 1.75' in rev.lines[0]


def test_the_once_per_run_state_is_a_REAL_WorkflowContext_field(monkeypatch):
    """Not a `setattr` on the fly, and not a dead `getattr`.

    `ArmProfile.grasp_held_max_rad` was resolved from `ctx.grasp_held_max_rad`
    on a `WorkflowContext` that has no such field, so the branch never executed
    and the two profile lines were decorative. This test drives the REAL
    dataclass, so the dedupe can only pass if the field is genuinely declared."""
    from physical_ai_server.workflow.workflow_manager import WorkflowContext

    lines: list[str] = []
    ctx = WorkflowContext(publisher=lambda _p: None,
                          gripper_closed_rad=0.0, gripper_open_rad=1.75,
                          log=lines.append)
    monkeypatch.setenv('EDUBOTICS_PICKUP_CLOSE_RAD', '5.0')
    monkeypatch.setattr(M, 'PICKUP_CLOSE_RAD', 5.0)
    for _ in range(5):
        assert M._pickup_close(ctx) == 5.0
    assert len(lines) == 1, lines
    assert ctx.gripper_knob_warned == {'EDUBOTICS_PICKUP_CLOSE_RAD'}


# ── the deleted ArmProfile.grasp_held_max_rad ──────────────────────────────
# `docs/KNOWN-ISSUES.md`, second sub-bullet of the same round. The field (0.12
# edu6, 0.10 edu1) had ZERO production readers: `_grasp_held_max` resolved it
# from `ctx.grasp_held_max_rad` and `WorkflowContext` has no such field, so
# every run already fell through to the derivation — which yields the IDENTICAL
# number on all four profiles. It is gone; the derivation stays, pinned here
# with literals so a silent drift is a failure and not a rename.

def test_the_derived_fallback_is_the_number_each_arm_actually_used():
    """Measured before AND after the deletion: identical on all four."""
    import types as _t
    from physical_ai_server import robot_profiles as rp

    expected = {
        'omx_full': -0.35,
        'omx_follower': -0.35,
        'edu6_studio': 0.12,
        'edu1_studio': 0.10,
    }
    assert set(rp.ROBOT_PROFILES) == set(expected), (
        'a profile was added or removed — give it a row here')
    for pid, want in expected.items():
        prof = rp.ROBOT_PROFILES[pid]
        ctx = _t.SimpleNamespace(
            gripper_closed_rad=prof.gripper_closed_rad,
            gripper_open_rad=prof.gripper_open_rad,
            grasp_held_margin_rad=prof.grasp_held_margin_rad)
        assert M._grasp_held_max(ctx) == pytest.approx(want, abs=1e-12), pid


def test_the_decorative_profile_field_is_gone_and_stays_gone():
    """A field nothing reads is not a seam, it is a claim. Re-adding it needs a
    matching `WorkflowContext` field in the same change — see the docstring of
    `motion._grasp_held_max`."""
    from physical_ai_server import robot_profiles as rp

    for pid, prof in rp.ROBOT_PROFILES.items():
        assert not hasattr(prof, 'grasp_held_max_rad'), pid


def test_a_ctx_that_still_carries_the_old_attribute_is_ignored_not_obeyed():
    """If the field ever comes back on a ctx WITHOUT the resolution rung, it
    must not look like it works. The derivation wins."""
    import types as _t
    ctx = _t.SimpleNamespace(gripper_closed_rad=0.0, gripper_open_rad=1.75,
                             grasp_held_margin_rad=0.12,
                             grasp_held_max_rad=99.0)
    assert M._grasp_held_max(ctx) == pytest.approx(0.12)
