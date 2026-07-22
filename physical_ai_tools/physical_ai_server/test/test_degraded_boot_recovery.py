"""Finding 1 regression: the degraded-boot re-init retry must recover the
ENV-RESOLVED profile, not the omx_full default the fallback stamps.

At merge-base every ``_init_robot_profile`` invocation re-ran ``resolve(env)``
and recovered correctly. The Fix-5 hoist moved ``resolve()`` into ``__init__``
and had ``_init_robot_profile`` REUSE the stash; the degraded-boot fallback then
overwrote that stash (``self._arm_profile``) with the omx_full default, so a
bounded retry after a TRANSIENT boot failure resurrected the WRONG profile on an
edu6 / omx_follower rig (the retry re-read the clobbered ``_arm_profile``). The
fix keeps the env-resolution in a SEPARATE ``self._resolved_profile_stash`` the
fallback never touches; the retry reads that.

Deps-free: reuses the ast-extraction harness from ``test_boot_init`` (the real
production methods execed onto a stub node — ``physical_ai_server.py`` imports
rclpy and cannot be imported in CI). ``test_boot_init``'s own retry test only
exercises env-ABSENT (default == correct), so it cannot see this bug; this file
drives the NON-default rigs where default != correct.
"""

from __future__ import annotations

import pytest

from physical_ai_server import robot_profiles
from test_boot_init import _FakeComm, _Node, _success


@pytest.mark.parametrize('env_id', ['edu6_studio', 'omx_follower'])
def test_reinit_retry_recovers_env_profile_not_default(monkeypatch, env_id):
    """A transient boot failure on a NON-default rig, then recovery: the retry
    must heal to the env-resolved profile (edu6_studio / omx_follower), never
    the omx_full default the fallback stamps for degraded reporting."""
    monkeypatch.setenv('EDUBOTICS_ROBOT_TYPE', env_id)
    resolved = robot_profiles.resolve(env_id)
    assert resolved.profile_id == env_id       # sanity: a real, non-default id

    # init_ros_params raises on the FIRST attempt (transient), succeeds on the
    # retry — the exact "self-heals a transient boot failure" case.
    calls = {'n': 0}

    def _raise_then_succeed(node, robot_type):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('transient boot failure')
        _success(node, robot_type)

    node = _Node(_raise_then_succeed)
    # Mirror the __init__ hoist: both the stash and _arm_profile start as the
    # env-resolved profile.
    node._resolved_profile_stash = resolved
    node._arm_profile = resolved

    node._init_robot_profile()                 # first attempt fails → degraded
    assert node.communicator is None
    # The fallback downgraded _arm_profile to the default FOR REPORTING …
    assert node._arm_profile.profile_id == 'omx_full'
    # … but the env-resolution stash is UNTOUCHED (this is the fix).
    assert node._resolved_profile_stash.profile_id == env_id
    assert node._reinit_timer is not None      # bounded retry armed

    node._reinit_retry_cb()                     # the retry fires

    # Healed to the CORRECT env profile — not the clobbered omx_full default.
    assert isinstance(node.communicator, _FakeComm)
    assert node._arm_profile.profile_id == env_id
    assert node.robot_profile == env_id
    assert node.robot_type == resolved.data_robot_type
    assert node.capabilities_json == robot_profiles.capabilities_json(resolved)
    assert node._reinit_timer is None           # retry destroyed itself


def test_reinit_stash_survives_repeated_transient_failures(monkeypatch):
    """Even across MULTIPLE failed attempts the stash is never clobbered, so the
    eventual recovery still lands on the env profile (each failed retry re-runs
    the fallback that only touches _arm_profile)."""
    monkeypatch.setenv('EDUBOTICS_ROBOT_TYPE', 'edu6_studio')
    resolved = robot_profiles.resolve('edu6_studio')

    calls = {'n': 0}

    def _raise_twice_then_succeed(node, robot_type):
        calls['n'] += 1
        if calls['n'] <= 2:
            raise RuntimeError('still booting')
        _success(node, robot_type)

    node = _Node(_raise_twice_then_succeed)
    node._resolved_profile_stash = resolved
    node._arm_profile = resolved

    node._init_robot_profile()                 # attempt 1 fails
    node._reinit_retry_cb()                     # attempt 2 fails
    assert node.communicator is None
    assert node._resolved_profile_stash.profile_id == 'edu6_studio'
    node._reinit_retry_cb()                     # attempt 3 succeeds

    assert isinstance(node.communicator, _FakeComm)
    assert node._arm_profile.profile_id == 'edu6_studio'
    assert node.robot_type == 'edu6_studio'
