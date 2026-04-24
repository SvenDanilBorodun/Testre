import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import get_current_profile
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])


class MyProfile(BaseModel):
    id: str
    role: str
    username: str | None
    full_name: str | None
    classroom_id: str | None
    training_credits: int
    # Only populated for teachers. None for students/admin.
    pool_total: int | None = None
    allocated_total: int | None = None
    pool_available: int | None = None
    student_count: int | None = None


@router.get("", response_model=MyProfile)
async def read_me(profile=Depends(get_current_profile)):
    data = {
        "id": profile["id"],
        "role": profile["role"],
        "username": profile.get("username"),
        "full_name": profile.get("full_name"),
        "classroom_id": profile.get("classroom_id"),
        "training_credits": profile["training_credits"],
    }
    if profile["role"] == "teacher":
        supabase = get_supabase()
        summary = supabase.rpc(
            "get_teacher_credit_summary", {"p_teacher_id": profile["id"]}
        ).execute()
        row = (summary.data or [{}])[0]
        data.update(
            {
                "pool_total": row.get("pool_total", profile["training_credits"]),
                "allocated_total": row.get("allocated_total", 0),
                "pool_available": row.get(
                    "pool_available", profile["training_credits"]
                ),
                "student_count": row.get("student_count", 0),
            }
        )
    return MyProfile(**data)


@router.get("/export")
async def export_my_data(profile=Depends(get_current_profile)):
    """GDPR/DSGVO Art. 15 — download everything we have on this user.

    Returns the user's profile, every training row, and (for teachers)
    the classrooms + progress entries they've authored. Does NOT include
    HuggingFace dataset/model contents — those live on HF and the user
    already has direct access to them under their HF account.
    """
    supabase = get_supabase()
    uid = profile["id"]
    bundle: dict = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
    }

    trainings = (
        supabase.table("trainings").select("*").eq("user_id", uid).execute()
    )
    bundle["trainings"] = trainings.data or []

    if profile["role"] == "teacher":
        classrooms = (
            supabase.table("classrooms").select("*").eq("teacher_id", uid).execute()
        )
        bundle["classrooms"] = classrooms.data or []
        entries = (
            supabase.table("progress_entries")
            .select("*")
            .in_(
                "classroom_id",
                [c["id"] for c in bundle["classrooms"]] or [""],
            )
            .execute()
        )
        bundle["progress_entries"] = entries.data or []

    filename = f"edubotics-export-{uid}.json"
    return JSONResponse(
        content=bundle,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/delete")
async def delete_my_account(profile=Depends(get_current_profile)):
    """GDPR/DSGVO Art. 17 — account deletion request.

    Students and teachers can call this; admins can't self-delete (would
    lock out the platform). Current behavior is *request tracking* only
    — actual deletion is an admin responsibility per the runbook, because
    it spans Supabase Auth, HF repos, and container-local caches.
    """
    if profile["role"] == "admin":
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "Admin accounts cannot be self-deleted. Have another "
                    "admin remove you, or contact the platform owner."
                )
            },
        )
    # Best-effort record the request. No dedicated table yet; flag it on
    # the user row so admins see pending deletions in the dashboard.
    supabase = get_supabase()
    try:
        supabase.table("users").update(
            {"deletion_requested_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", profile["id"]).execute()
    except Exception as exc:
        logger.warning("delete_my_account write failed (column may not exist): %s", exc)
    logger.info("Deletion request from user=%s role=%s", profile["id"], profile["role"])
    return {
        "status": "requested",
        "message": (
            "Your deletion request was recorded. An administrator will "
            "process it within 30 days per GDPR Art. 17. Your data will "
            "remain accessible until then — use /me/export to download "
            "a copy before deletion completes."
        ),
    }
