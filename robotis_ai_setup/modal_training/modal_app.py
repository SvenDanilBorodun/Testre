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

LEROBOT_VERSION = "0.5.1"

app = modal.App("edubotics-training")

image = (
    modal.Image.from_registry(
        # CUDA 12.6.1 + torch 2.7.0/cu126 chosen to match LeRobot v0.5.1's
        # `torch>=2.7,<2.11.0` pin (its lowest supported torch). cu126 wheel
        # index ships torch 2.7.0 and torchvision 0.22.0, both inside v0.5.1's
        # allowed ranges. Keeping torch on 2.7.0 matches the student image's
        # torch 2.7.0+cu128 from the Robotis base — one fewer surface to drift.
        # Modal L4 GPUs (Ampere) run R550+ drivers; CUDA 12.6 is fully supported.
        "nvidia/cuda:12.6.1-devel-ubuntu22.04",
        add_python="3.12",
    )
    # clang + build-essential needed because lerobot pulls in evdev, whose
    # setup.py compiles a C extension. The CUDA devel base does not include
    # either by default once Modal replaces Python via add_python.
    .apt_install("git", "ffmpeg", "clang", "build-essential")
    .pip_install(
        # LeRobot v0.5.1 from PyPI — pinned exact for reproducible builds.
        # Extras: [pi0] pulls PI0+PI0Fast deps, [smolvla] pulls transformers
        # (==5.3.0, the LeRobot exact pin) + SmolVLM deps, [peft] pulls PEFT
        # adapter support (required by lerobot.policies.factory.make_policy
        # when cfg.use_peft is True). [smolvla] also transitively covers the
        # new pi05 (Pi-0.5) policy's transformer/processor stack.
        # Do NOT pre-pin transformers here — let the [smolvla,peft] extras
        # resolve to the exact transformers==5.3.0 LeRobot requires.
        f"lerobot[pi0,smolvla,peft]=={LEROBOT_VERSION}",
        # accelerate is a hard runtime dep in v0.5.1 (was optional in 0.2.0).
        # Pin to a safe minor-version range so a future major doesn't break
        # the resolver out from under us.
        "accelerate>=1.10.0,<2.0.0",
        "huggingface_hub",
        "supabase",
    )
    .pip_install(
        # Pin torch + torchvision to the cu126 wheel that matches LeRobot v0.5.1's
        # range (torch>=2.7,<2.11.0; torchvision>=0.22.0,<0.26.0). 2.7.0 + 0.22.0
        # is the LCD that keeps Modal's GPU torch and the student image's
        # Robotis-base torch 2.7.0+cu128 on the same major+minor — minimal
        # cross-surface drift. Pinned exact so a future PyTorch release on
        # pytorch.org cannot silently shift us into a 2.10.x or 2.11.x build
        # that LeRobot doesn't allow.
        "torch==2.7.0",
        "torchvision==0.22.0",
        # Use `index_url` (not `extra_index_url`) so pip cannot fall back to
        # PyPI — without this constraint pip picks a CPU-only or cu130 wheel
        # and the CUDA base image runtime-crashes (the trap CLAUDE.md Rule §5
        # documents — same lesson applies on cu126).
        index_url="https://download.pytorch.org/whl/cu126",
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
