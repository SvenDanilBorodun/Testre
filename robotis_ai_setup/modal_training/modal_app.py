"""Modal app for EduBotics cloud GPU training.

Deploy:   modal deploy modal_app.py
Dev:      modal serve modal_app.py
Smoke:    modal run -m modal_app::smoke_test

The Railway FastAPI service dispatches training jobs by resolving this app's
`train` function via `modal.Function.from_name("edubotics-training", "train")`
and calling `.spawn(...)` — async dispatch returns a FunctionCall whose
object_id is persisted to Supabase as `cloud_job_id`.

Credentials (SUPABASE_URL, SUPABASE_ANON_KEY, HF_TOKEN) are injected as env
vars via the Modal Secret `edubotics-training-secrets`. Per-training args
(dataset_name, model_name, worker_token, ...) are passed as function kwargs.
"""

import modal

# LeRobot v0.5.1 (tag v0.5.1). Dataset format codebase_version "v3.0".
# This MUST agree with the 5 other pinning sites — see CLAUDE.md Rule §5.
LEROBOT_COMMIT = "1396b9fab7aecddd10006c33c47a487ffdcb54b4"

app = modal.App("edubotics-training")

image = (
    modal.Image.from_registry(
        # cu124 → cu126: LeRobot v0.5.1 requires torch>=2.7 (pyproject core
        # dep), and torch 2.7.x ships ONLY on the cu126/cu128 wheel indexes —
        # cu124 tops out at torch 2.6 (verified 2026-05-25 against
        # download.pytorch.org/whl/cu124|cu126). The torch pip wheel bundles
        # its own CUDA runtime libs, so the base-image CUDA version mainly
        # gates nvcc/devel builds; Modal L4 GPUs run R570+ drivers (CUDA 12.6
        # OK under minor-version compat).
        "nvidia/cuda:12.6.3-devel-ubuntu22.04",
        # 3.11 → 3.12: LeRobot v0.5.1 floors requires-python at ">=3.12".
        add_python="3.12",
    )
    # clang + build-essential needed because lerobot pulls in evdev, whose
    # setup.py compiles a C extension. The CUDA devel base does not include
    # either by default once Modal replaces Python via add_python.
    .apt_install("git", "ffmpeg", "clang", "build-essential")
    .pip_install(
        # v0.5.1 renamed the `[pi0]` extra to `[pi]`; add `[smolvla]` too so
        # both VLA policy families are trainable. torch/torchvision/torchcodec/
        # numpy/av are CORE deps in v0.5.1 and resolve automatically.
        f"lerobot[pi,smolvla] @ git+https://github.com/huggingface/lerobot.git@{LEROBOT_COMMIT}",
        "huggingface_hub",
        "supabase",
    )
    .pip_install(
        # Pin the exact cu126 pair so a future torch/torchvision release on
        # pytorch.org doesn't silently shift the image under us. 2.7.1 + 0.22.1
        # is the official pairing within LeRobot's floors (torch>=2.7,<2.11;
        # torchvision>=0.22,<0.26) and both ship on the cu126 cp312 index.
        "torch==2.7.1",
        "torchvision==0.22.1",
        # Use `index_url` (not `extra_index_url`) so pip cannot fall back to
        # PyPI — without this constraint pip picks a CPU-only or cu130 wheel
        # and the CUDA base image runtime-crashes (the trap CLAUDE.md Rule §5
        # documents).
        index_url="https://download.pytorch.org/whl/cu126",
        extra_options="--force-reinstall",
    )
    # NOTE: torchcodec is NO LONGER uninstalled. v3.0 datasets reference
    # videos that LeRobot decodes at training time via torchcodec (the safe
    # default video backend); it is a CORE dep in v0.5.1 and is left in place.
    .env({"PYTHONUNBUFFERED": "1"})
    .add_local_python_source("training_handler")
)

secrets = [modal.Secret.from_name("edubotics-training-secrets")]


@app.function(
    image=image,
    gpu="L4",
    timeout=7 * 3600,
    secrets=secrets,
    min_containers=0,
)
def train(
    dataset_name: str,
    model_name: str,
    model_type: str,
    training_params: dict,
    training_id: int,
    worker_token: str,
) -> dict:
    """Single training job. Returns {"status": "succeeded"|"failed", ...}."""
    from training_handler import run_training

    return run_training(
        dataset_name=dataset_name,
        model_name=model_name,
        model_type=model_type,
        training_params=training_params,
        training_id=training_id,
        worker_token=worker_token,
    )


@app.function(image=image, secrets=secrets)
def smoke_test():
    """Verify the image boots + secrets + GPU libs are importable.

    Usage: modal run -m modal_app::smoke_test
    """
    import os
    import torch

    required = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "HF_TOKEN")
    missing = [k for k in required if not os.environ.get(k)]
    print(f"torch={torch.__version__} cuda_available={torch.cuda.is_available()}")
    print(f"missing secrets: {missing or 'none'}")
    # Cast torch.__version__ to a plain `str` — it's actually a TorchVersion
    # subclass instance, and pickling it serializes the class reference, so
    # `modal run` deserializing on a torch-less Mac raises:
    #   DeserializationError: Deserialization failed because the 'torch'
    #   module is not available in the local environment.
    # Same for the cuda_available bool — wrap to plain Python types only.
    return {
        "torch": str(torch.__version__),
        "cuda_available": bool(torch.cuda.is_available()),
        "missing_secrets": list(missing),
    }
