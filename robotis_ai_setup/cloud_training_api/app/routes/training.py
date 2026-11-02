"""Student training routes (``/trainings``).

Since the training-hour ledger (migration 052, spec §8.2) only a student starts,
only managed ACT trains, and every start reserves 1–3 hours in the database
(``start_training_safe``, which refuses everything else again: P0060 / P0061).
Refused starts are recorded (``record_training_refusal``, best effort) for the
teacher's history. „Abbrechen" goes through ``services/stop_actions.py`` (one path
with the teacher's stop, §8.4); the legacy Modal reconciler only ever drives
pre-052 unmanaged rows.
"""

import asyncio
import json
import logging
import math
import os
import random
import string
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import get_current_user, get_user_profile
from app.services import artifact_validation, dataset_preflight, stop_actions, training_dispatch
from app.services import credit_texts_de as credit_texts
from app.services import training_texts_de as texts
from app.services.modal_client import (
    cancel_training_job,
    get_job_status,
)
from app.services.policy_profile import apply_training_tuning, get_policy_profile
from app.services.supabase_client import get_supabase
from app.services.workgroups import resolve_visible_workgroup_ids

logger = logging.getLogger(__name__)

# A worker that hasn't called update_training_progress() in this long is
# considered wedged. Default 25 min (was 15) to leave headroom for slow
# pi0 / smolvla checkpoint saves which can take 20+ min on disk-slow
# Modal workers. Override via env var for tighter ops control.
STALLED_WORKER_THRESHOLD = timedelta(
    minutes=int(os.environ.get("STALLED_WORKER_MINUTES", "25"))
)

# A row that's still queued with no cloud_job_id beyond this threshold
# means the API crashed (or the network dropped) between Modal dispatch
# and the row UPDATE that records the FunctionCall id. The row blocks
# the credit and cannot be canceled because we don't know the Modal
# call id. Conservative default — Modal cold-start + queue normally
# fits inside 5 minutes; 10 minutes leaves comfortable margin.
DISPATCH_LOST_THRESHOLD = timedelta(
    minutes=int(os.environ.get("DISPATCH_LOST_MINUTES", "10"))
)

# Idempotency window: a duplicate /start with the same (user, dataset, model)
# arriving inside this window returns the existing training_id instead of
# creating a new row. Catches both client retries on network timeout and
# accidental double-clicks in the UI.
DEDUPE_WINDOW = timedelta(seconds=60)

# Hard upper bounds on training_params. Steps especially is a cost-bomb risk:
# a malicious or buggy client could request 1B steps and burn the GPU budget.
# All three are env-configurable so a deployment with bigger GPUs (A100/H100)
# can raise them without a code change.
MAX_STEPS = int(os.environ.get("MAX_TRAINING_STEPS", "500000"))
MAX_BATCH_SIZE = int(os.environ.get("MAX_TRAINING_BATCH_SIZE", "256"))
MAX_TIMEOUT_HOURS = float(os.environ.get("MAX_TRAINING_TIMEOUT_HOURS", "12.0"))

# 052 (decision 12, H4): the cloud trains managed ACT and nothing else. The
# ALLOWED_POLICIES variable is gone; start_training_safe refuses any other type
# again in the database (P0061), so a direct API or PostgREST call cannot bypass it.
TRAINABLE_POLICIES = frozenset({"act"})

# Per-policy max timeout. Caps a wedged job from burning the full handler
# timeout. Bumped for serious training: ACT routinely needs 3-4h on real
# datasets; pi0/smolvla fine-tunes can run 8h+. Applied after validation
# and before Modal dispatch so it's always enforced regardless of what the
# client sends. Override via POLICY_TIMEOUT_OVERRIDES_JSON env var if a
# deployment needs different caps.
POLICY_MAX_TIMEOUT_HOURS = {
    "act": 3.0,              # 052: the longest reservation (MANAGED_ACT_CAP_S ≤ 10 800 s)
    "vqbet": 4.0,
    "tdmpc": 4.0,
    "diffusion": 6.0,
    "pi0_fast": 6.0,         # v0.5.1 underscore rename (was "pi0fast" pre-v2.5.0)
    "pi0": 10.0,
    "pi05": 10.0,            # v0.5.1 NEW: Pi-0.5 VLA (similar weight class to pi0)
    "smolvla": 10.0,
}
_policy_overrides_raw = os.environ.get("POLICY_TIMEOUT_OVERRIDES_JSON", "").strip()
if _policy_overrides_raw:
    try:
        _overrides = json.loads(_policy_overrides_raw)
        if isinstance(_overrides, dict):
            for _k, _v in _overrides.items():
                POLICY_MAX_TIMEOUT_HOURS[str(_k).lower()] = float(_v)
    except (ValueError, TypeError) as _e:
        logger.warning(
            "POLICY_TIMEOUT_OVERRIDES_JSON ignored (bad JSON): %s", _e
        )

router = APIRouter(prefix="/trainings", tags=["training"])


# ---------- Request / Response models ----------


class TrainingParams(BaseModel):
    """Validated training hyperparameters. Bounded so a malicious or buggy
    client cannot request a training that would burn the entire GPU budget.

    Field set must match what physical_ai_manager/src/components/TrainingControlPanel.js
    sends — fields not declared here are silently dropped by Pydantic and
    never reach the Modal handler. (Frontend sends: seed, num_workers,
    batch_size, steps, eval_freq, log_freq, save_freq, output_folder_name.)
    """
    steps: int = Field(..., ge=1, le=MAX_STEPS, description="Total training steps")
    batch_size: int | None = Field(default=None, ge=1, le=MAX_BATCH_SIZE)
    num_workers: int | None = Field(default=None, ge=0, le=16)
    log_freq: int | None = Field(default=None, ge=1, le=100_000)
    save_freq: int | None = Field(default=None, ge=1, le=100_000)
    # eval_freq=0 is the LeRobot convention for "no eval" — must allow ge=0.
    # The handler also forces --eval_freq=0 because the cloud worker has no
    # simulation env, but we still accept the field for forward compatibility.
    eval_freq: int | None = Field(default=None, ge=0, le=100_000)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    timeout_hours: float | None = Field(default=None, gt=0, le=MAX_TIMEOUT_HOURS)
    # Cosmetic: lets the student name the HF model folder. Sanitized server-side
    # before being baked into model_name.
    output_folder_name: str | None = Field(default=None, max_length=128)


class StartTrainingRequest(BaseModel):
    dataset_name: str = Field(..., min_length=1, max_length=200)
    model_type: str = Field(..., min_length=1, max_length=64)
    training_params: TrainingParams
    # 052 §8.2: one fresh UUID per confirmed start dialog; a retry of the same send
    # reuses it, and start_training_safe answers the same row (replay).
    idempotency_key: uuid.UUID | None = None

    @field_validator("dataset_name")
    @classmethod
    def _dataset_name_shape(cls, v: str) -> str:
        # HuggingFace dataset id is "user/repo" — reject obvious garbage early.
        if "/" not in v or v.startswith("/") or v.endswith("/"):
            raise ValueError("dataset_name must be in 'owner/repo' form")
        return v
    # The model type is judged by the route (a German 400 that records the refusal),
    # never by a pydantic validator, whose 422 is English (052 §8.2).


class CancelTrainingRequest(BaseModel):
    training_id: int


class TrainingJob(BaseModel):
    id: int
    status: str
    dataset_name: str
    model_name: str
    model_type: str
    training_params: dict | None
    current_step: int | None = 0
    total_steps: int | None = 0
    current_loss: float | None = None
    # Downsampled loss curve: [{"s": step, "l": loss, "t": ms_since_epoch}, ...].
    # Capped at 300 points by the update_training_progress RPC.
    loss_history: list[dict] | None = None
    requested_at: str
    terminated_at: str | None
    error_message: str | None
    last_progress_at: str | None = None
    # 028: full worker stdout (training_log.txt in the private HF model
    # repo); teacher/admin forensics, set on the terminal transition.
    log_url: str | None = None
    # 049: a managed (RunPod/Modal ladder) training's progress line, e.g. „GPU-Server
    # ausgefallen – das Training wird ab Schritt 2.500 … fortgesetzt." (S1–S3).
    status_note: str | None = None
    managed: bool = False
    # 050: a capped/wall-capped training that the student may continue (owner E32–E37).
    capped_step: int | None = None
    continuable_until: str | None = None
    continued_from: int | None = None
    # Server-computed: whether the caller started this training (owner E46; only the
    # starter sees „Fortsetzen"). user_id itself is never exposed.
    is_mine: bool | None = None
    # 052: the reservation, the settled charge, who stopped it, the uncharged lost time.
    reserved_hours: int | None = None
    charged_hours: int | None = None
    stop_requested_by_role: str | None = None
    lost_seconds: int | None = 0


class ContinueTrainingRequest(BaseModel):
    training_id: int


class StartTrainingResponse(BaseModel):
    training_id: int
    model_name: str
    status: str
    # 052: the hours this start reserved; replay = an earlier identical start answered.
    reserved_hours: int | None = None
    replay: bool = False


class UserQuota(BaseModel):
    """052 §8.2: the hour account a start would use, plus the three legacy keys a robot
    running an older SPA still reads."""
    account: str | None = None
    workgroup_name: str | None = None
    hours_free: int
    hours_reserved: int
    hours_spent: int
    member_limit_hours: int | None = None
    member_used_hours: int | None = None
    member_left_hours: int | None = None
    next_reservation_hours: int
    max_hours_per_training: int
    training_credits: int
    trainings_used: int
    remaining: int


class StopPreview(BaseModel):
    """GET …/stop-preview (§8.2): what „Abbrechen" / „Training stoppen" would do now. Advisory."""
    training_id: int
    trained: bool
    checkpoint_step: int | None = None
    next_checkpoint_step: int | None = None
    final_ready: bool
    reserved_hours: int
    charged_hours_now: int
    hours_back_now: int
    outcome: str


# ---------- Helpers ----------

MODAL_TO_DB_STATUS = {
    "QUEUED": "queued",
    "IN_QUEUE": "queued",
    "IN_PROGRESS": "running",
    "COMPLETED": "succeeded",
    "FAILED": "failed",
    "CANCELLED": "canceled",
    "TIMED_OUT": "failed",
}


def _sanitize_name(name: str) -> str:
    """Keep only HF-safe characters: alphanumeric, hyphens, underscores, dots."""
    import re
    return re.sub(r"[^a-zA-Z0-9._-]", "-", name).strip("-") or "unnamed"


def _generate_model_name(
    model_type: str, dataset_name: str, output_folder_name: str | None = None
) -> str:
    """Compose the HuggingFace repo id for the trained model.

    Format: EduBotics-Solutions/<output_folder>-<model_type>-<dataset>-<suffix>
    Where <output_folder>- is omitted if the user did not pick one. The
    random suffix prevents collisions when the same student starts the same
    training twice.
    """
    dataset_base = dataset_name.split("/")[-1] if "/" in dataset_name else dataset_name
    dataset_base = _sanitize_name(dataset_base)
    model_type_safe = _sanitize_name(model_type)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    parts: list[str] = []
    if output_folder_name:
        parts.append(_sanitize_name(output_folder_name))
    parts.extend([model_type_safe, dataset_base, suffix])
    return "EduBotics-Solutions/" + "-".join(parts)


def cap_hours() -> int:
    """The longest reservation this deploy grants (MANAGED_ACT_CAP_S // 3600, 1–3)."""
    return int(training_dispatch.SETTINGS.cap_s) // 3600


def credit_quota_row(user_id: str, sb=None) -> dict | None:
    """``credit_quota`` (052 §4.8, read-only DEFINER) for one user, or None when unknown.
    ``sb``: the caller's client (routes/me.py passes its own)."""
    data = (sb or get_supabase()).rpc(
        "credit_quota", {"p_user_id": str(user_id), "p_max_hours": cap_hours()}
    ).execute().data
    rows = data if isinstance(data, list) else ([data] if data else [])
    return rows[0] if rows else None


def quota_view(q: dict, workgroup_name: str | None = None) -> dict:
    """The quota row as the student's account view (§8.2). ``member_left_hours`` is floored
    at 0 (review 3 m12: a limit set below what a member already used never reads negative);
    ``startable_free`` is the free hours capped by it, and feeds the three legacy keys."""
    free = int(q.get("o_free") or 0)
    reserved = int(q.get("o_reserved") or 0)
    spent = int(q.get("o_spent") or 0)
    limit = q.get("o_member_limit")
    used = q.get("o_member_used")
    left = None if limit is None else max(0, int(limit) - int(used or 0))
    startable = free if left is None else min(free, left)
    return {
        "account": q.get("o_account_kind"),
        "workgroup_name": workgroup_name,
        "hours_free": free,
        "hours_reserved": reserved,
        "hours_spent": spent,
        "member_limit_hours": None if limit is None else int(limit),
        "member_used_hours": None if used is None else int(used),
        "member_left_hours": left,
        "next_reservation_hours": int(q.get("o_next_hours") or 0),
        "max_hours_per_training": cap_hours(),
        "training_credits": startable + reserved + spent,
        "trainings_used": reserved + spent,
        "remaining": startable,
    }


def _workgroup_name(q: dict) -> str | None:
    if q.get("o_account_kind") != "group" or not q.get("o_account_id"):
        return None
    try:
        rows = get_supabase().table("workgroups").select("name").eq("id", q["o_account_id"]).execute().data or []
    except Exception as exc:  # noqa: BLE001 - the name is cosmetic
        logger.warning("workgroup name lookup failed: %s", type(exc).__name__)
        return None
    return rows[0].get("name") if rows else None


def record_refusal(user_id: str, request_kind: str, code: str, model_type) -> None:
    """record_training_refusal, best effort: a failure never blocks the answer (§8.2)."""
    try:
        get_supabase().rpc("record_training_refusal", {
            "p_user_id": str(user_id), "p_request_kind": request_kind, "p_code": code,
            "p_model_type": model_type if isinstance(model_type, str) else None,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_training_refusal(%s, %s) failed: %s", request_kind, code, type(exc).__name__)


def _hours_refusal(user_id: str, request_kind: str, model_type, q: dict | None = None):
    """The 403 of a start without hours, recorded: ``member_limit`` when the group limit is
    the cause (set, used up, and the group still has free hours), else ``no_hours``."""
    member = bool(q) and q.get("o_member_limit") is not None and int(q.get("o_member_used") or 0) >= int(
        q.get("o_member_limit")) and int(q.get("o_free") or 0) >= 1
    record_refusal(user_id, request_kind, "member_limit" if member else "no_hours", model_type)
    return HTTPException(status_code=403,
                         detail=credit_texts.MEMBER_LIMIT_DE if member else credit_texts.NO_HOURS_DE)


def _same_start(row: dict, dataset_name: str, model_type: str, steps) -> bool:
    """§4.3 step 4 / §8.2 step 4: the same dataset, policy and step count."""
    params = row.get("training_params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = None
    row_steps = params.get("steps") if isinstance(params, dict) else None
    return (row.get("dataset_name") == dataset_name
            and str(row.get("model_type") or "").lower() == str(model_type).lower()
            and row_steps == steps)


def _parse_iso(s: str | None) -> datetime | None:
    """Parse a Postgres TIMESTAMPTZ ISO string from supabase-py output."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def _sync_modal_status(training: dict) -> dict:
    """Reconcile a training row against Modal's view of the job.

    Three cases get the row marked failed:
      1. Modal reports a terminal state (COMPLETED/FAILED/CANCELLED/TIMED_OUT)
      2. Modal can't find the job at all (unknown status)
      3. The worker is wedged: Modal still says IN_PROGRESS but no progress
         reported in STALLED_WORKER_THRESHOLD. We cancel the Modal call to
         stop burning GPU money and mark the row failed.

    Async: uses Modal's async SDK via .aio() to avoid blocking the FastAPI
    event loop. Safe to call from sync context only via asyncio.run().
    """
    if training["status"] not in ("queued", "running"):
        return training
    # 049: a managed row is driven by training_dispatch's dispatcher and sweep
    # alone (its cloud_job_id is unset and its stall clock is step_advanced_at);
    # this legacy reconciler would fail it as „dispatch lost" (§1 item 7).
    if training.get("managed"):
        return training

    job_id = training.get("cloud_job_id")
    if not job_id:
        # Row was inserted by start_training_safe but the API crashed (or
        # network dropped) before it could record the Modal FunctionCall id
        # via the UPDATE on /trainings/start step 4. The worker may or may
        # not be alive on Modal — we can't tell because we don't have its
        # id. Once the dispatch-lost threshold elapses, fail the row so the
        # credit isn't blocked forever. The Modal worker (if any) will
        # eventually be evicted by Modal's own timeout; if it tries to write
        # progress meanwhile, the worker_token still validates and the row
        # transitions back to running — self-healing.
        if training["status"] == "queued":
            requested = _parse_iso(training.get("requested_at"))
            now = datetime.now(timezone.utc)
            if requested and (now - requested) > DISPATCH_LOST_THRESHOLD:
                logger.warning(
                    "Dispatch lost: training %s queued %s with no cloud_job_id",
                    training["id"], now - requested,
                )
                supabase = get_supabase()
                update_data = {
                    "status": "failed",
                    "terminated_at": now.isoformat(),
                    "worker_token": None,
                    "error_message": (
                        "Dispatch an Cloud-Worker fehlgeschlagen "
                        "(keine Job-ID erhalten). Bitte Training neu "
                        "starten — der Credit wurde freigegeben."
                    ),
                }
                supabase.table("trainings").update(update_data).eq(
                    "id", training["id"]
                ).execute()
                training["status"] = "failed"
                training["terminated_at"] = update_data["terminated_at"]
                training["error_message"] = update_data["error_message"]
        return training

    try:
        modal_status = await get_job_status(job_id)
    except Exception as e:
        # get_job_status is supposed to swallow all Modal errors and return a
        # sentinel string. Reaching here means something unusual (e.g. an
        # asyncio.CancelledError). Don't touch the row — fall through safely.
        logger.warning("Modal status check failed for call %s: %s", job_id, e)
        return training

    db_status = MODAL_TO_DB_STATUS.get(modal_status, training["status"])

    # Case 3: stalled worker — Modal says IN_PROGRESS but no progress in N minutes.
    stalled = False
    if db_status == "running":
        last_progress = _parse_iso(
            training.get("last_progress_at") or training.get("requested_at")
        )
        now = datetime.now(timezone.utc)
        if last_progress and (now - last_progress) > STALLED_WORKER_THRESHOLD:
            logger.warning(
                "Stalled worker detected: training %s, no progress for %s",
                training["id"], now - last_progress,
            )
            try:
                await cancel_training_job(job_id)
            except Exception as e:
                logger.warning("Failed to cancel stalled job %s: %s", job_id, e)
            db_status = "failed"
            stalled = True

    if db_status == training["status"]:
        return training

    supabase = get_supabase()
    update_data: dict = {"status": db_status}
    if db_status in ("succeeded", "failed", "canceled"):
        update_data["terminated_at"] = datetime.now(timezone.utc).isoformat()
    if db_status == "failed":
        if stalled:
            stalled_minutes = int(STALLED_WORKER_THRESHOLD.total_seconds() / 60)
            update_data["error_message"] = (
                f"Worker hat über {stalled_minutes} Minuten keine Updates gesendet "
                f"(vermutlich hängt der Trainings-Prozess). Job wurde abgebrochen."
            )
        else:
            update_data["error_message"] = f"Training fehlgeschlagen (Modal-Status: {modal_status})."

    supabase.table("trainings").update(update_data).eq("id", training["id"]).execute()

    # Only a pre-052 legacy (`derived`) row reaches this reconciler: its credit is
    # derived from the status, so "failed" frees it; a `ledger` row is managed and
    # returned untouched above.

    training["status"] = db_status
    if "terminated_at" in update_data:
        training["terminated_at"] = update_data["terminated_at"]
    if "error_message" in update_data:
        training["error_message"] = update_data["error_message"]
    return training


def _find_recent_duplicate(
    user_id: str, dataset_name: str, model_type: str,
    training_params: dict | None = None,
    workgroup_id: str | None = None,
) -> dict | None:
    """Idempotency check. Returns an existing matching training if one was
    started within DEDUPE_WINDOW, otherwise None.

    Dedup key was historically only (user, dataset, model_type), so a
    student could tweak `steps=5000 -> 5001` to bypass dedup and burn a
    second credit. We now also compare training_params — if a pending/
    running row has the same params, it's a duplicate; different params
    are treated as a fresh request.

    Group-aware: when ``workgroup_id`` is set the search includes group
    siblings — otherwise three students racing the same dataset/model
    inside the same group would each pop a credit before the dedupe could
    fire on any one of them.

    Excludes failed/canceled rows so a user can immediately retry after a
    failure without being blocked by their own previous attempt.
    """
    supabase = get_supabase()
    window_start = (datetime.now(timezone.utc) - DEDUPE_WINDOW).isoformat()
    q = (
        supabase.table("trainings")
        .select("*")
        .eq("dataset_name", dataset_name)
        .eq("model_type", model_type)
        .gte("requested_at", window_start)
        .not_.in_("status", ["failed", "canceled"])
        # 050/n1: a continuation child shares its parent's dataset/model/params, so it
        # must never be returned as the duplicate of a fresh /trainings/start.
        .is_("continued_from", "null")
        .order("requested_at", desc=True)
        .limit(10)
    )
    if workgroup_id:
        q = q.eq("workgroup_id", workgroup_id)
    else:
        q = q.eq("user_id", user_id)
    result = q.execute()
    rows = result.data or []
    if not rows:
        return None

    if training_params is None:
        return rows[0]

    # Canonicalize params so key ordering + JSON-vs-dict representation
    # don't give false-negatives. Any row with matching params is a dup.
    def _canonical(params):
        try:
            if isinstance(params, str):
                params = json.loads(params)
        except (ValueError, TypeError):
            return None
        if not isinstance(params, dict):
            return None
        # A managed row stores the dataset commit (E17) AFTER this compare runs
        # on the request (§4.1 step 4), so a repeated request still matches.
        params = {k: v for k, v in params.items() if k != "dataset_revision"}
        return json.dumps(params, sort_keys=True, default=str)

    want = _canonical(training_params)
    for row in rows:
        if _canonical(row.get("training_params")) == want:
            return row
    return None


async def _sweep_user_running_jobs(user_id: str, workgroup_id: str | None = None) -> None:
    """Sync every running/queued row this user (or their group) owns.

    Used at top of /start so a stuck row from a previous session can't
    block a fresh credit check. When the caller is in a group we also
    sweep group siblings' rows because the credit check now runs against
    the group pool — a wedged sibling would otherwise eat a slot.

    Async + parallel: all rows are synced concurrently via asyncio.gather,
    which scales better than serial sync calls when a user has many active rows.
    """
    supabase = get_supabase()
    q = (
        supabase.table("trainings")
        .select("*")
        .in_("status", ["queued", "running"])
    )
    if workgroup_id:
        q = q.eq("workgroup_id", workgroup_id)
    else:
        q = q.eq("user_id", user_id)
    result = q.execute()

    async def _sync_one(row):
        try:
            await _sync_modal_status(row)
        except Exception as e:
            logger.warning("Sweep sync failed for training %s: %s", row.get("id"), e)

    # Managed rows are the managed sweep's (training_dispatch), never this one's.
    rows = [r for r in (result.data or []) if not r.get("managed")]
    if rows:
        await asyncio.gather(*[_sync_one(row) for row in rows])


# ---------- Endpoints ----------


@router.get("/quota", response_model=UserQuota)
async def get_quota(user=Depends(get_current_user)):
    q = credit_quota_row(str(user.id))
    if q is None:
        raise HTTPException(status_code=404, detail="Benutzerprofil nicht gefunden.")
    return UserQuota(**quota_view(q, _workgroup_name(q)))


@router.post("/start", response_model=StartTrainingResponse)
async def start_training(req: StartTrainingRequest, user=Depends(get_current_user)):
    """§8.2, in order: role → dispatch → policy → the key replay → the quota (fails fast,
    before the 60 s dataset check) → sweep, params, dedupe, dataset check → the
    reservation in ``start_training_safe`` (which judges role, policy and hours again)."""
    supabase = get_supabase()
    user_id = str(user.id)
    profile = get_user_profile(user_id)
    workgroup_id = profile.get("workgroup_id")
    model_type = req.model_type
    key = getattr(req, "idempotency_key", None)
    key = str(key) if key is not None else None

    logger.info(
        "POST /trainings/start user=%s group=%s dataset=%s model_type=%s steps=%s",
        user_id, workgroup_id, req.dataset_name, model_type, req.training_params.steps,
    )

    # 1. Students only (H6): teachers and admins have no start path.
    if profile.get("role") != "student":
        record_refusal(user_id, "start", "role", model_type)
        raise HTTPException(status_code=403, detail=credit_texts.ROLE_REFUSED_DE)
    # 2. TRAINING_DISPATCH=legacy stops new trainings (§9.1); not the student's doing,
    #    so not recorded.
    settings = training_dispatch.SETTINGS
    if not settings.managed:
        raise HTTPException(status_code=503, detail=credit_texts.DISPATCH_OFF_DE)
    # 3. Managed ACT only (H4, decision 12).
    if model_type.lower() not in TRAINABLE_POLICIES:
        record_refusal(user_id, "start", "policy", model_type)
        raise HTTPException(status_code=400, detail=credit_texts.POLICY_REFUSED_DE)
    # 4. A key already used: the same start answers the same row; another one is a 409
    #    (review 3 m10: the database's P0071 is the second line, not the only one).
    if key is not None:
        rows = (
            supabase.table("trainings").select("id, model_name, status, dataset_name, model_type, "
                                               "training_params, reserved_hours")
            .eq("user_id", user_id).eq("idempotency_key", key).execute()
        ).data or []
        if rows:
            row = rows[0]
            if not _same_start(row, req.dataset_name, model_type, req.training_params.steps):
                raise HTTPException(status_code=409, detail=credit_texts.REQUEST_REUSED_DE)
            logger.info("start replay: training %s for user=%s", row["id"], user_id)
            return StartTrainingResponse(training_id=row["id"], model_name=row["model_name"],
                                         status=row["status"], reserved_hours=row.get("reserved_hours"), replay=True)
    # 5. Hours: fail fast, before the dataset check (§4.2's one rule, via credit_quota).
    q = credit_quota_row(user_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Benutzerprofil nicht gefunden.")
    if int(q.get("o_next_hours") or 0) < 1:
        raise _hours_refusal(user_id, "start", model_type, q)

    # 6. Sweep stuck LEGACY rows (managed rows are the managed sweep's), then the params.
    await _sweep_user_running_jobs(user_id, workgroup_id=workgroup_id)
    training_params_dict = req.training_params.model_dump(exclude_none=True)
    policy_cap = POLICY_MAX_TIMEOUT_HOURS.get(model_type.lower())
    if policy_cap is not None:
        requested = training_params_dict.get("timeout_hours", policy_cap)
        training_params_dict["timeout_hours"] = min(requested, policy_cap)
    apply_training_tuning(get_policy_profile(model_type), training_params_dict)  # ACT: batch pinned to 8
    # The cadences are written into the row so a later environment change cannot strand
    # a running training (FR-12); the worker picks num_workers itself (owner D4/E3).
    training_params_dict["save_freq"] = settings.save_freq
    training_params_dict["mirror_every"] = settings.mirror_every
    training_params_dict.pop("num_workers", None)

    # The 60 s dedupe (unchanged; group-scoped when grouped): a hit answers the existing
    # row exactly like a key replay.
    duplicate = _find_recent_duplicate(
        user_id, req.dataset_name, model_type,
        training_params=training_params_dict,
        workgroup_id=workgroup_id,
    )
    if duplicate:
        logger.info("Dedupe hit: returning existing training %s for user=%s", duplicate["id"], user_id)
        return StartTrainingResponse(
            training_id=duplicate["id"], model_name=duplicate["model_name"], status=duplicate["status"],
            reserved_hours=duplicate.get("reserved_hours"), replay=True,
        )

    # The dataset check (§4.1 step 5): anonymously, as the pod will read it, pinned to the
    # commit the v3.0 tag points to now (E17); its features must be ones the robot sends (S20).
    try:
        info, revision = await asyncio.wait_for(
            asyncio.to_thread(_dataset_check, req.dataset_name, model_type, True), 60
        )
    except dataset_preflight.DatasetRefused as e:
        # Only this module's own German sentences reach the student: never a library
        # exception's text (a ValueError of huggingface_hub included).
        logger.info("Dataset check refused %s: %s", req.dataset_name, e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # HubUnavailable (timeout, rate limit, DNS blip, 5xx), the 60 s bound, anything
        # else — tell the student to retry. The type goes to the log, never its text.
        logger.warning("HF dataset check transient error for %s: %s", req.dataset_name, type(e).__name__)
        raise HTTPException(status_code=502, detail=texts.HF_UNREACHABLE_DE)
    if revision:
        training_params_dict["dataset_revision"] = revision

    # 7. The reservation and the row, in ONE transaction (§4.3). A managed row gets its
    #    worker token per attempt from claim_training_attempt.
    model_name = _generate_model_name(
        model_type, req.dataset_name, output_folder_name=req.training_params.output_folder_name,
    )
    try:
        rpc_result = supabase.rpc(
            "start_training_safe",
            {
                "p_user_id": user_id,
                "p_dataset_name": req.dataset_name,
                "p_model_name": model_name,
                "p_model_type": model_type,
                "p_training_params": training_params_dict,
                "p_total_steps": training_params_dict["steps"],
                "p_worker_token": None,
                "p_managed": True,
                "p_idempotency_key": key,
                "p_max_hours": cap_hours(),
            },
        ).execute()
    except Exception as e:
        code = training_dispatch.pg_code(e)
        if code in ("P0003", "P0063"):
            logger.info("start refused (%s) for user=%s", code, user_id)
            record_refusal(user_id, "start", "member_limit" if code == "P0063" else "no_hours", model_type)
            raise HTTPException(status_code=403, detail=credit_texts.MEMBER_LIMIT_DE if code == "P0063"
                                else credit_texts.NO_HOURS_DE)
        if code == "P0060":
            record_refusal(user_id, "start", "role", model_type)
            raise HTTPException(status_code=403, detail=credit_texts.ROLE_REFUSED_DE)
        if code == "P0061":
            record_refusal(user_id, "start", "policy", model_type)
            raise HTTPException(status_code=400, detail=credit_texts.POLICY_REFUSED_DE)
        if code == "P0071":
            raise HTTPException(status_code=409, detail=credit_texts.REQUEST_REUSED_DE)
        if code == "P0002":
            logger.warning("User profile not found: %s", user_id)
            raise HTTPException(status_code=404, detail="Benutzerprofil nicht gefunden.")
        logger.error("start_training_safe RPC failed for user=%s: %s", user_id, type(e).__name__)
        raise

    if not rpc_result.data:
        logger.error("start_training_safe returned no rows for user=%s", user_id)
        raise HTTPException(status_code=500, detail="Interner Fehler beim Starten des Trainings.")
    created = rpc_result.data[0]
    training_id = created["training_id"]
    reserved = created.get("reserved_hours")
    if created.get("replay"):
        rows = supabase.table("trainings").select("model_name, status").eq("id", training_id).execute().data or []
        row = rows[0] if rows else {"model_name": model_name, "status": "queued"}
        return StartTrainingResponse(training_id=training_id, model_name=row["model_name"], status=row["status"],
                                     reserved_hours=reserved, replay=True)
    logger.info("Created training %s for user=%s model=%s reserved=%s h", training_id, user_id, model_name, reserved)

    # The dispatcher places it (claim → RunPod/Modal → attach).
    fps = (info or {}).get("fps")
    if isinstance(fps, (int, float)) and not isinstance(fps, bool) and fps > 0:
        try:
            supabase.table("trainings").update({"dataset_fps": fps}).eq("id", training_id).eq(
                "managed", True
            ).execute()
        except Exception as e:  # metadata only: the publish re-reads meta/info.json
            logger.warning("dataset_fps write failed for training %s: %s", training_id, type(e).__name__)
    training_dispatch.kick()
    return StartTrainingResponse(training_id=training_id, model_name=model_name, status="queued",
                                 reserved_hours=reserved, replay=False)


def _dataset_check(dataset_name: str, model_type: str, managed: bool = True):
    """(info, revision) — BLOCKING; the route runs it in a thread under a 60 s bound.

    Since 052 every start is managed: the dataset is read anonymously at the commit
    the v3.0 tag points to now (E17). Raises dataset_preflight.DatasetRefused with the
    German sentence a student reads (400); anything else is the route's 502."""
    revision = dataset_preflight.resolve_v3_commit(dataset_name)
    info = dataset_preflight.preflight_dataset(
        dataset_name, False, model_type, revision=revision or dataset_preflight.EXPECTED_CODEBASE_VERSION
    )
    try:
        inputs, outputs = artifact_validation.dataset_policy_features(info.get("features"))
        artifact_validation.act_feature_rules(inputs, outputs)
    except artifact_validation.ArtifactRefused as exc:
        logger.info("dataset %s refused for managed ACT: %s", dataset_name, exc.reason)
        raise dataset_preflight.DatasetRefused(texts.S20) from None
    return info, revision


@router.post("/cancel")
async def cancel_training(req: CancelTrainingRequest, user=Depends(get_current_user)):
    """„Abbrechen" (052 §8.2, §8.4): ``stop_actions.stop`` — after a GPU trained the newest
    ready checkpoint is published (continuable, owner D6), else the two-phase cancel.
    Either way the 052 settlement charges the started hours; nothing is refunded. Answers
    ``{status, training_id, outcome, checkpoint_step, charged_hours}``."""
    user_id = str(user.id)
    logger.info("POST /trainings/cancel user=%s training_id=%s", user_id, req.training_id)
    return await stop_actions.stop(req.training_id, owner=user_id, actor=user_id, role="student", note=texts.S30)


def _next_checkpoint_step(row) -> int | None:
    """The next multiple of the row's own save_freq above its current step, capped at the
    total (review m13: right for a resumed or continued training too)."""
    total = training_dispatch.total_steps_of(row)
    every = training_dispatch.save_freq_of(row)
    step = int(row.get("current_step") or 0)
    nxt = (step // every + 1) * every
    return min(nxt, total) if total > 0 else nxt


def stop_preview_for(row: dict, attempts: list, ready: list, now: float) -> dict:
    """§8.2's preview with §4.9's rules, from the row, its attempts and its ready checkpoints."""
    D = training_dispatch
    tid = int(row["id"])
    total = D.total_steps_of(row)
    reserved = int(row.get("reserved_hours") or 0)
    trained = any(a.get("first_heartbeat_at") for a in attempts)
    n = int(row.get("current_attempt") or 0)
    cur = next((a for a in attempts if int(a.get("attempt_no") or 0) == n), None) if n > 0 else None
    final = D.final_ready(ready, total)
    newest = D.newest_ready_checkpoint(ready, total)
    outcome, step = "cancel", None
    if (row.get("status") != "cancel_requested" and row.get("managed") and D.is_ledger(row) and trained
            and cur is not None and cur.get("state") in ("placing", "starting", "running", "lost")
            and (final is not None or newest is not None)):
        outcome = "publish"
        step = total if final is not None else int(newest["step"])
    seconds = int(row.get("train_seconds_used") or 0)
    if cur is not None and cur.get("state") in ("placing", "starting", "running"):
        bound = D.ts(row.get("stop_requested_at"))
        seconds += D.attempt_seconds(cur, min(now, bound) if bound else now)
    charged = min(reserved, math.ceil(max(seconds, 0) / 3600)) if (trained and D.is_ledger(row)) else 0
    return {
        "training_id": tid, "trained": bool(trained), "checkpoint_step": step,
        "next_checkpoint_step": _next_checkpoint_step(row), "final_ready": final is not None,
        "reserved_hours": reserved, "charged_hours_now": charged, "hours_back_now": max(reserved - charged, 0),
        "outcome": outcome,
    }


def read_stop_preview(training_id: int, owner_id: str) -> dict:
    """The preview of ``owner_id``'s training (a row of another user is the German 404);
    a training that is not active is the stop's own 400. BLOCKING."""
    supabase = get_supabase()
    rows = supabase.table("trainings").select("*").eq("id", int(training_id)).execute().data or []
    row = rows[0] if rows else None
    if row is None or row.get("user_id") != str(owner_id):
        raise HTTPException(status_code=404, detail=texts.TRAINING_NOT_FOUND_DE)
    if row.get("status") not in ("queued", "running", "cancel_requested"):
        raise HTTPException(status_code=400, detail=texts.TRAINING_NOT_ACTIVE_DE)
    attempts = training_dispatch.read_attempts([int(training_id)])
    ready = training_dispatch.read_checkpoints([int(training_id)], ["ready"])
    return stop_preview_for(row, attempts, ready, training_dispatch._now())


@router.get("/{training_id}/stop-preview", response_model=StopPreview)
async def get_stop_preview(training_id: int, user=Depends(get_current_user)):
    """What „Abbrechen" would do now (advisory, starter only)."""
    return StopPreview(**await asyncio.to_thread(read_stop_preview, training_id, str(user.id)))


def _continuation_dataset_check(dataset_name: str, revision: str):
    """BLOCKING preflight at the parent's pinned revision (E17: never re-resolve the tag).
    Raises dataset_preflight.DatasetRefused (→ 400) or anything else (→ 502)."""
    return dataset_preflight.preflight_dataset(dataset_name, False, "act", revision=revision)


@router.post("/continue", response_model=StartTrainingResponse)
async def continue_training(req: ContinueTrainingRequest, user=Depends(get_current_user)):
    """Continue a capped/stopped/given-up training toward its original step target (owner
    E32–E37, D6, Q-A). Owner-only (E46), students only (052): a NEW reservation from the
    student's own or group hours, a managed child resuming from the 7-day kept copy,
    publishing into the same HF repo."""
    supabase = get_supabase()
    user_id = str(user.id)
    logger.info("POST /trainings/continue user=%s training_id=%s", user_id, req.training_id)
    profile = get_user_profile(user_id)
    if profile.get("role") != "student":
        record_refusal(user_id, "continue", "role", "act")
        raise HTTPException(status_code=403, detail=credit_texts.ROLE_REFUSED_DE)
    if not training_dispatch.SETTINGS.managed:
        raise HTTPException(status_code=409, detail=texts.S26)
    rows = supabase.table("trainings").select("*").eq("id", req.training_id).execute().data or []
    parent = rows[0] if rows else None
    if parent is None or parent.get("user_id") != user_id:  # never 403 (Rule §4)
        raise HTTPException(status_code=404, detail=texts.TRAINING_NOT_FOUND_DE)
    total = training_dispatch.total_steps_of(parent)
    capped = parent.get("capped_step")
    if (not parent.get("managed") or parent.get("status") != "succeeded"
            or not isinstance(capped, int) or capped >= total or not parent.get("continuable_until")):
        raise HTTPException(status_code=409, detail=texts.S26)
    until = training_dispatch.ts(parent.get("continuable_until"))
    if until is not None and until <= training_dispatch._now():
        raise HTTPException(status_code=409, detail=texts.S24)
    params = training_dispatch._params(parent)
    revision = params.get("dataset_revision") or dataset_preflight.EXPECTED_CODEBASE_VERSION
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_continuation_dataset_check, parent["dataset_name"], revision), 60
        )
    except dataset_preflight.DatasetRefused as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("continuation dataset check transient error for %s: %s",
                       parent["dataset_name"], type(e).__name__)
        raise HTTPException(status_code=502, detail=texts.HF_UNREACHABLE_DE)
    try:
        data = supabase.rpc("start_continuation_safe", {
            "p_user_id": user_id, "p_parent_id": req.training_id, "p_note": texts.s28(int(capped)),
            "p_max_hours": cap_hours(),
        }).execute().data
    except Exception as e:
        code = training_dispatch.pg_code(e)
        if code == "P0002":
            raise HTTPException(status_code=404, detail=texts.TRAINING_NOT_FOUND_DE)
        if code == "P0052":
            raise HTTPException(status_code=409, detail=texts.S26)
        if code == "P0053":
            raise HTTPException(status_code=409, detail=texts.S24)
        if code == "P0054":
            raise HTTPException(status_code=409, detail=texts.S25)
        if code in ("P0003", "P0063"):
            record_refusal(user_id, "continue", "member_limit" if code == "P0063" else "no_hours", "act")
            raise HTTPException(status_code=403, detail=credit_texts.MEMBER_LIMIT_DE if code == "P0063"
                                else credit_texts.NO_HOURS_DE)
        if code == "P0060":
            record_refusal(user_id, "continue", "role", "act")
            raise HTTPException(status_code=403, detail=credit_texts.ROLE_REFUSED_DE)
        if code == "P0061":
            raise HTTPException(status_code=400, detail=credit_texts.POLICY_REFUSED_DE)
        raise
    info = (data or [{}])[0] if isinstance(data, list) else (data or {})
    training_dispatch.kick()
    return StartTrainingResponse(
        training_id=int(info["o_training_id"]), model_name=parent["model_name"], status="queued",
        reserved_hours=info.get("o_reserved_hours"),
    )


@router.get("/list", response_model=list[TrainingJob])
async def list_trainings(user=Depends(get_current_user)):
    supabase = get_supabase()
    user_id = str(user.id)
    group_ids = resolve_visible_workgroup_ids(user_id)

    if group_ids:
        # PostgREST OR clause covers (own user_id) OR (in any visible group).
        ids_csv = ",".join(group_ids)
        q = (
            supabase.table("trainings")
            .select("*")
            .or_(f"user_id.eq.{user_id},workgroup_id.in.({ids_csv})")
            .order("requested_at", desc=True)
            .limit(100)
        )
    else:
        q = (
            supabase.table("trainings")
            .select("*")
            .eq("user_id", user_id)
            .order("requested_at", desc=True)
            .limit(50)
        )
    result = q.execute()

    # Sync status for any active jobs — parallel so N running rows don't
    # serialize into N× Modal roundtrip latency.
    rows = result.data or []
    trainings = list(await asyncio.gather(*[_sync_modal_status(t) for t in rows])) if rows else []
    for t in trainings:  # owner E46: only the starter sees „Fortsetzen"; user_id stays unexposed.
        t["is_mine"] = t.get("user_id") == user_id
    return trainings


@router.get("/{training_id}", response_model=TrainingJob)
async def get_training(training_id: int, user=Depends(get_current_user)):
    supabase = get_supabase()
    user_id = str(user.id)
    result = (
        supabase.table("trainings")
        .select("*")
        .eq("id", training_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Training nicht gefunden.")
    row = result.data[0]
    if row["user_id"] != user_id:
        # Allow group siblings (current and former) to read.
        wg = row.get("workgroup_id")
        if not wg or wg not in resolve_visible_workgroup_ids(user_id):
            raise HTTPException(status_code=404, detail="Training nicht gefunden.")

    training = await _sync_modal_status(row)
    training["is_mine"] = training.get("user_id") == user_id
    return training
