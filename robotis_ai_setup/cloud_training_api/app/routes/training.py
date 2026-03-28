import os
import random
import string
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.services.runpod_client import (
    cancel_training_job,
    get_job_status,
    start_training_job,
)
from app.services.supabase_client import get_supabase

router = APIRouter(prefix="/trainings", tags=["training"])


# ---------- Request / Response models ----------


class StartTrainingRequest(BaseModel):
    dataset_name: str
    model_type: str
    training_params: dict


class CancelTrainingRequest(BaseModel):
    training_id: int


class TrainingJob(BaseModel):
    id: int
    status: str
    dataset_name: str
    model_name: str
    model_type: str
    training_params: dict | None
    requested_at: str
    terminated_at: str | None
    error_message: str | None


class StartTrainingResponse(BaseModel):
    training_id: int
    model_name: str
    status: str


class UserQuota(BaseModel):
    training_credits: int
    trainings_used: int
    remaining: int


# ---------- Helpers ----------

RUNPOD_TO_DB_STATUS = {
    "QUEUED": "queued",
    "IN_QUEUE": "queued",
    "IN_PROGRESS": "running",
    "COMPLETED": "succeeded",
    "FAILED": "failed",
    "CANCELLED": "canceled",
    "TIMED_OUT": "failed",
}


def _generate_model_name(model_type: str, dataset_name: str) -> str:
    dataset_base = dataset_name.split("/")[-1] if "/" in dataset_name else dataset_name
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"edubotics/{model_type}-{dataset_base}-{suffix}"


def _sync_runpod_status(training: dict) -> dict:
    """Check RunPod for the latest status and sync to Supabase if changed."""
    if training["status"] not in ("queued", "running"):
        return training

    job_id = training.get("runpod_job_id")
    if not job_id:
        return training

    try:
        runpod_status = get_job_status(job_id)
    except Exception:
        return training

    db_status = RUNPOD_TO_DB_STATUS.get(runpod_status, training["status"])
    if db_status == training["status"]:
        return training

    supabase = get_supabase()
    update_data: dict = {"status": db_status}
    if db_status in ("succeeded", "failed", "canceled"):
        update_data["terminated_at"] = datetime.now(timezone.utc).isoformat()
    if db_status == "failed":
        update_data["error_message"] = f"RunPod status: {runpod_status}"

    supabase.table("trainings").update(update_data).eq("id", training["id"]).execute()
    training["status"] = db_status
    if "terminated_at" in update_data:
        training["terminated_at"] = update_data["terminated_at"]
    if "error_message" in update_data:
        training["error_message"] = update_data["error_message"]
    return training


# ---------- Endpoints ----------


@router.get("/quota", response_model=UserQuota)
async def get_quota(user=Depends(get_current_user)):
    supabase = get_supabase()
    result = (
        supabase.table("users")
        .select("training_credits, trainings_used")
        .eq("id", str(user.id))
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="User profile not found")

    row = result.data[0]
    credits = row["training_credits"]
    used = row["trainings_used"]
    return UserQuota(training_credits=credits, trainings_used=used, remaining=credits - used)


@router.post("/start", response_model=StartTrainingResponse)
async def start_training(req: StartTrainingRequest, user=Depends(get_current_user)):
    supabase = get_supabase()

    # Check quota
    user_row = (
        supabase.table("users")
        .select("training_credits, trainings_used")
        .eq("id", str(user.id))
        .execute()
    )
    if not user_row.data:
        raise HTTPException(status_code=404, detail="User profile not found")

    credits = user_row.data[0]["training_credits"]
    used = user_row.data[0]["trainings_used"]
    if used >= credits:
        raise HTTPException(
            status_code=403,
            detail=f"No training credits remaining. Used {used}/{credits}.",
        )

    # Generate model name
    model_name = _generate_model_name(req.model_type, req.dataset_name)

    # Insert training row
    insert_result = (
        supabase.table("trainings")
        .insert(
            {
                "user_id": str(user.id),
                "status": "queued",
                "dataset_name": req.dataset_name,
                "model_name": model_name,
                "model_type": req.model_type,
                "training_params": req.training_params,
            }
        )
        .execute()
    )
    training_id = insert_result.data[0]["id"]

    # Dispatch to RunPod
    try:
        job_id = start_training_job(
            dataset_name=req.dataset_name,
            model_name=model_name,
            model_type=req.model_type,
            training_params=req.training_params,
            training_id=training_id,
            supabase_url=os.environ["SUPABASE_URL"],
            supabase_key=os.environ["SUPABASE_SERVICE_ROLE_KEY"],
            hf_token=os.environ.get("HF_TOKEN", ""),
        )
    except Exception as e:
        # Mark as failed if dispatch fails
        supabase.table("trainings").update(
            {
                "status": "failed",
                "error_message": f"Failed to dispatch: {e}",
                "terminated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", training_id).execute()
        raise HTTPException(status_code=500, detail=f"Failed to start training: {e}")

    # Update with RunPod job ID and increment usage
    supabase.table("trainings").update(
        {"status": "running", "runpod_job_id": job_id}
    ).eq("id", training_id).execute()

    supabase.table("users").update({"trainings_used": used + 1}).eq(
        "id", str(user.id)
    ).execute()

    return StartTrainingResponse(
        training_id=training_id, model_name=model_name, status="running"
    )


@router.post("/cancel")
async def cancel_training(req: CancelTrainingRequest, user=Depends(get_current_user)):
    supabase = get_supabase()

    # Verify ownership
    result = (
        supabase.table("trainings")
        .select("*")
        .eq("id", req.training_id)
        .eq("user_id", str(user.id))
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Training not found")

    training = result.data[0]
    if training["status"] not in ("queued", "running"):
        raise HTTPException(status_code=400, detail="Training is not active")

    # Cancel on RunPod
    if training.get("runpod_job_id"):
        cancel_training_job(training["runpod_job_id"])

    # Update DB
    supabase.table("trainings").update(
        {
            "status": "canceled",
            "terminated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", req.training_id).execute()

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

    # Sync status for any active jobs
    trainings = [_sync_runpod_status(t) for t in (result.data or [])]
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

    training = _sync_runpod_status(result.data[0])
    return training
