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

        class _RevisionNotFoundError(Exception):
            pass

        utils.HfHubHTTPError = _HfHubHTTPError
        utils.RepositoryNotFoundError = _RepositoryNotFoundError
        utils.RevisionNotFoundError = _RevisionNotFoundError
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

    def test_image_transforms_disabled(self):
        """Image augmentation is deliberately DISABLED (C1, 2026-06-15): the
        worker must NOT emit --dataset.image_transforms.enable at all. In
        LeRobot v0.5.1 the default transform pool includes a geometric
        RandomAffine (±5°/5% translate) that warps fixed-camera scene geometry,
        and there's no per-key CLI override to keep only the photometric subset.
        So the flag is gone for EVERY policy (no enable=true, no enable=false).

        Iterates the full 8-policy `ALLOWED_POLICIES` set so a new policy keyword
        can't silently re-introduce augmentation."""
        for policy in (
            "act",
            "diffusion",
            "vqbet",
            "tdmpc",
            "pi0",
            "pi0_fast",
            "pi05",
            "smolvla",
        ):
            cmd = training_handler._build_training_command(
                dataset_name="user/data",
                model_type=policy,
                model_name="user/model",
                training_params={},
            )
            for arg in cmd:
                self.assertFalse(
                    arg.startswith("--dataset.image_transforms"),
                    f"image transforms must not be configured for policy={policy}: {arg}",
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

        Iterates the full non-ACT subset of `ALLOWED_POLICIES` (8-policy set)."""
        for policy in ("diffusion", "vqbet", "tdmpc", "pi0", "pi0_fast", "pi05", "smolvla"):
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
        for policy in ("diffusion", "vqbet", "tdmpc", "pi0", "pi0_fast", "pi05", "smolvla"):
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


class TestPolicyCliFlagsPassthrough(unittest.TestCase):
    """VLA fine-tune recipe (V1, 2026-06-15): the Cloud API injects per-policy
    base-checkpoint + precision flags as fully-formed --policy.* strings via
    training_params['policy_cli_flags']; the worker appends them verbatim but
    ONLY accepts --policy.* (so a malformed payload can't inject arbitrary CLI
    args). The per-policy *content* of the recipe is owned + tested in the
    Cloud API (app.services.policy_profile); here we test the passthrough."""

    def _cmd(self, training_params):
        return training_handler._build_training_command(
            dataset_name="user/data",
            model_type="pi05",
            model_name="user/model",
            training_params=training_params,
        )

    def test_policy_cli_flags_are_appended(self):
        flags = [
            "--policy.pretrained_path=lerobot/pi05_base",
            "--policy.dtype=bfloat16",
            "--policy.gradient_checkpointing=true",
            "--policy.train_expert_only=true",
        ]
        cmd = self._cmd({"policy_cli_flags": flags})
        for f in flags:
            self.assertIn(f, cmd)

    def test_non_policy_flags_are_filtered(self):
        # Defence: only --policy.* strings pass through; anything else (an
        # accidental/forged --output_dir, a bare token, a non-string) is dropped.
        cmd = self._cmd({"policy_cli_flags": [
            "--policy.dtype=bfloat16",
            "--output_dir=/etc/evil",
            "rm -rf /",
            42,
            None,
        ]})
        self.assertIn("--policy.dtype=bfloat16", cmd)
        self.assertNotIn("--output_dir=/etc/evil", cmd)
        self.assertNotIn("rm -rf /", cmd)
        self.assertNotIn(42, cmd)

    def test_missing_or_nonlist_flags_is_noop(self):
        # ACT-class jobs carry no policy_cli_flags — passthrough is a clean no-op.
        for params in ({}, {"policy_cli_flags": None}, {"policy_cli_flags": "x"}):
            cmd = self._cmd(params)
            self.assertFalse(
                any(a.startswith("--policy.pretrained_path") for a in cmd),
                f"unexpected pretrained_path for params={params}",
            )

    def test_act_emits_no_vla_flags_by_default(self):
        # An ACT job with no injected flags must never carry a pretrained_path
        # (ACT-from-scratch on the ImageNet resnet18 backbone is correct).
        cmd = training_handler._build_training_command(
            dataset_name="user/data", model_type="act",
            model_name="user/model", training_params={},
        )
        self.assertFalse(any(a.startswith("--policy.pretrained_path") for a in cmd))
        self.assertFalse(any(a.startswith("--policy.dtype") for a in cmd))


class TestTerminalCancelDetection(unittest.TestCase):
    """The worker self-terminates when its scoped progress RPC reports the row
    is terminal (canceled/failed API-side) — the defence that bounds the
    "cancel just continues on Modal" failure to one progress interval even if
    the Modal-side terminate raised. _is_terminal_cancel_error is the gate."""

    def test_terminal_errors_match(self):
        # update_training_progress raises ERRCODE P0001 + this message when the
        # row is terminal / the worker_token was nulled on cancel.
        for msg in (
            '{"code":"P0001","message":"Invalid worker token, training not '
            'found, or training already terminal"}',
            "training already terminal",
            "Invalid worker token",
            "... ERRCODE P0001 ...",
        ):
            self.assertTrue(
                training_handler._is_terminal_cancel_error(Exception(msg)),
                f"should be treated as a cancel signal: {msg!r}",
            )

    def test_transient_errors_do_not_match(self):
        # Network blips / 5xx must NOT trip self-termination — they get the
        # normal bounded retry so a flaky Supabase doesn't kill a live run.
        for msg in (
            "Connection reset by peer",
            "503 Service Unavailable",
            "read timeout",
            "Temporary failure in name resolution",
        ):
            self.assertFalse(
                training_handler._is_terminal_cancel_error(Exception(msg)),
                f"transient error must NOT be treated as cancel: {msg!r}",
            )


if __name__ == "__main__":
    unittest.main()
