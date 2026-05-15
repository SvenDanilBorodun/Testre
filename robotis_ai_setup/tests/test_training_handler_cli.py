"""Unit tests for modal_training.training_handler._build_training_command.

Locks in the inference-quality defaults landed by audit F63 (image
transforms enabled at train time) and F64 (ACT n_action_steps=15 unless
overridden), plus the F66 hardening (override gated on model_type=='act'
and on positive-int validity, with fallback to the F64 default on bad
inputs). Heavy imports (huggingface_hub, supabase) are stubbed so the
test runs in the same environment as the existing GUI tests — no Modal,
no Supabase, no LeRobot.
"""

from __future__ import annotations

import os
import sys
import types
import unittest


# ------------------------------------------------------------------
# Stub heavy module-level imports of training_handler so the function
# under test (_build_training_command) is reachable without installing
# huggingface_hub or supabase.
# ------------------------------------------------------------------
def _ensure_stubs() -> None:
    if "huggingface_hub" not in sys.modules:
        m = types.ModuleType("huggingface_hub")

        class _HfApiStub:
            def __init__(self, *a, **kw):
                pass

        m.HfApi = _HfApiStub
        m.hf_hub_download = lambda *a, **kw: None
        m.login = lambda *a, **kw: None
        utils = types.ModuleType("huggingface_hub.utils")

        class _HfHubHTTPError(Exception):
            pass

        class _RepositoryNotFoundError(Exception):
            pass

        utils.HfHubHTTPError = _HfHubHTTPError
        utils.RepositoryNotFoundError = _RepositoryNotFoundError
        sys.modules["huggingface_hub"] = m
        sys.modules["huggingface_hub.utils"] = utils

    if "supabase" not in sys.modules:
        m = types.ModuleType("supabase")
        m.create_client = lambda *a, **kw: None
        sys.modules["supabase"] = m


_ensure_stubs()


# Add modal_training/ to path so `import training_handler` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODAL_DIR = os.path.abspath(os.path.join(_HERE, "..", "modal_training"))
if _MODAL_DIR not in sys.path:
    sys.path.insert(0, _MODAL_DIR)

import training_handler  # noqa: E402


class TestBuildTrainingCommand(unittest.TestCase):
    """Regression suite for the CLI defaults that ship to the Modal worker."""

    def test_image_transforms_always_on(self):
        """Audit F63: every training call enables dataset image transforms,
        regardless of policy type. Without this, ACT/SmolVLA/Diffusion all
        train without brightness/contrast/saturation/hue jitter and
        struggle to generalise across classroom lighting changes.

        Iterates the full 7-policy `ALLOWED_POLICIES` set from CLAUDE.md
        §7.3 so a new policy keyword can't silently bypass F63."""
        for policy in (
            "act",
            "diffusion",
            "vqbet",
            "tdmpc",
            "pi0",
            "pi0fast",
            "smolvla",
        ):
            cmd = training_handler._build_training_command(
                dataset_name="user/data",
                model_type=policy,
                model_name="user/model",
                training_params={},
            )
            self.assertEqual(
                cmd.count("--dataset.image_transforms.enable=true"),
                1,
                f"image transforms should be enabled exactly once for policy={policy}",
            )

    def test_act_default_n_action_steps_15(self):
        """Audit F64: ACT defaults n_action_steps=15 so the policy re-queries
        the world every 0.5 s instead of committing to a 3.3 s open-loop
        chunk. Biggest inference-smoothness lever; LeRobot default is 100."""
        cmd = training_handler._build_training_command(
            dataset_name="user/data",
            model_type="act",
            model_name="user/model",
            training_params={},
        )
        self.assertIn("--policy.n_action_steps=15", cmd)

    def test_non_act_policies_skip_n_action_steps_default(self):
        """F64 must not leak into Diffusion / VQBet / SmolVLA — each has its
        own chunk semantics and their respective config classes use
        different field names or interpretations.

        Iterates the full non-ACT subset of `ALLOWED_POLICIES`."""
        for policy in ("diffusion", "vqbet", "tdmpc", "pi0", "pi0fast", "smolvla"):
            cmd = training_handler._build_training_command(
                dataset_name="user/data",
                model_type=policy,
                model_name="user/model",
                training_params={},
            )
            for arg in cmd:
                self.assertFalse(
                    arg.startswith("--policy.n_action_steps="),
                    f"policy={policy} unexpectedly received {arg}",
                )

    def test_non_act_with_n_action_steps_in_params_still_drops_it(self):
        """Audit F66: a diffusion / pi0 / vqbet job that happens to carry
        `n_action_steps` in training_params must still NOT receive
        `--policy.n_action_steps=` — that field is ACT-specific. The
        F64 verifier flagged this as a cross-policy leak."""
        for policy in ("diffusion", "vqbet", "tdmpc", "pi0", "pi0fast", "smolvla"):
            cmd = training_handler._build_training_command(
                dataset_name="user/data",
                model_type=policy,
                model_name="user/model",
                training_params={"n_action_steps": 30},
            )
            for arg in cmd:
                self.assertFalse(
                    arg.startswith("--policy.n_action_steps="),
                    f"policy={policy} unexpectedly received {arg} via F66 leak",
                )

    def test_explicit_n_action_steps_override_is_forwarded(self):
        """When training_params explicitly carries n_action_steps, that value
        must reach the CLI as `--policy.n_action_steps=X` — and the F64
        default must NOT also be appended (would shadow the override)."""
        cmd = training_handler._build_training_command(
            dataset_name="user/data",
            model_type="act",
            model_name="user/model",
            training_params={"n_action_steps": 30},
        )
        self.assertIn("--policy.n_action_steps=30", cmd)
        self.assertNotIn("--policy.n_action_steps=15", cmd)
        self.assertEqual(
            sum(1 for a in cmd if a.startswith("--policy.n_action_steps=")),
            1,
            "exactly one --policy.n_action_steps= arg must be emitted",
        )

    def test_invalid_n_action_steps_override_falls_back_to_default(self):
        """Audit F66: None / 0 / negative / non-int overrides on ACT must
        fall back to the F64 default (=15) rather than emit a broken CLI
        arg. None would otherwise produce the literal string
        `--policy.n_action_steps=None` and break draccus parsing; 0 or
        negative would crash inside ACTConfig.__post_init__ via the
        `n_action_steps > chunk_size` / deque(maxlen<0) validators."""
        invalid_values = [None, 0, -1, -100, "15", "fifteen", 1.5, []]
        for bad in invalid_values:
            cmd = training_handler._build_training_command(
                dataset_name="user/data",
                model_type="act",
                model_name="user/model",
                training_params={"n_action_steps": bad},
            )
            self.assertIn(
                "--policy.n_action_steps=15",
                cmd,
                f"invalid override {bad!r} should fall back to F64 default",
            )
            # No literal "None" / negative / string sneaking through.
            for arg in cmd:
                self.assertFalse(
                    arg.startswith("--policy.n_action_steps=")
                    and arg != "--policy.n_action_steps=15",
                    f"invalid override {bad!r} produced bad arg {arg}",
                )

    def test_existing_basic_args_still_present(self):
        """Belt-and-suspenders: confirm the audit edits didn't drop or
        reorder the pre-existing CLI args that the Modal job relies on."""
        cmd = training_handler._build_training_command(
            dataset_name="user/data",
            model_type="act",
            model_name="user/model",
            training_params={"batch_size": 16, "steps": 50000},
        )
        self.assertIn("--policy.type=act", cmd)
        self.assertIn("--policy.device=cuda", cmd)
        self.assertIn("--dataset.repo_id=user/data", cmd)
        self.assertIn("--policy.push_to_hub=false", cmd)
        self.assertIn("--eval_freq=0", cmd)
        self.assertIn("--batch_size=16", cmd)
        self.assertIn("--steps=50000", cmd)


if __name__ == "__main__":
    unittest.main()
