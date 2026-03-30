"""
RunPod Serverless handler for EduBotics cloud training.

Receives a training job, runs LeRobot training, and pushes
the trained model to HuggingFace Hub.

Reference: phosphobot modal/lerobot_modal/app.py
"""

import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import json

import runpod
from huggingface_hub import HfApi, login
from supabase import create_client


OUTPUT_DIR = Path("/tmp/training_output")


def _parse_abbreviated_number(s: str) -> int:
    """Parse LeRobot's abbreviated numbers: '50K' → 50000, '1.5M' → 1500000."""
    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    s = s.strip()
    for suffix, mult in multipliers.items():
        if s.upper().endswith(suffix):
            return int(float(s[:-1]) * mult)
    return int(float(s))


def _get_supabase_client(supabase_url: str, supabase_key: str):
    """Create a Supabase client (cached per handler invocation)."""
    return create_client(supabase_url, supabase_key)


def _update_supabase_status(
    supabase_url: str,
    supabase_key: str,
    training_id: int,
    status: str,
    error_message: str | None = None,
):
    """Update training status in Supabase."""
    client = _get_supabase_client(supabase_url, supabase_key)
    update_data = {"status": status}
    if status in ("succeeded", "failed", "canceled"):
        update_data["terminated_at"] = datetime.now(timezone.utc).isoformat()
    if error_message:
        update_data["error_message"] = error_message
    client.table("trainings").update(update_data).eq("id", training_id).execute()


def _update_supabase_progress(
    supabase_url: str,
    supabase_key: str,
    training_id: int,
    current_step: int,
    total_steps: int,
    current_loss: float | None = None,
):
    """Update training progress in Supabase."""
    client = _get_supabase_client(supabase_url, supabase_key)
    update_data = {"current_step": current_step, "total_steps": total_steps}
    if current_loss is not None:
        update_data["current_loss"] = current_loss
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
        # Disable eval — no simulation env available on cloud worker
        "--eval_freq=0",
    ]

    # Map training params to CLI args (eval_freq excluded — forced to 0 above)
    param_mapping = {
        "seed": "--seed",
        "num_workers": "--num_workers",
        "batch_size": "--batch_size",
        "steps": "--steps",
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

    # Write camera/observation metadata from config.json
    config_path = checkpoint_dir / "config.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                model_config = json.load(f)
            image_keys = [k for k in model_config.get("input_features", {})
                          if k.startswith("observation.images.")]
            camera_meta = {
                "cameras": [k.replace("observation.images.", "") for k in image_keys],
                "observation_image_keys": image_keys,
            }
            meta_path = checkpoint_dir / "camera_config.json"
            with open(meta_path, "w") as f:
                json.dump(camera_meta, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not write camera metadata: {e}")

    # Upload all files from the checkpoint directory
    hf_api.upload_folder(
        repo_id=model_name,
        folder_path=str(checkpoint_dir),
        repo_type="model",
    )

    # Verify upload succeeded
    info = hf_api.repo_info(repo_id=model_name, repo_type="model")
    if not info:
        raise RuntimeError(f"Upload verification failed: repo {model_name} not found after upload")

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

    total_steps = training_params.get("steps", 100000)

    try:
        # Build and run training command
        cmd = _build_training_command(dataset_name, model_type, model_name, training_params)
        print(f"Running training command: {' '.join(cmd)}")

        # Use Popen with both stdout and stderr streamed in threads
        # so that proc.wait(timeout) actually works for timeout protection.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        stdout_lines = []
        stderr_lines = []
        last_progress_step = -1
        # LeRobot logs abbreviated numbers: "step:50K smpl:1.6M loss:0.1234"
        step_pattern = re.compile(r"step[:\s]+(\d+\.?\d*[KMBkmb]?)")
        loss_pattern = re.compile(r"loss[:\s]+([\d.]+(?:e[+-]?\d+)?)")

        def _read_stdout():
            nonlocal last_progress_step
            for line in proc.stdout:
                print(line, end="")
                stdout_lines.append(line)
                step_match = step_pattern.search(line)
                if step_match:
                    try:
                        step = _parse_abbreviated_number(step_match.group(1))
                    except (ValueError, TypeError):
                        continue
                    loss_match = loss_pattern.search(line)
                    loss = float(loss_match.group(1)) if loss_match else None
                    if step > last_progress_step:
                        last_progress_step = step
                        try:
                            _update_supabase_progress(
                                supabase_url, supabase_key, training_id,
                                step, total_steps, loss,
                            )
                        except Exception:
                            pass

        def _read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        # Wait for process with actual timeout protection
        proc.wait(timeout=3 * 3600)
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        stderr_text = "".join(stderr_lines)

        if proc.returncode != 0:
            if len(stderr_text) > 2000:
                error_msg = stderr_text[:1000] + "\n...[truncated]...\n" + stderr_text[-1000:]
            else:
                error_msg = stderr_text or "Unknown error"
            _update_supabase_status(
                supabase_url, supabase_key, training_id, "failed", error_msg
            )
            return {"status": "failed", "error": error_msg}

        # Upload model to HuggingFace
        model_url = _upload_model_to_hf(model_name, hf_token)

        # Update final progress + status
        _update_supabase_progress(
            supabase_url, supabase_key, training_id, total_steps, total_steps, None
        )
        _update_supabase_status(supabase_url, supabase_key, training_id, "succeeded")

        return {"status": "succeeded", "model_url": model_url}

    except subprocess.TimeoutExpired:
        proc.kill()
        _update_supabase_status(
            supabase_url, supabase_key, training_id, "failed", "Training timed out (3h limit)"
        )
        return {"status": "failed", "error": "Training timed out"}

    except Exception as e:
        err = str(e)
        error_msg = err[:1000] + "\n...[truncated]...\n" + err[-1000:] if len(err) > 2000 else err
        _update_supabase_status(
            supabase_url, supabase_key, training_id, "failed", error_msg
        )
        return {"status": "failed", "error": error_msg}


runpod.serverless.start({"handler": handler})
