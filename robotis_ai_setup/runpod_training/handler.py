"""
RunPod Serverless handler for EduBotics cloud training.

Receives a training job, runs LeRobot training, and pushes
the trained model to HuggingFace Hub.

Reference: phosphobot modal/lerobot_modal/app.py
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import runpod
from huggingface_hub import HfApi, login
from supabase import create_client


OUTPUT_DIR = Path("/tmp/training_output")


def _update_supabase_status(
    supabase_url: str,
    supabase_key: str,
    training_id: int,
    status: str,
    error_message: str | None = None,
):
    """Update training status in Supabase."""
    client = create_client(supabase_url, supabase_key)
    update_data = {"status": status}
    if status in ("succeeded", "failed", "canceled"):
        update_data["terminated_at"] = datetime.now(timezone.utc).isoformat()
    if error_message:
        update_data["error_message"] = error_message
    client.table("trainings").update(update_data).eq("id", training_id).execute()


def _build_training_command(
    dataset_name: str,
    model_type: str,
    model_name: str,
    training_params: dict,
) -> list[str]:
    """
    Build the LeRobot training command.

    Mirrors the arg pattern from physical_ai_server/training/training_manager.py.
    """
    output_dir = str(OUTPUT_DIR / model_name.replace("/", "_"))

    cmd = [
        sys.executable,
        "-m",
        "lerobot.scripts.train",
        f"--policy.type={model_type}",
        "--policy.device=cuda",
        f"--dataset.repo_id={dataset_name}",
        f"--output_dir={output_dir}",
        "--policy.push_to_hub=false",
    ]

    # Map training params to CLI args
    param_mapping = {
        "seed": "--seed",
        "num_workers": "--num_workers",
        "batch_size": "--batch_size",
        "steps": "--steps",
        "eval_freq": "--eval_freq",
        "log_freq": "--log_freq",
        "save_freq": "--save_freq",
    }

    for param_key, cli_flag in param_mapping.items():
        value = training_params.get(param_key)
        if value is not None and value != 0:
            cmd.append(f"{cli_flag}={value}")

    return cmd


def _upload_model_to_hf(
    model_name: str,
    hf_token: str,
) -> str:
    """
    Upload trained model to HuggingFace Hub.

    Reference: phosphobot modal/lerobot_modal/helper.py _upload_dataset_to_hf
    """
    hf_api = HfApi(token=hf_token)

    # Create repo if it doesn't exist
    hf_api.create_repo(repo_id=model_name, repo_type="model", exist_ok=True)

    # Find the model checkpoint directory
    output_path = OUTPUT_DIR / model_name.replace("/", "_")

    # LeRobot saves to checkpoints/last/pretrained_model/
    checkpoint_dir = output_path / "checkpoints" / "last" / "pretrained_model"
    if not checkpoint_dir.exists():
        # Fallback: look for any pretrained_model directory
        for p in output_path.rglob("pretrained_model"):
            checkpoint_dir = p
            break

    if not checkpoint_dir.exists():
        raise FileNotFoundError(
            f"No pretrained_model directory found in {output_path}"
        )

    # Upload all files from the checkpoint directory
    hf_api.upload_folder(
        repo_id=model_name,
        folder_path=str(checkpoint_dir),
        repo_type="model",
    )

    return f"https://huggingface.co/{model_name}"


def handler(job):
    """RunPod serverless handler for training jobs."""
    job_input = job["input"]

    dataset_name = job_input["dataset_name"]
    model_name = job_input["model_name"]
    model_type = job_input["model_type"]
    training_params = job_input.get("training_params", {})
    training_id = job_input["training_id"]
    supabase_url = job_input["supabase_url"]
    supabase_key = job_input["supabase_key"]
    hf_token = job_input.get("hf_token", "")

    # Login to HuggingFace
    if hf_token:
        login(token=hf_token)

    # Update status to running
    _update_supabase_status(supabase_url, supabase_key, training_id, "running")

    try:
        # Build and run training command
        cmd = _build_training_command(dataset_name, model_type, model_name, training_params)
        print(f"Running training command: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3 * 3600,  # 3 hour timeout
        )

        if result.returncode != 0:
            error_msg = result.stderr[-2000:] if result.stderr else "Unknown error"
            _update_supabase_status(
                supabase_url, supabase_key, training_id, "failed", error_msg
            )
            return {"status": "failed", "error": error_msg}

        # Upload model to HuggingFace
        model_url = _upload_model_to_hf(model_name, hf_token)

        # Update status to succeeded
        _update_supabase_status(supabase_url, supabase_key, training_id, "succeeded")

        return {"status": "succeeded", "model_url": model_url}

    except subprocess.TimeoutExpired:
        _update_supabase_status(
            supabase_url, supabase_key, training_id, "failed", "Training timed out (3h limit)"
        )
        return {"status": "failed", "error": "Training timed out"}

    except Exception as e:
        error_msg = str(e)[-2000:]
        _update_supabase_status(
            supabase_url, supabase_key, training_id, "failed", error_msg
        )
        return {"status": "failed", "error": error_msg}


runpod.serverless.start({"handler": handler})
