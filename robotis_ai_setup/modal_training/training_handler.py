"""EduBotics cloud training handler (Modal worker body).

Invoked by `modal_app.train`.

Responsibilities:
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
        "--policy.device=cuda",
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
