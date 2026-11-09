"""Teacher routes (``/teacher``): classrooms, students, their training hours and trainings.

Training hours (migration 052, spec §8.3) are an append-only ledger: every summary
is read from ``credit_accounts`` / ``credit_teacher_summary``, every change goes
through a SECURITY DEFINER function (``credit_assign`` / ``credit_reclaim``), which
judges ownership itself (P0066 → the German 404), and every change carries the
client's ``request_id`` (a replay answers the same, a reuse for another change is
P0071 → 409). No route here reads or writes ``users.training_credits`` (a dead
column since 052). The history and account helpers here are shared by
``routes/workgroups.py``, ``routes/admin.py`` and ``routes/me.py``.
"""

import logging
import os
import re
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from huggingface_hub import HfApi
from huggingface_hub.utils import RepositoryNotFoundError
from pydantic import BaseModel, Field

from app.auth import get_current_teacher, get_user_profile
from app.services import credit_texts_de as credit_texts
from app.services import passwords
from app.services.supabase_client import get_supabase
from app.services.usernames import synthetic_email, validate_username
from app.validators.workflow import validate_blockly_json

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/teacher", tags=["teacher"])


# ---------- Models ----------


class ClassroomCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ClassroomRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class StudentCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    # Checked by hand (services/passwords.py, ≥ 8 for students since 2026-11-10):
    # pydantic's 422 is English and echoes the password back.
    password: str
    full_name: str = Field(..., min_length=1, max_length=100)
    initial_credits: int = Field(default=0, ge=0, le=1000)


class StudentPatch(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    classroom_id: str | None = None


class PasswordReset(BaseModel):
    new_password: str  # checked by hand, see StudentCreate.password


class ClassroomSummary(BaseModel):
    id: str
    name: str
    created_at: str
    student_count: int


class StudentSummary(BaseModel):
    id: str
    username: str | None
    full_name: str | None
    # 052: the legacy fields are derived from the hour account (free + reserved +
    # spent, reserved + spent, free); the hour fields are what the dashboard shows.
    training_credits: int
    trainings_used: int
    remaining: int
    hours_free: int = 0
    hours_reserved: int = 0
    hours_spent: int = 0
    classroom_id: str | None
    # When the student is in a workgroup, surface the group so the
    # teacher dashboard can route hour edits to the group endpoint
    # (credit_assign refuses a grouped student, P0023).
    workgroup_id: str | None = None
    workgroup_name: str | None = None


class TrainingSummary(BaseModel):
    id: int
    status: str
    dataset_name: str
    model_name: str
    model_type: str
    current_step: int | None
    total_steps: int | None
    current_loss: float | None
    requested_at: str
    terminated_at: str | None
    error_message: str | None
    # 028: full worker stdout in the private HF model repo (terminal
    # transitions only) — the teacher's failure-forensics artifact.
    log_url: str | None = None
    # 052 (review m8): „Reserviert/Berechnet", the lost time, „Training stoppen".
    managed: bool = False
    reserved_hours: int | None = None
    charged_hours: int | None = None
    stop_requested_by_role: str | None = None
    lost_seconds: int | None = 0


class ClassroomDetail(BaseModel):
    id: str
    name: str
    created_at: str
    students: list[StudentSummary]


class CreditsResponse(BaseModel):
    new_amount: int
    pool_available: int
    hours_free: int = 0
    hours_reserved: int = 0
    hours_spent: int = 0


class WorkflowTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    blockly_json: dict


class WorkflowTemplateSummary(BaseModel):
    id: str
    name: str
    description: str
    classroom_id: str
    blockly_json: dict
    created_at: str
    updated_at: str


# ---------- Helpers ----------


def _assert_classroom_owned(teacher_id: str, classroom_id: str) -> dict:
    supabase = get_supabase()
    result = (
        supabase.table("classrooms")
        .select("*")
        .eq("id", classroom_id)
        .eq("teacher_id", teacher_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Klassenzimmer nicht gefunden")
    return result.data[0]


def _assert_student_owned(teacher_id: str, student_id: str) -> dict:
    supabase = get_supabase()
    result = (
        supabase.table("users")
        .select("id, username, full_name, classroom_id, workgroup_id, role")
        .eq("id", student_id)
        .eq("role", "student")
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Schüler nicht gefunden")
    student = result.data[0]
    if not student.get("classroom_id"):
        raise HTTPException(status_code=404, detail="Schüler gehört zu keinem Klassenzimmer")
    _assert_classroom_owned(teacher_id, student["classroom_id"])
    return student


# ---------- Training hours (052, spec §8.3) ----------

ACCOUNT_COLUMNS = "kind, owner_id, parent_teacher_id, free, reserved, spent, member_hour_limit, closed_at"


def credit_accounts(kind: str, owner_ids) -> dict:
    """``credit_accounts`` rows of one kind for many owners, in one select (service-role
    SELECT; an owner without an account is simply absent: no hours yet)."""
    ids = [str(i) for i in owner_ids if i]
    if not ids:
        return {}
    rows = (
        get_supabase().table("credit_accounts").select(ACCOUNT_COLUMNS)
        .eq("kind", kind).in_("owner_id", ids).execute()
    ).data or []
    return {str(r["owner_id"]): r for r in rows}


def hours_fields(acct: dict | None) -> dict:
    """The hour fields of a summary and the legacy keys derived from them (§8.3)."""
    acct = acct or {}
    free, reserved, spent = int(acct.get("free") or 0), int(acct.get("reserved") or 0), int(acct.get("spent") or 0)
    return {"hours_free": free, "hours_reserved": reserved, "hours_spent": spent,
            "training_credits": free + reserved + spent, "trainings_used": reserved + spent, "remaining": free}


def teacher_summary(teacher_id: str, sb=None) -> dict:
    """``credit_teacher_summary`` (§4.13) as a dict (zeros for an empty answer). ``sb``: the
    caller's client (routes/me.py passes its own)."""
    data = (sb or get_supabase()).rpc("credit_teacher_summary", {"p_teacher_id": str(teacher_id)}).execute().data
    rows = data if isinstance(data, list) else ([data] if data else [])
    row = rows[0] if rows else {}
    keys = ("o_free", "o_assigned_free", "o_assigned_reserved", "o_spent", "o_compensated",
            "o_student_count", "o_group_count", "o_group_hours")
    return {k[2:]: int(row.get(k) or 0) for k in keys}


def pool_fields(summary: dict) -> dict:
    """The teacher's pool readout with its pre-052 meaning (§8.6): available = free,
    allocated = what sits in child accounts (free + reserved), total = both."""
    allocated = summary["assigned_free"] + summary["assigned_reserved"]
    return {"pool_total": summary["free"] + allocated, "allocated_total": allocated,
            "pool_available": summary["free"], "student_count": summary["student_count"],
            "group_count": summary["group_count"], "group_credits_total": summary["group_hours"],
            "hours_spent": summary["spent"], "compensated": summary["compensated"]}


def _pg_code(exc) -> str | None:
    from app.services.training_dispatch import pg_code

    return pg_code(exc)


def parse_request_id(value) -> str | None:
    """A client ``request_id`` (UUID) or None; anything else is a 400."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail=credit_texts.CREDIT_CHANGE_FAILED_DE) from None


def parse_delta(body, max_abs: int, range_detail: str) -> tuple[int, str | None]:
    """``{delta: int, request_id?: uuid}``, validated by hand (a pydantic 422 is English)."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail=credit_texts.CREDIT_CHANGE_FAILED_DE)
    delta = body.get("delta")
    if type(delta) is not int:
        raise HTTPException(status_code=400, detail=range_detail)
    if delta == 0:
        raise HTTPException(status_code=400, detail=credit_texts.DELTA_ZERO_DE)
    if abs(delta) > max_abs:
        raise HTTPException(status_code=400, detail=range_detail)
    return delta, parse_request_id(body.get("request_id"))


def change_hours(teacher_id: str, kind: str, target_id: str, delta: int, request_id: str | None,
                 not_found_detail: str) -> dict:
    """``credit_assign`` (delta > 0) or ``credit_reclaim(−delta)``, then the target account,
    as the credits response (§8.3)."""
    fn = "credit_assign" if delta > 0 else "credit_reclaim"
    args = {"p_teacher_id": str(teacher_id), "p_target_kind": kind, "p_target_id": str(target_id),
            "p_hours": abs(int(delta)), "p_request_id": request_id}
    sb = get_supabase()
    try:
        call = sb.rpc("credit_assign", args) if delta > 0 else sb.rpc("credit_reclaim", args)
        data = call.execute().data
    except Exception as exc:
        code = _pg_code(exc)
        if code == "P0065":
            raise HTTPException(status_code=409, detail=credit_texts.POOL_TOO_SMALL_DE)
        if code == "P0064":
            raise HTTPException(status_code=409, detail=credit_texts.RECLAIM_TOO_MUCH_DE)
        if code == "P0023":
            raise HTTPException(status_code=409, detail=credit_texts.GROUPED_STUDENT_DE)
        if code == "P0071":
            raise HTTPException(status_code=409, detail=credit_texts.REQUEST_REUSED_DE)
        if code in ("P0066", "P0002"):
            raise HTTPException(status_code=404, detail=not_found_detail)
        logger.error("%s for %s %s failed: %s", fn, kind, target_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail=credit_texts.CREDIT_CHANGE_FAILED_DE)
    row = (data or [{}])[0] if isinstance(data, list) else (data or {})
    acct = credit_accounts(kind, [target_id]).get(str(target_id))
    fields = hours_fields(acct)
    return {"new_amount": int(row.get("o_target_free") if row.get("o_target_free") is not None
                              else fields["hours_free"]),
            "pool_available": int(row.get("o_teacher_free") or 0),
            "hours_free": fields["hours_free"], "hours_reserved": fields["hours_reserved"],
            "hours_spent": fields["hours_spent"]}


def _account_side(row: dict) -> tuple[str | None, str | None]:
    """The account a ledger row is ABOUT: its child side (student/group) when it has one,
    else its teacher side (a grant/revoke of the pool)."""
    for side in ("from", "to"):
        if row.get(f"{side}_kind") in ("student", "group"):
            return row[f"{side}_kind"], row.get(f"{side}_id")
    for side in ("from", "to"):
        if row.get(f"{side}_kind"):
            return row[f"{side}_kind"], row.get(f"{side}_id")
    return None, None


def history_entry(row: dict, model_names: dict, *, admin: bool = False) -> dict:
    """One ``credit_history`` row as the history entry of §8.3. A teacher's entry never
    carries another user's id; an admin's carries the ids a compensation needs (§8.5)."""
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    kind = row.get("kind")
    account_kind, account_id = _account_side(row)
    tid = row.get("training_id")
    entry = {
        "id": row.get("id"), "at": row.get("at"), "kind": kind, "hours": row.get("hours"),
        "account": account_kind, "training_id": tid,
        "model_name": model_names.get(tid) if tid is not None else None,
        "actor": row.get("actor_role"),
        "reason": row.get("reason"),
        "reason_erased": row.get("reason_id") is not None and row.get("reason") is None,
        "stopped_by": None, "status": None, "lost_seconds": None, "losses": None, "waiver": None,
        "limit_old": None, "limit_new": None,
    }
    if kind == "charge":
        entry.update(stopped_by=meta.get("stopped_by"), status=meta.get("status"),
                     lost_seconds=meta.get("lost_seconds"),
                     losses=meta.get("losses") if isinstance(meta.get("losses"), list) else None,
                     waiver=meta.get("waiver"))
    elif kind == "limit_change":
        entry.update(limit_old=meta.get("old"), limit_new=meta.get("new"))
    if admin:
        entry.update(account_kind=account_kind, account_id=account_id, member_id=row.get("member_id"),
                     reason_id=row.get("reason_id"), actor_role=row.get("actor_role"))
    return entry


def parse_history_page(before, limit) -> tuple[int | None, int]:
    """``?before=&limit=``: a ledger id and 1…200 (the database clamps too)."""
    def _int(v, default):
        if v is None or v == "":
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=credit_texts.HISTORY_FAILED_DE) from None

    b = _int(before, None)
    lim = max(1, min(200, _int(limit, 100)))
    return b, lim


def history_payload(scope: str, owner_id: str, before, limit, *, admin: bool = False,
                    refusals_user: str | None = None) -> dict:
    """``{entries, refusals}`` (§8.3; refusals only for a student's history). Any read
    failure is the 500 HISTORY_FAILED_DE."""
    b, lim = parse_history_page(before, limit)
    supabase = get_supabase()
    try:
        rows = supabase.rpc("credit_history", {
            "p_scope": scope, "p_id": owner_id, "p_before": b, "p_limit": lim,
        }).execute().data or []
        tids = sorted({r["training_id"] for r in rows if r.get("training_id") is not None})
        names = {}
        if tids:
            trows = supabase.table("trainings").select("id, model_name").in_("id", tids).execute().data or []
            names = {t["id"]: t.get("model_name") for t in trows}
        refusals = []
        if refusals_user is not None:
            refusals = (
                supabase.table("training_start_refusals").select("at, request_kind, code, model_type")
                .eq("user_id", str(refusals_user)).order("id", desc=True).limit(50).execute()
            ).data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("credit history (%s) failed: %s", scope, type(exc).__name__)
        raise HTTPException(status_code=500, detail=credit_texts.HISTORY_FAILED_DE)
    return {"entries": [history_entry(r, names, admin=admin) for r in rows],
            "refusals": [{k: r.get(k) for k in ("at", "request_kind", "code", "model_type")} for r in refusals]}


def _student_summary(
    row: dict,
    *,
    account: dict | None = None,
    group_name_lookup: dict | None = None,
) -> StudentSummary:
    """Build a StudentSummary from the users row and the student's hour account (pass the
    batched ``account``; a single-student caller reads it here). A grouped student's
    hours live in the group's account, so a grouped student reads 0 here."""
    if account is None:
        account = credit_accounts("student", [row["id"]]).get(str(row["id"]))
    workgroup_id = row.get("workgroup_id")
    workgroup_name = None
    if workgroup_id and group_name_lookup is not None:
        workgroup_name = group_name_lookup.get(workgroup_id)
    elif workgroup_id:
        try:
            g = (
                get_supabase()
                .table("workgroups")
                .select("name")
                .eq("id", workgroup_id)
                .single()
                .execute()
            )
            if g.data:
                workgroup_name = g.data.get("name")
        except Exception as e:
            logger.warning("workgroup name lookup failed: %s", e)
    return StudentSummary(
        id=row["id"],
        username=row.get("username"),
        full_name=row.get("full_name"),
        **hours_fields(account),
        classroom_id=row.get("classroom_id"),
        workgroup_id=workgroup_id,
        workgroup_name=workgroup_name,
    )


# ---------- Classrooms ----------


@router.get("/classrooms", response_model=list[ClassroomSummary])
def list_classrooms(teacher=Depends(get_current_teacher)):
    supabase = get_supabase()
    classrooms = (
        supabase.table("classrooms")
        .select("*")
        .eq("teacher_id", teacher["id"])
        .order("created_at", desc=False)
        .execute()
    ).data or []
    if not classrooms:
        return []

    # Single batched roster query → group in Python. Replaces an N+1
    # `count="exact"` per classroom that dominated dashboard load time.
    classroom_ids = [c["id"] for c in classrooms]
    students = (
        supabase.table("users")
        .select("classroom_id")
        .in_("classroom_id", classroom_ids)
        .eq("role", "student")
        .execute()
    ).data or []
    counts: dict[str, int] = {}
    for u in students:
        cid = u.get("classroom_id")
        if cid:
            counts[cid] = counts.get(cid, 0) + 1

    return [
        ClassroomSummary(
            id=c["id"],
            name=c["name"],
            created_at=c["created_at"],
            student_count=counts.get(c["id"], 0),
        )
        for c in classrooms
    ]


@router.post("/classrooms", response_model=ClassroomSummary)
def create_classroom(req: ClassroomCreate, teacher=Depends(get_current_teacher)):
    supabase = get_supabase()
    try:
        result = (
            supabase.table("classrooms")
            .insert({"teacher_id": teacher["id"], "name": req.name.strip()})
            .execute()
        )
    except Exception as e:
        msg = str(e)
        if "duplicate" in msg.lower() or "unique" in msg.lower():
            raise HTTPException(status_code=409, detail="Klassenzimmer mit diesem Namen existiert bereits")
        logger.error("create_classroom failed: %s", e)
        raise HTTPException(status_code=500, detail="Klassenzimmer konnte nicht erstellt werden")

    c = result.data[0]
    return ClassroomSummary(
        id=c["id"], name=c["name"], created_at=c["created_at"], student_count=0
    )


@router.get("/classrooms/{classroom_id}", response_model=ClassroomDetail)
def get_classroom(classroom_id: str, teacher=Depends(get_current_teacher)):
    c = _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    students_raw = (
        supabase.table("users")
        .select("id, username, full_name, classroom_id, workgroup_id")
        .eq("classroom_id", classroom_id)
        .eq("role", "student")
        .order("full_name", desc=False)
        .execute()
    ).data or []
    # Single roundtrip for group names instead of N+1.
    group_ids = list({s["workgroup_id"] for s in students_raw if s.get("workgroup_id")})
    group_lookup: dict = {}
    if group_ids:
        groups_raw = (
            supabase.table("workgroups")
            .select("id, name")
            .in_("id", group_ids)
            .execute()
        ).data or []
        group_lookup = {g["id"]: g["name"] for g in groups_raw}
    # One select for every student's hour account (052) instead of one per student.
    accounts = credit_accounts("student", [s["id"] for s in students_raw])
    return ClassroomDetail(
        id=c["id"],
        name=c["name"],
        created_at=c["created_at"],
        students=[
            _student_summary(
                s,
                account=accounts.get(str(s["id"])) or {},
                group_name_lookup=group_lookup,
            )
            for s in students_raw
        ],
    )


@router.get("/classrooms/{classroom_id}/workflow-templates", response_model=list[WorkflowTemplateSummary])
def list_workflow_templates(
    classroom_id: str,
    teacher=Depends(get_current_teacher),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    rows = (
        supabase.table("workflows")
        .select("id, name, description, classroom_id, blockly_json, created_at, updated_at")
        .eq("classroom_id", classroom_id)
        .eq("is_template", True)
        .order("updated_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    ).data or []
    return [WorkflowTemplateSummary(**r) for r in rows]


@router.post("/classrooms/{classroom_id}/workflow-templates", response_model=WorkflowTemplateSummary)
def create_workflow_template(
    classroom_id: str,
    payload: WorkflowTemplateCreate,
    teacher=Depends(get_current_teacher),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    # Audit §2.1: this endpoint used to insert without size/depth
    # validation, leaving the teacher path as a DOS hole. Use the same
    # validator the student /workflows router uses.
    validate_blockly_json(payload.blockly_json)
    supabase = get_supabase()
    insert_payload = {
        "owner_user_id": teacher["id"],
        "classroom_id": classroom_id,
        "name": payload.name,
        "description": payload.description,
        "blockly_json": payload.blockly_json,
        "is_template": True,
    }
    result = supabase.table("workflows").insert(insert_payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Vorlage konnte nicht gespeichert werden")
    row = result.data[0]
    return WorkflowTemplateSummary(
        id=row["id"],
        name=row["name"],
        description=row.get("description", ""),
        classroom_id=row["classroom_id"],
        blockly_json=row["blockly_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.delete("/classrooms/{classroom_id}/workflow-templates/{template_id}")
def delete_workflow_template(
    classroom_id: str,
    template_id: str,
    teacher=Depends(get_current_teacher),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    supabase.table("workflows").delete().eq("id", template_id).eq("classroom_id", classroom_id).eq("is_template", True).execute()
    return {"ok": True}


@router.patch("/classrooms/{classroom_id}", response_model=ClassroomSummary)
def rename_classroom(
    classroom_id: str,
    req: ClassroomRename,
    teacher=Depends(get_current_teacher),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    try:
        result = (
            supabase.table("classrooms")
            .update({"name": req.name.strip()})
            .eq("id", classroom_id)
            .execute()
        )
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Klassenzimmer mit diesem Namen existiert bereits")
        logger.error("rename_classroom failed: %s", e)
        raise HTTPException(status_code=500, detail="Klassenzimmer konnte nicht umbenannt werden")
    c = result.data[0]
    count_res = (
        supabase.table("users")
        .select("id", count="exact")
        .eq("classroom_id", c["id"])
        .eq("role", "student")
        .execute()
    )
    return ClassroomSummary(
        id=c["id"],
        name=c["name"],
        created_at=c["created_at"],
        student_count=int(count_res.count or 0),
    )


@router.delete("/classrooms/{classroom_id}")
def delete_classroom(classroom_id: str, teacher=Depends(get_current_teacher)):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    count_res = (
        supabase.table("users")
        .select("id", count="exact")
        .eq("classroom_id", classroom_id)
        .eq("role", "student")
        .execute()
    )
    if (count_res.count or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="Klassenzimmer ist nicht leer - erst alle Schüler entfernen",
        )
    # Workflow templates and student workflows tied to this classroom would
    # otherwise be left with classroom_id=NULL (FK is ON DELETE SET NULL),
    # which makes them unreachable through every read path (RLS + API both
    # filter by classroom_id) but they still occupy storage and contain
    # teacher-authored content. Delete them explicitly so the GDPR /
    # cleanup story is "deleting a classroom removes its content".
    try:
        supabase.table("workflows").delete().eq("classroom_id", classroom_id).execute()
    except Exception as e:
        # Don't block classroom deletion on a workflow cleanup failure —
        # the orphan rows can be cleaned up later. Surface the issue
        # in the logs so an operator can follow up.
        logger.warning(
            "Workflow cleanup failed for classroom %s: %s", classroom_id, e
        )
    supabase.table("classrooms").delete().eq("id", classroom_id).execute()
    return {"ok": True}


# ---------- Students ----------


@router.post("/classrooms/{classroom_id}/students", response_model=StudentSummary)
def create_student(
    classroom_id: str,
    req: StudentCreate,
    teacher=Depends(get_current_teacher),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()

    try:
        username = validate_username(req.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    refusal = passwords.password_error_de(req.password, "student")
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)

    existing = (
        supabase.table("users").select("id").eq("username", username).execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Benutzername bereits vergeben")

    email = synthetic_email(username)
    try:
        created = supabase.auth.admin.create_user(
            {
                "email": email,
                "password": req.password,
                "email_confirm": True,
            }
        )
    except Exception as e:
        if passwords.is_weak_password_error(e):
            raise HTTPException(status_code=400, detail=passwords.WEAK_PASSWORD_DE)
        msg = str(e).lower()
        if "already" in msg or "exists" in msg or "duplicate" in msg:
            raise HTTPException(status_code=409, detail="Benutzername bereits vergeben")
        logger.error("auth.admin.create_user failed: %s", e)
        raise HTTPException(status_code=500, detail="Konto konnte nicht erstellt werden")

    auth_user = getattr(created, "user", None)
    if auth_user is None:
        raise HTTPException(status_code=500, detail="Konto konnte nicht erstellt werden")
    student_id = auth_user.id

    # handle_new_user trigger has created a public.users row with role='student', credits=0.
    # Set the student's metadata now; credits stay 0 here and get allocated below.
    try:
        supabase.table("users").update(
            {
                "role": "student",
                "username": username,
                "full_name": req.full_name.strip(),
                "classroom_id": classroom_id,
                "created_by": teacher["id"],
            }
        ).eq("id", student_id).execute()
    except Exception as e:
        # Rollback the auth user on metadata failure so the account isn't orphaned.
        logger.error("Failed to set student metadata, rolling back: %s", e)
        try:
            supabase.auth.admin.delete_user(student_id)
        except Exception as del_err:
            logger.error("Rollback delete_user failed: %s", del_err)
        msg = str(e).lower()
        if "p0010" in msg or "kapazitaet" in msg:
            raise HTTPException(status_code=409, detail="Klassenzimmer voll (30 Schüler)")
        raise HTTPException(status_code=500, detail="Schüler-Profil konnte nicht gesetzt werden")

    # The start hours (052): an assign from the teacher's pool, after the creation.
    if req.initial_credits > 0:
        try:
            supabase.rpc(
                "credit_assign",
                {
                    "p_teacher_id": teacher["id"],
                    "p_target_kind": "student",
                    "p_target_id": student_id,
                    "p_hours": req.initial_credits,
                    "p_request_id": None,
                },
            ).execute()
        except Exception as e:
            if _pg_code(e) == "P0065":
                # Pool too small — the student is still created, with 0 hours.
                logger.warning("Insufficient pool when creating %s", username)
                raise HTTPException(status_code=409, detail=credit_texts.STUDENT_CREATED_POOL_SHORT_DE)
            logger.error("Initial hour assignment failed: %s", type(e).__name__)
            # Student still exists with 0 hours — return instead of failing.

    row = (
        supabase.table("users")
        .select("id, username, full_name, classroom_id, workgroup_id")
        .eq("id", student_id)
        .single()
        .execute()
    ).data
    return _student_summary(row)


@router.patch("/students/{student_id}", response_model=StudentSummary)
def patch_student(
    student_id: str,
    req: StudentPatch,
    teacher=Depends(get_current_teacher),
):
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()

    updates: dict = {}
    if req.full_name is not None:
        updates["full_name"] = req.full_name.strip()
    if req.classroom_id is not None:
        # Moving to another classroom — must also be owned by teacher.
        _assert_classroom_owned(teacher["id"], req.classroom_id)
        updates["classroom_id"] = req.classroom_id
    if not updates:
        raise HTTPException(status_code=400, detail="Keine Änderungen")

    try:
        supabase.table("users").update(updates).eq("id", student_id).execute()
    except Exception as e:
        msg = str(e).lower()
        if "p0010" in msg or "kapazitaet" in msg:
            raise HTTPException(status_code=409, detail="Ziel-Klassenzimmer voll (30 Schüler)")
        logger.error("patch_student failed: %s", e)
        raise HTTPException(status_code=500, detail="Aktualisierung fehlgeschlagen")

    row = (
        supabase.table("users")
        .select("id, username, full_name, classroom_id, workgroup_id")
        .eq("id", student_id)
        .single()
        .execute()
    ).data
    return _student_summary(row)


def _enumerate_student_hf_artifacts(
    student_id: str,
) -> tuple[list[tuple[str, str]], bool]:
    """READ-ONLY: which HF repos would an erasure delete, and can we tell?

    Split out of ``_delete_student_hf_artifacts`` on 2026-08-31 so that
    ``delete_student`` can ask the question BEFORE it deletes the auth user.
    This touches ``trainings`` and ``datasets`` and nothing else — no
    HuggingFace call, no write — so running it first is free, and running it
    on a student who then turns out not to be deletable costs nothing.

    Returns ``(candidates, registry_readable)``.

    ``candidates`` are deduplicated ``(repo_id, repo_type)`` pairs in erasure
    order — registry datasets FIRST, because those are the ones the old
    trainings-only enumeration could never see (a recording never used for
    training has no ``trainings`` row at all, which is the whole point of a
    GDPR erasure on a student who only ever recorded). Workgroup-shared
    datasets are already removed (migration 011: siblings still depend on
    them, and the teacher can delete those by hand from HF Hub).

    ``registry_readable`` is False when the ``datasets`` query failed. That is
    not merely "we enumerated less" — the registry is the ONLY source of the
    shared-repo skip list, so with it unreadable every dataset named by a
    surviving ``trainings`` row looks unshared. Measured 2026-08-08: answering
    that query 503 made a group-shared repo observably reach ``delete_repo``.
    Both callers fail closed on it — ``delete_student`` refuses the whole
    request with 503 while the student still exists, and
    ``_delete_student_hf_artifacts`` refuses the dataset half and names each
    repo it refused.
    """
    supabase = get_supabase()
    rows = (
        supabase.table("trainings")
        .select("dataset_name, model_name, workgroup_id")
        .eq("user_id", student_id)
        .execute()
    ).data or []

    # Build the set of HF repos that are shared with a group. We must
    # NOT delete these because group siblings still need them.
    shared_dataset_repos: set[str] = set()
    registry_repos: list[str] = []
    registry_readable = True
    try:
        ds_rows = (
            supabase.table("datasets")
            .select("hf_repo_id, owner_user_id, workgroup_id")
            .eq("owner_user_id", student_id)
            .execute()
        ).data or []
        for d in ds_rows:
            repo = d.get("hf_repo_id")
            if not repo:
                continue
            if d.get("workgroup_id"):
                shared_dataset_repos.add(repo)
            else:
                registry_repos.append(repo)
    except Exception as e:
        registry_readable = False
        logger.warning("Dataset registry query failed during HF cleanup: %s", e)

    ordered: list[tuple[str, str]] = [
        (repo, "dataset") for repo in registry_repos
    ]
    for row in rows:
        # Skip trainings that are part of a shared workgroup pool.
        if row.get("workgroup_id"):
            continue
        ordered.append((row.get("model_name"), "model"))
        ordered.append((row.get("dataset_name"), "dataset"))

    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for repo_id, repo_type in ordered:
        if not repo_id or (repo_id, repo_type) in seen:
            continue
        seen.add((repo_id, repo_type))
        if repo_type == "dataset" and repo_id in shared_dataset_repos:
            logger.info(
                "Skipping HF dataset %s — still shared with workgroup",
                repo_id,
            )
            continue
        candidates.append((repo_id, repo_type))
    return candidates, registry_readable


def _delete_student_hf_artifacts(
    student_id: str,
    enumeration: tuple[list[tuple[str, str]], bool] | None = None,
) -> tuple[list[dict], int]:
    """Best-effort deletion of HuggingFace repos owned by the student.

    GDPR Art. 17: when a student account is deleted, the personal data
    they generated must be erased. The Modal worker uploads trained
    models to ``EduBotics-Solutions/<...>`` and the recording stack
    pushes datasets to ``<student_username>/<robot_type>_<task>`` (or
    EduBotics-Solutions/, depending on the recording config) — both
    contain robot demonstrations that may include faces, classroom
    audio, or other identifying detail.

    Group-aware (migration 011): if a dataset row in the registry has
    a workgroup_id, deleting it would wipe content other group members
    still depend on. We skip those repos so the data remains available
    to surviving group members. The teacher can manually delete them
    from HF Hub if cleanup is needed.

    Errors are logged but do not block the auth deletion; an HF Hub
    outage during a deletion request would otherwise leave the auth
    user dangling. A cron-style cleanup pass can reconcile later.

    2026-08-06 — WHAT THIS STILL DOES NOT DO, and why the caller must say so.
    Erasure here is best-effort in a way that was previously INVISIBLE:

      * It enumerated the ``trainings`` table ONLY, so a recording that was
        never used for training had no row and was never found. The
        ``datasets`` registry is now enumerated too, which is where a plain
        recording lands.
      * It authenticates with the PLATFORM ``HF_TOKEN`` against repos in
        STUDENT namespaces (``<student_username>/<robot_type>_<task>``). That
        token does not own them, so the delete 403s — and the 403 was
        swallowed by a bare ``except Exception`` while the caller returned
        ``{"ok": True}``. A teacher was told the data was erased when it was
        not.

    So this now RETURNS the list of repos it could not erase, and the caller
    surfaces it. That is a patch, not a fix: making erasure actually work needs
    the org-account / per-student-sub-namespace change (see
    docs/plans/ — it touches ``_save_repo_name``, migration 026's
    ``register_dataset_safe`` HF-author anchor and ``datasets.py::sync_datasets``).

    Returns:
        ``(failures, attempted)``.

        ``failures`` is a ``{"repo_id", "repo_type", "reason"}`` entry for every
        repo that was NOT erased. ``attempted`` is how many repos were actually
        handed to ``delete_repo`` — see the note on the caller below.

    2026-08-31 — WHY ``attempted`` EXISTS. It used to return the failure list
    alone, and the caller read an empty list as "erasure complete". Those are
    different claims: a student who registered nothing produces an empty list
    after contacting HuggingFace ZERO times, and the endpoint then answered
    ``hf_erasure_complete: true`` — a positive assertion about a service it had
    never called. ``attempted == 0`` lets the caller say what actually happened.
    """
    failures: list[dict] = []
    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        logger.warning(
            "HF_TOKEN not set; skipping HF artifact cleanup for %s", student_id
        )
        return [{
            "repo_id": "*", "repo_type": "*",
            "reason": "HF_TOKEN ist nicht gesetzt — es wurde nichts gelöscht.",
        }], 0

    # The enumeration is normally handed in by `delete_student`, which runs it
    # BEFORE the auth user is deleted (see there). Enumerating here when it is
    # not is what keeps this function callable on its own.
    candidates, registry_readable = (
        enumeration if enumeration is not None
        else _enumerate_student_hf_artifacts(student_id)
    )

    if not registry_readable:
        failures.append({
            "repo_id": "?", "repo_type": "dataset",
            "reason": (
                "Datensatz-Register nicht lesbar — es ist unbekannt, welche "
                "Datensätze mit einer Arbeitsgruppe geteilt sind. Es wurde "
                "deshalb KEIN Datensatz gelöscht. Bitte später erneut versuchen."
            ),
        })

    if not candidates:
        # Nothing enumerable. That is HONEST only when the student genuinely
        # registered nothing: a recording is discoverable here only if it
        # reached the `datasets` registry, so a repo pushed under an
        # unregistered name is invisible to this function and always was.
        # Reporting an unqualified `hf_erasure_complete: true` after contacting
        # HuggingFace ZERO times is the same overstatement this whole change
        # exists to remove — which is why `attempted` (0 here) is returned and
        # the caller says what was actually established.
        if registry_readable:
            logger.info(
                "No HF artifacts registered for %s — nothing to erase", student_id
            )
        return failures, 0

    api = HfApi(token=hf_token)
    attempted = 0
    for repo_id, repo_type in candidates:
        if repo_type == "dataset" and not registry_readable:
            # Fail CLOSED, and NAME the repo. The generic "?" entry above says
            # why; without this per-repo entry the teacher is warned in a way
            # that never mentions the datasets, so a sibling group's data would
            # go missing invisibly. Models are unaffected: a pooled training is
            # already skipped by its own row's workgroup_id, which does not come
            # from the datasets query.
            logger.warning(
                "Refusing to delete HF dataset %s — shared-repo skip list "
                "unavailable (datasets registry unreadable)",
                repo_id,
            )
            failures.append({
                "repo_id": repo_id, "repo_type": repo_type,
                "reason": (
                    "Nicht gelöscht: unbekannt, ob dieser Datensatz mit einer "
                    "Arbeitsgruppe geteilt ist."
                ),
            })
            continue
        # Counted BEFORE the call: a repo that was already gone
        # (RepositoryNotFoundError) and a repo that 403s were both genuinely
        # asked about, which is exactly what `attempted` claims.
        attempted += 1
        try:
            api.delete_repo(repo_id=repo_id, repo_type=repo_type, missing_ok=True)
            logger.info("Deleted HF %s repo %s", repo_type, repo_id)
        except RepositoryNotFoundError:
            # Already gone — that is the desired end state, not a failure.
            continue
        except Exception as e:
            # NOT swallowed any more. The common case is a 403: we hold the
            # PLATFORM token and the repo lives in the STUDENT's namespace, so
            # erasure was never actually possible for it. Recording that as a
            # silent log line while the caller answered {"ok": true} is how a
            # teacher came to believe data had been erased that had not.
            logger.warning(
                "HF delete %s %s failed: %s", repo_type, repo_id, e
            )
            failures.append({
                "repo_id": repo_id,
                "repo_type": repo_type,
                "reason": str(e)[:300],
            })

    return failures, attempted


@router.delete("/students/{student_id}")
def delete_student(student_id: str, teacher=Depends(get_current_teacher)):
    _assert_student_owned(teacher["id"], student_id)

    # ORDER MATTERS HERE, and until 2026-08-31 it was wrong in a way that made
    # a lawful Art. 17 request destructive AND unfinishable:
    #
    #   * HF erasure ran FIRST and `auth.admin.delete_user` second. Migration
    #     038 exists because `trainings.user_id` had no ON DELETE clause, so
    #     for any student who had ever started ONE training the delete raised
    #     23503 and this handler answered 500 — AFTER the student's HuggingFace
    #     repos were already gone. The teacher was told only that the account
    #     could not be deleted, and no retry could ever succeed.
    #   * A registry outage deleted the account anyway. `public.users` cascades,
    #     the `datasets` rows go with it, and the retry then hits
    #     `_assert_student_owned` → 404. The request could never be completed.
    #
    # So: enumerate (read-only) → refuse retryably if we cannot see straight →
    # delete the account (if this fails, nothing has been erased) → erase HF.
    # Enumerating first is safe: `_enumerate_student_hf_artifacts` reads
    # `trainings` and `datasets`, both of which the account delete cascades
    # away, and it takes its HF token from the environment, not from a row.
    candidates, registry_readable = _enumerate_student_hf_artifacts(student_id)

    if not registry_readable:
        # 503, not 500: this is transient and the student is STILL THERE, so
        # the teacher can simply try again. Nothing has been deleted.
        logger.error(
            "Refusing to delete student %s: dataset registry unreadable, so "
            "the workgroup-shared skip list is unknown", student_id,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Das Datensatz-Register ist zurzeit nicht erreichbar. Es wurde "
                "nichts gelöscht — bitte in einigen Minuten erneut versuchen."
            ),
        )

    supabase = get_supabase()
    try:
        supabase.auth.admin.delete_user(student_id)
    except Exception as e:
        logger.error("delete_user failed: %s", e)
        raise HTTPException(status_code=500, detail="Konto konnte nicht gelöscht werden")

    hf_failures, hf_attempted = _delete_student_hf_artifacts(
        student_id, (candidates, registry_readable)
    )

    # `ok` refers to the ACCOUNT deletion, which did succeed. HF erasure is
    # best-effort and must not block it — but it must also not be reported as
    # having happened when it did not. Until 2026-08-06 every failure (the
    # common one being a 403: we hold the PLATFORM token while the repo lives
    # in the STUDENT's namespace) was swallowed into a log line and this
    # endpoint answered a bare {"ok": true}, so a teacher discharging a GDPR
    # Art. 17 request was told the data was erased when it was not.
    if hf_failures:
        logger.error(
            "Student %s deleted but %d HF repo(s) NOT erased: %s",
            student_id, len(hf_failures),
            [f["repo_id"] for f in hf_failures],
        )
        return {
            "ok": True,
            "hf_erasure_complete": False,
            "hf_failures": hf_failures,
            "detail": (
                "Das Konto wurde gelöscht, aber "
                f"{len(hf_failures)} HuggingFace-Repository/-s konnten NICHT "
                "gelöscht werden — die Daten liegen noch dort. Bitte im "
                "HuggingFace-Konto der Schülerin/des Schülers manuell löschen."
            ),
        }

    if hf_attempted == 0:
        # Nothing failed — but nothing was tried either, because this student
        # had no registered HuggingFace dataset we are allowed to delete. That
        # is NOT the same as "erasure complete", and saying so would repeat the
        # exact overstatement this endpoint was fixed to stop making: recordings
        # pushed under a name that never reached the `datasets` registry are
        # invisible here and always were.
        logger.info(
            "Student %s deleted; no HF repos were enumerable, so HuggingFace "
            "was never contacted", student_id,
        )
        return {
            "ok": True,
            "hf_erasure_complete": False,
            "hf_failures": [],
            "detail": (
                "Das Konto wurde gelöscht. Für diese Schülerin/diesen Schüler "
                "waren keine HuggingFace-Datensätze registriert, die hier "
                "gelöscht werden können — es wurde deshalb nicht bei "
                "HuggingFace nachgefragt und nicht geprüft, ob dort noch Daten "
                "liegen. Bitte im HuggingFace-Konto der Schülerin/des Schülers "
                "nachsehen."
            ),
        }

    return {"ok": True, "hf_erasure_complete": True}


@router.post("/students/{student_id}/password")
def reset_student_password(
    student_id: str,
    req: PasswordReset,
    teacher=Depends(get_current_teacher),
):
    _assert_student_owned(teacher["id"], student_id)
    refusal = passwords.password_error_de(req.new_password, "student")
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)
    supabase = get_supabase()
    try:
        supabase.auth.admin.update_user_by_id(
            student_id, {"password": req.new_password}
        )
    except Exception as e:
        if passwords.is_weak_password_error(e):
            raise HTTPException(status_code=400, detail=passwords.WEAK_PASSWORD_DE)
        logger.error("reset_student_password failed: %s", e)
        raise HTTPException(status_code=500, detail="Passwort konnte nicht gesetzt werden")
    return {"ok": True}


@router.post("/students/{student_id}/credits", response_model=CreditsResponse)
def adjust_credits(
    student_id: str,
    body: dict | None = Body(default=None),
    teacher=Depends(get_current_teacher),
):
    """Assign (delta > 0) or reclaim (delta < 0) a student's hours (§8.3). Only free
    hours can be reclaimed (P0064); a grouped student is managed through the group."""
    _assert_student_owned(teacher["id"], student_id)
    delta, request_id = parse_delta(body, 1000, credit_texts.TEACHER_DELTA_RANGE_DE)
    return CreditsResponse(**change_hours(teacher["id"], "student", student_id, delta, request_id,
                                          credit_texts.STUDENT_NOT_FOUND_DE))


@router.get("/students/{student_id}/credit-history")
def student_credit_history(
    student_id: str,
    before: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    teacher=Depends(get_current_teacher),
):
    """The student's hour history (scope ``student``: its own account and every row about
    it as a group member) plus its newest 50 refused starts (§8.3)."""
    _assert_student_owned(teacher["id"], student_id)
    return history_payload("student", student_id, before, limit, refusals_user=student_id)


@router.get("/credit-history")
def own_credit_history(
    before: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    teacher=Depends(get_current_teacher),
):
    """The teacher's own pool history (scope ``teacher``)."""
    return history_payload("teacher", teacher["id"], before, limit)


@router.post("/students/{student_id}/trainings/{training_id}/stop")
async def stop_student_training(student_id: str, training_id: int, teacher=Depends(get_current_teacher)):
    """„Training stoppen" (decision 13): the student's own „Abbrechen" path (§8.4) with
    the teacher's role. A training of another student is the same German 404 (P0002)."""
    import asyncio

    from app.services import stop_actions
    from app.services import training_texts_de as texts

    await asyncio.to_thread(_assert_student_owned, teacher["id"], student_id)
    return await stop_actions.stop(training_id, owner=student_id, actor=teacher["id"], role="teacher",
                                   note=texts.S32)


@router.get("/students/{student_id}/trainings/{training_id}/stop-preview")
async def student_training_stop_preview(student_id: str, training_id: int,
                                        teacher=Depends(get_current_teacher)):
    """The student preview's body for the teacher (same assert)."""
    import asyncio

    from app.routes.training import read_stop_preview

    await asyncio.to_thread(_assert_student_owned, teacher["id"], student_id)
    return await asyncio.to_thread(read_stop_preview, training_id, student_id)


@router.get("/students/{student_id}/trainings", response_model=list[TrainingSummary])
def list_student_trainings(
    student_id: str,
    teacher=Depends(get_current_teacher),
):
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    result = (
        supabase.table("trainings")
        .select(
            "id, status, dataset_name, model_name, model_type, "
            "current_step, total_steps, current_loss, "
            "requested_at, terminated_at, error_message, log_url, "
            "workgroup_id, managed, reserved_hours, charged_hours, stop_requested_by_role, lost_seconds"
        )
        .eq("user_id", student_id)
        .order("requested_at", desc=True)
        .limit(100)
        .execute()
    )
    rows = result.data or []
    # Strip workgroup_id from the response model — internal only.
    return [TrainingSummary(**{k: v for k, v in t.items() if k != "workgroup_id"}) for t in rows]


# ---------- Mid-training checkpoints (leLab-comparison PR-5a) ----------
#
# The Modal worker uploads each intermediate checkpoint to the model repo
# as checkpoints/<step>/pretrained_model (watcher thread, save_freq
# default 20k). Teachers introspect them here via Hub list_repo_files —
# the same pattern leLab's jobs.py uses. TEACHER-ONLY diagnostics: the
# student flow stays one-click (no checkpoint selection).

_CKPT_PATH_RE = re.compile(r"^checkpoints/(\d+)/pretrained_model/config\.json$")
# (repo_id) -> (expires_unix, [steps]); 30 s TTL bounds Hub traffic when a
# teacher dashboard polls during a live run.
_ckpt_cache: dict[str, tuple[float, list[int]]] = {}
_CKPT_CACHE_TTL_S = 30.0


class TrainingCheckpoints(BaseModel):
    training_id: int
    model_name: str
    steps: list[int]


@router.get(
    "/students/{student_id}/trainings/{training_id}/checkpoints",
    response_model=TrainingCheckpoints,
)
def list_training_checkpoints(
    student_id: str,
    training_id: int,
    teacher=Depends(get_current_teacher),
):
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    row = (
        supabase.table("trainings")
        .select("id, model_name, user_id")
        .eq("id", training_id)
        .limit(1)
        .execute()
    )
    if not row.data or row.data[0].get("user_id") != student_id:
        raise HTTPException(
            status_code=404,
            detail="Training nicht gefunden oder gehört nicht zu dieser "
                   "Schülerin/diesem Schüler.",
        )
    model_name = row.data[0]["model_name"]

    now = time.time()
    cached = _ckpt_cache.get(model_name)
    if cached and cached[0] > now:
        steps = cached[1]
    else:
        try:
            files = HfApi(token=os.environ.get("HF_TOKEN") or None).list_repo_files(
                model_name, repo_type="model")
        except Exception:
            raise HTTPException(
                status_code=502,
                detail="Checkpoint-Liste konnte nicht von Hugging Face "
                       "geladen werden.",
            )
        steps = sorted(
            int(m.group(1))
            for f in files
            if (m := _CKPT_PATH_RE.match(f)) is not None
        )
        _ckpt_cache[model_name] = (now + _CKPT_CACHE_TTL_S, steps)

    return TrainingCheckpoints(
        training_id=training_id, model_name=model_name, steps=steps)


# ---------- Inference run records (leLab-comparison PR-5b, Cloud-Inferenz 048) ----------


class InferenceRunSummary(BaseModel):
    id: int
    policy_repo: str
    started_at: str | None
    duration_s: float | None
    exit_reason: str
    error_message_de: str
    created_at: str
    # Migration 048: 'cloud' (Cloud-Inferenz, written by the session sweep) or
    # 'jetson' (a run on the retired classroom Jetson, written before migration
    # 053; those rows stay readable). Teachers READ their students' cloud runs;
    # they cannot start one (students only, owner A-Q4).
    backend: str = "jetson"


@router.get(
    "/students/{student_id}/inference-runs",
    response_model=list[InferenceRunSummary],
)
def list_student_inference_runs(
    student_id: str,
    teacher=Depends(get_current_teacher),
):
    """Compact inference run records (Cloud-Inferenz; older rows from the retired classroom Jetson) for failure forensics."""
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    result = (
        supabase.table("inference_runs")
        .select(
            "id, policy_repo, started_at, duration_s, exit_reason, "
            "error_message_de, created_at, backend"
        )
        .eq("student_user_id", student_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return [InferenceRunSummary(**r) for r in (result.data or [])]


# ---------- Roboter Studio: a student's programs + „Abgeben" (migration 040) ----------
#
# What a teacher sees (decision A3): (a) any of their students' CURRENT
# programs and (b) the snapshots the student deliberately submitted. Four
# reads, all German 404 (never 403 — no existence leakage), all behind
# _assert_student_owned FIRST and scoped on the row's own student column
# (workflows.owner_user_id / workflow_submissions.student_user_id).
# workflows.classroom_id is NEVER a filter here: a client sets it at create
# with no membership check, so keying on it would let one student's row show
# up under another teacher's classroom (Rule §4). A source-level test asserts
# these four bodies contain no "classroom_id" at all.


class StudentWorkflowSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    code_language: str = ""
    created_at: str
    updated_at: str


class StudentWorkflowDocument(StudentWorkflowSummary):
    blockly_json: dict = Field(default_factory=dict)
    code_files: dict = Field(default_factory=dict)
    sim_scene: dict | None = None


class StudentSubmissionSummary(BaseModel):
    id: str
    workflow_id: str
    name: str
    code_language: str = ""
    note: str = ""
    submitted_at: str


class StudentSubmissionDocument(StudentSubmissionSummary):
    code_files: dict = Field(default_factory=dict)
    blockly_json: dict = Field(default_factory=dict)
    sim_scene: dict = Field(default_factory=dict)


@router.get(
    "/students/{student_id}/workflows",
    response_model=list[StudentWorkflowSummary],
)
def list_student_workflows(
    student_id: str,
    teacher=Depends(get_current_teacher),
):
    """The student's own non-template programs, newest first, summaries only."""
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflows")
        .select("id, name, description, code_language, created_at, updated_at")
        .eq("owner_user_id", student_id)
        .eq("is_template", False)
        .order("updated_at", desc=True)
        .limit(DEFAULT_LIST_LIMIT)
        .execute()
    )
    return [StudentWorkflowSummary(**r) for r in (result.data or [])]


@router.get(
    "/students/{student_id}/workflows/{workflow_id}",
    response_model=StudentWorkflowDocument,
)
def get_student_workflow(
    student_id: str,
    workflow_id: str,
    teacher=Depends(get_current_teacher),
):
    """One of the student's programs, with its document."""
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflows")
        .select("*")
        .eq("id", workflow_id)
        .eq("owner_user_id", student_id)
        .eq("is_template", False)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Programm nicht gefunden")
    return StudentWorkflowDocument(**result.data[0])


@router.get(
    "/students/{student_id}/submissions",
    response_model=list[StudentSubmissionSummary],
)
def list_student_submissions(
    student_id: str,
    teacher=Depends(get_current_teacher),
):
    """The student's „Abgaben", newest first, summaries only."""
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflow_submissions")
        .select("id, workflow_id, name, code_language, note, submitted_at")
        .eq("student_user_id", student_id)
        .order("submitted_at", desc=True)
        .limit(DEFAULT_LIST_LIMIT)
        .execute()
    )
    return [StudentSubmissionSummary(**r) for r in (result.data or [])]


@router.get(
    "/students/{student_id}/submissions/{submission_id}",
    response_model=StudentSubmissionDocument,
)
def get_student_submission(
    student_id: str,
    submission_id: str,
    teacher=Depends(get_current_teacher),
):
    """One „Abgabe" of the student, with its snapshot."""
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflow_submissions")
        .select("*")
        .eq("id", submission_id)
        .eq("student_user_id", student_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Abgabe nicht gefunden")
    return StudentSubmissionDocument(**result.data[0])


# ---------- Daily progress entries ----------
#
# Each entry is scoped to a single day (entry_date) under a classroom.
# - student_id present  -> per-student daily note
# - student_id absent   -> class-wide daily note
# UNIQUE (classroom_id, student_id, entry_date) is enforced in Postgres
# via two partial indexes (migration 004).


class ProgressEntryCreate(BaseModel):
    note: str = Field(..., min_length=1, max_length=4000)
    # ISO date string YYYY-MM-DD. Defaults to server "today" (UTC) when omitted.
    entry_date: str | None = None
    # Three mutually-exclusive scopes (CHECK constraint enforces no two
    # set simultaneously):
    #   student_id NULL, workgroup_id NULL  -> class-wide
    #   student_id set                       -> per-student
    #   workgroup_id set                     -> per-group
    student_id: str | None = None
    workgroup_id: str | None = None


class ProgressEntryPatch(BaseModel):
    note: str = Field(..., min_length=1, max_length=4000)


class ProgressEntrySummary(BaseModel):
    id: str
    classroom_id: str
    student_id: str | None = None
    workgroup_id: str | None = None
    entry_date: str
    note: str
    created_at: str
    updated_at: str


def _serialize_progress_entry(row: dict) -> ProgressEntrySummary:
    return ProgressEntrySummary(
        id=row["id"],
        classroom_id=row["classroom_id"],
        student_id=row.get("student_id"),
        workgroup_id=row.get("workgroup_id"),
        entry_date=str(row["entry_date"]),
        note=row["note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _assert_entry_owned(teacher_id: str, entry_id: str) -> dict:
    supabase = get_supabase()
    result = (
        supabase.table("progress_entries").select("*").eq("id", entry_id).execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
    entry = result.data[0]
    _assert_classroom_owned(teacher_id, entry["classroom_id"])
    return entry


def _assert_workgroup_in_classroom(
    teacher_id: str, classroom_id: str, workgroup_id: str
) -> None:
    """Workgroup must exist, belong to the teacher, and live in this classroom."""
    supabase = get_supabase()
    g = (
        supabase.table("workgroups")
        .select("classroom_id")
        .eq("id", workgroup_id)
        .execute()
    )
    if not g.data:
        raise HTTPException(status_code=404, detail="Arbeitsgruppe nicht gefunden")
    if g.data[0].get("classroom_id") != classroom_id:
        raise HTTPException(
            status_code=400, detail="Arbeitsgruppe gehört nicht zu dieser Klasse"
        )
    _assert_classroom_owned(teacher_id, classroom_id)


@router.get(
    "/classrooms/{classroom_id}/progress-entries",
    response_model=list[ProgressEntrySummary],
)
def list_progress_entries(
    classroom_id: str,
    student_id: str | None = None,
    workgroup_id: str | None = None,
    scope: str | None = None,  # "student" | "classroom" | "group"
    teacher=Depends(get_current_teacher),
):
    """List entries for a classroom.

    - no filter              -> all entries (student + group + class-wide)
    - student_id=<uuid>      -> only that student's entries
    - workgroup_id=<uuid>    -> only that group's entries
    - scope=classroom        -> only class-wide entries (both scope cols NULL)
    - scope=student          -> only student-scoped entries
    - scope=group            -> only group-scoped entries
    """
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    q = supabase.table("progress_entries").select("*").eq("classroom_id", classroom_id)
    if student_id is not None:
        _assert_student_owned(teacher["id"], student_id)
        q = q.eq("student_id", student_id)
    elif workgroup_id is not None:
        _assert_workgroup_in_classroom(teacher["id"], classroom_id, workgroup_id)
        q = q.eq("workgroup_id", workgroup_id)
    elif scope == "classroom":
        q = q.is_("student_id", "null").is_("workgroup_id", "null")
    elif scope == "student":
        q = q.not_.is_("student_id", "null")
    elif scope == "group":
        q = q.not_.is_("workgroup_id", "null")
    rows = q.order("entry_date", desc=True).order("updated_at", desc=True).execute().data or []
    return [_serialize_progress_entry(r) for r in rows]


@router.post(
    "/classrooms/{classroom_id}/progress-entries",
    response_model=ProgressEntrySummary,
)
def create_progress_entry(
    classroom_id: str,
    req: ProgressEntryCreate,
    teacher=Depends(get_current_teacher),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    if req.student_id and req.workgroup_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Eintrag kann entweder einem Schüler oder einer Gruppe "
                "zugewiesen werden, aber nicht beidem."
            ),
        )
    if req.student_id:
        student = _assert_student_owned(teacher["id"], req.student_id)
        if student.get("classroom_id") != classroom_id:
            raise HTTPException(
                status_code=400,
                detail="Schüler gehört nicht zu dieser Klasse",
            )
    if req.workgroup_id:
        _assert_workgroup_in_classroom(teacher["id"], classroom_id, req.workgroup_id)

    payload: dict = {
        "classroom_id": classroom_id,
        "student_id": req.student_id,
        "workgroup_id": req.workgroup_id,
        "note": req.note.strip(),
    }
    if req.entry_date:
        payload["entry_date"] = req.entry_date

    supabase = get_supabase()
    try:
        result = supabase.table("progress_entries").insert(payload).execute()
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg:
            raise HTTPException(
                status_code=409,
                detail="Für diesen Tag existiert bereits ein Eintrag - bearbeite ihn stattdessen",
            )
        logger.error("create_progress_entry failed: %s", e)
        raise HTTPException(status_code=500, detail="Eintrag konnte nicht erstellt werden")
    return _serialize_progress_entry(result.data[0])


@router.patch(
    "/progress-entries/{entry_id}", response_model=ProgressEntrySummary
)
def patch_progress_entry(
    entry_id: str,
    req: ProgressEntryPatch,
    teacher=Depends(get_current_teacher),
):
    _assert_entry_owned(teacher["id"], entry_id)
    supabase = get_supabase()
    try:
        result = (
            supabase.table("progress_entries")
            .update({"note": req.note.strip()})
            .eq("id", entry_id)
            .execute()
        )
    except Exception as e:
        logger.error("patch_progress_entry failed: %s", e)
        raise HTTPException(status_code=500, detail="Eintrag konnte nicht aktualisiert werden")
    return _serialize_progress_entry(result.data[0])


@router.delete("/progress-entries/{entry_id}")
def delete_progress_entry(
    entry_id: str, teacher=Depends(get_current_teacher)
):
    _assert_entry_owned(teacher["id"], entry_id)
    supabase = get_supabase()
    supabase.table("progress_entries").delete().eq("id", entry_id).execute()
    return {"ok": True}



