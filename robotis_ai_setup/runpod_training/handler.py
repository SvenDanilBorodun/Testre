"""
RunPod Serverless handler for EduBotics cloud training.

Receives a training job, runs LeRobot training, and pushes
the trained model to HuggingFace Hub.

Reference: phosphobot modal/lerobot_modal/app.py
"""

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import runpod
from huggingface_hub import HfApi, hf_hub_download, login
from huggingface_hub.utils import HfHubHTTPError, RepositoryNotFoundError
from supabase import create_client


OUTPUT_DIR = Path("/tmp/training_output")

# ---------------- ROBOTIS OMX expected schema (Phase 6) ----------------
#
# All students record on the ROBOTIS OpenMANIPULATOR-X (OMX), so the dataset
# schema is fixed and known. Any deviation from the expected joint set,
# missing camera, or wrong codebase version is a real recording bug, not
# a false-positive — we fail-fast at preflight before spinning up the GPU.

EXPECTED_OMX_JOINTS = {
    "joint1", "joint2", "joint3", "joint4", "joint5", "gripper_joint_1",
}
EXPECTED_CODEBASE_VERSION = "v2.1"
KNOWN_OMX_CAMERAS = {"gripper", "scene"}  # informational, not strictly enforced

# Module-level reference to the in-flight job. Used by the SIGTERM handler to
# mark the training as failed and clean up before the worker is killed. Only
# ever one job in flight per RunPod worker invocation.
_current_job: dict | None = None


# ---------------- Parsing helpers ----------------


def _safe_float(value: str) -> float | None:
    """Parse a float and reject NaN / inf / parse errors. Returns None on failure."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _parse_abbreviated_number(s: str) -> int | None:
    """Parse LeRobot's abbreviated numbers: '50K' → 50000, '1.5M' → 1500000.

    Returns None for NaN/inf/garbage instead of returning 0 or raising.
    """
    if s is None:
        return None
    s = s.strip()
    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    suffix_mult = 1
    for suffix, mult in multipliers.items():
        if s.upper().endswith(suffix):
            suffix_mult = mult
            s = s[:-1]
            break
    base = _safe_float(s)
    if base is None:
        return None
    return int(base * suffix_mult)


# ---------------- Supabase RPC helpers ----------------


def _get_supabase_client(supabase_url: str, supabase_anon_key: str):
    """Create a Supabase client using the public anon key.

    The worker has no direct table access — it can only call the
    update_training_progress() RPC, which validates a per-row worker_token.
    """
    return create_client(supabase_url, supabase_anon_key)


def _call_progress_rpc(
    supabase_url: str,
    supabase_anon_key: str,
    worker_token: str,
    training_id: int,
    *,
    status: str | None = None,
    current_step: int | None = None,
    total_steps: int | None = None,
    current_loss: float | None = None,
    error_message: str | None = None,
):
    """Invoke the scoped RPC. Only the row matching (id, worker_token) is updated."""
    client = _get_supabase_client(supabase_url, supabase_anon_key)
    payload = {
        "p_training_id": training_id,
        "p_token": worker_token,
        "p_status": status,
        "p_current_step": current_step,
        "p_total_steps": total_steps,
        "p_current_loss": current_loss,
        "p_error_message": error_message,
    }
    client.rpc("update_training_progress", payload).execute()


def _update_supabase_status(
    supabase_url: str,
    supabase_anon_key: str,
    worker_token: str,
    training_id: int,
    status: str,
    error_message: str | None = None,
):
    """Update training status in Supabase via the scoped RPC."""
    _call_progress_rpc(
        supabase_url, supabase_anon_key, worker_token, training_id,
        status=status, error_message=error_message,
    )


def _update_supabase_progress(
    supabase_url: str,
    supabase_anon_key: str,
    worker_token: str,
    training_id: int,
    current_step: int,
    total_steps: int,
    current_loss: float | None = None,
):
    """Update training progress in Supabase via the scoped RPC."""
    _call_progress_rpc(
        supabase_url, supabase_anon_key, worker_token, training_id,
        current_step=current_step, total_steps=total_steps, current_loss=current_loss,
    )


# ---------------- Dataset preflight ----------------


def _preflight_dataset(dataset_name: str, hf_token: str) -> None:
    """Download just meta/info.json and validate the OMX schema contract.

    Catches the following failure modes BEFORE we waste 10+ GPU minutes:
      - Dataset doesn't exist or worker token can't see it
      - Malformed meta/info.json (missing fields, bad JSON)
      - codebase_version mismatch (recording software is too old/new)
      - fps missing or zero (LeRobot data loader would explode)
      - Joint set in observation.state doesn't match the OMX hardware
      - Joint set in action doesn't match the OMX hardware
      - No camera features at all (OMX always records at least one)

    All deviations are real recording bugs (single hardware target = one
    valid schema), so we fail loud rather than warn.

    Raises ValueError with a German operator-facing message on failure.
    """
    try:
        info_path = hf_hub_download(
            repo_id=dataset_name,
            filename="meta/info.json",
            repo_type="dataset",
            token=hf_token,
        )
    except RepositoryNotFoundError:
        raise ValueError(
            f"Dataset '{dataset_name}' wurde auf HuggingFace nicht gefunden "
            f"oder ist privat (Worker hat keinen Zugriff)."
        )
    except HfHubHTTPError as e:
        raise ValueError(
            f"Dataset '{dataset_name}' konnte nicht geladen werden: {e}"
        )

    try:
        with open(info_path) as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(
            f"Dataset '{dataset_name}' hat ein ungueltiges meta/info.json: {e}"
        )

    # ---- 1. codebase_version exact match ----
    version = info.get("codebase_version")
    if not version:
        raise ValueError(
            f"Dataset '{dataset_name}' hat kein 'codebase_version' Feld. "
            f"Bitte mit aktueller Recording-Software neu aufnehmen."
        )
    if version != EXPECTED_CODEBASE_VERSION:
        raise ValueError(
            f"Dataset '{dataset_name}' hat codebase_version='{version}', "
            f"erwartet wird '{EXPECTED_CODEBASE_VERSION}'. "
            f"Bitte mit aktueller Recording-Software neu aufnehmen."
        )

    # ---- 2. fps present and positive ----
    fps = info.get("fps")
    if not fps or fps <= 0:
        raise ValueError(
            f"Dataset '{dataset_name}' hat keine gueltige 'fps' Angabe ({fps!r})."
        )

    # ---- 3+4. Joint set on observation.state and action ----
    features = info.get("features") or {}

    def _check_joints(feature_key: str, label: str):
        feat = features.get(feature_key)
        if not feat:
            raise ValueError(
                f"Dataset '{dataset_name}' hat kein '{feature_key}' Feature "
                f"({label}). Aufnahme ist beschaedigt."
            )
        names = feat.get("names")
        if not names or not isinstance(names, list):
            raise ValueError(
                f"Dataset '{dataset_name}' hat keine '{feature_key}.names' "
                f"Liste. Bitte neu aufnehmen."
            )
        actual = set(names)
        if actual != EXPECTED_OMX_JOINTS:
            missing = EXPECTED_OMX_JOINTS - actual
            extra = actual - EXPECTED_OMX_JOINTS
            details = []
            if missing:
                details.append(f"fehlend: {sorted(missing)}")
            if extra:
                details.append(f"unerwartet: {sorted(extra)}")
            raise ValueError(
                f"Dataset '{dataset_name}' hat falsche {label}-Gelenke "
                f"({', '.join(details)}). Erwartet werden die OMX-Gelenke: "
                f"{sorted(EXPECTED_OMX_JOINTS)}."
            )

    _check_joints("observation.state", "Follower")
    _check_joints("action", "Action")

    # ---- 5. At least one camera feature ----
    image_keys = [k for k in features if k.startswith("observation.images.")]
    if not image_keys:
        raise ValueError(
            f"Dataset '{dataset_name}' enthaelt keine Kamera-Features. "
            f"Mindestens eine Kamera ist erforderlich."
        )
    cameras = [k.replace("observation.images.", "") for k in image_keys]
    unknown = [c for c in cameras if c not in KNOWN_OMX_CAMERAS]
    if unknown:
        # Informational only — students may have custom camera names.
        print(
            f"Warning: dataset has cameras not on the standard OMX list: "
            f"{unknown} (known: {sorted(KNOWN_OMX_CAMERAS)})"
        )

    print(
        f"Preflight OK: dataset='{dataset_name}' codebase_version={version} "
        f"fps={fps} joints=OMX cameras={cameras}"
    )


# ---------------- Training command ----------------


def _build_training_command(
    dataset_name: str,
    model_type: str,
    model_name: str,
    training_params: dict,
) -> list[str]:
    """Build the LeRobot training command.

    Mirrors the arg pattern from physical_ai_server/training/training_manager.py.
    """
    output_dir = str(OUTPUT_DIR / model_name.replace("/", "_"))

    # Clean output directory if it exists from a previous attempt (retry safety).
    # LeRobot raises FileExistsError if output_dir exists and resume=False.
    if os.path.isdir(output_dir):
        try:
            shutil.rmtree(output_dir)
        except OSError as e:
            print(f"Warning: could not clean output dir {output_dir}: {e}")

    cmd = [
        sys.executable,
        "-m",
        "lerobot.scripts.train",
        f"--policy.type={model_type}",
        "--policy.device=cuda",
        f"--dataset.repo_id={dataset_name}",
        f"--output_dir={output_dir}",
        "--policy.push_to_hub=false",
        # Disable eval — no simulation env available on cloud worker.
        "--eval_freq=0",
    ]

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


# ---------------- HuggingFace upload ----------------


def _upload_model_to_hf(model_name: str, hf_token: str) -> str:
    """Upload trained model checkpoint via upload_large_folder.

    upload_large_folder splits the upload into chunks, retries failed chunks,
    and skips files already on the hub — so a transient network failure during
    a multi-GB upload no longer kills the entire training job.
    """
    hf_api = HfApi(token=hf_token)

    # Create repo if it doesn't exist (idempotent).
    hf_api.create_repo(repo_id=model_name, repo_type="model", exist_ok=True)

    output_path = OUTPUT_DIR / model_name.replace("/", "_")

    # LeRobot saves to checkpoints/last/pretrained_model/
    checkpoint_dir = output_path / "checkpoints" / "last" / "pretrained_model"
    if not checkpoint_dir.exists():
        for p in output_path.rglob("pretrained_model"):
            checkpoint_dir = p
            break

    if not checkpoint_dir.exists():
        raise FileNotFoundError(
            f"No pretrained_model directory found in {output_path}"
        )

    # Note: LeRobot already writes config.json with `input_features` (which
    # includes every observation.images.* key the model expects). The inference
    # overlay reads that file directly, so no separate camera_config.json is
    # needed — it would be a second source of truth for data that already exists.

    hf_api.upload_large_folder(
        repo_id=model_name,
        folder_path=str(checkpoint_dir),
        repo_type="model",
    )

    info = hf_api.repo_info(repo_id=model_name, repo_type="model")
    if not info:
        raise RuntimeError(
            f"Upload verification failed: repo {model_name} not found after upload"
        )

    return f"https://huggingface.co/{model_name}"


# ---------------- Cleanup + signal handler ----------------


def _cleanup_output(model_name: str) -> None:
    """Always remove the per-job output directory. Disk fills up otherwise."""
    try:
        path = OUTPUT_DIR / model_name.replace("/", "_")
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        print(f"Warning: cleanup of {model_name} failed: {e}", flush=True)


def _on_sigterm(signum, frame):
    """Pod eviction handler. Mark training as failed and clean up before death."""
    job = _current_job
    if not job:
        sys.exit(0)
    print(f"[SIGTERM] Worker eviction received, marking training {job['training_id']} as failed", flush=True)

    # Try to kill the LeRobot subprocess if it's still running.
    proc = job.get("proc")
    if proc is not None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    try:
        _update_supabase_status(
            job["supabase_url"],
            job["supabase_anon_key"],
            job["worker_token"],
            job["training_id"],
            "failed",
            "Worker wurde vom Cloud-Anbieter beendet (SIGTERM). "
            "Bitte Training neu starten.",
        )
    except Exception as e:
        print(f"[SIGTERM] supabase update failed: {e}", flush=True)

    _cleanup_output(job.get("model_name", ""))
    sys.exit(0)


# Register once at module load. Only the main thread can install handlers.
try:
    signal.signal(signal.SIGTERM, _on_sigterm)
except (ValueError, OSError):
    # Not in main thread (e.g. when imported by tests). Skip silently.
    pass


# ---------------- Main handler ----------------


def handler(job):
    """RunPod serverless handler for training jobs."""
    global _current_job

    job_input = job["input"]

    dataset_name = job_input["dataset_name"]
    model_name = job_input["model_name"]
    model_type = job_input["model_type"]
    training_params = job_input.get("training_params", {})
    training_id = job_input["training_id"]
    supabase_url = job_input["supabase_url"]
    supabase_anon_key = job_input["supabase_anon_key"]
    worker_token = job_input["worker_token"]
    hf_token = job_input.get("hf_token", "")

    _current_job = {
        "supabase_url": supabase_url,
        "supabase_anon_key": supabase_anon_key,
        "worker_token": worker_token,
        "training_id": training_id,
        "model_name": model_name,
        "proc": None,
    }

    if hf_token:
        login(token=hf_token)

    proc = None
    try:
        # ----- 1. Preflight dataset (cheap, catches schema/auth issues early) -----
        try:
            _preflight_dataset(dataset_name, hf_token)
        except ValueError as e:
            _update_supabase_status(
                supabase_url, supabase_anon_key, worker_token, training_id,
                "failed", str(e),
            )
            return {"status": "failed", "error": str(e)}

        # ----- 2. Mark running -----
        _update_supabase_status(
            supabase_url, supabase_anon_key, worker_token, training_id, "running"
        )

        total_steps = training_params.get("steps", 100000)

        # ----- 3. Spawn LeRobot training subprocess -----
        cmd = _build_training_command(dataset_name, model_type, model_name, training_params)
        print(f"Running training command: {' '.join(cmd)}")

        # Pass HF_TOKEN only to the subprocess env, not the handler's global env.
        # Limits exposure of the secret in /proc/<handler-pid>/environ.
        subprocess_env = {**os.environ}
        if hf_token:
            subprocess_env["HF_TOKEN"] = hf_token

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=subprocess_env,
        )
        _current_job["proc"] = proc

        # Bounded ring buffer — long failures previously OOM'd the worker.
        # 4000 lines @ ~80B = ~320 KB max.
        stderr_lines: deque[str] = deque(maxlen=4000)
        last_progress_step = -1
        step_pattern = re.compile(r"step[:\s]+(\d+\.?\d*[KMBkmb]?)")
        loss_pattern = re.compile(r"loss[:\s]+([\d.]+(?:e[+-]?\d+)?)")

        def _read_stdout():
            nonlocal last_progress_step
            try:
                for line in proc.stdout:
                    print(line, end="")
                    step_match = step_pattern.search(line)
                    if not step_match:
                        continue
                    step = _parse_abbreviated_number(step_match.group(1))
                    if step is None:
                        continue
                    loss_match = loss_pattern.search(line)
                    loss = _safe_float(loss_match.group(1)) if loss_match else None
                    if step <= last_progress_step:
                        continue
                    last_progress_step = step
                    for _attempt in range(3):
                        try:
                            _update_supabase_progress(
                                supabase_url, supabase_anon_key, worker_token,
                                training_id, step, total_steps, loss,
                            )
                            break
                        except Exception as _e:
                            print(
                                f"Warnung: Supabase Update fehlgeschlagen "
                                f"(Versuch {_attempt + 1}/3): {_e}"
                            )
                            if _attempt < 2:
                                time.sleep(2 ** _attempt)
            except UnicodeDecodeError as e:
                print(f"Warning: Error decoding subprocess output: {e}")

        def _read_stderr():
            try:
                for line in proc.stderr:
                    stderr_lines.append(line)
            except Exception as e:
                print(f"Warning: stderr reader crashed: {e}")

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        # Wait for process with timeout protection (default 5h, configurable).
        timeout_hours = training_params.get("timeout_hours", 5)
        try:
            proc.wait(timeout=timeout_hours * 3600)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)  # reap zombie
            except subprocess.TimeoutExpired:
                pass
            _update_supabase_status(
                supabase_url, supabase_anon_key, worker_token, training_id, "failed",
                f"Training Zeitlimit ueberschritten ({timeout_hours}h Limit)",
            )
            return {"status": "failed", "error": f"Training timed out ({timeout_hours}h limit)"}

        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        stderr_text = "".join(stderr_lines)

        if proc.returncode != 0:
            if len(stderr_text) > 2000:
                error_msg = stderr_text[:1000] + "\n...[truncated]...\n" + stderr_text[-1000:]
            else:
                error_msg = stderr_text or "Unknown error"
            _update_supabase_status(
                supabase_url, supabase_anon_key, worker_token, training_id,
                "failed", error_msg,
            )
            return {"status": "failed", "error": error_msg}

        # ----- 4. Training succeeded — push progress to 100% before upload -----
        _update_supabase_progress(
            supabase_url, supabase_anon_key, worker_token, training_id,
            total_steps, total_steps, None,
        )

        # ----- 5. Upload to HuggingFace (with built-in chunked retry) -----
        model_url = _upload_model_to_hf(model_name, hf_token)

        _update_supabase_status(
            supabase_url, supabase_anon_key, worker_token, training_id, "succeeded"
        )
        return {"status": "succeeded", "model_url": model_url}

    except Exception as e:
        err = str(e)
        error_msg = err[:1000] + "\n...[truncated]...\n" + err[-1000:] if len(err) > 2000 else err
        try:
            _update_supabase_status(
                supabase_url, supabase_anon_key, worker_token, training_id,
                "failed", error_msg,
            )
        except Exception as inner:
            print(f"Failed to mark training as failed: {inner}")
        return {"status": "failed", "error": error_msg}

    finally:
        # Always clean up disk + clear in-flight job reference,
        # even on success or unexpected exit. Disk exhaustion was a real risk.
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        _cleanup_output(model_name)
        _current_job = None


runpod.serverless.start({"handler": handler})
