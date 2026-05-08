"""Work groups (Arbeitsgruppen) inside classrooms.

A workgroup bundles 2-N students who share a single training credit pool
and see each other's trainings, datasets, and Roboter Studio workflows.
Belongs to one classroom; a student is in at most one group per classroom.

Mirrors the patterns of routes/teacher.py:
- service-role Supabase client throughout
- explicit ownership assertion (`_assert_workgroup_owned`) per endpoint
- German error strings surfaced to the teacher dashboard

Lifecycle (per user-confirmed plan):
- Removing a member sets users.workgroup_id = NULL AND closes the audit
  row in workgroup_memberships (left_at = NOW()) so historical visibility
  via RLS is preserved for ex-members.
- Deleting a group with members is refused (mirrors classroom delete UX);
  remaining shared_credits are returned to the teacher pool naturally
  (the pool summary subtracts allocated, and the row is gone).
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_teacher
from app.routes.teacher import _assert_classroom_owned, _assert_student_owned
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/teacher", tags=["workgroups"])

# Mirrors enforce_workgroup_capacity() trigger in 011_workgroups.sql.
MAX_GROUP_SIZE = 10


# ---------- Models ----------


class WorkgroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class WorkgroupRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class WorkgroupMemberAdd(BaseModel):
    student_id: str


class WorkgroupCreditsDelta(BaseModel):
    delta: int = Field(..., ge=-1000, le=1000)


class WorkgroupMember(BaseModel):
    id: str
    username: str | None
    full_name: str | None


class WorkgroupSummary(BaseModel):
    id: str
    classroom_id: str
    name: str
    shared_credits: int
    member_count: int
    trainings_used: int
    remaining: int
    created_at: str
    updated_at: str


class WorkgroupDetail(WorkgroupSummary):
    members: list[WorkgroupMember]


class WorkgroupCreditsResponse(BaseModel):
    new_amount: int
    pool_available: int


# ---------- Helpers ----------


def _assert_workgroup_owned(teacher_id: str, workgroup_id: str) -> dict:
    """Return the workgroup row + classroom row if teacher owns the
    classroom this group belongs to; 404 otherwise.
    """
    supabase = get_supabase()
    result = (
        supabase.table("workgroups")
        .select("*")
        .eq("id", workgroup_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Arbeitsgruppe nicht gefunden")
    group = result.data[0]
    # 404 (not 403) on classroom mismatch to avoid existence leakage.
    classroom = (
        supabase.table("classrooms")
        .select("*")
        .eq("id", group["classroom_id"])
        .eq("teacher_id", teacher_id)
        .execute()
    )
    if not classroom.data:
        raise HTTPException(status_code=404, detail="Arbeitsgruppe nicht gefunden")
    return group


def _group_usage(workgroup_id: str) -> int:
    supabase = get_supabase()
    result = (
        supabase.table("trainings")
        .select("id", count="exact")
        .eq("workgroup_id", workgroup_id)
        .not_.in_("status", ["failed", "canceled"])
        .execute()
    )
    return int(result.count or 0)


def _group_member_count(workgroup_id: str) -> int:
    supabase = get_supabase()
    result = (
        supabase.table("users")
        .select("id", count="exact")
        .eq("workgroup_id", workgroup_id)
        .execute()
    )
    return int(result.count or 0)


def _group_summary(row: dict) -> WorkgroupSummary:
    credits = int(row.get("shared_credits") or 0)
    used = _group_usage(row["id"])
    return WorkgroupSummary(
        id=row["id"],
        classroom_id=row["classroom_id"],
        name=row["name"],
        shared_credits=credits,
        member_count=_group_member_count(row["id"]),
        trainings_used=used,
        remaining=max(credits - used, 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _group_detail(row: dict) -> WorkgroupDetail:
    summary = _group_summary(row)
    supabase = get_supabase()
    members_raw = (
        supabase.table("users")
        .select("id, username, full_name")
        .eq("workgroup_id", row["id"])
        .order("full_name", desc=False)
        .execute()
    ).data or []
    return WorkgroupDetail(
        **summary.model_dump(),
        members=[WorkgroupMember(**m) for m in members_raw],
    )


# ---------- Endpoints ----------


@router.get(
    "/classrooms/{classroom_id}/workgroups",
    response_model=list[WorkgroupSummary],
)
async def list_workgroups(classroom_id: str, teacher=Depends(get_current_teacher)):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    rows = (
        supabase.table("workgroups")
        .select("*")
        .eq("classroom_id", classroom_id)
        .order("created_at", desc=False)
        .execute()
    ).data or []
    return [_group_summary(r) for r in rows]


@router.post(
    "/classrooms/{classroom_id}/workgroups",
    response_model=WorkgroupSummary,
)
async def create_workgroup(
    classroom_id: str,
    req: WorkgroupCreate,
    teacher=Depends(get_current_teacher),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    try:
        result = (
            supabase.table("workgroups")
            .insert({"classroom_id": classroom_id, "name": req.name.strip()})
            .execute()
        )
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg:
            raise HTTPException(
                status_code=409,
                detail="Arbeitsgruppe mit diesem Namen existiert bereits",
            )
        logger.error("create_workgroup failed: %s", e)
        raise HTTPException(
            status_code=500, detail="Arbeitsgruppe konnte nicht erstellt werden"
        )
    return _group_summary(result.data[0])


@router.get("/workgroups/{workgroup_id}", response_model=WorkgroupDetail)
async def get_workgroup(workgroup_id: str, teacher=Depends(get_current_teacher)):
    group = _assert_workgroup_owned(teacher["id"], workgroup_id)
    return _group_detail(group)


@router.patch("/workgroups/{workgroup_id}", response_model=WorkgroupSummary)
async def rename_workgroup(
    workgroup_id: str,
    req: WorkgroupRename,
    teacher=Depends(get_current_teacher),
):
    _assert_workgroup_owned(teacher["id"], workgroup_id)
    supabase = get_supabase()
    try:
        result = (
            supabase.table("workgroups")
            .update({"name": req.name.strip()})
            .eq("id", workgroup_id)
            .execute()
        )
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg:
            raise HTTPException(
                status_code=409,
                detail="Arbeitsgruppe mit diesem Namen existiert bereits",
            )
        logger.error("rename_workgroup failed: %s", e)
        raise HTTPException(
            status_code=500, detail="Arbeitsgruppe konnte nicht umbenannt werden"
        )
    return _group_summary(result.data[0])


@router.delete("/workgroups/{workgroup_id}")
async def delete_workgroup(
    workgroup_id: str, teacher=Depends(get_current_teacher)
):
    _assert_workgroup_owned(teacher["id"], workgroup_id)
    if _group_member_count(workgroup_id) > 0:
        raise HTTPException(
            status_code=409,
            detail="Arbeitsgruppe ist nicht leer - erst alle Mitglieder entfernen",
        )
    supabase = get_supabase()
    # ON DELETE SET NULL on trainings/workflows/datasets preserves history;
    # ON DELETE CASCADE on workgroup_memberships cleans the audit table.
    supabase.table("workgroups").delete().eq("id", workgroup_id).execute()
    return {"ok": True}


@router.post(
    "/workgroups/{workgroup_id}/members",
    response_model=WorkgroupDetail,
)
async def add_workgroup_member(
    workgroup_id: str,
    req: WorkgroupMemberAdd,
    teacher=Depends(get_current_teacher),
):
    group = _assert_workgroup_owned(teacher["id"], workgroup_id)
    student = _assert_student_owned(teacher["id"], req.student_id)

    # Belt + braces: also enforced by the enforce_workgroup_classroom_match
    # trigger in 011_workgroups.sql (raises P0020).
    if student.get("classroom_id") != group["classroom_id"]:
        raise HTTPException(
            status_code=409,
            detail="Schueler ist nicht im selben Klassenzimmer wie die Gruppe",
        )

    # Refuse if student is already in another group.
    if student.get("workgroup_id") and student["workgroup_id"] != workgroup_id:
        raise HTTPException(
            status_code=409,
            detail="Schueler ist bereits in einer anderen Arbeitsgruppe",
        )

    # Capacity belt+braces; trigger raises P0021 if we somehow miss it.
    if _group_member_count(workgroup_id) >= MAX_GROUP_SIZE:
        raise HTTPException(
            status_code=409,
            detail=f"Arbeitsgruppe ist voll (max {MAX_GROUP_SIZE} Schueler)",
        )

    supabase = get_supabase()
    try:
        supabase.table("users").update({"workgroup_id": workgroup_id}).eq(
            "id", req.student_id
        ).execute()
    except Exception as e:
        msg = str(e)
        if "P0020" in msg:
            raise HTTPException(
                status_code=409,
                detail="Schueler ist nicht im selben Klassenzimmer wie die Gruppe",
            )
        if "P0021" in msg:
            raise HTTPException(
                status_code=409,
                detail=f"Arbeitsgruppe ist voll (max {MAX_GROUP_SIZE} Schueler)",
            )
        logger.error("add_workgroup_member update failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Schueler konnte nicht zur Gruppe hinzugefuegt werden",
        )

    # Maintain the membership-audit row. Re-add resets left_at to NULL so
    # the visibility policies treat them as currently in the group.
    try:
        existing = (
            supabase.table("workgroup_memberships")
            .select("workgroup_id")
            .eq("workgroup_id", workgroup_id)
            .eq("user_id", req.student_id)
            .execute()
        )
        if existing.data:
            supabase.table("workgroup_memberships").update(
                {
                    "left_at": None,
                    "joined_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("workgroup_id", workgroup_id).eq("user_id", req.student_id).execute()
        else:
            supabase.table("workgroup_memberships").insert(
                {"workgroup_id": workgroup_id, "user_id": req.student_id}
            ).execute()
    except Exception as e:
        logger.warning(
            "Failed to upsert workgroup_membership for %s/%s: %s",
            workgroup_id,
            req.student_id,
            e,
        )

    refreshed = (
        supabase.table("workgroups").select("*").eq("id", workgroup_id).execute()
    )
    return _group_detail(refreshed.data[0])


@router.delete("/workgroups/{workgroup_id}/members/{student_id}")
async def remove_workgroup_member(
    workgroup_id: str,
    student_id: str,
    teacher=Depends(get_current_teacher),
):
    _assert_workgroup_owned(teacher["id"], workgroup_id)
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    # Only clear the FK if it currently points at this group (avoids a
    # race where the teacher already moved the student elsewhere).
    supabase.table("users").update({"workgroup_id": None}).eq(
        "id", student_id
    ).eq("workgroup_id", workgroup_id).execute()
    # Mark the audit row as left so RLS keeps history visible.
    try:
        supabase.table("workgroup_memberships").update(
            {"left_at": datetime.now(timezone.utc).isoformat()}
        ).eq("workgroup_id", workgroup_id).eq("user_id", student_id).execute()
    except Exception as e:
        logger.warning(
            "Failed to mark left_at for %s/%s: %s", workgroup_id, student_id, e
        )
    return {"ok": True}


@router.post(
    "/workgroups/{workgroup_id}/credits",
    response_model=WorkgroupCreditsResponse,
)
async def adjust_workgroup_credits(
    workgroup_id: str,
    req: WorkgroupCreditsDelta,
    teacher=Depends(get_current_teacher),
):
    _assert_workgroup_owned(teacher["id"], workgroup_id)
    if req.delta == 0:
        raise HTTPException(status_code=400, detail="Delta darf nicht 0 sein")
    supabase = get_supabase()
    try:
        result = supabase.rpc(
            "adjust_workgroup_credits",
            {
                "p_teacher_id": teacher["id"],
                "p_workgroup_id": workgroup_id,
                "p_delta": req.delta,
            },
        ).execute()
    except Exception as e:
        msg = str(e)
        if "P0022" in msg:
            raise HTTPException(
                status_code=403,
                detail="Arbeitsgruppe gehoert nicht zu diesem Lehrer",
            )
        if "P0012" in msg:
            raise HTTPException(
                status_code=409,
                detail="Neuer Betrag waere kleiner als bereits verbrauchte Credits",
            )
        if "P0013" in msg:
            raise HTTPException(
                status_code=409, detail="Credits duerfen nicht negativ werden"
            )
        if "P0014" in msg:
            raise HTTPException(
                status_code=409,
                detail="Lehrer hat nicht genug Credits im Pool",
            )
        logger.error("adjust_workgroup_credits failed: %s", e)
        raise HTTPException(
            status_code=500, detail="Credit-Anpassung fehlgeschlagen"
        )
    row = (result.data or [{}])[0]
    return WorkgroupCreditsResponse(
        new_amount=int(row.get("new_amount", 0)),
        pool_available=int(row.get("pool_available", 0)),
    )
