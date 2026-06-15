"""Tests for app.services.policy_profile — the per-policy GPU tier + VLA recipe.

This module is dependency-free by design (no fastapi/pydantic/supabase), so it
imports directly. The tests lock in the facts verified against LeRobot v0.5.1
config dataclasses, so a careless edit can't (a) route an ACT job to the
expensive A100, (b) drop a VLA base checkpoint (-> random-init OOM), or (c)
emit a flag a policy's config doesn't support (-> draccus rejects the job).
"""

from __future__ import annotations

import unittest

from app.services.policy_profile import (
    GPU_A100,
    GPU_L4,
    LANGUAGE_CONDITIONED_POLICIES,
    apply_training_tuning,
    get_policy_profile,
)


class TestPolicyProfile(unittest.TestCase):
    def test_act_class_is_l4_no_flags(self):
        for p in ("act", "diffusion", "vqbet", "tdmpc"):
            prof = get_policy_profile(p)
            self.assertEqual(prof["gpu_tier"], GPU_L4, p)
            self.assertEqual(prof["policy_flags"], [], p)

    def test_vlas_route_to_a100(self):
        for p in ("pi0", "pi05", "pi0_fast", "smolvla"):
            self.assertEqual(get_policy_profile(p)["gpu_tier"], GPU_A100, p)

    def test_every_vla_loads_a_base_checkpoint(self):
        # Without --policy.pretrained_path these VLAs init fully random and OOM.
        bases = {
            "pi0": "lerobot/pi0_base",
            "pi05": "lerobot/pi05_base",
            "pi0_fast": "lerobot/pi0fast-base",
            "smolvla": "lerobot/smolvla_base",
        }
        for p, base in bases.items():
            flags = get_policy_profile(p)["policy_flags"]
            self.assertIn(f"--policy.pretrained_path={base}", flags, p)

    def test_pi0_pi05_full_memory_recipe(self):
        for p in ("pi0", "pi05"):
            flags = get_policy_profile(p)["policy_flags"]
            self.assertIn("--policy.dtype=bfloat16", flags, p)
            self.assertIn("--policy.gradient_checkpointing=true", flags, p)
            self.assertIn("--policy.train_expert_only=true", flags, p)

    def test_pi0_fast_has_no_train_expert_only(self):
        # PI0FastConfig has dtype + gradient_checkpointing but NO
        # train_expert_only field — emitting it would make draccus reject.
        flags = get_policy_profile("pi0_fast")["policy_flags"]
        self.assertIn("--policy.dtype=bfloat16", flags)
        self.assertIn("--policy.gradient_checkpointing=true", flags)
        self.assertFalse(
            any(f.startswith("--policy.train_expert_only") for f in flags)
        )

    def test_pi0_fast_uses_short_autoregressive_horizon(self):
        # FAST autoregressive head: docs pair pi0_fast with chunk_size=10 /
        # n_action_steps=10 (vs config default 50) to keep token sequences short.
        flags = get_policy_profile("pi0_fast")["policy_flags"]
        self.assertIn("--policy.chunk_size=10", flags)
        self.assertIn("--policy.n_action_steps=10", flags)

    def test_smolvla_only_pretrained_path(self):
        # SmolVLAConfig has NEITHER dtype NOR gradient_checkpointing — emitting
        # them is rejected by draccus. Base checkpoint only (its config already
        # defaults to freeze_vision_encoder + train_expert_only).
        flags = get_policy_profile("smolvla")["policy_flags"]
        self.assertEqual(flags, ["--policy.pretrained_path=lerobot/smolvla_base"])

    def test_all_flags_are_policy_namespaced(self):
        for p in ("pi0", "pi05", "pi0_fast", "smolvla"):
            for f in get_policy_profile(p)["policy_flags"]:
                self.assertTrue(f.startswith("--policy."), f)

    def test_unknown_policy_falls_back_to_l4_no_flags(self):
        prof = get_policy_profile("totally-new-policy")
        self.assertEqual(prof["gpu_tier"], GPU_L4)
        self.assertEqual(prof["policy_flags"], [])

    def test_case_insensitive(self):
        self.assertEqual(get_policy_profile("PI05")["gpu_tier"], GPU_A100)
        self.assertEqual(get_policy_profile("Act")["gpu_tier"], GPU_L4)

    def test_language_conditioned_set(self):
        self.assertEqual(
            LANGUAGE_CONDITIONED_POLICIES,
            frozenset({"pi0", "pi05", "pi0_fast", "smolvla"}),
        )

    def test_returned_flags_are_a_copy_not_shared_mutable(self):
        # get_policy_profile hands out the table's list; mutating a caller's copy
        # must not poison the table. (Route code does list(...) before injecting,
        # but assert the table entry stays intact regardless.)
        flags = get_policy_profile("pi05")["policy_flags"]
        flags.append("--policy.evil=1")
        self.assertNotIn("--policy.evil=1", get_policy_profile("pi05")["policy_flags"])


class TestApplyTrainingTuning(unittest.TestCase):
    def _tuned(self, model_type, params):
        p = dict(params)
        apply_training_tuning(get_policy_profile(model_type), p)
        return p

    def test_act_is_noop(self):
        # ACT-class has no tuning fields → steps/batch untouched.
        out = self._tuned("act", {"steps": 50000, "batch_size": 8})
        self.assertEqual(out["steps"], 50000)
        self.assertEqual(out["batch_size"], 8)

    def test_pi05_caps_steps_and_raises_batch(self):
        out = self._tuned("pi05", {"steps": 50000, "batch_size": 8})
        self.assertEqual(out["steps"], 10000)      # capped
        self.assertEqual(out["batch_size"], 16)    # raised into [16,24]

    def test_pi0_steps_below_cap_unchanged(self):
        # A cap never RAISES steps.
        out = self._tuned("pi0", {"steps": 3000, "batch_size": 20})
        self.assertEqual(out["steps"], 3000)
        self.assertEqual(out["batch_size"], 20)    # already in range

    def test_smolvla_raises_small_batch(self):
        out = self._tuned("smolvla", {"steps": 50000, "batch_size": 8})
        self.assertEqual(out["steps"], 20000)
        self.assertEqual(out["batch_size"], 32)    # raised into [32,64]

    def test_pi0_fast_caps_batch_no_steps_cap(self):
        # Full 2.9B FT: batch clamped to <=8; no steps_cap (timeout-bound).
        out = self._tuned("pi0_fast", {"steps": 50000, "batch_size": 64})
        self.assertEqual(out["steps"], 50000)      # not capped
        self.assertEqual(out["batch_size"], 8)     # clamped to max 8

    def test_batch_clamped_to_max(self):
        out = self._tuned("pi0", {"steps": 5000, "batch_size": 256})
        self.assertEqual(out["batch_size"], 24)    # clamped to [16,24]

    def test_missing_batch_falls_back_to_min(self):
        out = self._tuned("smolvla", {"steps": 5000})
        self.assertEqual(out["batch_size"], 32)    # batch_min


if __name__ == "__main__":
    unittest.main()
