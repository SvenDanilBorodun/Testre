"""Unit tests for the training_handler correctness guards.

Covers:
  * _validate_stats / _stat_value (#3): reject a dataset whose baked
    meta/stats.json is missing/incomplete BEFORE the GPU spins up. lerobot_train
    trusts the dataset's stats (never recomputes), so an incomplete stats.json
    would crash at processor build or train on wrong normalization.
  * _pretrained_load_failed (#4): fail a VLA base-checkpoint run that silently
    fell back to random init (LeRobot prints "Could not load state dict" and
    returns a randomly-initialized model with exit code 0).

Heavy module-level imports (huggingface_hub, supabase) are stubbed so the test
runs without Modal/Supabase/LeRobot — same pattern as test_training_handler_cli.
"""

from __future__ import annotations

import os
import sys
import types
import unittest


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

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODAL_DIR = os.path.abspath(os.path.join(_HERE, "..", "modal_training"))
if _MODAL_DIR not in sys.path:
    sys.path.insert(0, _MODAL_DIR)

import training_handler  # noqa: E402


def _complete_stats(n=6):
    """A flat (serialize_dict shape) stats dict with every required key for
    observation.state + action, each a list of length n."""
    stats = {}
    for feature in ("observation.state", "action"):
        for key in training_handler._REQUIRED_STATS_KEYS:
            stats[f"{feature}/{key}"] = [0.0] * n
    # camera + extra keys the validator must ignore
    stats["observation.images.scene/mean"] = [0.1, 0.2, 0.3]
    stats["observation.state/count"] = [100]
    return stats


class TestValidateStats(unittest.TestCase):

    def test_complete_flat_stats_pass(self):
        # Must not raise.
        training_handler._validate_stats(_complete_stats(6), "u/d", 6)

    def test_nested_shape_also_accepted(self):
        # Legacy/hand-built nested {feature: {key: [...]}} shape.
        stats = {}
        for feature in ("observation.state", "action"):
            stats[feature] = {
                k: [0.0] * 5 for k in training_handler._REQUIRED_STATS_KEYS
            }
        training_handler._validate_stats(stats, "u/d", 5)

    def test_missing_quantile_key_rejected(self):
        # pi05 needs q01/q99 — a stats file without them must fail.
        stats = _complete_stats(6)
        del stats["observation.state/q01"]
        with self.assertRaises(ValueError) as ctx:
            training_handler._validate_stats(stats, "u/d", 6)
        self.assertIn("q01", str(ctx.exception))

    def test_missing_std_rejected(self):
        stats = _complete_stats(6)
        del stats["action/std"]
        with self.assertRaises(ValueError):
            training_handler._validate_stats(stats, "u/d", 6)

    def test_wrong_length_rejected(self):
        stats = _complete_stats(6)
        stats["observation.state/mean"] = [0.0] * 5  # joint-count mismatch
        with self.assertRaises(ValueError):
            training_handler._validate_stats(stats, "u/d", 6)

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            training_handler._validate_stats(["not", "a", "dict"], "u/d", 6)

    def test_empty_stats_rejected(self):
        with self.assertRaises(ValueError):
            training_handler._validate_stats({}, "u/d", 6)


class TestPretrainedLoadFailed(unittest.TestCase):

    _BASE = "--policy.pretrained_path=lerobot/pi05_base"

    def test_pretrained_run_with_load_failure_is_failed(self):
        params = {"policy_cli_flags": [self._BASE, "--policy.dtype=bfloat16"]}
        log = "...\nWarning: Could not load state dict\n...training continues..."
        self.assertTrue(training_handler._pretrained_load_failed(log, params))

    def test_pretrained_run_clean_log_is_ok(self):
        params = {"policy_cli_flags": [self._BASE]}
        log = "step:1K loss:0.5\nstep:2K loss:0.4\n"
        self.assertFalse(training_handler._pretrained_load_failed(log, params))

    def test_act_run_is_never_gated(self):
        # ACT carries no pretrained_path; even a stray matching line is ignored.
        params = {"policy_cli_flags": []}
        log = "Could not load state dict"
        self.assertFalse(training_handler._pretrained_load_failed(log, params))

    def test_missing_flags_key_is_safe(self):
        self.assertFalse(training_handler._pretrained_load_failed("x", {}))
        self.assertFalse(
            training_handler._pretrained_load_failed("x", {"policy_cli_flags": None})
        )

    def test_empty_output_is_safe(self):
        params = {"policy_cli_flags": [self._BASE]}
        self.assertFalse(training_handler._pretrained_load_failed("", params))
        self.assertFalse(training_handler._pretrained_load_failed(None, params))

    def test_smolvla_strict_false_missing_keys_is_failed(self):
        # SmolVLA has no from_pretrained override: the base
        # PreTrainedPolicy.from_pretrained loads with strict=False, where a
        # key mismatch is only logging.warning'd (policies/utils.py
        # log_model_loading_keys) and training continues on partially
        # random submodules. The gate must catch that marker too.
        params = {"policy_cli_flags": [
            "--policy.pretrained_path=lerobot/smolvla_base"]}
        log = ("WARNING Missing key(s) when loading model: "
               "['model.vlm.embed_tokens.weight']\nstep:1K loss:0.5\n")
        self.assertTrue(training_handler._pretrained_load_failed(log, params))
        log2 = ("WARNING Unexpected key(s) when loading model: "
                "['model.old_head.weight']\n")
        self.assertTrue(training_handler._pretrained_load_failed(log2, params))

    def test_smolvla_markers_not_gated_without_pretrained_path(self):
        params = {"policy_cli_flags": []}
        log = "Missing key(s) when loading model: ['x']"
        self.assertFalse(training_handler._pretrained_load_failed(log, params))


if __name__ == "__main__":
    unittest.main()
