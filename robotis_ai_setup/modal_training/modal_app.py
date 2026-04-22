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
        "nvidia/cuda:12.1.1-devel-ubuntu22.04",
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
        "torch",
        "torchvision",
        # `index_url` (not extra_index_url) matches the RunPod Dockerfile's
        # --index-url flag. extra_index_url leaves the default mirror in play,
        # so pip picks torch+cu130 from Modal's mirror instead of cu121 from
        # pytorch.org. Use the single index to force cu121.
        index_url="https://download.pytorch.org/whl/cu121",
        extra_options="--force-reinstall",
    )
    .run_commands("python -m pip uninstall -y torchcodec || true")
    .env({"PYTHONUNBUFFERED": "1"})
    .add_local_python_source("training_handler")
)

secrets = [modal.Secret.from_name("edubotics-training-secrets")]


@app.function(
    image=image,
    gpu="A100-80GB",
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
    return {"torch": torch.__version__, "missing_secrets": missing}
