"""Admin routes (``/admin``): teachers, their training-hour pools, compensations, history.

Since the training-hour ledger (migration 052, spec §8.5) the admin changes a pool
by DELTAS only (``credit_admin_grant`` / ``credit_admin_revoke``, decision 8: there
is no absolute set; the old ``PATCH /admin/teachers/{tid}/credits`` is gone and
answers 405), compensates a wrongly charged training straight into the student's or
group's account from outside the teacher pool (``credit_admin_compensate``, owner D4,
a mandatory reason the teacher sees), erases a reason on an Art. 17 request
(``credit_erase_reason``) and reads every ledger row. No route reads or writes
``users.training_credits``.
"""

import logging
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import get_current_admin
from app.routes.teacher import (
    erase_student_account,
    history_payload,
    parse_history_page,
    parse_request_id,
    pool_fields,
    teacher_summary,
)
from app.services import credit_texts_de as credit_texts
from app.services import deletion_requests
from app.services import passwords
from app.services.supabase_client import get_supabase
from app.services.training_dispatch import pg_code
from app.services.usernames import synthetic_email, validate_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------- Models ----------


class TeacherCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    # Checked by hand (services/passwords.py, ≥ 12 for staff since 2026-11-10):
    # pydantic's 422 is English and echoes the password back.
    password: str
    full_name: str = Field(..., min_length=1, max_length=100)
    credits: int = Field(default=0, ge=0, le=10000)


class PasswordReset(BaseModel):
    new_password: str  # checked by hand, see TeacherCreate.password


class TeacherSummary(BaseModel):
    id: str
    username: str | None
    full_name: str | None
    pool_total: int
    allocated_total: int
    pool_available: int
    student_count: int
    classroom_count: int
    # 052: spent from this pool ever, the admin's compensations into it, and the
    # pre-052 credit number (the migrate row's meta, orientation for the re-grant).
    hours_spent: int = 0
    compensated: int = 0
    old_credits: int | None = None


def _old_credits(teacher_ids) -> dict:
    """``{teacher id: old_credits}`` from the 052 ``migrate`` rows (one select)."""
    ids = [str(t) for t in teacher_ids if t]
    if not ids:
        return {}
    rows = (
        get_supabase().table("credit_ledger").select("to_id, meta")
        .eq("kind", "migrate").eq("to_kind", "teacher").in_("to_id", ids).execute()
    ).data or []
    out = {}
    for r in rows:
        meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
        if isinstance(meta.get("old_credits"), int):
            out[str(r["to_id"])] = meta["old_credits"]
    return out


def _teacher_summary_row(t: dict, *, classroom_count: int, old: dict) -> TeacherSummary:
    fields = pool_fields(teacher_summary(t["id"]))
    return TeacherSummary(
        id=t["id"], username=t.get("username"), full_name=t.get("full_name"),
        pool_total=fields["pool_total"], allocated_total=fields["allocated_total"],
        pool_available=fields["pool_available"], student_count=fields["student_count"],
        classroom_count=classroom_count, hours_spent=fields["hours_spent"],
        compensated=fields["compensated"], old_credits=old.get(str(t["id"])),
    )


# ---------- Endpoints ----------


@router.get("/teachers", response_model=list[TeacherSummary])
def list_teachers(admin=Depends(get_current_admin)):
    supabase = get_supabase()
    teachers = (
        supabase.table("users")
        .select("id, username, full_name")
        .eq("role", "teacher")
        .order("full_name", desc=False)
        .execute()
    ).data or []
    old = _old_credits([t["id"] for t in teachers])
    out: list[TeacherSummary] = []
    for t in teachers:
        classroom_count_res = (
            supabase.table("classrooms")
            .select("id", count="exact")
            .eq("teacher_id", t["id"])
            .execute()
        )
        out.append(_teacher_summary_row(t, classroom_count=int(classroom_count_res.count or 0), old=old))
    return out


@router.post("/teachers", response_model=TeacherSummary)
def create_teacher(req: TeacherCreate, admin=Depends(get_current_admin)):
    supabase = get_supabase()

    try:
        username = validate_username(req.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    refusal = passwords.password_error_de(req.password, "teacher")
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
    teacher_id = auth_user.id

    try:
        supabase.table("users").update(
            {
                "role": "teacher",
                "username": username,
                "full_name": req.full_name.strip(),
                "created_by": admin["id"],
            }
        ).eq("id", teacher_id).execute()
    except Exception as e:
        logger.error("Failed to set teacher metadata, rolling back: %s", e)
        try:
            supabase.auth.admin.delete_user(teacher_id)
        except Exception as del_err:
            logger.error("Rollback delete_user failed: %s", del_err)
        raise HTTPException(status_code=500, detail="Lehrer-Profil konnte nicht gesetzt werden")

    # 052: the start hours are a grant from outside (reason NULL), after the profile update.
    if req.credits > 0:
        try:
            supabase.rpc("credit_admin_grant", {
                "p_admin_id": admin["id"], "p_teacher_id": teacher_id, "p_hours": req.credits,
                "p_reason": None, "p_request_id": None,
            }).execute()
        except Exception as e:  # the teacher exists; the admin re-grants with „Stunden vergeben"
            logger.error("start-hour grant for teacher %s failed: %s", teacher_id, type(e).__name__)
    return _teacher_summary_row({"id": teacher_id, "username": username, "full_name": req.full_name.strip()},
                                classroom_count=0, old={})


def _free_in(exc) -> int | None:
    """The teacher's free hours a P0062 names (DETAIL, else the message „only N free")."""
    import re

    details = getattr(exc, "details", None)
    for text in (details, str(exc)):
        if isinstance(text, (str, int)):
            m = re.search(r"(\d+)", str(text)) if text is details else re.search(r"only (\d+) free", str(text))
            if m:
                return int(m.group(1))
    return None


@router.post("/teachers/{teacher_id}/credits")
def change_teacher_credits(
    teacher_id: str,
    body: dict | None = Body(default=None),
    admin=Depends(get_current_admin),
):
    """Grant (delta > 0) or revoke (delta < 0) a teacher's FREE pool hours (§8.5, decision
    8). A revoke never touches assigned hours (P0062). Answers ``{teacher_id, delta,
    pool_available}`` (the teacher's free hours after); the dashboard then reloads
    ``GET /admin/teachers``."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail=credit_texts.ADMIN_DELTA_RANGE_DE)
    delta = body.get("delta")
    if type(delta) is not int:
        raise HTTPException(status_code=400, detail=credit_texts.ADMIN_DELTA_RANGE_DE)
    if delta == 0:
        raise HTTPException(status_code=400, detail=credit_texts.DELTA_ZERO_DE)
    if abs(delta) > 10000:
        raise HTTPException(status_code=400, detail=credit_texts.ADMIN_DELTA_RANGE_DE)
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=400, detail=credit_texts.REASON_TOO_LONG_DE)
    reason = (reason or "").strip() or None
    if reason is not None and len(reason) > 200:
        raise HTTPException(status_code=400, detail=credit_texts.REASON_TOO_LONG_DE)
    request_id = parse_request_id(body.get("request_id"))
    try:
        uuid.UUID(str(teacher_id))
    except ValueError:
        raise HTTPException(status_code=404, detail=credit_texts.TEACHER_NOT_FOUND_DE) from None
    fn = "credit_admin_grant" if delta > 0 else "credit_admin_revoke"
    try:
        data = get_supabase().rpc(fn, {
            "p_admin_id": admin["id"], "p_teacher_id": teacher_id, "p_hours": abs(delta),
            "p_reason": reason, "p_request_id": request_id,
        }).execute().data
    except Exception as exc:
        code = pg_code(exc)
        if code in ("P0002", "P0067"):
            raise HTTPException(status_code=404, detail=credit_texts.TEACHER_NOT_FOUND_DE)
        if code == "P0062":
            free = _free_in(exc)
            if free is None:
                free = pool_fields(teacher_summary(teacher_id))["pool_available"]
            raise HTTPException(status_code=409, detail=credit_texts.revoke_too_much(free))
        if code == "P0071":
            raise HTTPException(status_code=409, detail=credit_texts.REQUEST_REUSED_DE)
        logger.error("%s for teacher %s failed: %s", fn, teacher_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail=credit_texts.CREDIT_CHANGE_FAILED_DE)
    value = data[0] if isinstance(data, list) and data else data
    if isinstance(value, dict):
        value = next(iter(value.values()), None)
    return {"teacher_id": teacher_id, "delta": delta, "pool_available": int(value or 0)}


def _target_not_found(kind: str) -> HTTPException:
    return HTTPException(status_code=404, detail=credit_texts.STUDENT_NOT_FOUND_DE if kind == "student"
                         else credit_texts.WORKGROUP_NOT_FOUND_DE)


@router.post("/credit-grants")
def compensate(body: dict | None = Body(default=None), admin=Depends(get_current_admin)):
    """An admin's compensation (owner D4, §8.5): hours straight into the student's or
    group's account, funded from outside the teacher pool, with a mandatory reason the
    teacher sees in the history. Naming a training credits the account that paid for it
    and gives that member's group-limit headroom back (owner Q-D). Validated by hand in
    the spec's order."""
    body = body if isinstance(body, dict) else {}
    kind = body.get("target_kind")
    if kind not in ("student", "group"):
        raise HTTPException(status_code=400, detail=credit_texts.COMPENSATION_TARGET_KIND_DE)
    hours = body.get("hours")
    if type(hours) is not int or not 1 <= hours <= 100:
        raise HTTPException(status_code=400, detail=credit_texts.COMPENSATION_HOURS_RANGE_DE)
    reason = body.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise HTTPException(status_code=400, detail=credit_texts.COMPENSATION_REASON_REQUIRED_DE)
    if len(reason) > 200:
        raise HTTPException(status_code=400, detail=credit_texts.REASON_TOO_LONG_DE)
    try:
        target_id = str(uuid.UUID(str(body.get("target_id"))))
    except (ValueError, TypeError, AttributeError):
        raise _target_not_found(kind) from None
    training_id = body.get("training_id")
    if training_id is not None and (type(training_id) is not int or training_id < 1):
        raise HTTPException(status_code=409, detail=credit_texts.COMPENSATION_TRAINING_MISMATCH_DE)
    request_id = parse_request_id(body.get("request_id"))
    try:
        data = get_supabase().rpc("credit_admin_compensate", {
            "p_admin_id": admin["id"], "p_target_kind": kind, "p_target_id": target_id, "p_hours": hours,
            "p_reason": reason, "p_training_id": training_id, "p_request_id": request_id,
        }).execute().data
    except Exception as exc:
        code = pg_code(exc)
        if code == "P0002":
            raise _target_not_found(kind)
        if code == "P0023":
            raise HTTPException(status_code=409, detail=credit_texts.COMPENSATION_GROUPED_DE)
        if code == "P0066":
            raise HTTPException(status_code=409, detail=credit_texts.COMPENSATION_NO_ACCOUNT_DE)
        if code == "P0070":
            raise HTTPException(status_code=409, detail=credit_texts.COMPENSATION_TRAINING_MISMATCH_DE)
        if code == "P0071":
            raise HTTPException(status_code=409, detail=credit_texts.REQUEST_REUSED_DE)
        # P0067 is impossible behind get_current_admin: a bug, never a leak
        logger.error("credit_admin_compensate failed: %s (%s)", type(exc).__name__, code)
        raise HTTPException(status_code=500, detail=credit_texts.CREDIT_CHANGE_FAILED_DE)
    value = data[0] if isinstance(data, list) and data else data
    if isinstance(value, dict):
        value = next(iter(value.values()), None)
    return {"target_kind": kind, "target_id": target_id, "hours_free": int(value or 0)}


@router.delete("/credit-reasons/{reason_id}")
def erase_reason(reason_id: str, admin=Depends(get_current_admin)):
    """Erase one reason text (an Art. 17 request about what an admin typed, review m11);
    the ledger row keeps a dangling id and the history shows „(gelöscht)"."""
    try:
        rid = int(str(reason_id))
    except ValueError:
        raise HTTPException(status_code=404, detail=credit_texts.REASON_NOT_FOUND_DE) from None
    if rid < 1:
        raise HTTPException(status_code=404, detail=credit_texts.REASON_NOT_FOUND_DE)
    try:
        data = get_supabase().rpc("credit_erase_reason", {"p_admin_id": admin["id"], "p_reason_id": rid}).execute().data
    except Exception as exc:
        logger.error("credit_erase_reason failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=credit_texts.CREDIT_CHANGE_FAILED_DE)
    value = data[0] if isinstance(data, list) and data else data
    if isinstance(value, dict):
        value = next(iter(value.values()), None)
    return {"erased": bool(value)}


@router.get("/credit-history")
def admin_credit_history(
    scope: str | None = Query(default=None),
    teacher_id: str | None = Query(default=None),
    before: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    admin=Depends(get_current_admin),
):
    """Every ledger row (scope ``all``) or one teacher's pool and every account it funds
    (``teacher_all``), with the ids a compensation needs (§8.5)."""
    scope = scope or "all"
    if scope not in ("all", "teacher_all"):
        raise HTTPException(status_code=400, detail=credit_texts.HISTORY_FAILED_DE)
    owner = None
    if scope == "teacher_all":
        try:
            owner = str(uuid.UUID(str(teacher_id)))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=404, detail=credit_texts.TEACHER_NOT_FOUND_DE) from None
    return history_payload(scope, owner, before, limit, admin=True)


@router.get("/refusals")
def admin_refusals(
    user_id: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    admin=Depends(get_current_admin),
):
    """Refused training starts (decision 14), newest first, of one user or of everyone."""
    _, lim = parse_history_page(None, limit if limit is not None else 50)
    q = get_supabase().table("training_start_refusals").select("id, at, user_id, role, request_kind, code, model_type")
    if user_id:
        try:
            q = q.eq("user_id", str(uuid.UUID(str(user_id))))
        except ValueError:
            return {"refusals": []}
    try:
        rows = q.order("id", desc=True).limit(lim).execute().data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("refusal list failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=credit_texts.HISTORY_FAILED_DE)
    return {"refusals": rows}


@router.post("/teachers/{teacher_id}/password")
def reset_teacher_password(
    teacher_id: str,
    req: PasswordReset,
    admin=Depends(get_current_admin),
):
    refusal = passwords.password_error_de(req.new_password, "teacher")
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)
    supabase = get_supabase()
    existing = (
        supabase.table("users").select("role").eq("id", teacher_id).execute()
    )
    if not existing.data or existing.data[0]["role"] != "teacher":
        raise HTTPException(status_code=404, detail="Lehrer nicht gefunden")
    try:
        supabase.auth.admin.update_user_by_id(teacher_id, {"password": req.new_password})
    except Exception as e:
        if passwords.is_weak_password_error(e):
            raise HTTPException(status_code=400, detail=passwords.WEAK_PASSWORD_DE)
        logger.error("reset_teacher_password failed: %s", e)
        raise HTTPException(status_code=500, detail="Passwort konnte nicht gesetzt werden")
    return {"ok": True}


@router.delete("/teachers/{teacher_id}")
def delete_teacher(teacher_id: str, admin=Depends(get_current_admin)):
    supabase = get_supabase()
    existing = (
        supabase.table("users").select("id, role").eq("id", teacher_id).execute()
    )
    if not existing.data or existing.data[0]["role"] != "teacher":
        raise HTTPException(status_code=404, detail="Lehrer nicht gefunden")
    return erase_teacher_account(teacher_id)


def erase_teacher_account(teacher_id: str) -> dict:
    """BLOCKING: delete a teacher's account; 409 while the teacher still has
    classrooms (their students need a teacher first). Shared by
    ``DELETE /admin/teachers/{id}`` and the confirmation of a teacher's own
    deletion request; the caller has checked the role."""
    supabase = get_supabase()
    classroom_count_res = (
        supabase.table("classrooms")
        .select("id", count="exact")
        .eq("teacher_id", teacher_id)
        .execute()
    )
    if (classroom_count_res.count or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="Lehrer hat noch Klassenzimmer — bitte diese zuerst löschen",
        )
    try:
        supabase.auth.admin.delete_user(teacher_id)
    except Exception as e:
        logger.error("delete_user failed: %s", e)
        raise HTTPException(status_code=500, detail="Konto konnte nicht gelöscht werden")
    return {"ok": True}


# ---------- Deletion requests (GDPR Art. 17, launch plan step 5) ----------


class AdminDeletionRequest(BaseModel):
    user_id: str
    role: str
    username: str | None = None
    full_name: str | None = None
    classroom_id: str | None = None
    classroom_name: str | None = None
    # The teacher who normally confirms (a student's classroom's teacher); None
    # when the student has no classroom (then only the admin can confirm).
    teacher_name: str | None = None
    requested_at: str
    due_by: str | None = None


@router.get("/deletion-requests", response_model=list[AdminDeletionRequest])
def list_deletion_requests(admin=Depends(get_current_admin)):
    """Every open deletion request, students' and teachers', oldest first.

    The admin may confirm any of them (owner decision „teacher confirms", with
    the admin as the backstop): a teacher who left the school, a deleted
    classroom or a teacher's own request would otherwise leave a request open past
    Art. 12(3)'s one month with nobody able to carry it out."""
    supabase = get_supabase()
    try:
        rows = (
            supabase.table("users")
            .select("id, role, username, full_name, classroom_id, deletion_requested_at")
            .not_.is_("deletion_requested_at", "null")
            .in_("role", ["student", "teacher"])
            .order("deletion_requested_at", desc=False)
            .execute()
        ).data or []
        class_ids = sorted({r["classroom_id"] for r in rows if r.get("role") == "student" and r.get("classroom_id")})
        classrooms = {}
        if class_ids:
            classrooms = {
                c["id"]: c for c in (
                    supabase.table("classrooms").select("id, name, teacher_id").in_("id", class_ids).execute()
                ).data or []
            }
        teacher_ids = sorted({c.get("teacher_id") for c in classrooms.values() if c.get("teacher_id")})
        teachers = {}
        if teacher_ids:
            teachers = {
                t["id"]: t for t in (
                    supabase.table("users").select("id, username, full_name").in_("id", teacher_ids).execute()
                ).data or []
            }
    except Exception as exc:
        logger.warning("admin deletion-request list failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=deletion_requests.LIST_FAILED_DE) from None
    out = []
    for r in rows:
        if not r.get("deletion_requested_at"):
            continue
        classroom = classrooms.get(r.get("classroom_id")) if r.get("role") == "student" else None
        teacher = teachers.get(classroom.get("teacher_id")) if classroom else None
        out.append(AdminDeletionRequest(
            user_id=r["id"], role=r.get("role") or "", username=r.get("username"),
            full_name=r.get("full_name"),
            classroom_id=classroom["id"] if classroom else None,
            classroom_name=classroom.get("name") if classroom else None,
            teacher_name=(teacher.get("full_name") or teacher.get("username")) if teacher else None,
            requested_at=r["deletion_requested_at"],
            due_by=deletion_requests.due_by(r["deletion_requested_at"]),
        ))
    return out


@router.post("/deletion-requests/{user_id}/confirm")
def confirm_deletion_request(user_id: str, admin=Depends(get_current_admin)):
    """The admin carries out an OPEN deletion request: a student's through the
    teacher's own full deletion (``routes/teacher.py::erase_student_account``,
    same five outcomes), a teacher's through ``DELETE /admin/teachers/{id}``'s
    rule (409 while classrooms exist). A user without an open request is refused
    (409), so this route deletes nobody who did not ask."""
    rows = (
        get_supabase().table("users").select("id, role, deletion_requested_at").eq("id", user_id).execute()
    ).data or []
    if not rows or rows[0].get("role") not in ("student", "teacher"):
        raise HTTPException(status_code=404, detail=deletion_requests.NO_REQUESTER_DE)
    if not rows[0].get("deletion_requested_at"):
        raise HTTPException(status_code=409, detail=deletion_requests.NOT_OPEN_DE)
    logger.info("deletion request of user=%s (%s) confirmed by admin=%s", user_id, rows[0]["role"], admin["id"])
    if rows[0]["role"] == "teacher":
        return erase_teacher_account(user_id)
    return erase_student_account(user_id)
