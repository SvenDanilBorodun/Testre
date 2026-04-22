import asyncio
import logging
import os
import random
import string
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import get_current_user
from app.services.modal_client import (
    cancel_training_job,
    get_job_status,
    start_training_job,
)
from app.services.supabase_client import get_supabase
from huggingface_hub import HfApi
from huggingface_hub.utils import RepositoryNotFoundError

logger = logging.getLogger(__name__)

# A worker that hasn't called update_training_progress() in this long is
# considered wedged. Configurable via env var for ops flexibility.
STALLED_WORKER_THRESHOLD = timedelta(
    minutes=int(os.environ.get("STALLED_WORKER_MINUTES", "15"))
)

# Idempotency window: a duplicate /start with the same (user, dataset, model)
# arriving inside this window returns the existing training_id instead of
# creating a new row. Catches both client retries on network timeout and
# accidental double-clicks in the UI.
DEDUPE_WINDOW = timedelta(seconds=60)

# Hard upper bounds on training_params. Steps especially is a cost-bomb risk:
# a malicious or buggy client could request 1B steps and burn the GPU budget.
MAX_STEPS = int(os.environ.get("MAX_TRAINING_STEPS", "500000"))
MAX_BATCH_SIZE = 256
MAX_TIMEOUT_HOURS = 12.0

# Env-driven policy allowlist. Students get ALLOWED_POLICIES=act on Railway so
# only ACT training reaches the GPU. Admin/dev deployments leave this unset or
# set it to a comma list → the allowlist expands accordingly. The full training
# code path stays intact for every policy; this is a routing gate, not a delete.
ALLOWED_POLICIES = {
    p.strip().lower()
    for p in os.environ.get(
        "ALLOWED_POLICIES",
        "tdmpc,diffusion,act,vqbet,pi0,pi0fast,smolvla",
    ).split(",")
    if p.strip()
}

# Per-policy max timeout. Prevents a wedged ACT job from burning the 5h handler
# default when ACT really needs <90 min. Applied after validation and before
# Modal dispatch so it's always enforced regardless of what the client sends.
POLICY_MAX_TIMEOUT_HOURS = {
    "act": 1.5,
    "vqbet": 2.0,
    "tdmpc": 2.0,
    "diffusion": 4.0,
    "pi0fast": 4.0,
    "pi0": 6.0,
    "smolvla": 6.0,
}

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

    @field_validator("dataset_name")
    @classmethod
    def _dataset_name_shape(cls, v: str) -> str:
        # HuggingFace dataset id is "user/repo" — reject obvious garbage early.
        if "/" not in v or v.startswith("/") or v.endswith("/"):
            raise ValueError("dataset_name must be in 'owner/repo' form")
        return v

    @field_validator("model_type")
    @classmethod
    def _model_type_allowed(cls, v: str) -> str:
        # Enforce the env-driven allowlist so a direct API call can't bypass the
        # frontend policy filter. German message because the operator UI surfaces it.
        if v.lower() not in ALLOWED_POLICIES:
            raise ValueError(
                f"Modelltyp '{v}' ist für dieses Konto nicht freigeschaltet."
            )
        return v


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


class StartTrainingResponse(BaseModel):
    training_id: int
    model_name: str
    status: str


class UserQuota(BaseModel):
    training_credits: int
    trainings_used: int
    remaining: int


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


def _get_remaining_credits(user_id: str) -> dict:
    """Get remaining credits derived from actual trainings data.

    Credits are "used" by trainings with status NOT IN ('failed', 'canceled').
    No counter — self-healing, no race conditions, no double-refund risk.
    """
    supabase = get_supabase()
    result = supabase.rpc(
        "get_remaining_credits", {"p_user_id": user_id}
    ).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="User profile not found")
    row = result.data[0]
    return {
        "training_credits": row["training_credits"],
        "trainings_used": row["trainings_used"],
        "remaining": row["remaining"],
    }


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

    job_id = training.get("cloud_job_id")
    if not job_id:
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
                f"Worker hat ueber {stalled_minutes} Minuten keine Updates gesendet "
                f"(vermutlich haengt der Trainings-Prozess). Job wurde abgebrochen."
            )
        else:
            update_data["error_message"] = f"Modal status: {modal_status}"

    supabase.table("trainings").update(update_data).eq("id", training["id"]).execute()

    # No refund needed — setting status to "failed" automatically frees the credit
    # because get_remaining_credits counts only non-failed/canceled trainings.

    training["status"] = db_status
    if "terminated_at" in update_data:
        training["terminated_at"] = update_data["terminated_at"]
    if "error_message" in update_data:
        training["error_message"] = update_data["error_message"]
    return training


def _find_recent_duplicate(
    user_id: str, dataset_name: str, model_type: str
) -> dict | None:
    """Idempotency check. Returns an existing matching training if one was
    started within DEDUPE_WINDOW, otherwise None.

    Excludes failed/canceled rows so a user can immediately retry after a
    failure without being blocked by their own previous attempt.
    """
    supabase = get_supabase()
    window_start = (datetime.now(timezone.utc) - DEDUPE_WINDOW).isoformat()
    result = (
        supabase.table("trainings")
        .select("*")
        .eq("user_id", user_id)
        .eq("dataset_name", dataset_name)
        .eq("model_type", model_type)
        .gte("requested_at", window_start)
        .not_.in_("status", ["failed", "canceled"])
        .order("requested_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


async def _sweep_user_running_jobs(user_id: str) -> None:
    """Sync every running/queued row this user owns. Used at top of /start so a
    stuck row from a previous session can't block a fresh credit check.

    Async + parallel: all rows are synced concurrently via asyncio.gather,
    which scales better than serial sync calls when a user has many active rows.
    """
    supabase = get_supabase()
    result = (
        supabase.table("trainings")
        .select("*")
        .eq("user_id", user_id)
        .in_("status", ["queued", "running"])
        .execute()
    )

    async def _sync_one(row):
        try:
            await _sync_modal_status(row)
        except Exception as e:
            logger.warning("Sweep sync failed for training %s: %s", row.get("id"), e)

    rows = result.data or []
    if rows:
        await asyncio.gather(*[_sync_one(row) for row in rows])


# ---------- Endpoints ----------


@router.get("/quota", response_model=UserQuota)
async def get_quota(user=Depends(get_current_user)):
    quota = _get_remaining_credits(str(user.id))
    return UserQuota(**quota)


@router.post("/start", response_model=StartTrainingResponse)
async def start_training(req: StartTrainingRequest, user=Depends(get_current_user)):
    supabase = get_supabase()
    user_id = str(user.id)
    logger.info(
        "POST /trainings/start user=%s dataset=%s model_type=%s steps=%s",
        user_id, req.dataset_name, req.model_type, req.training_params.steps,
    )

    # 0a. Sweep this user's stuck rows BEFORE counting credits. If a previous
    #     job died hard (no SIGTERM, no exception), the row is still 'running'
    #     and would block the credit check. The sweep flips it to failed first.
    await _sweep_user_running_jobs(user_id)

    # 0b. Idempotency: a duplicate /start within DEDUPE_WINDOW returns the
    #     existing training instead of creating a new one. Catches network
    #     retries and accidental double-clicks. Zero schema/client cost.
    duplicate = _find_recent_duplicate(user_id, req.dataset_name, req.model_type)
    if duplicate:
        logger.info(
            "Dedupe hit: returning existing training %s for user=%s",
            duplicate["id"], user_id,
        )
        return StartTrainingResponse(
            training_id=duplicate["id"],
            model_name=duplicate["model_name"],
            status=duplicate["status"],
        )

    # 1. Validate dataset exists on HuggingFace Hub. Distinguish real 404 from
    #    transient errors — the student sees different messages so they know
    #    whether to fix the name or retry.
    try:
        hf_api = HfApi(token=os.environ.get("HF_TOKEN", ""))
        hf_api.dataset_info(req.dataset_name)
    except RepositoryNotFoundError:
        logger.warning("Dataset not found on HF: %s", req.dataset_name)
        raise HTTPException(
            status_code=400,
            detail=f"Dataset '{req.dataset_name}' not found on HuggingFace Hub.",
        )
    except Exception as e:
        # Rate limit, DNS blip, 5xx — tell the student to retry, don't send
        # them chasing a typo that isn't there.
        logger.warning("HF dataset check transient error for %s: %s", req.dataset_name, e)
        raise HTTPException(
            status_code=502,
            detail="HuggingFace Hub is temporarily unavailable. Please retry in a moment.",
        )

    # 2. Atomic credit-check + training row insert via start_training_safe RPC.
    #    The function locks the user row, counts active trainings, and inserts
    #    in one transaction — concurrent /start calls cannot both pass the check.
    #    worker_token is the only DB credential the Modal worker receives.
    model_name = _generate_model_name(
        req.model_type,
        req.dataset_name,
        output_folder_name=req.training_params.output_folder_name,
    )
    worker_token = str(uuid.uuid4())
    # Pydantic model → plain dict for JSON serialization to RPC + Modal.
    training_params_dict = req.training_params.model_dump(exclude_none=True)

    # Cap timeout_hours per-policy. Protects against a wedged ACT job burning
    # the handler's 5h default when ACT converges in <90 min.
    policy_cap = POLICY_MAX_TIMEOUT_HOURS.get(req.model_type.lower())
    if policy_cap is not None:
        requested = training_params_dict.get("timeout_hours", policy_cap)
        training_params_dict["timeout_hours"] = min(requested, policy_cap)

    try:
        rpc_result = supabase.rpc(
            "start_training_safe",
            {
                "p_user_id": user_id,
                "p_dataset_name": req.dataset_name,
                "p_model_name": model_name,
                "p_model_type": req.model_type,
                "p_training_params": training_params_dict,
                "p_total_steps": req.training_params.steps,
                "p_worker_token": worker_token,
            },
        ).execute()
    except Exception as e:
        # Map Postgres error codes raised by start_training_safe back to HTTP.
        msg = str(e)
        if "P0003" in msg or "credits remaining" in msg:
            logger.info("Credit-exhausted /start for user=%s", user_id)
            raise HTTPException(status_code=403, detail="No training credits remaining.")
        if "P0002" in msg or "User profile not found" in msg:
            logger.warning("User profile not found: %s", user_id)
            raise HTTPException(status_code=404, detail="User profile not found")
        logger.error("start_training_safe RPC failed for user=%s: %s", user_id, e)
        raise

    if not rpc_result.data:
        logger.error("start_training_safe returned no rows for user=%s", user_id)
        raise HTTPException(status_code=500, detail="start_training_safe returned no row")
    training_id = rpc_result.data[0]["training_id"]
    logger.info("Created training %s for user=%s model=%s", training_id, user_id, model_name)

    # 3. Dispatch to Modal. The worker receives the training_id + worker_token
    #    and reads SUPABASE_URL / SUPABASE_ANON_KEY / HF_TOKEN from its own
    #    Modal Secret — we don't leak them through the function payload.
    #    With the token + anon key it can ONLY call update_training_progress()
    #    on this one row.
    try:
        job_id = await start_training_job(
            dataset_name=req.dataset_name,
            model_name=model_name,
            model_type=req.model_type,
            training_params=training_params_dict,
            training_id=training_id,
            worker_token=worker_token,
        )
    except Exception as e:
        # Dispatch failed — mark training as failed (auto-frees the credit)
        logger.error("Modal dispatch failed for training %s: %s", training_id, e)
        supabase.table("trainings").update(
            {
                "status": "failed",
                "error_message": f"Failed to dispatch: {e}",
                "terminated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", training_id).execute()
        raise HTTPException(status_code=500, detail=f"Failed to start training: {e}")

    # 4. Update with the Modal FunctionCall id.
    supabase.table("trainings").update(
        {"status": "running", "cloud_job_id": job_id}
    ).eq("id", training_id).execute()
    logger.info("Dispatched training %s as Modal call %s", training_id, job_id)

    return StartTrainingResponse(
        training_id=training_id, model_name=model_name, status="running"
    )


@router.post("/cancel")
async def cancel_training(req: CancelTrainingRequest, user=Depends(get_current_user)):
    supabase = get_supabase()
    user_id = str(user.id)
    logger.info("POST /trainings/cancel user=%s training_id=%s", user_id, req.training_id)

    # Verify ownership
    result = (
        supabase.table("trainings")
        .select("*")
        .eq("id", req.training_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Training not found")

    training = result.data[0]
    if training["status"] not in ("queued", "running"):
        raise HTTPException(status_code=400, detail="Training is not active")

    # Cancel on Modal
    if training.get("cloud_job_id"):
        try:
            await cancel_training_job(training["cloud_job_id"])
        except Exception as e:
            # Still mark as canceled locally even if Modal fails — but log it
            # so a stuck-on-Modal job is at least visible in Railway logs.
            logger.warning(
                "Modal cancel failed for training %s call %s: %s",
                req.training_id, training["cloud_job_id"], e,
            )

    # Mark as canceled (auto-frees the credit)
    supabase.table("trainings").update(
        {
            "status": "canceled",
            "terminated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", req.training_id).execute()
    logger.info("Canceled training %s for user=%s", req.training_id, user_id)

    return {"status": "canceled", "training_id": req.training_id}


@router.get("/list", response_model=list[TrainingJob])
async def list_trainings(user=Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("trainings")
        .select("*")
        .eq("user_id", str(user.id))
        .order("requested_at", desc=True)
        .limit(50)
        .execute()
    )

    # Sync status for any active jobs — parallel so N running rows don't
    # serialize into N× Modal roundtrip latency.
    rows = result.data or []
    trainings = list(await asyncio.gather(*[_sync_modal_status(t) for t in rows])) if rows else []
    return trainings


@router.get("/{training_id}", response_model=TrainingJob)
async def get_training(training_id: int, user=Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("trainings")
        .select("*")
        .eq("id", training_id)
        .eq("user_id", str(user.id))
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Training not found")

    training = await _sync_modal_status(result.data[0])
    return training
