"""Per-policy training recipe + GPU tier — single source of truth.

Why this exists
---------------
A worker that runs ``lerobot_train --policy.type=X --dataset.repo_id=…`` with no
other flags trains the VLA policies (pi0 / pi05 / pi0_fast / smolvla) from
RANDOM init in fp32 — they OOM on a 24 GB L4 and produce nothing usable. Each
VLA needs (a) its base checkpoint loaded via ``--policy.pretrained_path`` and
(b) the memory/precision flags its own config actually supports. Those flag sets
DIFFER per policy and emitting an unsupported one makes draccus reject the whole
job, so the set is curated here against the LeRobot v0.5.1 config dataclasses:

  * pi0 / pi05 : ``dtype``, ``gradient_checkpointing``, ``train_expert_only``
                 all exist  (train_expert_only=true freezes the 3 B VLM and
                 trains only the action expert — the L4/A100-friendly recipe;
                 only meaningful WITH pretrained_path).
  * pi0_fast   : ``dtype``, ``gradient_checkpointing`` exist; ``train_expert_only``
                 does NOT — do not emit it.
  * smolvla    : NEITHER ``dtype`` NOR ``gradient_checkpointing`` is a config
                 field — emit ONLY ``pretrained_path``. Its config already
                 defaults to ``freeze_vision_encoder=true`` + ``train_expert_only=true``.

ACT-class policies (act / diffusion / vqbet / tdmpc) stay on the cheap L4 with
NO extra flags — ACT-from-scratch on an ImageNet-pretrained resnet18 backbone is
the correct, standard ACT recipe; do NOT add a pretrained_path for them.

Per-policy step caps + batch ranges (apply_training_tuning): the student UI
sends steps=50000 / batch=8, but the VLAs converge far below 50k (docs: pi0/pi05
~3k, smolvla ~20k) and batch 8 is either too small (smolvla/pi0/pi05, docs use
32/64) or the memory ceiling (pi0_fast = a full ~2.9B fine-tune). So VLA steps
are CAPPED (never raised) and batch is CLAMPED into a safe+effective range; this
saves A100 hours and improves convergence. ACT-class is left UI-controlled.

TWO OPERATIONAL REALITIES (operator decision 2026-06-15 = "leave open, document
only"; not enforced in code):
  1. GEMMA-LICENSE GATING (pi0/pi05/pi0_fast): their PaliGemma tokenizer
     `google/paligemma-3b-pt-224` is GATED and downloaded at train start. The
     Modal worker's HF_TOKEN account MUST have accepted the Gemma license
     (huggingface.co/google/paligemma-3b-pt-224) or all three FAIL at train
     start with a 401. smolvla is immune (apache-2.0 deps).
  2. INFERENCE IS EXPORT-ONLY for pi0/pi05/pi0_fast: ~3B VLAs need ~10-13 GB and
     CANNOT run on the EduBotics inference fleet (student PCs have no GPU; the
     classroom Jetson Orin Nano is 8 GB shared / 6 GB container cap → OOM; LeRobot
     v0.5.1 has no quantized path). They are trainable-but-undeployable in-fleet
     (teacher/external-GPU export only). smolvla (450 MB) WOULD fit the Jetson
     but is gated off on aarch64 (inference_manager, bug #3636) — kept gated.

The flag is ``--policy.pretrained_path`` (NOT ``--policy.path``): on v0.5.1
``pretrained_path`` is the real ``PreTrainedConfig`` field; ``--policy.path`` is
an unimplemented wishlist alias on that release and draccus would reject it.

This module is intentionally dependency-free so it can be imported by both the
route layer (for GPU routing + flag injection) and unit tests without pulling
fastapi/pydantic/supabase.
"""

from __future__ import annotations

# GPU tiers map to the two deployed Modal functions (see modal_app.py +
# modal_client._get_train_function): "l4" -> `train`, "a100" -> `train_vla`.
GPU_L4 = "l4"
GPU_A100 = "a100"

# Base-checkpoint repo ids — confirmed live on the HuggingFace Hub (verified via
# hub_repo_details: pi0_base 3501M, pi05_base 3617M, pi0fast-base 2923M,
# smolvla_base 450M). pi0_fast's repo is the HYPHEN form `lerobot/pi0fast-base`
# — the underscore form `lerobot/pi0fast_base` does NOT exist (HF returns 401).
_PI0_FLAGS = [
    "--policy.pretrained_path=lerobot/pi0_base",
    "--policy.dtype=bfloat16",
    "--policy.gradient_checkpointing=true",
    "--policy.train_expert_only=true",
]
_PI05_FLAGS = [
    "--policy.pretrained_path=lerobot/pi05_base",
    "--policy.dtype=bfloat16",
    "--policy.gradient_checkpointing=true",
    "--policy.train_expert_only=true",
]
_PI0FAST_FLAGS = [
    "--policy.pretrained_path=lerobot/pi0fast-base",
    "--policy.dtype=bfloat16",
    "--policy.gradient_checkpointing=true",
    # NOTE: pi0_fast has no train_expert_only / freeze_vision_encoder field —
    # must NOT emit them. This is a FULL ~2.9B fine-tune (the heaviest VLA).
    # Short autoregressive horizon: pi0_fast is FAST-token autoregressive, and
    # the LeRobot docs pair it with chunk_size=10 / n_action_steps=10 (vs the
    # config default 50). A 50-step chunk produces ~5x longer FAST token
    # sequences -> slower decode, more memory, weaker results. max_action_tokens
    # stays at its 256 default. (chunk_size/n_action_steps/max_action_tokens are
    # all real PI0FastConfig fields in v0.5.1.)
    "--policy.chunk_size=10",
    "--policy.n_action_steps=10",
]
_SMOLVLA_FLAGS = [
    # SmolVLAConfig has no dtype / gradient_checkpointing field; emitting them
    # would make draccus reject the job. Base checkpoint only.
    "--policy.pretrained_path=lerobot/smolvla_base",
]

# Policies whose LeRobot config consumes a natural-language task string — used
# by the worker preflight to fail a task-less dataset BEFORE GPU spin-up.
LANGUAGE_CONDITIONED_POLICIES = frozenset({"pi0", "pi05", "pi0_fast", "smolvla"})

# Per-VLA tuning (apply_training_tuning): steps_cap = max useful steps for a
# small OMX dataset (docs: pi0/pi05 ~3k, smolvla ~20k); batch_min/batch_max =
# the safe+effective batch range on an A100-80GB. pi0_fast has NO steps_cap (its
# full ~2.9B FT is timeout-bound) and the tightest batch (memory ceiling).
POLICY_PROFILE: dict[str, dict] = {
    "act":       {"gpu_tier": GPU_L4,   "policy_flags": []},
    "diffusion": {"gpu_tier": GPU_L4,   "policy_flags": []},
    "vqbet":     {"gpu_tier": GPU_L4,   "policy_flags": []},
    "tdmpc":     {"gpu_tier": GPU_L4,   "policy_flags": []},
    "smolvla":   {"gpu_tier": GPU_A100, "policy_flags": list(_SMOLVLA_FLAGS),
                  "steps_cap": 20000, "batch_min": 32, "batch_max": 64},
    "pi0":       {"gpu_tier": GPU_A100, "policy_flags": list(_PI0_FLAGS),
                  "steps_cap": 10000, "batch_min": 16, "batch_max": 24},
    "pi05":      {"gpu_tier": GPU_A100, "policy_flags": list(_PI05_FLAGS),
                  "steps_cap": 10000, "batch_min": 16, "batch_max": 24},
    "pi0_fast":  {"gpu_tier": GPU_A100, "policy_flags": list(_PI0FAST_FLAGS),
                  "batch_min": 4, "batch_max": 8},
}

# Unknown policy -> conservative L4, no extra flags (ACT-like default).
_DEFAULT_PROFILE: dict = {"gpu_tier": GPU_L4, "policy_flags": []}

_TUNING_KEYS = ("steps_cap", "batch_min", "batch_max")


def get_policy_profile(model_type: str) -> dict:
    """Return the profile for a policy (case-insensitive): always "gpu_tier" +
    "policy_flags", plus "steps_cap"/"batch_min"/"batch_max" for tuned VLAs.

    Falls back to the L4 / no-flags default for an unrecognized policy so a new
    policy keyword can never silently route to the expensive A100 function or
    emit flags it doesn't support. The returned dict + flag list are fresh
    copies, so a caller mutating them can't poison the shared table.
    """
    prof = POLICY_PROFILE.get((model_type or "").lower(), _DEFAULT_PROFILE)
    out = {"gpu_tier": prof["gpu_tier"], "policy_flags": list(prof["policy_flags"])}
    for k in _TUNING_KEYS:
        if k in prof:
            out[k] = prof[k]
    return out


def apply_training_tuning(profile: dict, training_params: dict) -> None:
    """Mutate training_params in place: cap steps + clamp batch_size per policy.

    No-op for policies without tuning fields (ACT-class). Call AFTER the
    timeout cap and BEFORE both the row insert and the Modal dispatch, so the
    stored total_steps, the dispatched payload, and the worker all agree.

    - steps: capped (never raised) to steps_cap — VLAs converge far below the
      UI's 50k default, so this just stops wasting A100 hours.
    - batch_size: clamped into [batch_min, batch_max] — 8 is too small for
      smolvla/pi0/pi05 (under-uses the A100) and is the memory ceiling for the
      pi0_fast full fine-tune. A missing/invalid batch falls back to batch_min.
    """
    steps_cap = profile.get("steps_cap")
    if steps_cap and isinstance(training_params.get("steps"), int):
        training_params["steps"] = min(training_params["steps"], steps_cap)
    bmin, bmax = profile.get("batch_min"), profile.get("batch_max")
    if bmin and bmax:
        b = training_params.get("batch_size")
        if not isinstance(b, int):
            b = bmin
        training_params["batch_size"] = max(bmin, min(b, bmax))
