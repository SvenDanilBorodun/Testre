import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import get_current_profile
from app.services.modal_client import cancel_training_job
from app.services.supabase_client import get_supabase
from app.services.workgroups import resolve_visible_workgroup_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])


class MyProfile(BaseModel):
    id: str
    role: str
    username: str | None
    full_name: str | None
    classroom_id: str | None
    workgroup_id: str | None = None
    workgroup_name: str | None = None
    training_credits: int
    # Only populated for teachers. None for students/admin.
    pool_total: int | None = None
    allocated_total: int | None = None
    pool_available: int | None = None
    student_count: int | None = None
    group_count: int | None = None
    group_credits_total: int | None = None


@router.get("", response_model=MyProfile)
async def read_me(profile=Depends(get_current_profile)):
    data = {
        "id": profile["id"],
        "role": profile["role"],
        "username": profile.get("username"),
        "full_name": profile.get("full_name"),
        "classroom_id": profile.get("classroom_id"),
        "workgroup_id": profile.get("workgroup_id"),
        "training_credits": profile["training_credits"],
    }
    supabase = get_supabase()

    # Look up the group name for any user that's in one (students
    # primarily; teachers query groups via the dedicated routes).
    if profile.get("workgroup_id"):
        try:
            g = (
                supabase.table("workgroups")
                .select("name")
                .eq("id", profile["workgroup_id"])
                .single()
                .execute()
            )
            if g.data:
                data["workgroup_name"] = g.data.get("name")
        except Exception as exc:
            logger.warning(
                "workgroup lookup failed for user=%s group=%s: %s",
                profile["id"],
                profile["workgroup_id"],
                exc,
            )

    if profile["role"] == "teacher":
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
                "group_count": row.get("group_count", 0),
                "group_credits_total": row.get("group_credits_total", 0),
            }
        )
    return MyProfile(**data)


@router.get("/export")
async def export_my_data(profile=Depends(get_current_profile)):
    """GDPR/DSGVO Art. 15 — download everything we have on this user.

    Returns the user's profile, every training row, the audit trail of
    workgroup memberships, every workflow they authored, every dataset
    they registered, and the progress entries that apply to them. Does
    NOT include HuggingFace dataset/model contents — those live on HF
    and the user already has direct access to them under their HF
    account.

    Workgroup-aware (migration 011): the audit table workgroup_memberships
    is itself personal data and must be exportable. Group-scoped progress
    entries are personal data the moment a teacher writes one against a
    group the student is/was in.
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

    # Workgroup memberships audit — current + past, per GDPR Art. 15.
    memberships = (
        supabase.table("workgroup_memberships")
        .select("*")
        .eq("user_id", uid)
        .execute()
    )
    bundle["workgroup_memberships"] = memberships.data or []

    # Workflows the user authored. Stored in our DB even when the
    # Blockly JSON references upstream blocks; the JSON itself is the
    # personal-data part (a student's classroom assignment solution).
    workflows = (
        supabase.table("workflows")
        .select("*")
        .eq("owner_user_id", uid)
        .execute()
    )
    bundle["workflows"] = workflows.data or []

    # Dataset registry rows the user owns. We do NOT pull HF Hub
    # contents — the user has direct access via their HF account.
    datasets = (
        supabase.table("datasets")
        .select("*")
        .eq("owner_user_id", uid)
        .execute()
    )
    bundle["datasets"] = datasets.data or []

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
    elif profile["role"] == "student":
        # GDPR Art. 15: a student has the right to receive every piece of
        # personal data we hold about them — including the per-student
        # notes their teacher writes (student_id == uid) AND the
        # class-wide notes that apply to them by virtue of belonging to
        # the classroom (student_id IS NULL, classroom_id = theirs).
        student_entries = (
            supabase.table("progress_entries")
            .select("*")
            .eq("student_id", uid)
            .execute()
        )
        bundle["progress_entries"] = student_entries.data or []

        if profile.get("classroom_id"):
            class_entries = (
                supabase.table("progress_entries")
                .select("*")
                .eq("classroom_id", profile["classroom_id"])
                .is_("student_id", "null")
                .is_("workgroup_id", "null")
                .execute()
            )
            bundle["classroom_progress_entries"] = class_entries.data or []

        # Group-scoped entries for any group the student is or was in.
        visible_groups = resolve_visible_workgroup_ids(uid)
        if visible_groups:
            group_entries = (
                supabase.table("progress_entries")
                .select("*")
                .in_("workgroup_id", visible_groups)
                .execute()
            )
            bundle["workgroup_progress_entries"] = group_entries.data or []

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

    Side effects:
      1. Cancels any running/queued training the user owns so credits and
         GPU stop burning while the request sits in the admin queue.
      2. Records deletion_requested_at on the users row. If that write
         fails the endpoint returns 500 — silently returning success
         used to mean an admin would never see the request.
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

    supabase = get_supabase()
    uid = profile["id"]

    # 1. Cancel any in-flight trainings. Best-effort: a Modal cancel
    #    failure shouldn't block the user's deletion request, but the row
    #    is still marked canceled locally so credits free up.
    active = (
        supabase.table("trainings")
        .select("id, cloud_job_id")
        .eq("user_id", uid)
        .in_("status", ["queued", "running"])
        .execute()
    )
    cancelled_ids: list = []
    for row in active.data or []:
        if row.get("cloud_job_id"):
            try:
                await cancel_training_job(row["cloud_job_id"])
            except Exception as exc:
                logger.warning(
                    "Modal cancel failed in /me/delete for training %s: %s",
                    row["id"], exc,
                )
        try:
            supabase.table("trainings").update(
                {
                    "status": "canceled",
                    "terminated_at": datetime.now(timezone.utc).isoformat(),
                    "error_message": "Auto-canceled: account deletion requested",
                }
            ).eq("id", row["id"]).execute()
            cancelled_ids.append(row["id"])
        except Exception as exc:
            logger.warning(
                "Could not mark training %s canceled in /me/delete: %s",
                row["id"], exc,
            )

    # 2. Disengage from any current workgroup so the slot frees during
    #    the up-to-30-day admin processing window — otherwise a phantom
    #    member would block the teacher from filling the slot. The audit
    #    row is left in place with left_at = NOW() so siblings keep
    #    historical visibility on this user's group-shared content (the
    #    RLS policies query workgroup_memberships, not users.workgroup_id).
    if profile.get("workgroup_id"):
        wg_id = profile["workgroup_id"]
        try:
            supabase.table("users").update({"workgroup_id": None}).eq(
                "id", uid
            ).execute()
            supabase.table("workgroup_memberships").update(
                {"left_at": datetime.now(timezone.utc).isoformat()}
            ).eq("workgroup_id", wg_id).eq("user_id", uid).is_(
                "left_at", "null"
            ).execute()
            logger.info(
                "Workgroup disengagement on /me/delete: user=%s group=%s",
                uid, wg_id,
            )
        except Exception as exc:
            logger.warning(
                "Workgroup disengagement failed in /me/delete for %s: %s",
                uid, exc,
            )

    # 3. Record the deletion request. Migration 007 guarantees the
    #    column exists; a failure here is a real DB / network error and
    #    must be surfaced — silently returning success used to mean an
    #    admin would never see the request.
    try:
        result = (
            supabase.table("users")
            .update({"deletion_requested_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", uid)
            .execute()
        )
        if not result.data:
            raise RuntimeError("update returned no rows")
    except Exception as exc:
        logger.error("delete_my_account write failed for %s: %s", uid, exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    "Loeschanfrage konnte nicht gespeichert werden — bitte "
                    "erneut versuchen oder den Administrator informieren."
                )
            },
        )

    logger.info(
        "Deletion request from user=%s role=%s (canceled %d active trainings)",
        uid, profile["role"], len(cancelled_ids),
    )
    return {
        "status": "requested",
        "canceled_trainings": cancelled_ids,
        "message": (
            "Your deletion request was recorded. An administrator will "
            "process it within 30 days per GDPR Art. 17. Your data will "
            "remain accessible until then — use /me/export to download "
            "a copy before deletion completes."
        ),
    }
