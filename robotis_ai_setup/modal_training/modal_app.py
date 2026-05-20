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

LEROBOT_COMMIT = "989f3d05ba47f872d75c587e76838e9cc574857a"

app = modal.App("edubotics-training")

image = (
    modal.Image.from_registry(
        # Bumped 12.1.1 → 12.4.1 to satisfy LeRobot's pyproject.toml at the
        # pinned SHA — that pyproject.toml declares `torchvision>=0.21.0`,
        # and torchvision 0.21.x ships only on the cu124/cu126 wheel indexes
        # (cu121 tops out at 0.20.1). Without this bump the index_url below
        # silently resolves torchvision DOWN to 0.20.1, which breaks v2
        # transform dispatch with NotImplementedError on ColorJitter /
        # SharpnessJitter at training time. Verified 2026-05-20 via direct
        # fetch of LeRobot's pyproject.toml at the SHA constant above + an
        # HTML scrape of the cu121 wheel index. Modal L4 GPUs run R550+
        # drivers which support CUDA 12.4 fine.
        "nvidia/cuda:12.4.1-devel-ubuntu22.04",
        add_python="3.11",
    )
    # clang + build-essential needed because lerobot pulls in evdev, whose
    # setup.py compiles a C extension. The CUDA devel base does not include
    # either by default once Modal replaces Python via add_python.
    .apt_install("git", "ffmpeg", "clang", "build-essential")
    .pip_install(
        f"lerobot[pi0] @ git+https://github.com/huggingface/lerobot.git@{LEROBOT_COMMIT}",
        "huggingface_hub",
        "supabase",
    )
    .pip_install(
        # Both versions pinned so a future torch/torchvision release on
        # pytorch.org doesn't silently shift the image under us. The pair
        # 2.6.0 + 0.21.0 is the latest on the cu124 index as of 2026-05-20
        # and matches LeRobot's pyproject floor (torch>=2.2.1, torchvision>=0.21.0).
        "torch==2.6.0",
        "torchvision==0.21.0",
        # Use `index_url` (not `extra_index_url`) so pip cannot fall back to
        # PyPI — without this constraint pip picks a CPU-only or cu130 wheel
        # and the CUDA base image runtime-crashes (the trap CLAUDE.md Rule §5
        # documents — same lesson applies on cu124).
        index_url="https://download.pytorch.org/whl/cu124",
        extra_options="--force-reinstall",
    )
    .run_commands("python -m pip uninstall -y torchcodec || true")
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
