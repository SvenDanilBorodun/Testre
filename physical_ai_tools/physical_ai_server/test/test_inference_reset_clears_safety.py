"""Regression test for the d408378 safety-envelope-extraction bug.

Pre-extraction, ``reset_policy`` cleared an InferenceManager-local
``_last_action`` attribute that the per-tick delta cap consulted. The
extraction moved the delta-cap memory onto a dedicated ``SafetyEnvelope``
instance but the in-class ``self._last_action = None`` line stayed in
``reset_policy`` — pointing at a now-dead attribute. The shared
``SafetyEnvelope`` instance was never reset, so the first action of a
new episode got clamped against the LAST action of the previous one.
With the operator typically having repositioned the arm between
episodes, that clamp showed up as a "frozen" arm walking across the
gap at the per-tick delta budget.

This test would have caught the bug if it had existed at extraction
time. It pins the contract that ``reset_policy`` delegates to
``self._safety.reset()``.
"""

from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def manager():
    pytest.importorskip('torch')
    pytest.importorskip('lerobot')
    from physical_ai_server.inference.inference_manager import InferenceManager
    # device='cpu' so the constructor doesn't require CUDA for a unit
    # test. ``load_policy`` is the path that would actually need a GPU.
    return InferenceManager(device='cpu')


def test_reset_policy_clears_safety_delta_cap(manager):
    """Without the fix, the second 0.9 call would be clamped to 0.5 +
    0.1 = 0.6 because the SafetyEnvelope still remembers 0.5 as the
    previous tick. After the fix, ``reset_policy`` clears that memory
    and 0.9 passes through."""

    manager.set_action_limits(
        joint_min=[-1.0] * 6,
        joint_max=[1.0] * 6,
        max_delta_per_tick=[0.1] * 6,
    )

    # Seed the envelope's _last_action to 0.5.
    seed = np.array([0.5, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    out = manager._apply_safety_envelope(seed)
    assert out is not None
    np.testing.assert_array_almost_equal(out, seed)

    manager.reset_policy()

    far = np.array([0.9, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    out = manager._apply_safety_envelope(far)
    assert out is not None
    np.testing.assert_array_almost_equal(out, far)


def test_reset_policy_clears_stale_camera_state(manager):
    """Sanity check that the extraction-era state-clearing for camera
    hashes still works — guard against a future refactor that removes
    too much."""
    manager._last_image_hashes['scene'] = 12345
    manager._last_image_change_time['scene'] = 100.0
    manager._last_stale_warn_time['scene'] = 100.0

    manager.reset_policy()

    assert manager._last_image_hashes == {}
    assert manager._last_image_change_time == {}
    assert manager._last_stale_warn_time == {}
