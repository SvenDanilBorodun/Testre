"""Work groups (Arbeitsgruppen) inside classrooms.

A workgroup bundles 2-N students who share one training-hour account (migration
052: ``credit_accounts`` kind ``group``, with an optional per-member hour limit)
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
  the AFTER-DELETE close of 052 returns the group's free hours to the
  funding teacher (spent stays spent). ``workgroups.shared_credits`` is a
  dead column since 052 and is read by no route.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import get_current_teacher
from app.routes.teacher import (
    _assert_classroom_owned,
    _assert_student_owned,
    change_hours,
    credit_accounts,
    history_payload,
    hours_fields,
    parse_delta,
)
from app.services import credit_texts_de as credit_texts
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


class WorkgroupMember(BaseModel):
    id: str
    username: str | None
    full_name: str | None
    # 052: the member's hours in this group (§4.2's member formula)
    hours_used: int = 0


class WorkgroupSummary(BaseModel):
    id: str
    classroom_id: str
    name: str
    # 052: derived from the group's hour account (free + reserved + spent, reserved +
    # spent, free) — never the dead shared_credits column.
    shared_credits: int
    member_count: int
    trainings_used: int
    remaining: int
    hours_free: int = 0
    hours_reserved: int = 0
    hours_spent: int = 0
    member_limit_hours: int | None = None
    created_at: str
    updated_at: str


class WorkgroupDetail(WorkgroupSummary):
    members: list[WorkgroupMember]


class WorkgroupCreditsResponse(BaseModel):
    new_amount: int
    pool_available: int
    hours_free: int = 0
    hours_reserved: int = 0
    hours_spent: int = 0


class WorkgroupTrainingSummary(BaseModel):
    id: int
    status: str
    dataset_name: str | None
    model_name: str | None
    model_type: str | None
    current_step: int | None = None
    total_steps: int | None = None
    requested_at: str
    terminated_at: str | None = None
    error_message: str | None = None
    user_id: str
    started_by_username: str | None = None
    started_by_full_name: str | None = None
    # 052 (review m8)
    managed: bool = False
    reserved_hours: int | None = None
    charged_hours: int | None = None
    stop_requested_by_role: str | None = None
    lost_seconds: int | None = 0


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


def _group_member_count(workgroup_id: str) -> int:
    supabase = get_supabase()
    result = (
        supabase.table("users")
        .select("id", count="exact")
        .eq("workgroup_id", workgroup_id)
        .execute()
    )
    return int(result.count or 0)


def _batch_member_counts(group_ids: list[str]) -> dict[str, int]:
    """One select for the member counts of many groups."""
    if not group_ids:
        return {}
    members = (
        get_supabase().table("users")
        .select("workgroup_id")
        .in_("workgroup_id", group_ids)
        .execute()
    ).data or []
    member_counts: dict[str, int] = {}
    for u in members:
        gid = u.get("workgroup_id")
        if gid:
            member_counts[gid] = member_counts.get(gid, 0) + 1
    return member_counts


def _group_summary(
    row: dict,
    *,
    account: dict | None = None,
    member_count: int | None = None,
) -> WorkgroupSummary:
    """Build a WorkgroupSummary from the group's hour account (pass the batched
    ``account``; a single-group caller reads it here)."""
    if account is None:
        account = credit_accounts("group", [row["id"]]).get(str(row["id"]))
    members = (
        member_count if member_count is not None else _group_member_count(row["id"])
    )
    fields = hours_fields(account)
    limit = (account or {}).get("member_hour_limit")
    return WorkgroupSummary(
        id=row["id"],
        classroom_id=row["classroom_id"],
        name=row["name"],
        shared_credits=fields["training_credits"],
        member_count=members,
        trainings_used=fields["trainings_used"],
        remaining=fields["remaining"],
        hours_free=fields["hours_free"],
        hours_reserved=fields["hours_reserved"],
        hours_spent=fields["hours_spent"],
        member_limit_hours=None if limit is None else int(limit),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def member_hours_used(group_id: str, member_ids) -> dict[str, int]:
    """§4.2's member formula, one ledger select: ``max(0, Σ reserve − Σ release −
    Σ compensate)`` of each member's rows on this group's account."""
    ids = [str(m) for m in member_ids if m]
    if not ids:
        return {}
    rows = (
        get_supabase().table("credit_ledger")
        .select("kind, hours, member_id, from_kind, from_id, to_kind, to_id")
        .in_("kind", ["reserve", "release", "compensate"]).in_("member_id", ids).execute()
    ).data or []
    used: dict[str, int] = {m: 0 for m in ids}
    gid = str(group_id)
    for r in rows:
        mid = str(r.get("member_id"))
        hours = int(r.get("hours") or 0)
        if r.get("kind") in ("reserve", "release") and r.get("from_kind") == "group" and str(r.get("from_id")) == gid:
            used[mid] = used.get(mid, 0) + (hours if r["kind"] == "reserve" else -hours)
        elif r.get("kind") == "compensate" and r.get("to_kind") == "group" and str(r.get("to_id")) == gid:
            used[mid] = used.get(mid, 0) - hours
    return {m: max(0, v) for m, v in used.items()}


def _group_detail(row: dict) -> WorkgroupDetail:
    """Single-group detail. Pulls members in one query, then derives the
    summary's member_count from len(members) — one fewer round-trip.
    """
    supabase = get_supabase()
    members_raw = (
        supabase.table("users")
        .select("id, username, full_name")
        .eq("workgroup_id", row["id"])
        .order("full_name", desc=False)
        .execute()
    ).data or []
    summary = _group_summary(row, member_count=len(members_raw))
    used = member_hours_used(row["id"], [m["id"] for m in members_raw])
    return WorkgroupDetail(
        **summary.model_dump(),
        members=[WorkgroupMember(**m, hours_used=used.get(str(m["id"]), 0)) for m in members_raw],
    )


# ---------- Endpoints ----------


@router.get(
    "/classrooms/{classroom_id}/workgroups",
    response_model=list[WorkgroupSummary],
)
def list_workgroups(classroom_id: str, teacher=Depends(get_current_teacher)):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    rows = (
        supabase.table("workgroups")
        .select("*")
        .eq("classroom_id", classroom_id)
        .order("created_at", desc=False)
        .execute()
    ).data or []
    ids = [r["id"] for r in rows]
    members = _batch_member_counts(ids)
    accounts = credit_accounts("group", ids)
    return [
        _group_summary(
            r,
            account=accounts.get(str(r["id"])) or {},
            member_count=members.get(r["id"], 0),
        )
        for r in rows
    ]


@router.post(
    "/classrooms/{classroom_id}/workgroups",
    response_model=WorkgroupSummary,
)
def create_workgroup(
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
def get_workgroup(workgroup_id: str, teacher=Depends(get_current_teacher)):
    group = _assert_workgroup_owned(teacher["id"], workgroup_id)
    return _group_detail(group)


@router.patch("/workgroups/{workgroup_id}", response_model=WorkgroupSummary)
def rename_workgroup(
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
def delete_workgroup(
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
def add_workgroup_member(
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
            detail="Schüler ist nicht im selben Klassenzimmer wie die Gruppe",
        )

    # Refuse if student is already in another group.
    if student.get("workgroup_id") and student["workgroup_id"] != workgroup_id:
        raise HTTPException(
            status_code=409,
            detail="Schüler ist bereits in einer anderen Arbeitsgruppe",
        )

    # Capacity belt+braces; trigger raises P0021 if we somehow miss it.
    if _group_member_count(workgroup_id) >= MAX_GROUP_SIZE:
        raise HTTPException(
            status_code=409,
            detail=f"Arbeitsgruppe ist voll (max {MAX_GROUP_SIZE} Schüler)",
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
                detail="Schüler ist nicht im selben Klassenzimmer wie die Gruppe",
            )
        if "P0021" in msg:
            raise HTTPException(
                status_code=409,
                detail=f"Arbeitsgruppe ist voll (max {MAX_GROUP_SIZE} Schüler)",
            )
        logger.error("add_workgroup_member update failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Schüler konnte nicht zur Gruppe hinzugefügt werden",
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
def remove_workgroup_member(
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


@router.get(
    "/workgroups/{workgroup_id}/trainings",
    response_model=list[WorkgroupTrainingSummary],
)
def list_workgroup_trainings(
    workgroup_id: str, teacher=Depends(get_current_teacher)
):
    """All trainings spawned within this workgroup, latest first.

    Returns up to 100 rows. Uses workgroup_id (not user_id) so even
    trainings spawned by an ex-member are still listed — matches the
    audit-table visibility students get on their own list view.
    """
    _assert_workgroup_owned(teacher["id"], workgroup_id)
    supabase = get_supabase()
    rows = (
        supabase.table("trainings")
        .select(
            "id, status, dataset_name, model_name, model_type, "
            "current_step, total_steps, requested_at, terminated_at, "
            "error_message, user_id, managed, reserved_hours, charged_hours, "
            "stop_requested_by_role, lost_seconds"
        )
        .eq("workgroup_id", workgroup_id)
        .order("requested_at", desc=True)
        .limit(100)
        .execute()
    ).data or []

    # Single users-table lookup for "Started by" attribution rather
    # than N+1 round-trips.
    uids = list({r["user_id"] for r in rows if r.get("user_id")})
    name_lookup: dict[str, dict] = {}
    if uids:
        try:
            users = (
                supabase.table("users")
                .select("id, username, full_name")
                .in_("id", uids)
                .execute()
            ).data or []
            name_lookup = {u["id"]: u for u in users}
        except Exception as exc:
            logger.warning(
                "list_workgroup_trainings: user lookup failed: %s", exc
            )

    out: list[WorkgroupTrainingSummary] = []
    for r in rows:
        u = name_lookup.get(r.get("user_id") or "", {})
        out.append(
            WorkgroupTrainingSummary(
                id=r["id"],
                status=r["status"],
                dataset_name=r.get("dataset_name"),
                model_name=r.get("model_name"),
                model_type=r.get("model_type"),
                current_step=r.get("current_step"),
                total_steps=r.get("total_steps"),
                requested_at=r["requested_at"],
                terminated_at=r.get("terminated_at"),
                error_message=r.get("error_message"),
                user_id=r["user_id"],
                started_by_username=u.get("username"),
                started_by_full_name=u.get("full_name"),
                managed=bool(r.get("managed")),
                reserved_hours=r.get("reserved_hours"),
                charged_hours=r.get("charged_hours"),
                stop_requested_by_role=r.get("stop_requested_by_role"),
                lost_seconds=r.get("lost_seconds"),
            )
        )
    return out


@router.post(
    "/workgroups/{workgroup_id}/credits",
    response_model=WorkgroupCreditsResponse,
)
def adjust_workgroup_credits(
    workgroup_id: str,
    body: dict | None = Body(default=None),
    teacher=Depends(get_current_teacher),
):
    """Assign (delta > 0) or reclaim (delta < 0) a group's hours (§8.3)."""
    _assert_workgroup_owned(teacher["id"], workgroup_id)
    delta, request_id = parse_delta(body, 1000, credit_texts.TEACHER_DELTA_RANGE_DE)
    return WorkgroupCreditsResponse(**change_hours(teacher["id"], "group", workgroup_id, delta, request_id,
                                                   credit_texts.WORKGROUP_NOT_FOUND_DE))


@router.put("/workgroups/{workgroup_id}/member-limit")
def set_member_limit(
    workgroup_id: str,
    body: dict | None = Body(default=None),
    teacher=Depends(get_current_teacher),
):
    """The group's per-member hour limit (owner D5): 1…1000 or null (no limit). One number
    for every member, counted over the member's whole time in the group, never reset."""
    _assert_workgroup_owned(teacher["id"], workgroup_id)
    if not isinstance(body, dict) or "hours" not in body:
        raise HTTPException(status_code=400, detail=credit_texts.LIMIT_RANGE_DE)
    hours = body.get("hours")
    if hours is not None and (type(hours) is not int or not 1 <= hours <= 1000):
        raise HTTPException(status_code=400, detail=credit_texts.LIMIT_RANGE_DE)
    try:
        data = get_supabase().rpc("credit_set_member_limit", {
            "p_teacher_id": teacher["id"], "p_group_id": workgroup_id, "p_hours": hours,
        }).execute().data
    except Exception as exc:
        from app.services.training_dispatch import pg_code

        code = pg_code(exc)
        if code in ("P0066", "P0002"):
            raise HTTPException(status_code=404, detail=credit_texts.WORKGROUP_NOT_FOUND_DE)
        if code == "22023":
            raise HTTPException(status_code=400, detail=credit_texts.LIMIT_RANGE_DE)
        logger.error("credit_set_member_limit failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=credit_texts.CREDIT_CHANGE_FAILED_DE)
    value = data[0] if isinstance(data, list) and data else data
    if isinstance(value, dict):
        value = next(iter(value.values()), None)
    return {"member_limit_hours": None if value is None else int(value)}


@router.get("/workgroups/{workgroup_id}/credit-history")
def workgroup_credit_history(
    workgroup_id: str,
    before: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    teacher=Depends(get_current_teacher),
):
    """The group's hour history (scope ``group``), incl. its limit changes."""
    _assert_workgroup_owned(teacher["id"], workgroup_id)
    return history_payload("group", workgroup_id, before, limit)
