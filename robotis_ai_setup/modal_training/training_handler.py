"""EduBotics cloud training handler (Modal worker body and the RunPod trainer).

Invoked by `modal_app.train` / `modal_app.train_vla` (legacy mode, `run_training`)
and by `modal_app.train_managed` and the RunPod trainer image's `entry.py`
(managed mode, `run_managed`, ACT only; spec §4.7). Managed mode is at the end
of this file.

Legacy responsibilities:
  - Preflight the HuggingFace dataset (schema, codebase_version, joints, cameras).
  - Spawn `python -m lerobot.scripts.lerobot_train` and stream its stdout.
  - Parse `step: N loss: X.Y` lines and push progress to Supabase via the
    scoped `update_training_progress` RPC (anon key + per-row worker_token).
  - Upload the trained checkpoint to HuggingFace Hub on success.
  - On SIGINT (Modal preemption/cancel/timeout, 30s grace) or SIGTERM
    (belt-and-suspenders), mark the row failed and clean up.

Credentials (SUPABASE_URL, SUPABASE_ANON_KEY, HF_TOKEN) come from the Modal
Secret `edubotics-training-secrets` via os.environ. Per-training values
(dataset_name, worker_token, ...) are passed as function kwargs.
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
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download, login
from huggingface_hub.utils import (
    HfHubHTTPError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)
from supabase import create_client


OUTPUT_DIR = Path("/tmp/training_output")

# The device behind --policy.device=. A module constant so the CPU proof harness
# (runpod_training/e2e/harness_entry.py) can switch it; production never does.
TRAIN_DEVICE = "cuda"

# S16 (spec §3.7), the one German database sentence of this file: the LEGACY path
# writes it itself through update_training_progress when the host check fails.
# Byte-equal to cloud_training_api/app/services/training_texts_de.py::S16.
# Managed mode writes no German sentence to the database (E11).
LEGACY_HOST_UNFIT_DE = (
    "Der Cloud-GPU-Server ist nicht nutzbar (GPU oder Speicherplatz fehlt). "
    "Das Training wurde nicht gestartet – das Trainingsguthaben wurde erstattet."
)

# ---------------- ROBOTIS OMX expected schema ----------------
# LeRobot v0.5.1 ships codebase_version="v3.0" (moved out of lerobot_dataset.py
# into lerobot/datasets/dataset_metadata.py). v2.1 datasets recorded with the
# pre-v2.5.0 EduBotics stack are NOT compatible — students must re-record.
EXPECTED_CODEBASE_VERSION = "v3.0"
MIN_JOINTS = 4
MAX_JOINTS = 20

# Normalization stats keys required on observation.state + action.
# lerobot_train TRUSTS the dataset's baked meta/stats.json — it never recomputes
# stats at train start — so a missing/incomplete stats.json crashes training at
# processor build AFTER the (expensive, A100 for VLAs) GPU has spun up. We reject
# in preflight instead. The required set is the UNION across every policy's
# normalization mode, so the check is policy-independent and drift-proof:
#   MEAN_STD (act/pi0/pi0_fast/smolvla) -> mean,std
#   MIN_MAX  (diffusion/vqbet, tdmpc action) -> min,max
#   QUANTILES (pi05 state+action) -> q01,q99
# LeRobot v0.5.1's RunningQuantileStats writes ALL of these together for every
# real recording, so requiring the union is zero-false-positive on a normally
# recorded EduBotics dataset and only rejects corrupt/legacy/hand-built stats.
_REQUIRED_STATS_KEYS = ("mean", "std", "min", "max", "q01", "q99")

# VLA policies whose LeRobot config is language-conditioned: they build the
# prompt from a per-frame task string and raise ValueError("No task found in
# complementary data") deep in training if the dataset has none. The preflight
# rejects a task-less dataset for these BEFORE the (expensive A100) GPU spins
# up. ACT-class policies have no task requirement and are not gated.
_LANGUAGE_CONDITIONED_POLICIES = frozenset({"pi0", "pi05", "pi0_fast", "smolvla"})

# Module-level reference to the in-flight job. Used by the signal handler to
# mark the training as failed and clean up before the container is killed.
# Only ever one job in flight per Modal container invocation.
_current_job: dict | None = None
# Managed mode's LeRobot process, killed by the signal handler (no DB write).
_managed_proc = None


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
    """Parse LeRobot's abbreviated numbers: '50K' -> 50000, '1.5M' -> 1500000.

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


def _is_terminal_cancel_error(exc: BaseException) -> bool:
    """True iff a progress-RPC failure means the training row is now terminal.

    update_training_progress (migration 010/028) raises ERRCODE P0001 with
    "Invalid worker token, training not found, or training already terminal"
    when the Cloud API has marked this training canceled/failed and nulled the
    worker_token. That is the worker's only in-band signal that it was canceled
    (it has no SELECT access to the row — anon key + scoped RPC only). When the
    worker sees it, it must stop training and let the container exit so the GPU
    is released — otherwise a cancel whose Modal-side terminate failed leaves
    the job running to its timeout_hours cap ("cancel just continues on Modal").

    Matched defensively on the PG error code AND the message text so it survives
    supabase-py error-shape changes. Transient network/5xx errors do NOT match,
    so a flaky Supabase still gets the normal bounded retry, not a false stop.
    """
    s = str(exc)
    return (
        "P0001" in s
        or "already terminal" in s
        or "Invalid worker token" in s
    )


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
    log_url: str | None = None,
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
        # 028: full-log pointer; the RPC persists it only on the terminal
        # transition (succeeded/failed/canceled).
        "p_log_url": log_url,
    }
    client.rpc("update_training_progress", payload).execute()


def _update_supabase_status(
    supabase_url: str,
    supabase_anon_key: str,
    worker_token: str,
    training_id: int,
    status: str,
    error_message: str | None = None,
    log_url: str | None = None,
):
    """Update training status in Supabase via the scoped RPC."""
    _call_progress_rpc(
        supabase_url, supabase_anon_key, worker_token, training_id,
        status=status, error_message=error_message, log_url=log_url,
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


def _update_status_with_retry(
    supabase_url: str,
    supabase_anon_key: str,
    worker_token: str,
    training_id: int,
    status: str,
    error_message: str | None = None,
    log_url: str | None = None,
    attempts: int = 3,
) -> bool:
    """Terminal status write with bounded retry (mirrors the progress retry).

    The progress reader already retries 3x, but the TERMINAL failed/succeeded
    writes used to be single-shot. A dropped terminal write left the row
    'running' — and because the Modal function returns normally even on an
    application-level failure, the API reconciler then mapped that return to
    'succeeded', stamping success over a failed run (no model on HF, credit
    wrongly consumed). Retrying closes that window.

    Stops early (returns False, no retry) if the RPC reports the row is already
    terminal API-side (P0001 — e.g. canceled): that's not a transient failure,
    and the row is correctly terminal. Returns True iff the write landed.
    """
    for attempt in range(attempts):
        try:
            _update_supabase_status(
                supabase_url, supabase_anon_key, worker_token, training_id,
                status, error_message, log_url,
            )
            return True
        except Exception as e:
            if _is_terminal_cancel_error(e):
                print(
                    f"Status-Update '{status}' uebersprungen — Zeile bereits "
                    f"terminal (serverseitig): {e}",
                    flush=True,
                )
                return False
            print(
                f"Warnung: Status-Update '{status}' fehlgeschlagen "
                f"(Versuch {attempt + 1}/{attempts}): {e}",
                flush=True,
            )
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    return False


# ---------------- Dataset preflight ----------------


def _stat_value(stats: dict, feature: str, key: str):
    """Read a single stat from a meta/stats.json dict, tolerant of both shapes.

    On disk LeRobot v0.5.1 serializes stats FLAT with '/'-joined keys
    (serialize_dict -> flatten_dict), e.g. "observation.state/mean". Older /
    hand-built files may use the nested {feature: {key: ...}} shape. Accept both.
    Returns None when absent.
    """
    flat = stats.get(f"{feature}/{key}")
    if flat is not None:
        return flat
    nested = stats.get(feature)
    if isinstance(nested, dict):
        return nested.get(key)
    return None


def _validate_stats(stats: dict, dataset_name: str, n_joints: int) -> None:
    """Reject a dataset whose normalization stats are missing/incomplete.

    Requires observation.state + action to each carry every key in
    _REQUIRED_STATS_KEYS as a list of length == n_joints. Raises ValueError with
    a German operator-facing message on failure.
    """
    if not isinstance(stats, dict):
        raise ValueError(
            f"Dataset '{dataset_name}' hat eine ungueltige meta/stats.json "
            f"(kein JSON-Objekt). Bitte mit aktueller Recording-Software neu "
            f"aufnehmen."
        )
    for feature in ("observation.state", "action"):
        for key in _REQUIRED_STATS_KEYS:
            value = _stat_value(stats, feature, key)
            if not isinstance(value, list) or len(value) != n_joints:
                raise ValueError(
                    f"Dataset '{dataset_name}' hat unvollstaendige "
                    f"Normalisierungs-Statistiken (meta/stats.json: '{feature}' "
                    f"'{key}' fehlt oder hat die falsche Laenge). Das Modell "
                    f"wuerde mit falscher Normalisierung trainieren. Bitte mit "
                    f"aktueller Recording-Software neu aufnehmen."
                )


def _preflight_dataset(dataset_name: str, hf_token: str, model_type: str = "") -> dict:
    """Download just meta/info.json and validate the dataset is trainable.

    Returns the parsed meta/info.json dict on success.

    Catches the following failure modes BEFORE we waste 10+ GPU minutes:
      - Dataset doesn't exist or worker token can't see it
      - Malformed meta/info.json (missing fields, bad JSON)
      - codebase_version mismatch (recording software is too old/new)
      - fps missing or zero (LeRobot data loader would explode)
      - observation.state or action missing entirely
      - Joint count out of reasonable bounds (< MIN_JOINTS or > MAX_JOINTS)
      - observation.state and action use different joint names (paired bug)
      - No camera features at all
      - For language-conditioned VLAs (pi0/pi05/pi0_fast/smolvla): no task
        strings (info.total_tasks == 0), which LeRobot would only surface deep
        in training as ValueError("No task found in complementary data").

    Raises ValueError with a German operator-facing message on failure.
    """
    # Run the HF download in a background thread and bail after 60s. Without
    # this, a stalled HF Hub (known to go slow / 503 under load) can block the
    # worker for the full 7-hour Modal function timeout, burning GPU credits.
    download_result: dict = {}

    # Everything the preflight reads is pinned to the SAME revision the
    # trainer will read: lerobot 0.5.1 loads datasets at revision
    # CODEBASE_VERSION ('v3.0'), never main HEAD. An unpinned preflight
    # validates whatever a re-upload last pushed to main while the GPU
    # trains the tagged snapshot — the two can diverge silently.
    def _download_worker():
        try:
            download_result["path"] = hf_hub_download(
                repo_id=dataset_name,
                filename="meta/info.json",
                repo_type="dataset",
                revision=EXPECTED_CODEBASE_VERSION,
                token=hf_token,
            )
        except BaseException as exc:  # propagate to main thread
            download_result["error"] = exc

    thread = threading.Thread(target=_download_worker, daemon=True)
    thread.start()
    thread.join(timeout=60)

    if thread.is_alive():
        raise ValueError(
            f"Dataset '{dataset_name}' Preflight hat das Zeitlimit (60s) "
            f"ueberschritten — HuggingFace Hub erreichbar? Bitte spaeter "
            f"erneut starten."
        )
    err = download_result.get("error")
    if isinstance(err, RepositoryNotFoundError):
        raise ValueError(
            f"Dataset '{dataset_name}' wurde auf HuggingFace nicht gefunden "
            f"oder ist privat (Worker hat keinen Zugriff)."
        )
    if isinstance(err, RevisionNotFoundError):
        raise ValueError(
            f"Dataset '{dataset_name}' hat keinen "
            f"'{EXPECTED_CODEBASE_VERSION}'-Versions-Tag auf HuggingFace — "
            f"das Training laedt den Datensatz an genau diesem Tag. "
            f"Bitte den Datensatz mit der EduBotics-App erneut hochladen."
        )
    if isinstance(err, HfHubHTTPError):
        raise ValueError(
            f"Dataset '{dataset_name}' konnte nicht geladen werden: {err}"
        )
    if err is not None:
        raise ValueError(
            f"Dataset '{dataset_name}' Preflight fehlgeschlagen: {err}"
        )
    info_path = download_result.get("path")
    if info_path is None:
        raise ValueError(
            f"Dataset '{dataset_name}' Preflight lieferte keinen Pfad zurueck."
        )

    try:
        with open(info_path) as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(
            f"Dataset '{dataset_name}' hat ein ungueltiges meta/info.json: {e}"
        )

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

    fps = info.get("fps")
    if not fps or fps <= 0:
        raise ValueError(
            f"Dataset '{dataset_name}' hat keine gueltige 'fps' Angabe ({fps!r})."
        )

    total_episodes = info.get("total_episodes")
    if not total_episodes or total_episodes < 1:
        raise ValueError(
            f"Dataset '{dataset_name}' enthaelt keine Episoden "
            f"(total_episodes={total_episodes!r}). Bitte zuerst aufnehmen "
            f"und hochladen."
        )

    features = info.get("features") or {}

    def _get_joint_names(feature_key: str, label: str) -> list:
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
        if not (MIN_JOINTS <= len(names) <= MAX_JOINTS):
            raise ValueError(
                f"Dataset '{dataset_name}' hat {len(names)} {label}-Gelenke — "
                f"erwartet werden {MIN_JOINTS}-{MAX_JOINTS}. Aufnahme pruefen."
            )
        return names

    state_names = _get_joint_names("observation.state", "Follower")
    action_names = _get_joint_names("action", "Action")

    if state_names != action_names:
        raise ValueError(
            f"Dataset '{dataset_name}' hat unterschiedliche Gelenk-Namen "
            f"fuer observation.state und action. "
            f"state: {state_names}, action: {action_names}. "
            f"Aufnahme ist beschaedigt — bitte neu aufnehmen."
        )

    image_keys = [k for k in features if k.startswith("observation.images.")]
    if not image_keys:
        raise ValueError(
            f"Dataset '{dataset_name}' enthaelt keine Kamera-Features. "
            f"Mindestens eine Kamera ist erforderlich."
        )
    cameras = [k.replace("observation.images.", "") for k in image_keys]

    # Normalization stats: lerobot_train trusts the dataset's baked
    # meta/stats.json (it never recomputes). Download + validate it now so an
    # incomplete/missing stats file fails cheaply here instead of crashing at
    # processor build after the GPU has spun up. The repo is already proven
    # reachable by the info.json download above, so a direct call is fine.
    try:
        stats_path = hf_hub_download(
            repo_id=dataset_name,
            filename="meta/stats.json",
            repo_type="dataset",
            revision=EXPECTED_CODEBASE_VERSION,
            token=hf_token,
        )
        with open(stats_path) as f:
            stats = json.load(f)
    except (OSError, json.JSONDecodeError, HfHubHTTPError,
            RepositoryNotFoundError) as e:
        raise ValueError(
            f"Dataset '{dataset_name}' hat keine ladbare meta/stats.json "
            f"({e}). Ohne Normalisierungs-Statistiken kann nicht trainiert "
            f"werden. Bitte mit aktueller Recording-Software neu aufnehmen."
        )
    _validate_stats(stats, dataset_name, len(state_names))

    # Layout completeness at the training revision: a crash mid-upload can
    # leave meta/info.json + meta/stats.json on the hub while the data
    # parquet, episode metadata, or videos are missing — every check above
    # still passes, and training then burns GPU minutes before failing at
    # dataset load. Require the moving parts to actually exist.
    try:
        repo_files = HfApi(token=hf_token).list_repo_files(
            dataset_name, repo_type="dataset",
            revision=EXPECTED_CODEBASE_VERSION,
        )
    except Exception as e:
        raise ValueError(
            f"Dataset '{dataset_name}' Dateiliste konnte nicht geladen "
            f"werden: {e}"
        )

    def _layout_has(prefix: str, suffix: str) -> bool:
        return any(
            f.startswith(prefix) and f.endswith(suffix) for f in repo_files
        )

    missing_parts = []
    if not _layout_has("data/", ".parquet"):
        missing_parts.append("data/*.parquet")
    if not _layout_has("meta/episodes/", ".parquet"):
        missing_parts.append("meta/episodes/*.parquet")
    for image_key in image_keys:
        if not _layout_has(f"videos/{image_key}/", ".mp4"):
            missing_parts.append(f"videos/{image_key}/*.mp4")
    if missing_parts:
        raise ValueError(
            f"Dataset '{dataset_name}' ist unvollstaendig hochgeladen — es "
            f"fehlen: {', '.join(missing_parts)}. Vermutlich wurde der "
            f"Upload unterbrochen. Bitte den Datensatz mit der EduBotics-App "
            f"erneut hochladen."
        )

    # Language-conditioned VLAs need per-frame task strings. info.json carries
    # total_tasks (== number of distinct prompts); 0 means the dataset was
    # recorded without any Aufgabenanweisung and would crash these policies
    # deep in training. Gate only for those policies — ACT et al. don't care.
    if (model_type or "").lower() in _LANGUAGE_CONDITIONED_POLICIES:
        total_tasks = info.get("total_tasks")
        if not total_tasks or total_tasks < 1:
            raise ValueError(
                f"Dataset '{dataset_name}' enthaelt keine Aufgaben-Texte "
                f"(total_tasks={total_tasks!r}), die das Modell '{model_type}' "
                f"benoetigt. Bitte mit einer Aufgabenanweisung neu aufnehmen "
                f"oder ein ACT-Modell waehlen."
            )

    print(
        f"Preflight OK: dataset='{dataset_name}' codebase_version={version} "
        f"fps={fps} joints={state_names} cameras={cameras} "
        f"total_tasks={info.get('total_tasks')}"
    )
    # The parsed info.json rides back to run_training so the final model
    # artifact can carry the dataset fps (edubotics_model_meta.json) — the
    # inference side uses it to refuse a tick rate that would time-scale
    # the policy vs its training data.
    return info


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

    if os.path.isdir(output_dir):
        try:
            shutil.rmtree(output_dir)
        except OSError as e:
            print(f"Warning: could not clean output dir {output_dir}: {e}")

    cmd = [
        sys.executable,
        "-m",
        # v0.5.1: the legacy `lerobot.scripts:train` module was renamed to
        # `lerobot.scripts.lerobot_train` (PR #2033, v0.4.0). The old module
        # is GONE — no back-compat alias.
        "lerobot.scripts.lerobot_train",
        f"--policy.type={model_type}",
        f"--policy.device={TRAIN_DEVICE}",
        f"--dataset.repo_id={dataset_name}",
        f"--output_dir={output_dir}",
        "--policy.push_to_hub=false",
        # Disable eval — no simulation env available on cloud worker.
        "--eval_freq=0",
        # Image augmentation is deliberately DISABLED — we leave LeRobot at its
        # enable=False default by NOT emitting --dataset.image_transforms.enable.
        # In v0.5.1 the default transform pool includes a GEOMETRIC `affine`
        # (RandomAffine ±5°/5% translate) at weight 1.0 alongside the photometric
        # jitter; on fixed gripper/scene cameras feeding an absolute-action
        # policy that warps the very image→action geometry the policy must learn.
        # There is no per-key CLI override on v0.5.1 (the `tfs` dict is parsed
        # atomically by draccus), so enabling only the photometric subset isn't
        # reachable from the CLI — we disable augmentation entirely rather than
        # ship the geometric warp. (Earlier code emitted enable=true with a
        # comment that wrongly claimed the set was photometric-only.)
    ]

    # Audit F64: ACT-specific inference-quality default. With chunk_size=100 at
    # 30 fps the policy commits to 3.3 s of open-loop action between queries —
    # any small prediction error compounds. Setting n_action_steps=15 makes the
    # policy re-query the world every 0.5 s while still predicting the full
    # 100-step chunk during training (the smoothness benefit of chunking is
    # preserved). User can override by passing `n_action_steps` in
    # training_params. NOTE: only safe for `act`; other policies have their own
    # chunk-vs-step semantics.
    # Audit F66: only apply the F64 ACT default *and* only forward an override
    # when model_type == "act". A diffusion/vqbet/smolvla/pi0 job that happens
    # to carry n_action_steps in training_params would otherwise emit
    # --policy.n_action_steps=N for a policy whose ACTConfig-style validator
    # doesn't exist — flagged by the F64 verifier as a real cross-policy leak.
    # The None / 0 / negative guard is the same hardening — those would
    # otherwise produce the literal string "None" on the CLI or crash inside
    # ACTConfig.__post_init__ with an opaque error.
    n_action_steps_override = training_params.get("n_action_steps")
    if model_type == "act":
        if n_action_steps_override is None:
            cmd.append("--policy.n_action_steps=15")
        elif isinstance(n_action_steps_override, int) and n_action_steps_override > 0:
            cmd.append(f"--policy.n_action_steps={n_action_steps_override}")
        else:
            # Invalid override (None already handled, plus 0/negative/non-int)
            # → fall back to the F64 default rather than emit a broken CLI arg.
            cmd.append("--policy.n_action_steps=15")

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
        if value is not None:
            cmd.append(f"{cli_flag}={value}")

    # VLA fine-tune recipe (pi0/pi05/pi0_fast/smolvla): the Cloud API injects the
    # per-policy base-checkpoint + precision/memory flags here as fully-formed
    # --policy.* strings (app.services.policy_profile). Without a base checkpoint
    # these VLAs train from random init and OOM. The set is curated per policy
    # (e.g. SmolVLAConfig has no dtype/gradient_checkpointing field), so the
    # worker just appends the pre-validated strings — and only --policy.* ones,
    # so a malformed payload can't inject arbitrary CLI args.
    policy_flags = training_params.get("policy_cli_flags")
    if isinstance(policy_flags, list):
        for flag in policy_flags:
            if isinstance(flag, str) and flag.startswith("--policy."):
                cmd.append(flag)

    return cmd


# Log lines that mean the base checkpoint did NOT fully load. Two distinct
# v0.5.1 code paths produce them (subprocess runs with stderr merged into
# stdout, so logging.warning lines are captured too):
#   - pi0/pi05/pi0_fast define their own from_pretrained that wraps the
#     weight load in a broad try/except, prints "Could not load state dict",
#     and continues on a RANDOMLY-INITIALIZED model.
#   - smolvla has NO from_pretrained override: the base
#     PreTrainedPolicy.from_pretrained loads with strict=False, where
#     missing/unexpected KEYS are only logging.warning'd
#     (policies/utils.py log_model_loading_keys) and training continues
#     with those submodules randomly initialized. (Shape mismatches still
#     raise loudly; only key mismatches are silent.)
_PRETRAINED_LOAD_FAILURE_MARKERS = (
    "Could not load state dict",
    "Missing key(s) when loading model",
    "Unexpected key(s) when loading model",
)


def _pretrained_load_failed(output_text: str, training_params: dict) -> bool:
    """True iff a VLA base-checkpoint run silently fell back to random init.

    For the VLA recipe (pi0/pi05/pi0_fast/smolvla) the Cloud API injects
    --policy.pretrained_path=<base>. On v0.5.1 a failed/partial base-weight
    load does not stop training — it "fine-tunes from scratch" and exits 0
    (see _PRETRAINED_LOAD_FAILURE_MARKERS for the per-policy mechanics).
    That is a silently-wrong model (no base knowledge), so when we detect
    any of those log lines on a pretrained run we fail the job instead of
    reporting success. Plain ACT-class runs carry no pretrained_path and
    are never gated.
    """
    flags = training_params.get("policy_cli_flags") or []
    is_pretrained_run = any(
        isinstance(f, str) and f.startswith("--policy.pretrained_path=")
        for f in flags
    )
    if not is_pretrained_run:
        return False
    text = output_text or ""
    return any(marker in text for marker in _PRETRAINED_LOAD_FAILURE_MARKERS)


# ---------------- HuggingFace upload ----------------


def _upload_training_log(
    model_name: str, hf_token: str, output_lines,
) -> str | None:
    """Persist the FULL worker stdout as training_log.txt in the model repo.

    leLab-comparison PR-5a: error_message keeps only a 2 KB truncated
    blob (the student-facing German path); the complete log is the
    teacher/admin forensics artifact. Reuses the repo + token the model
    upload already owns — zero new infra, and the repo is private by
    default. Returns the hf.co blob URL, or None on ANY failure: the log
    is telemetry and must never change the job outcome.
    """
    try:
        hf_api = HfApi(token=hf_token)
        hf_api.create_repo(repo_id=model_name, repo_type="model", exist_ok=True)
        log_bytes = "".join(output_lines).encode("utf-8", errors="replace")
        hf_api.upload_file(
            path_or_fileobj=log_bytes,
            path_in_repo="training_log.txt",
            repo_id=model_name,
            repo_type="model",
        )
        return f"https://huggingface.co/{model_name}/blob/main/training_log.txt"
    except Exception as e:  # noqa: BLE001 — never fail the job over the log
        print(f"Warnung: Trainingslog-Upload fehlgeschlagen: {e}")
        return None


def _start_checkpoint_watcher(
    model_name: str, hf_token: str, output_path: Path, stop_event,
):
    """Upload intermediate checkpoints to the Hub as LeRobot writes them.

    leLab-comparison PR-5a (mirrors leLab's hf_cloud sidecar): lerobot
    saves checkpoints/<step>/ every save_freq steps (upstream default
    20_000, save_checkpoint defaults True) but EduBotics only shipped
    checkpoints/last at the very end — a crashed/canceled long run left
    nothing. The watcher polls every 60 s and uploads each NEW numeric
    checkpoint's pretrained_model to checkpoints/<step>/pretrained_model
    in the model repo (teacher-side introspection lists them via
    list_repo_files). Best-effort: failures log and retry next tick;
    MAX_STEPS=500k / save_freq=20k bounds this at <=25 uploads.
    Returns the daemon thread (joined briefly at shutdown).
    """
    def _watch():
        uploaded: set[str] = set()
        hf_api = HfApi(token=hf_token)
        repo_ready = False
        while not stop_event.wait(60):
            try:
                ckpt_root = output_path / "checkpoints"
                if not ckpt_root.is_dir():
                    continue
                for step_dir in sorted(ckpt_root.iterdir()):
                    if (not step_dir.is_dir()
                            or not step_dir.name.isdigit()
                            or step_dir.name in uploaded):
                        continue
                    pretrained = step_dir / "pretrained_model"
                    if not pretrained.is_dir():
                        continue
                    if not repo_ready:
                        hf_api.create_repo(
                            repo_id=model_name, repo_type="model",
                            exist_ok=True)
                        repo_ready = True
                    print(f"Checkpoint-Watcher: lade Schritt "
                          f"{step_dir.name} hoch ...", flush=True)
                    hf_api.upload_folder(
                        repo_id=model_name,
                        repo_type="model",
                        folder_path=str(pretrained),
                        path_in_repo=(
                            f"checkpoints/{step_dir.name}/pretrained_model"),
                    )
                    uploaded.add(step_dir.name)
            except Exception as e:  # noqa: BLE001 — retry next tick
                print(f"Warnung: Checkpoint-Upload fehlgeschlagen "
                      f"(nächster Versuch in 60 s): {e}")

    thread = threading.Thread(target=_watch, daemon=True)
    thread.start()
    return thread


def _upload_model_to_hf(
    model_name: str, hf_token: str, model_meta: dict | None = None,
) -> str:
    """Upload trained model checkpoint via upload_large_folder.

    upload_large_folder splits the upload into chunks, retries failed chunks,
    and skips files already on the hub — so a transient network failure during
    a multi-GB upload no longer kills the entire training job.
    """
    hf_api = HfApi(token=hf_token)

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

    # What LeRobot's checkpoint does NOT carry is anything temporal: the
    # policy config has no fps, so the inference side cannot know the rate
    # the training data was recorded at. Stamp the dataset fps (from the
    # preflighted meta/info.json) next to config.json so it rides the same
    # upload; the node preflights its tick rate against it.
    if model_meta:
        try:
            meta_path = Path(checkpoint_dir) / "edubotics_model_meta.json"
            meta_path.write_text(
                json.dumps(model_meta, indent=2), encoding="utf-8"
            )
        except OSError as e:
            print(
                f"Warnung: edubotics_model_meta.json konnte nicht "
                f"geschrieben werden: {e}",
                flush=True,
            )

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


def _on_shutdown(signum, frame):
    """Container shutdown handler.

    Modal sends SIGINT (30s grace) on preemption, FunctionCall.cancel, and
    when the function timeout is hit. We also handle SIGTERM as a safety net.
    Mark training as failed and clean up before the container is killed.
    """
    job = _current_job
    if not job:
        # Managed mode (spec §4.7 step 10): kill LeRobot, exit, NO database
        # write. The API's sweep decides what happens to the attempt.
        managed = _managed_proc
        if managed is not None:
            try:
                managed.kill()
                managed.wait(timeout=5)
            except Exception:
                pass
        sys.exit(0)
    print(
        f"[shutdown sig={signum}] marking training {job['training_id']} as failed",
        flush=True,
    )

    proc = job.get("proc")
    if proc is not None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    # Retry the status update 3x with short backoff. We're inside Modal's
    # 30s SIGINT grace; the previous backoff (1+2+4 = 7s sleeps + RPC
    # latency + a 5s proc.wait above) could exceed it under a slow
    # Supabase, leaving the row stuck. Total sleep budget here is now
    # ~1.5s (0.5 + 1.0); the API-side stalled-worker sweep catches any
    # remaining zombie rows.
    for attempt in range(3):
        try:
            _update_supabase_status(
                job["supabase_url"],
                job["supabase_anon_key"],
                job["worker_token"],
                job["training_id"],
                "failed",
                "Worker wurde vom Cloud-Anbieter beendet. "
                "Bitte Training neu starten.",
            )
            break
        except Exception as e:
            print(
                f"[shutdown] supabase update attempt {attempt + 1}/3 "
                f"failed: {e}",
                flush=True,
            )
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))

    _cleanup_output(job.get("model_name", ""))
    sys.exit(0)


# Register on module import. Modal sends SIGINT on preemption/cancel/timeout;
# SIGTERM is kept as belt-and-suspenders. Only the main thread can install
# handlers — skip silently in non-main-thread imports (e.g. tests).
try:
    signal.signal(signal.SIGINT, _on_shutdown)
    signal.signal(signal.SIGTERM, _on_shutdown)
except (ValueError, OSError):
    pass


# ---------------- Main entry point ----------------


def run_training(
    dataset_name: str,
    model_name: str,
    model_type: str,
    training_params: dict,
    training_id: int,
    worker_token: str,
) -> dict:
    """Run a single training job. Returns {"status": "...", "model_url"|"error": ...}.

    Credentials (SUPABASE_URL, SUPABASE_ANON_KEY, HF_TOKEN) are read from env
    (injected by the Modal Secret `edubotics-training-secrets`).
    """
    global _current_job

    # Validate the Modal Secret `edubotics-training-secrets` actually
    # injected the values we need. With bare os.environ[K] a missing
    # secret raised KeyError mid-run, Modal marked the call FAILED, and
    # the student's UI showed "Training fehlgeschlagen" with no actionable
    # cause. Naming the missing var lets the on-call engineer fix the
    # Modal Secret in 30 seconds instead of bisecting the traceback.
    _missing = [
        k for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY")
        if not os.environ.get(k)
    ]
    if _missing:
        raise RuntimeError(
            "Modal Secret 'edubotics-training-secrets' is missing or has "
            f"empty values for: {', '.join(_missing)}. "
            "Re-sync via `modal secret create edubotics-training-secrets` "
            "with all of SUPABASE_URL, SUPABASE_ANON_KEY, HF_TOKEN."
        )
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_anon_key = os.environ["SUPABASE_ANON_KEY"]
    hf_token = os.environ.get("HF_TOKEN", "")

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
            dataset_info = _preflight_dataset(dataset_name, hf_token, model_type)
        except ValueError as e:
            _update_status_with_retry(
                supabase_url, supabase_anon_key, worker_token, training_id,
                "failed", str(e),
            )
            return {"status": "failed", "error": str(e)}

        # ----- 1b. Host check (spec §4.7 legacy mode): a GPU-less or too-old host fails
        # the row with S16 BEFORE it is marked running. No /tmp floor here (V2-2): the
        # free bytes are only printed, since nobody measured the legacy L4 / A100's /tmp. -----
        try:
            _assert_training_host(disk_floor=False)
        except HostUnfit as e:
            print(f"Host check failed ({e.reason}): {e.detail}", flush=True)
            _update_status_with_retry(
                supabase_url, supabase_anon_key, worker_token, training_id,
                "failed", LEGACY_HOST_UNFIT_DE,
            )
            return {"status": "failed", "error": LEGACY_HOST_UNFIT_DE}

        # ----- 2. Mark running -----
        _update_supabase_status(
            supabase_url, supabase_anon_key, worker_token, training_id, "running"
        )

        total_steps = training_params.get("steps", 100000)

        # Push an immediate 0/total point so the UI leaves the "Warte auf
        # GPU-Worker" state the instant we're running. LeRobot's first real log
        # line only lands at log_freq steps, and the cold start + dataset
        # download before it can take a minute+. No loss => the RPC records no
        # history point, it just sets total_steps/current_step + last_progress_at.
        try:
            _update_supabase_progress(
                supabase_url, supabase_anon_key, worker_token,
                training_id, 0, total_steps, None,
            )
        except Exception as _e:
            print(f"Warnung: Initiales Fortschritts-Update fehlgeschlagen: {_e}", flush=True)

        # ----- 3. Spawn LeRobot training subprocess -----
        cmd = _build_training_command(dataset_name, model_type, model_name, training_params)
        print(f"Running training command: {' '.join(cmd)}")

        # Pass HF_TOKEN only to the subprocess env.
        # PYTHONUNBUFFERED forces LeRobot to flush stdout/stderr immediately.
        subprocess_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if hf_token:
            subprocess_env["HF_TOKEN"] = hf_token

        # Merge stderr into stdout. LeRobot uses Python `logging` which writes
        # to stderr by default — if we kept them separate, the reader thread
        # would never see any progress lines.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=subprocess_env,
        )
        _current_job["proc"] = proc

        # Bounded ring buffer — long failures previously OOM'd the worker.
        # Shared between stdout display and error-report-on-failure.
        output_lines: deque[str] = deque(maxlen=4000)
        # Set by the reader thread when the scoped progress RPC reports the row
        # is terminal (canceled/failed API-side). Drives an early, clean exit so
        # the GPU is released on cancel even if the Modal-side terminate failed.
        cancel_detected = threading.Event()
        # Stops the liveness heartbeat thread. Always set BEFORE any terminal
        # status write so a late heartbeat can never race a succeeded/failed
        # transition (which nulls the worker_token → P0001 → false cancel).
        progress_stop = threading.Event()
        last_progress_step = -1
        step_pattern = re.compile(r"step[:\s]+(\d+\.?\d*[KMBkmb]?)")
        loss_pattern = re.compile(r"loss[:\s]+([\d.]+(?:e[+-]?\d+)?)")

        def _read_output():
            nonlocal last_progress_step
            try:
                for line in proc.stdout:
                    print(line, end="", flush=True)
                    output_lines.append(line)
                    step_match = step_pattern.search(line)
                    if not step_match:
                        continue
                    step = _parse_abbreviated_number(step_match.group(1))
                    if step is None:
                        continue
                    # LeRobot logs the step abbreviated + rounded
                    # (format_big_number(step, precision=0): 1500 -> "2K" -> 2000),
                    # so the parsed value can overshoot total_steps and the UI
                    # would render >100%. Clamp before reporting.
                    if total_steps and step > total_steps:
                        step = total_steps
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
                            # Row went terminal API-side (canceled/failed, token
                            # nulled) → this training was canceled. Kill the
                            # subprocess and stop reading so the worker exits and
                            # Modal frees the GPU — don't burn the timeout cap.
                            if _is_terminal_cancel_error(_e):
                                print(
                                    "Training wurde serverseitig abgebrochen — "
                                    "beende Trainingsprozess und gebe GPU frei.",
                                    flush=True,
                                )
                                cancel_detected.set()
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                                return
                            print(
                                f"Warnung: Supabase Update fehlgeschlagen "
                                f"(Versuch {_attempt + 1}/3): {_e}"
                            )
                            if _attempt < 2:
                                time.sleep(2 ** _attempt)
            except UnicodeDecodeError as e:
                print(f"Warning: Error decoding subprocess output: {e}")

        reader_thread = threading.Thread(target=_read_output, daemon=True)
        reader_thread.start()

        # Liveness heartbeat (every ~15s). LeRobot only logs every log_freq
        # steps, and some phases (dataset download, periodic eval, checkpoint
        # save) emit nothing for a while — during which last_progress_at would
        # go stale and the student's chart would look frozen. A light periodic
        # touch (current step, no new loss point) keeps last_progress_at fresh
        # so the UI shows "aktiv · vor Xs". It also gives a cancel a second
        # exit: a server-side cancel nulls the worker_token → P0001 here even
        # while the reader is blocked on a quiet stdout, so the GPU is freed
        # promptly instead of at the timeout cap. Each RPC call builds its own
        # Supabase client, so running alongside the reader thread is safe.
        heartbeat_interval_s = 15

        def _heartbeat():
            while not progress_stop.wait(heartbeat_interval_s):
                if cancel_detected.is_set():
                    return
                try:
                    _update_supabase_progress(
                        supabase_url, supabase_anon_key, worker_token,
                        training_id, max(last_progress_step, 0), total_steps, None,
                    )
                except Exception as _e:
                    if _is_terminal_cancel_error(_e):
                        print(
                            "Training serverseitig abgebrochen (Heartbeat) — "
                            "beende Trainingsprozess und gebe GPU frei.",
                            flush=True,
                        )
                        cancel_detected.set()
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return
                    # Transient (network blip) — keep the heart beating.
                    print(f"Warnung: Heartbeat-Update fehlgeschlagen: {_e}", flush=True)

        heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat_thread.start()

        # Mid-training checkpoint uploads (leLab-comparison PR-5a): lerobot
        # writes checkpoints/<step>/ every save_freq (default 20k) steps;
        # the watcher mirrors each new one to the model repo so a teacher
        # can inspect (and a crashed run isn't a total loss). Best-effort.
        ckpt_stop = threading.Event()
        _start_checkpoint_watcher(
            model_name, hf_token,
            OUTPUT_DIR / model_name.replace("/", "_"), ckpt_stop,
        )

        # Wait for process with timeout protection (default 5h, configurable).
        # The Modal function's own timeout=7h is a hard outer bound.
        timeout_hours = training_params.get("timeout_hours", 5)
        try:
            proc.wait(timeout=timeout_hours * 3600)
        except subprocess.TimeoutExpired:
            progress_stop.set()
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            ckpt_stop.set()
            log_url = _upload_training_log(model_name, hf_token, output_lines)
            _update_status_with_retry(
                supabase_url, supabase_anon_key, worker_token, training_id, "failed",
                f"Training Zeitlimit ueberschritten ({timeout_hours}h Limit)",
                log_url=log_url,
            )
            return {"status": "failed", "error": f"Training timed out ({timeout_hours}h limit)"}

        # Stop the heartbeat before evaluating the outcome — every path below
        # writes a terminal status, and the heartbeat must not race it.
        progress_stop.set()
        reader_thread.join(timeout=10)
        ckpt_stop.set()
        output_text = "".join(output_lines)

        # Canceled API-side mid-run: the reader thread already killed proc and
        # the row is terminal. Exit cleanly — a status write here would be a
        # P0001 no-op, and there's no checkpoint worth uploading. The container
        # returning is what releases the GPU.
        if cancel_detected.is_set():
            print(
                "Training serverseitig abgebrochen — Worker beendet sich.",
                flush=True,
            )
            return {"status": "canceled"}

        if proc.returncode != 0:
            if len(output_text) > 2000:
                error_msg = output_text[:1000] + "\n...[truncated]...\n" + output_text[-1000:]
            else:
                error_msg = output_text or "Unknown error"
            # error_message stays the truncated student-facing blob; the
            # FULL stdout goes to training_log.txt for teacher forensics.
            log_url = _upload_training_log(model_name, hf_token, output_lines)
            _update_status_with_retry(
                supabase_url, supabase_anon_key, worker_token, training_id,
                "failed", error_msg, log_url=log_url,
            )
            return {"status": "failed", "error": error_msg}

        # Exit code 0 is NOT sufficient for a VLA base-checkpoint run: LeRobot
        # silently falls back to random init if the base weights fail to load.
        # Catch that here so we never report success on a from-scratch model.
        if _pretrained_load_failed(output_text, training_params):
            err_msg = (
                "Training abgebrochen: Das vortrainierte Basismodell konnte "
                "nicht vollstaendig geladen werden (LeRobot meldete einen "
                "fehlgeschlagenen oder unvollstaendigen Gewichte-Load) — das "
                "Modell waere teilweise mit zufaelligen Gewichten statt dem "
                "Basismodell trainiert worden. Bitte Basismodell und Datensatz "
                "pruefen und Training neu starten."
            )
            log_url = _upload_training_log(model_name, hf_token, output_lines)
            _update_status_with_retry(
                supabase_url, supabase_anon_key, worker_token, training_id,
                "failed", err_msg, log_url=log_url,
            )
            return {"status": "failed", "error": err_msg}

        # ----- 4. Training succeeded — push progress to 100% before upload -----
        _update_supabase_progress(
            supabase_url, supabase_anon_key, worker_token, training_id,
            total_steps, total_steps, None,
        )

        # ----- 5. Upload to HuggingFace (with built-in chunked retry) -----
        # Only mark 'succeeded' AFTER the upload actually lands. Previously
        # a failing upload still flipped status to 'succeeded' while the
        # model was missing on HF — the student then couldn't use it for
        # inference and the row lied about its state.
        try:
            model_url = _upload_model_to_hf(
                model_name, hf_token,
                model_meta={
                    "dataset_repo_id": dataset_name,
                    "dataset_fps": (dataset_info or {}).get("fps"),
                    "policy_type": model_type,
                },
            )
        except Exception as upload_err:
            err_msg = (
                f"Training erfolgreich, aber Model-Upload zu HuggingFace "
                f"fehlgeschlagen: {upload_err}. Checkpoint liegt im Worker-"
                f"Output; bitte HF_TOKEN pruefen und Training neu starten."
            )
            log_url = _upload_training_log(model_name, hf_token, output_lines)
            _update_status_with_retry(
                supabase_url, supabase_anon_key, worker_token, training_id,
                "failed", err_msg, log_url=log_url,
            )
            return {"status": "failed", "error": err_msg}

        log_url = _upload_training_log(model_name, hf_token, output_lines)
        _update_status_with_retry(
            supabase_url, supabase_anon_key, worker_token, training_id, "succeeded",
            log_url=log_url,
        )
        return {"status": "succeeded", "model_url": model_url}

    except Exception as e:
        err = str(e)
        error_msg = err[:1000] + "\n...[truncated]...\n" + err[-1000:] if len(err) > 2000 else err
        # _update_status_with_retry swallows its own exceptions (and stops on a
        # P0001 terminal-row signal), so no surrounding try/except is needed.
        _update_status_with_retry(
            supabase_url, supabase_anon_key, worker_token, training_id,
            "failed", error_msg,
        )
        return {"status": "failed", "error": error_msg}

    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        _cleanup_output(model_name)
        _current_job = None


# =====================================================================
# Managed mode (spec §3.2, §3.5, §4.7): ACT on a RunPod pod or Modal's
# train_managed. The worker holds no long-lived secret: it talks to the
# cloud API's worker endpoints with its per-attempt token, to R2 only
# through presigned URLs the API mints, and to Supabase only through the
# scoped update_training_progress RPC (status NULL or 'running', never an
# error text: E11). It defines no German database sentence and no save or
# mirror cadence of its own (both come from `start`'s spec).
#
# torch, draccus, httpx and every lerobot module are imported inside
# functions: the deps-free tests stub only huggingface_hub and supabase.
# =====================================================================

# ---- Checkpoint content (§3.5; lockstep-tested against artifact_validation.py) ----
PRETRAINED_MODEL_FILES = (
    "config.json",
    "model.safetensors",
    "train_config.json",
    "policy_preprocessor.json",
    "policy_preprocessor_step_3_normalizer_processor.safetensors",
    "policy_postprocessor.json",
    "policy_postprocessor_step_0_unnormalizer_processor.safetensors",
)
TRAINING_STATE_FILES = (
    "optimizer_param_groups.json",
    "optimizer_state.safetensors",
    "rng_state.safetensors",
    "training_step.json",
)
# The final checkpoint (step == total_steps) uploads only these 7.
FINAL_CHECKPOINT_RELPATHS = tuple(f"pretrained_model/{f}" for f in PRETRAINED_MODEL_FILES)
# Every other checkpoint uploads exactly these 11.
CHECKPOINT_RELPATHS = FINAL_CHECKPOINT_RELPATHS + tuple(
    f"training_state/{f}" for f in TRAINING_STATE_FILES
)
ACT_PRE_STEPS = (
    "rename_observations_processor",
    "to_batch_processor",
    "device_processor",
    "normalizer_processor",
)
ACT_POST_STEPS = ("unnormalizer_processor", "device_processor")
PRE_STATE_FILE = "policy_preprocessor_step_3_normalizer_processor.safetensors"
POST_STATE_FILE = "policy_postprocessor_step_0_unnormalizer_processor.safetensors"
PRE_STATE_STEP_INDEX = 3
POST_STATE_STEP_INDEX = 0
# The four train_config.json keys LeRobot itself sets on a resume
# (lerobot/configs/train.py:92-109); everything else must equal the regenerated file.
RESUME_IGNORED_KEYS = (
    ("resume",),
    ("num_workers",),
    ("checkpoint_path",),
    ("policy", "pretrained_path"),
)


def step_id(step: int, total_steps: int) -> str:
    """LeRobot's checkpoint directory name (lerobot/utils/train_utils.py:37-39)."""
    return f"{step:0{max(6, len(str(total_steps)))}d}"


# ---- Worker constants (§3.9) ----
START_LOOP_MAX = 5
FINAL_UPLOAD_WAIT_S = 1080
HEARTBEAT_INTERVAL_S = 15
UPLOAD_TICK_S = 15
NUM_WORKERS_MAX = 12
MIN_FREE_DISK_B = 30 * 1024 ** 3
MIN_GPU_MEMORY_B = int(9.5e9)
GPU_CAPABILITY_MIN = (8, 0)
GPU_CAPABILITY_MAX = (9, 0)
# "Retry (start class)" of §3.2: back-off 5, 15, 45, 120 s, then every 120 s,
# give up after 10 min.
START_RETRY_DELAYS_S = (5, 15, 45, 120)
START_RETRY_EVERY_S = 120
START_RETRY_GIVEUP_S = 600
# ckpt-bad on a 5xx/network: 3 tries (5, 15, 45 s).
CKPT_BAD_RETRY_DELAYS_S = (5, 15, 45)
RATE_LIMIT_MAX_WAIT_S = 60
RATE_LIMIT_MAX_REPEATS = 20
# HTTP timeouts (impl-notes WP-B 2, TR-9): API calls connect 10 s / read 60 s;
# ckpt-bad sha256 read 300 s (the API streams and hashes ~620 MB in that request);
# a PUT to R2 has a 600 s read timeout (§3.2); a presigned GET reads with 60 s.
API_CONNECT_TIMEOUT_S = 10.0
API_READ_TIMEOUT_S = 60.0
CKPT_BAD_SHA256_READ_TIMEOUT_S = 300.0
PUT_READ_TIMEOUT_S = 600.0
GET_READ_TIMEOUT_S = 60.0
PUT_TRIES = 3
PUT_REASK_MAX = 3
DOWNLOAD_TRIES = 3
LOG_MAX_BYTES = 32 * 1024 * 1024 - 64 * 1024
LOG_HEAD_BYTES = 1024 * 1024
JSON_READ_CAP_B = 1024 * 1024
DETAIL_MAX_CHARS = 500
LOCK_CANDIDATES = (Path("/app/training-lock.txt"), Path("/root/training-lock.txt"))

# Test seams: an httpx transport (tests pass httpx.MockTransport) and the clock.
_HTTP_TRANSPORT = None
_sleep = time.sleep
_monotonic = time.monotonic

_STEP_ID_RE = re.compile(r"^\d{6,}$")


class HostUnfit(Exception):
    """The host cannot train (§4.7 step 1). `reason` is a §3.2 host_unfit reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _assert_training_host(*, disk_floor: bool = True) -> None:
    """CUDA, capability (8,0)-(9,0), >= 9.5 GB, a finite synchronised matmul, >= 30 GiB at /tmp.

    The free /tmp bytes are measured and printed on the "Host check:" line of every host
    that gets that far (the P2 record, SR-24 i). Only managed mode (`disk_floor`, the
    default) refuses below MIN_FREE_DISK_B; the legacy Modal functions pass
    disk_floor=False (V2-2: nobody measured the /tmp of Modal's L4 / A100-80GB, and the
    floor must not turn a path that works today into S16).
    """
    measured = {}
    try:
        import torch
    except Exception as e:  # noqa: BLE001
        raise HostUnfit("no_cuda", f"torch import failed: {type(e).__name__}")
    try:
        available = bool(torch.cuda.is_available())
    except Exception as e:  # noqa: BLE001
        raise HostUnfit("no_cuda", f"cuda probe failed: {type(e).__name__}")
    measured["cuda"] = available
    if not available:
        print(f"Host check: {measured}", flush=True)
        raise HostUnfit("no_cuda", "torch.cuda.is_available() is False")
    try:
        cap = tuple(int(x) for x in torch.cuda.get_device_capability(0))
        props = torch.cuda.get_device_properties(0)
        total_memory = int(props.total_memory)
        measured.update(gpu=str(props.name), capability=cap, total_memory_b=total_memory)
    except Exception as e:  # noqa: BLE001
        raise HostUnfit("no_cuda", f"device query failed: {type(e).__name__}")
    if not (GPU_CAPABILITY_MIN <= cap <= GPU_CAPABILITY_MAX):
        print(f"Host check: {measured}", flush=True)
        raise HostUnfit("gpu_capability", f"capability {cap[0]}.{cap[1]} outside 8.0-9.0")
    if total_memory < MIN_GPU_MEMORY_B:
        print(f"Host check: {measured}", flush=True)
        raise HostUnfit("gpu_memory", f"total_memory {total_memory} B < {MIN_GPU_MEMORY_B} B")
    try:
        a = torch.randn(1024, 1024, device="cuda:0", dtype=torch.float32)
        b = torch.randn(1024, 1024, device="cuda:0", dtype=torch.float32)
        c = a @ b
        torch.cuda.synchronize(0)
        finite = bool(torch.isfinite(c).all().item())
    except Exception as e:  # noqa: BLE001
        print(f"Host check: {measured}", flush=True)
        raise HostUnfit("matmul", f"matmul failed: {type(e).__name__}")
    measured["matmul_finite"] = finite
    if not finite:
        print(f"Host check: {measured}", flush=True)
        raise HostUnfit("matmul", "1024x1024 fp32 matmul produced a non-finite value")
    free = shutil.disk_usage("/tmp").free
    measured["tmp_free_b"] = int(free)
    print(f"Host check: {measured}", flush=True)
    if disk_floor and free < MIN_FREE_DISK_B:
        raise HostUnfit("disk", f"/tmp free {free} B < {MIN_FREE_DISK_B} B")


# ---- small helpers ----

def _short(text, limit: int = DETAIL_MAX_CHARS) -> str:
    s = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    return s[:limit]


def _hash_file(path: Path) -> dict:
    """{size, sha256 (hex), md5 (base64)} in one pass."""
    import base64
    import hashlib
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return {"size": size, "sha256": sha.hexdigest(),
            "md5": base64.b64encode(md5.digest()).decode("ascii")}


def _hash_bytes(data: bytes) -> dict:
    import base64
    import hashlib
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "md5": base64.b64encode(hashlib.md5(data).digest()).decode("ascii")}


def _read_json_capped(path: Path):
    with open(path, "rb") as f:
        data = f.read(JSON_READ_CAP_B + 1)
    if len(data) > JSON_READ_CAP_B:
        raise ValueError(f"{path.name} is larger than {JSON_READ_CAP_B} bytes")
    return json.loads(data.decode("utf-8"))


def _find_lock_file() -> Path | None:
    for p in LOCK_CANDIDATES:
        if p.is_file():
            return p
    return None


def _lock_sha256() -> str:
    """sha256 of the lock file this image was built from (§3.10); 64 zeros when none is found."""
    import hashlib
    path = _find_lock_file()
    if path is None:
        print("[edubotics-worker] no training-lock.txt found; start will be refused", flush=True)
        return "0" * 64
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_id() -> str:
    return _short(os.environ.get("EDUBOTICS_BUILD") or "unknown", 64)


def _http_client(read_timeout: float = API_READ_TIMEOUT_S):
    import httpx
    return httpx.Client(
        timeout=httpx.Timeout(connect=API_CONNECT_TIMEOUT_S, read=read_timeout,
                              write=read_timeout, pool=API_CONNECT_TIMEOUT_S),
        follow_redirects=False,
        transport=_HTTP_TRANSPORT,
    )


def _spawn_lerobot(cmd: list[str], env: dict):
    """The one place LeRobot is started (a seam for the tests' fake lerobot_train)."""
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, env=env, errors="replace",
    )


def _lerobot_env() -> dict:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    # LeRobot needs none of the worker's credentials.
    for key in ("EDUBOTICS_WORKER_TOKEN", "SUPABASE_ANON_KEY", "RUNPOD_API_KEY", "HF_TOKEN"):
        env.pop(key, None)
    return env


def _num_workers(cpu_budget: int) -> int:
    try:
        n = int(cpu_budget)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n - 1, NUM_WORKERS_MAX))


def _fresh_args(spec: dict, num_workers: int) -> list[str]:
    """The fresh-start LeRobot command (§4.7 step 5). `_build_training_command`'s rmtree runs here."""
    cmd = _build_training_command(spec["dataset_name"], "act", spec["model_name"],
                                  spec["training_params"])
    revision = spec.get("dataset_revision")
    if revision:
        cmd.append(f"--dataset.revision={revision}")
    cmd.append(f"--num_workers={num_workers}")
    return cmd


def _output_path(model_name: str) -> Path:
    return OUTPUT_DIR / model_name.replace("/", "_")


def _regenerate_train_config(fresh_args: list[str]) -> dict:
    """LeRobot's own TrainPipelineConfig for these CLI args + the dataset's features (§B.4).

    Proven equal to the train_config.json LeRobot writes for a fresh run (dict
    equality) and to a resumed run's after dropping RESUME_IGNORED_KEYS. Must run
    while the output directory does NOT exist (validate() refuses an existing one).
    """
    import draccus
    import lerobot.policies.factory  # noqa: F401  (the same choice registry as lerobot_train)
    from lerobot.configs.train import TrainPipelineConfig
    from lerobot.configs.types import FeatureType
    from lerobot.datasets.dataset_metadata import LeRobotDatasetMetadata
    from lerobot.datasets.feature_utils import dataset_to_policy_features

    args = list(fresh_args)
    saved = sys.argv
    sys.argv = ["lerobot_train"] + args
    try:
        cfg = draccus.parse(TrainPipelineConfig, None, args=args)
        cfg.validate()
    finally:
        sys.argv = saved
    meta = LeRobotDatasetMetadata(cfg.dataset.repo_id, root=cfg.dataset.root,
                                  revision=cfg.dataset.revision)
    feats = dataset_to_policy_features(meta.features)
    cfg.policy.output_features = {k: f for k, f in feats.items() if f.type is FeatureType.ACTION}
    if not cfg.policy.input_features:
        cfg.policy.input_features = {k: f for k, f in feats.items()
                                     if k not in cfg.policy.output_features}
    if hasattr(cfg.policy, "action_feature_names"):
        names = meta.features.get("action", {}).get("names")
        if names is not None:
            cfg.policy.action_feature_names = list(names)
    with draccus.config_type("json"):
        return json.loads(draccus.dump(cfg))


def _normalise_train_config(doc: dict) -> dict:
    doc = json.loads(json.dumps(doc))
    for path in RESUME_IGNORED_KEYS:
        node = doc
        for k in path[:-1]:
            node = node.get(k, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return doc


class _CkptBad(Exception):
    """The handed checkpoint is unusable; `reason` is a §3.2 ckpt-bad reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class _DownloadFailed(Exception):
    """Incomplete bytes or a transport failure after DOWNLOAD_TRIES (-> host_unfit/download)."""


def _check_steps(doc, want: tuple, name: str, state_index: int, state_file: str) -> None:
    if not isinstance(doc, dict) or doc.get("name") != name:
        raise _CkptBad("steps", f"{name}: wrong name")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise _CkptBad("steps", f"{name}: steps is not a list of objects")
    if tuple(s.get("registry_name") for s in steps) != want:
        raise _CkptBad("steps", f"{name}: unexpected processor steps")
    for i, s in enumerate(steps):
        if "class" in s:
            raise _CkptBad("steps", f"{name}: a step names a class")
        if i == state_index:
            if s.get("state_file") != state_file:
                raise _CkptBad("steps", f"{name}: wrong state_file")
        elif "state_file" in s:
            raise _CkptBad("steps", f"{name}: unexpected state_file")


def _validate_resume_dir(stage: Path, step: int) -> None:
    """Exact 11 files, no links, the ACT processor lists and state files, training_step (§4.7 6.3)."""
    found = []
    for root, dirs, files in os.walk(stage):
        for name in dirs + files:
            if os.path.islink(os.path.join(root, name)):
                raise _CkptBad("file_list", "the checkpoint contains a link")
        for name in files:
            found.append(os.path.relpath(os.path.join(root, name), stage).replace(os.sep, "/"))
    if sorted(found) != sorted(CHECKPOINT_RELPATHS):
        raise _CkptBad("file_list", "the checkpoint's file list is not the expected one")
    pm = stage / "pretrained_model"
    for fname, want, name, idx, sfile in (
        ("policy_preprocessor.json", ACT_PRE_STEPS, "policy_preprocessor",
         PRE_STATE_STEP_INDEX, PRE_STATE_FILE),
        ("policy_postprocessor.json", ACT_POST_STEPS, "policy_postprocessor",
         POST_STATE_STEP_INDEX, POST_STATE_FILE),
    ):
        try:
            doc = _read_json_capped(pm / fname)
        except (OSError, ValueError) as e:
            raise _CkptBad("steps", f"{fname}: {type(e).__name__}")
        _check_steps(doc, want, name, idx, sfile)
    try:
        ts = _read_json_capped(stage / "training_state" / "training_step.json")
    except (OSError, ValueError) as e:
        raise _CkptBad("training_step", f"training_step.json: {type(e).__name__}")
    if ts != {"step": step}:
        raise _CkptBad("training_step", "training_step.json does not name the handed step")


def _compare_and_overwrite_train_config(stage: Path, ref: dict) -> None:
    path = stage / "pretrained_model" / "train_config.json"
    try:
        host = _read_json_capped(path)
    except (OSError, ValueError) as e:
        raise _CkptBad("train_config", f"train_config.json: {type(e).__name__}")
    if not isinstance(host, dict) or _normalise_train_config(host) != _normalise_train_config(ref):
        raise _CkptBad("train_config", "train_config.json differs from the regenerated config")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(ref, indent=4))


def _download_file(client, url: str, dest: Path, size: int, sha256: str) -> None:
    """One presigned GET with DOWNLOAD_TRIES tries.

    Raises _DownloadFailed for incomplete bytes / transport errors after the tries,
    and _CkptBad("sha256") for complete bytes whose size or sha256 differ.
    """
    import hashlib
    import httpx
    last = "no try"
    for attempt in range(DOWNLOAD_TRIES):
        if attempt:
            _sleep(2 * attempt)
        h = hashlib.sha256()
        got = 0
        too_long = False
        try:
            with client.stream("GET", url, timeout=httpx.Timeout(
                    connect=API_CONNECT_TIMEOUT_S, read=GET_READ_TIMEOUT_S,
                    write=GET_READ_TIMEOUT_S, pool=API_CONNECT_TIMEOUT_S)) as resp:
                if resp.status_code != 200:
                    last = f"HTTP {resp.status_code}"
                    continue
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 20):
                        got += len(chunk)
                        if got > size:
                            too_long = True
                            break
                        h.update(chunk)
                        f.write(chunk)
        except (httpx.HTTPError, OSError) as e:
            last = type(e).__name__
            continue
        if too_long or got != size or h.hexdigest() != sha256:
            raise _CkptBad("sha256", f"{dest.name}: complete bytes differ from the manifest")
        return
    raise _DownloadFailed(f"{dest.name}: {last}")


def _prepare_resume(resume: dict, fresh_args: list[str], num_workers: int,
                    model_name: str, total_steps: int, client) -> list[str]:
    """§4.7 step 6: stage, validate, regenerate + overwrite train_config, move into place.

    Returns the resume command. Raises _CkptBad (ckpt-bad), _DownloadFailed, or
    any other exception (all -> host_unfit/download, never a checkpoint count).
    """
    step = resume.get("step")
    sid = resume.get("step_id")
    files = resume.get("files")
    if not isinstance(step, int) or isinstance(step, bool) or step < 1:
        raise ValueError("resume.step is not a positive integer")
    if not isinstance(sid, str) or not _STEP_ID_RE.match(sid) or sid != step_id(step, total_steps):
        raise ValueError("resume.step_id does not match the step")
    if not isinstance(files, dict):
        raise ValueError("resume.files is not an object")
    if sorted(files) != sorted(CHECKPOINT_RELPATHS):
        # The API's own hand-out, not the checkpoint's content: host_unfit/download (FR-13).
        raise ValueError("resume.files does not name exactly the 11 checkpoint files")
    out = _output_path(model_name)
    # 0. Nothing of an earlier run may remain (validate() refuses an existing output_dir).
    if out.exists():
        shutil.rmtree(out)
    # 1. Our own config, from the server-built arguments and the dataset's metadata.
    ref = _regenerate_train_config(fresh_args[3:])
    # 2. Stage every file.
    stage = OUTPUT_DIR / f".stage-{sid}"
    if stage.exists():
        shutil.rmtree(stage)
    for rel in CHECKPOINT_RELPATHS:
        meta = files[rel]
        if (not isinstance(meta, dict) or not isinstance(meta.get("url"), str)
                or not isinstance(meta.get("size"), int)
                or not isinstance(meta.get("sha256"), str)):
            raise ValueError(f"resume.files[{rel}] is malformed")
        dest = stage / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        _download_file(client, meta["url"], dest, meta["size"], meta["sha256"].lower())
    # 3. Structure.
    _validate_resume_dir(stage, step)
    # 4. train_config.json: equal to ours apart from the 4 resume keys, then replaced by ours.
    _compare_and_overwrite_train_config(stage, ref)
    # 5. Into place.
    ckpt = out / "checkpoints" / sid
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, ckpt)
    # 6. LeRobot's own resume.
    return [
        sys.executable, "-m", "lerobot.scripts.lerobot_train",
        "--resume=true",
        f"--config_path={ckpt}/pretrained_model/train_config.json",
        f"--num_workers={num_workers}",
    ]


class _LogBuffer:
    """The host's training_log.txt: the first LOG_HEAD_BYTES, then the newest lines."""

    def __init__(self):
        self._lock = threading.Lock()
        self._head: list[str] = []
        self._head_bytes = 0
        self._tail: deque = deque()
        self._tail_bytes = 0
        self._dropped = 0

    def add(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        n = len(line.encode("utf-8", errors="replace"))
        with self._lock:
            if self._head_bytes + n <= LOG_HEAD_BYTES and not self._tail:
                self._head.append(line)
                self._head_bytes += n
                return
            self._tail.append(line)
            self._tail_bytes += n
            while self._tail and self._head_bytes + self._tail_bytes > LOG_MAX_BYTES:
                old = self._tail.popleft()
                self._tail_bytes -= len(old.encode("utf-8", errors="replace"))
                self._dropped += 1

    def to_bytes(self) -> bytes:
        with self._lock:
            parts = list(self._head)
            if self._dropped:
                parts.append(f"[... {self._dropped} lines omitted ...]\n")
            parts.extend(self._tail)
        return "".join(parts).encode("utf-8", errors="replace")


class _Answer:
    """One worker-API answer: status None = network (connect/read timeout, reset)."""

    def __init__(self, status=None, body=None, error=""):
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.error = error

    @property
    def code(self):
        return self.body.get("code")

    @property
    def network(self) -> bool:
        return self.status is None

    def is_5xx_class(self) -> bool:
        return self.status is None or self.status >= 500


_STOP = "stop"
_GIVEUP = "giveup"
_RETRY = "retry"


class _ManagedRun:
    """One worker process for one attempt. All state of run_managed lives here."""

    def __init__(self, api_url, supabase_url, supabase_anon_key, training_id, attempt,
                 worker_token, cpu_budget, on_budget):
        self.api_url = str(api_url).rstrip("/")
        self.supabase_url = supabase_url
        self.supabase_anon_key = supabase_anon_key
        self.training_id = int(training_id)
        self.attempt = int(attempt)
        self.token = str(worker_token)
        self.cpu_budget = cpu_budget
        self.on_budget = on_budget
        self.log = _LogBuffer()
        self.fenced = threading.Event()        # 401 / P0001 / 409 start|finish: stop everything
        self.uploader_stop = threading.Event()
        self.progress_stop = threading.Event()
        self.state_lock = threading.Lock()
        self.proc = None
        self.spec: dict = {}
        self.total_steps = 0
        self.resume_step = 0
        self.resumed = False
        self.last_step = 0
        self.new_step_logged = False
        self.deadline = None
        self.ck_state: dict[int, str] = {}     # step -> "put" | "uploaded" | "skipped"
        self.ck_manifest: dict[int, dict] = {}
        self.points = None
        self.threads: list[threading.Thread] = []
        self.client = _http_client()

    # ---------- logging ----------
    def wlog(self, msg: str) -> None:
        line = f"[edubotics-worker] {msg}"
        print(line, flush=True)
        self.log.add(line)

    # ---------- fence ----------
    def fence(self, why: str) -> None:
        if not self.fenced.is_set():
            self.wlog(f"stopping: {why}")
        self.fenced.set()
        self.uploader_stop.set()
        self.progress_stop.set()
        self._kill_lerobot()

    def _kill_lerobot(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass

    # ---------- the worker API (§3.2) ----------
    def _url(self, action: str) -> str:
        return f"{self.api_url}/trainings/worker/{self.training_id}/{self.attempt}/{action}"

    def api(self, action: str, body: dict, *, read_timeout: float = API_READ_TIMEOUT_S,
            client=None, deadline=None) -> _Answer:
        """POST one action. A 429 waits min(Retry-After, 60) s and repeats (bounded)."""
        import httpx
        client = client or self.client
        timeout = httpx.Timeout(connect=API_CONNECT_TIMEOUT_S, read=read_timeout,
                                write=API_READ_TIMEOUT_S, pool=API_CONNECT_TIMEOUT_S)
        payload = json.dumps(body).encode("utf-8")
        for _ in range(RATE_LIMIT_MAX_REPEATS + 1):
            if self.fenced.is_set():
                return _Answer(status=401, body={"code": "fenced"})
            try:
                resp = client.post(self._url(action), content=payload, timeout=timeout, headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                })
            except httpx.HTTPError as e:
                return _Answer(None, None, type(e).__name__)
            try:
                parsed = resp.json()
            except ValueError:
                parsed = None
            if resp.status_code == 429:
                wait = _retry_after_s(resp.headers.get("Retry-After"))
                if deadline is not None and _monotonic() + wait > deadline:
                    return _Answer(429, parsed)
                self.wlog(f"{action}: 429, waiting {wait} s")
                _sleep(wait)
                continue
            if resp.status_code == 401:
                self.fence(f"{action} answered 401")
            return _Answer(resp.status_code, parsed)
        return _Answer(429, None)

    def _start_class(self, fn, what: str):
        """§3.2 'retry (start class)': fn() -> value | _RETRY; returns the value, _STOP or _GIVEUP."""
        giveup_at = _monotonic() + START_RETRY_GIVEUP_S
        delays = list(START_RETRY_DELAYS_S)
        while True:
            if self.fenced.is_set():
                return _STOP
            result = fn(giveup_at)
            if result is not _RETRY:
                return result
            delay = delays.pop(0) if delays else START_RETRY_EVERY_S
            if _monotonic() + delay > giveup_at:
                self.wlog(f"{what}: giving up after {START_RETRY_GIVEUP_S} s")
                return _GIVEUP
            _sleep(delay)

    def call_start(self):
        """POST start; returns the 200 body, or _STOP."""
        body = {"lock_sha256": _lock_sha256(), "build": _build_id()}

        def once(deadline):
            a = self.api("start", body, deadline=deadline)
            if a.status == 200:
                if not _valid_start_answer(a.body):
                    self.wlog("start: ERROR malformed 200 answer")
                    return _RETRY
                return a.body
            if a.status == 401:
                return _STOP
            if a.status == 409:
                self.fence(f"start answered 409 {_short(a.code, 40)}")
                return _STOP
            if not a.is_5xx_class():
                self.wlog(f"start: ERROR unexpected status {a.status}")
            else:
                self.wlog(f"start: transient ({a.status or a.error})")
            return _RETRY

        result = self._start_class(once, "start")
        if result in (_STOP, _GIVEUP):
            self.fence("start could not be completed")
            return _STOP
        return result

    def call_finish(self, payload: dict) -> str:
        """POST finish; returns its status string, or _STOP."""
        if "detail" in payload:
            payload = {**payload, "detail": _short(payload["detail"])}
        self.uploader_stop.set()
        self.progress_stop.set()

        def once(deadline):
            a = self.api("finish", payload, deadline=deadline)
            if a.status == 200:
                return str(a.body.get("status", ""))
            if a.status in (401, 409):
                return _STOP
            if not a.is_5xx_class():
                self.wlog(f"finish: ERROR unexpected status {a.status}")
            else:
                self.wlog(f"finish: transient ({a.status or a.error})")
            return _RETRY

        result = self._start_class(once, "finish")
        if result in (_STOP, _GIVEUP):
            self.fence("finish could not be completed")
            return _STOP
        self.wlog(f"finish {payload.get('outcome')}/{payload.get('reason', '-')} -> {result}")
        return result

    def call_ckpt_bad(self, step: int, reason: str) -> str:
        """POST ckpt-bad; returns 'refused' | 'kept' | 'ignored' | _STOP | _GIVEUP."""
        timeout = CKPT_BAD_SHA256_READ_TIMEOUT_S if reason == "sha256" else API_READ_TIMEOUT_S
        delays = list(CKPT_BAD_RETRY_DELAYS_S)
        while True:
            a = self.api("ckpt-bad", {"step": step, "reason": reason}, read_timeout=timeout)
            if a.status == 200 and a.body.get("state") in ("refused", "kept", "ignored"):
                return a.body["state"]
            if a.status == 401:
                return _STOP
            self.wlog(f"ckpt-bad: {'ERROR unexpected ' if not a.is_5xx_class() else ''}"
                      f"answer {a.status or a.error}")
            if not delays:
                return _GIVEUP
            _sleep(delays.pop(0))

    # ---------- the RPC (§3.3) ----------
    def rpc(self, **kw) -> bool:
        """update_training_progress; False on a transport error, fences on P0001."""
        try:
            _call_progress_rpc(self.supabase_url, self.supabase_anon_key, self.token,
                               self.training_id, total_steps=self.total_steps or None, **kw)
            return True
        except Exception as e:  # noqa: BLE001
            if _is_terminal_cancel_error(e):
                self.fence("update_training_progress answered P0001")
            else:
                self.wlog(f"update_training_progress failed: {type(e).__name__}")
            return False

    def first_running_write(self) -> bool:
        def once(_deadline):
            if self.rpc(status="running", current_step=self.resume_step):
                return True
            return _STOP if self.fenced.is_set() else _RETRY

        result = self._start_class(once, "first running write")
        if result is not True:
            self.fence("the first running write failed")
            return False
        return True

    def _progress_loop(self) -> None:
        import queue
        next_beat = _monotonic() + HEARTBEAT_INTERVAL_S
        while not self.progress_stop.is_set():
            wait = max(0.0, next_beat - _monotonic())
            try:
                item = self.points.get(timeout=wait)
            except queue.Empty:
                item = None
            if self.progress_stop.is_set():
                return
            if item is not None:
                step, loss = item
                self.rpc(current_step=step, current_loss=loss)
            else:
                with self.state_lock:
                    step = max(self.last_step, self.resume_step)
                self.rpc(current_step=step)
            next_beat = _monotonic() + HEARTBEAT_INTERVAL_S

    def start_progress(self) -> None:
        import queue
        self.points = queue.Queue(maxsize=1000)
        t = threading.Thread(target=self._progress_loop, name="managed-progress", daemon=True)
        t.start()
        self.threads.append(t)

    # ---------- LeRobot output ----------
    def _read_output(self, proc) -> None:
        step_pattern = re.compile(r"step[:\s]+(\d+\.?\d*[KMBkmb]?)")
        loss_pattern = re.compile(r"loss[:\s]+([\d.]+(?:e[+-]?\d+)?)")
        try:
            for line in proc.stdout:
                print(line, end="", flush=True)
                self.log.add(line)
                m = step_pattern.search(line)
                if not m:
                    continue
                step = _parse_abbreviated_number(m.group(1))
                if step is None:
                    continue
                step = min(step, self.total_steps)
                lm = loss_pattern.search(line)
                loss = _safe_float(lm.group(1)) if lm else None
                with self.state_lock:
                    if step <= self.last_step:
                        continue
                    self.last_step = step
                    if step > self.resume_step:
                        self.new_step_logged = True
                try:
                    self.points.put_nowait((step, loss))
                except Exception:  # noqa: BLE001  (queue full: the heartbeat still carries the step)
                    pass
        except (UnicodeDecodeError, ValueError, OSError):
            pass

    # ---------- uploader (§4.7 step 8) ----------
    def _must_upload(self, step: int) -> bool:
        return step % self.spec["mirror_every"] == 0 or step == self.total_steps

    def _ckpt_root(self) -> Path:
        return _output_path(self.spec["model_name"]) / "checkpoints"

    def _newest_complete(self):
        try:
            target = os.readlink(self._ckpt_root() / "last")
        except OSError:
            return None
        name = os.path.basename(target.rstrip("/"))
        return int(name) if name.isdigit() else None

    def _present(self) -> dict:
        out = {}
        root = self._ckpt_root()
        try:
            for entry in os.scandir(root):
                if entry.name.isdigit() and entry.is_dir(follow_symlinks=False):
                    out[int(entry.name)] = Path(entry.path)
        except OSError:
            pass
        return out

    def uploader_tick(self, client) -> None:
        newest = self._newest_complete()
        if newest is None:
            return
        present = self._present()
        todo = sorted(
            s for s in present
            if self.resume_step < s <= newest
            and self.ck_state.get(s) not in ("uploaded", "skipped")
            and (self._must_upload(s) or s == newest)
        )
        for s in todo:
            if self.uploader_stop.is_set() or self.fenced.is_set():
                return
            self.ck_state[s] = self._upload_step(s, present[s], client) or self.ck_state.get(s)
        self._prune(newest, present)

    def _prune(self, newest: int, present: dict) -> None:
        for s, path in present.items():
            if s >= newest:
                continue
            if self.ck_state.get(s) in ("uploaded", "skipped") or not self._must_upload(s) \
                    or s <= self.resume_step:
                shutil.rmtree(path, ignore_errors=True)

    def _manifest(self, step: int, path: Path) -> dict | None:
        if step in self.ck_manifest:
            return self.ck_manifest[step]
        rels = FINAL_CHECKPOINT_RELPATHS if step == self.total_steps else CHECKPOINT_RELPATHS
        manifest = {}
        for rel in rels:
            f = path / rel
            if f.is_symlink() or not f.is_file():
                self.wlog(f"checkpoint {step}: ERROR {rel} is missing")
                return None
            manifest[rel] = _hash_file(f)
        self.ck_manifest[step] = manifest
        return manifest

    def _upload_step(self, step: int, path: Path, client):
        """One step on one tick. Returns the new state, or None to retry next tick."""
        if self.ck_state.get(step) != "put":
            manifest = self._manifest(step, path)
            if manifest is None:
                return "skipped"
            done_files: set = set()
            for _ in range(PUT_REASK_MAX + 1):
                a = self.api("upload-url", {"kind": "ckpt", "step": step, "files": manifest},
                             client=client)
                if a.status == 200:
                    urls = a.body.get("urls") or {}
                    headers = a.body.get("headers") or {}
                    result = self._put_files(client, path, manifest, urls, headers, done_files)
                    if result == "ok":
                        break
                    if result == "403":
                        continue
                    return None                        # 5xx/network after PUT_TRIES: next tick
                if a.status == 401:
                    return None
                if a.status == 409 and a.code == "already_ready":
                    self.wlog(f"checkpoint {step}: already ready")
                    return "uploaded"
                if a.status == 409 and a.code == "already_refused":
                    self.wlog(f"checkpoint {step}: already refused, skipped")
                    return "skipped"
                if a.status == 422:
                    self.wlog(f"checkpoint {step}: upload-url refused the manifest (422), skipped")
                    return "skipped"
                if not a.is_5xx_class():
                    self.wlog(f"checkpoint {step}: upload-url ERROR unexpected status {a.status}")
                return None                            # 5xx/network: next tick
            else:
                return None                            # PUT 403 more than PUT_REASK_MAX times
            self.ck_state[step] = "put"
        a = self.api("ckpt-done", {"step": step}, client=client)
        if a.status == 200:
            self.wlog(f"checkpoint {step}: ready")
            return "uploaded"
        if a.status == 409 and a.code == "not_uploading":
            self.wlog(f"checkpoint {step}: ckpt-done 409 not_uploading, skipped")
            return "skipped"
        if a.status == 422:
            self.wlog(f"checkpoint {step}: refused by the API (422), skipped")
            return "skipped"
        if a.status == 401:
            return None
        if not a.is_5xx_class():
            self.wlog(f"checkpoint {step}: ckpt-done ERROR unexpected status {a.status}")
        return None                                    # ckpt-done again next tick

    def _put_files(self, client, base: Path, manifest: dict, urls: dict, headers: dict,
                   done_files: set) -> str:
        """PUT every file not yet put with exactly the returned headers. 'ok' | '403' | 'fail'."""
        import httpx
        for rel in manifest:
            if rel in done_files:
                continue
            url, hdrs = urls.get(rel), headers.get(rel)
            if not isinstance(url, str) or not isinstance(hdrs, dict):
                self.wlog(f"upload-url answer lacks {rel}")
                return "fail"
            for attempt in range(PUT_TRIES):
                if self.uploader_stop.is_set() or self.fenced.is_set():
                    return "fail"
                try:
                    resp = client.put(
                        url, content=_file_chunks(base / rel), headers=dict(hdrs),
                        timeout=httpx.Timeout(connect=API_CONNECT_TIMEOUT_S,
                                              read=PUT_READ_TIMEOUT_S, write=PUT_READ_TIMEOUT_S,
                                              pool=API_CONNECT_TIMEOUT_S))
                    status = resp.status_code
                except (httpx.HTTPError, OSError) as e:
                    status = None
                    self.wlog(f"PUT {rel}: {type(e).__name__}")
                if status is not None and 200 <= status < 300:
                    done_files.add(rel)
                    break
                if status == 403:
                    return "403"
                if attempt < PUT_TRIES - 1:
                    _sleep(2 * (attempt + 1))
            else:
                return "fail"
        return "ok"

    def _uploader_loop(self) -> None:
        client = _http_client()
        try:
            while not self.uploader_stop.is_set():
                try:
                    self.uploader_tick(client)
                except Exception as e:  # noqa: BLE001  (an upload failure never stops training)
                    self.wlog(f"uploader: {type(e).__name__}")
                if self.uploader_stop.wait(UPLOAD_TICK_S):
                    return
        finally:
            client.close()

    def final_uploaded(self) -> bool:
        return self.ck_state.get(self.total_steps) == "uploaded"

    def final_given_up(self) -> bool:
        return self.ck_state.get(self.total_steps) == "skipped"

    # ---------- the log ----------
    def upload_log(self) -> None:
        """Best effort: upload-url {kind: log} and one PUT. Never changes the outcome."""
        try:
            data = self.log.to_bytes()
            meta = _hash_bytes(data)
            a = self.api("upload-url", {"kind": "log", "files": {"training_log.txt": meta}})
            if a.status != 200:
                self.wlog(f"log upload-url answered {a.status or a.error}")
                return
            url = (a.body.get("urls") or {}).get("training_log.txt")
            hdrs = (a.body.get("headers") or {}).get("training_log.txt")
            if not isinstance(url, str) or not isinstance(hdrs, dict):
                return
            import httpx
            for attempt in range(PUT_TRIES):
                try:
                    resp = self.client.put(url, content=data, headers=dict(hdrs),
                                           timeout=httpx.Timeout(
                                               connect=API_CONNECT_TIMEOUT_S, read=PUT_READ_TIMEOUT_S,
                                               write=PUT_READ_TIMEOUT_S, pool=API_CONNECT_TIMEOUT_S))
                    if 200 <= resp.status_code < 300:
                        return
                except httpx.HTTPError:
                    pass
                if attempt < PUT_TRIES - 1:
                    _sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            self.wlog(f"log upload failed: {type(e).__name__}")

    # ---------- the run ----------
    def run(self) -> dict:
        # 1. Host check.
        try:
            _assert_training_host()
        except HostUnfit as e:
            self.wlog(f"host check failed: {e.reason} ({e.detail})")
            status = self.call_finish({"outcome": "host_unfit", "reason": e.reason,
                                       "detail": e.detail})
            return {"outcome": "host_unfit", "reason": e.reason, "api": status}

        cmd = None
        for loop in range(1, START_LOOP_MAX + 2):
            if loop > START_LOOP_MAX:
                self.wlog(f"no usable checkpoint after {START_LOOP_MAX} start calls")
                status = self.call_finish({"outcome": "host_unfit", "reason": "download",
                                           "detail": "start loop exhausted"})
                return {"outcome": "host_unfit", "reason": "download", "api": status}
            # 2. start.
            answer = self.call_start()
            if answer is _STOP:
                return {"outcome": "stopped"}
            self.spec = answer["spec"]
            self.total_steps = int(self.spec["total_steps"])
            resume = answer.get("resume")
            if answer.get("finished"):
                status = self.call_finish({"outcome": "succeeded"})
                return {"outcome": "succeeded", "api": status}
            budget_s = int(answer["budget_s"])
            self.resume_step = int(resume["step"]) if resume else 0
            self.resumed = resume is not None
            with self.state_lock:
                self.last_step = self.resume_step
            if loop == 1:
                expected = str(_output_path(self.spec["model_name"]))
                if self.spec.get("output_dir") != expected:
                    self.wlog("spec.output_dir differs from the worker's own; using the worker's")
                # 3. The process's one first running write, then the heartbeat.
                if not self.first_running_write():
                    return {"outcome": "stopped"}
                self.start_progress()
            self.deadline = _monotonic() + budget_s
            if self.on_budget is not None:
                try:
                    self.on_budget(budget_s)
                except Exception:  # noqa: BLE001
                    pass
            # 4./5. Workers and the fresh command (its rmtree runs first).
            try:
                n = _num_workers(self.cpu_budget)
                fresh = _fresh_args(self.spec, n)
                if resume is None:
                    cmd = fresh
                    break
                # 6. Resume.
                cmd = _prepare_resume(resume, fresh, n, self.spec["model_name"],
                                      self.total_steps, self.client)
                break
            except _CkptBad as bad:
                self.wlog(f"checkpoint {self.resume_step} is bad: {bad.reason} ({bad.detail})")
                shutil.rmtree(OUTPUT_DIR / f".stage-{resume['step_id']}", ignore_errors=True)
                verdict = self.call_ckpt_bad(self.resume_step, bad.reason)
                if verdict == _STOP:
                    return {"outcome": "stopped"}
                if verdict == "refused":
                    continue
                status = self.call_finish({"outcome": "host_unfit", "reason": "download",
                                           "detail": f"ckpt-bad {bad.reason} -> {verdict}"})
                return {"outcome": "host_unfit", "reason": "download", "api": status}
            except _DownloadFailed as e:
                self.wlog(f"download failed: {e}")
                status = self.call_finish({"outcome": "host_unfit", "reason": "download",
                                           "detail": f"download: {e}"})
                return {"outcome": "host_unfit", "reason": "download", "api": status}
            except Exception as e:  # noqa: BLE001  (FR-13: never counts against the checkpoint)
                self.wlog(f"preparing the run failed: {type(e).__name__}: {_short(e, 300)}")
                status = self.call_finish({"outcome": "host_unfit", "reason": "download",
                                           "detail": f"{type(e).__name__}"})
                return {"outcome": "host_unfit", "reason": "download", "api": status}

        # 7. LeRobot.
        global _managed_proc
        self.wlog(f"running LeRobot ({'resume from ' + str(self.resume_step) if self.resumed else 'fresh'})")
        try:
            self.proc = _spawn_lerobot(cmd, _lerobot_env())
        except Exception as e:  # noqa: BLE001
            self.wlog(f"LeRobot could not be started: {type(e).__name__}")
            status = self.call_finish({"outcome": "host_unfit", "reason": "download",
                                       "detail": f"spawn: {type(e).__name__}"})
            return {"outcome": "host_unfit", "reason": "download", "api": status}
        _managed_proc = self.proc
        reader = threading.Thread(target=self._read_output, args=(self.proc,),
                                  name="managed-reader", daemon=True)
        reader.start()
        uploader = threading.Thread(target=self._uploader_loop, name="managed-uploader",
                                    daemon=True)
        uploader.start()
        self.threads += [reader, uploader]

        timed_out = False
        while self.proc.poll() is None:
            if self.fenced.is_set():
                self._kill_lerobot()
                return {"outcome": "stopped"}
            if _monotonic() >= self.deadline:
                self.wlog("training-time budget reached; stopping LeRobot")
                timed_out = True
                self._kill_lerobot()
                break
            _sleep(1)
        rc = self.proc.wait()
        reader.join(timeout=10)
        if self.fenced.is_set():
            return {"outcome": "stopped"}

        if timed_out:
            self.uploader_stop.set()
            self.upload_log()
            status = self.call_finish({"outcome": "failed", "reason": "time_cap"})
            return {"outcome": "failed", "reason": "time_cap", "api": status}

        # 9. Exit (E12, SR-3).
        if rc == 0:
            wait_until = _monotonic() + FINAL_UPLOAD_WAIT_S
            while (not self.final_uploaded() and not self.final_given_up()
                   and not self.fenced.is_set() and _monotonic() < wait_until):
                _sleep(1)
            if self.fenced.is_set():
                return {"outcome": "stopped"}
            if not self.final_uploaded():
                self.wlog("the final checkpoint was not uploaded in time")
            self.uploader_stop.set()
            self.upload_log()
            status = self.call_finish({"outcome": "succeeded"})
            return {"outcome": "succeeded", "api": status}

        self.uploader_stop.set()
        with self.state_lock:
            progressed = self.new_step_logged
        self.wlog(f"LeRobot exited with {rc} (new step logged: {progressed})")
        self.upload_log()
        detail = f"returncode={rc}"
        if progressed:
            payload = {"outcome": "host_unfit", "reason": "lerobot_crash", "detail": detail}
        elif not self.resumed:
            payload = {"outcome": "failed", "reason": "lerobot_error", "detail": detail}
        else:
            payload = {"outcome": "host_unfit", "reason": "resume_crash", "detail": detail}
        status = self.call_finish(payload)
        return {"outcome": payload["outcome"], "reason": payload["reason"], "api": status}

    def close(self) -> None:
        global _managed_proc
        self.uploader_stop.set()
        self.progress_stop.set()
        self._kill_lerobot()
        _managed_proc = None
        for t in self.threads:
            t.join(timeout=2)
        if self.proc is not None and self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def _file_chunks(path: Path):
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            yield chunk


def _retry_after_s(value) -> float:
    try:
        return float(min(max(int(str(value).strip()), 1), RATE_LIMIT_MAX_WAIT_S))
    except (TypeError, ValueError):
        return 5.0


def _valid_start_answer(body: dict) -> bool:
    spec = body.get("spec")
    if not isinstance(spec, dict) or not isinstance(body.get("finished"), bool):
        return False
    if not isinstance(body.get("budget_s"), int) or isinstance(body.get("budget_s"), bool):
        return False
    for key in ("dataset_name", "model_name"):
        if not isinstance(spec.get(key), str) or not spec[key]:
            return False
    if not isinstance(spec.get("training_params"), dict):
        return False
    for key in ("total_steps", "save_freq", "mirror_every"):
        v = spec.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            return False
    if spec.get("dataset_revision") is not None and not isinstance(spec["dataset_revision"], str):
        return False
    resume = body.get("resume")
    return resume is None or (isinstance(resume, dict) and isinstance(resume.get("step"), int))


def run_managed(api_url: str, supabase_url: str, supabase_anon_key: str, training_id: int,
                attempt: int, worker_token: str, cpu_budget: int, *, on_budget=None) -> dict:
    """Run one managed attempt (§4.7). Returns a small JSON-safe summary.

    `on_budget(budget_s)` is called whenever `start` answers (entry.py's watchdog).
    Never raises for an API, RPC, R2 or LeRobot failure: every reaction is §3.2's.
    """
    run = _ManagedRun(api_url, supabase_url, supabase_anon_key, training_id, attempt,
                      worker_token, cpu_budget, on_budget)
    try:
        return run.run()
    finally:
        run.close()
