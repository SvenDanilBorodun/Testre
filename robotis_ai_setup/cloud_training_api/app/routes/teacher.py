import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from huggingface_hub import HfApi
from huggingface_hub.utils import RepositoryNotFoundError
from pydantic import BaseModel, Field

from app.auth import get_current_teacher, get_user_profile
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
    password: str = Field(..., min_length=6, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=100)
    initial_credits: int = Field(default=0, ge=0, le=1000)


class StudentPatch(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    classroom_id: str | None = None


class PasswordReset(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)


class CreditsDelta(BaseModel):
    delta: int = Field(..., ge=-1000, le=1000)


class ClassroomSummary(BaseModel):
    id: str
    name: str
    created_at: str
    student_count: int


class StudentSummary(BaseModel):
    id: str
    username: str | None
    full_name: str | None
    training_credits: int
    trainings_used: int
    remaining: int
    classroom_id: str | None


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


class ClassroomDetail(BaseModel):
    id: str
    name: str
    created_at: str
    students: list[StudentSummary]


class CreditsResponse(BaseModel):
    new_amount: int
    pool_available: int


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
        .select("id, username, full_name, training_credits, classroom_id, role")
        .eq("id", student_id)
        .eq("role", "student")
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Schueler nicht gefunden")
    student = result.data[0]
    if not student.get("classroom_id"):
        raise HTTPException(status_code=404, detail="Schueler gehoert zu keinem Klassenzimmer")
    _assert_classroom_owned(teacher_id, student["classroom_id"])
    return student


def _student_usage(student_id: str) -> int:
    supabase = get_supabase()
    result = (
        supabase.table("trainings")
        .select("id", count="exact")
        .eq("user_id", student_id)
        .not_.in_("status", ["failed", "canceled"])
        .execute()
    )
    return int(result.count or 0)


def _student_summary(row: dict) -> StudentSummary:
    credits = int(row.get("training_credits") or 0)
    used = _student_usage(row["id"])
    return StudentSummary(
        id=row["id"],
        username=row.get("username"),
        full_name=row.get("full_name"),
        training_credits=credits,
        trainings_used=used,
        remaining=credits - used,
        classroom_id=row.get("classroom_id"),
    )


# ---------- Classrooms ----------


@router.get("/classrooms", response_model=list[ClassroomSummary])
async def list_classrooms(teacher=Depends(get_current_teacher)):
    supabase = get_supabase()
    classrooms = (
        supabase.table("classrooms")
        .select("*")
        .eq("teacher_id", teacher["id"])
        .order("created_at", desc=False)
        .execute()
    ).data or []

    out: list[ClassroomSummary] = []
    for c in classrooms:
        count_res = (
            supabase.table("users")
            .select("id", count="exact")
            .eq("classroom_id", c["id"])
            .eq("role", "student")
            .execute()
        )
        out.append(
            ClassroomSummary(
                id=c["id"],
                name=c["name"],
                created_at=c["created_at"],
                student_count=int(count_res.count or 0),
            )
        )
    return out


@router.post("/classrooms", response_model=ClassroomSummary)
async def create_classroom(req: ClassroomCreate, teacher=Depends(get_current_teacher)):
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
async def get_classroom(classroom_id: str, teacher=Depends(get_current_teacher)):
    c = _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    students_raw = (
        supabase.table("users")
        .select("id, username, full_name, training_credits, classroom_id")
        .eq("classroom_id", classroom_id)
        .eq("role", "student")
        .order("full_name", desc=False)
        .execute()
    ).data or []
    return ClassroomDetail(
        id=c["id"],
        name=c["name"],
        created_at=c["created_at"],
        students=[_student_summary(s) for s in students_raw],
    )


@router.get("/classrooms/{classroom_id}/workflow-templates", response_model=list[WorkflowTemplateSummary])
async def list_workflow_templates(
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
async def create_workflow_template(
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
async def delete_workflow_template(
    classroom_id: str,
    template_id: str,
    teacher=Depends(get_current_teacher),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    supabase.table("workflows").delete().eq("id", template_id).eq("classroom_id", classroom_id).eq("is_template", True).execute()
    return {"ok": True}


@router.patch("/classrooms/{classroom_id}", response_model=ClassroomSummary)
async def rename_classroom(
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
async def delete_classroom(classroom_id: str, teacher=Depends(get_current_teacher)):
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
            detail="Klassenzimmer ist nicht leer - erst alle Schueler entfernen",
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
async def create_student(
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
            raise HTTPException(status_code=409, detail="Klassenzimmer voll (30 Schueler)")
        raise HTTPException(status_code=500, detail="Schueler-Profil konnte nicht gesetzt werden")

    # Allocate initial credits via the RPC (enforces teacher-pool limits).
    if req.initial_credits > 0:
        try:
            supabase.rpc(
                "adjust_student_credits",
                {
                    "p_teacher_id": teacher["id"],
                    "p_student_id": student_id,
                    "p_delta": req.initial_credits,
                },
            ).execute()
        except Exception as e:
            msg = str(e)
            if "P0014" in msg:
                # Pool exhausted — student is still created with 0 credits.
                logger.warning("Insufficient pool when creating %s: %s", username, e)
                raise HTTPException(
                    status_code=409,
                    detail="Schueler erstellt, aber Lehrer-Pool reicht nicht fuer die Startguthaben",
                )
            logger.error("Initial credit allocation failed: %s", e)
            # Student still exists with 0 credits — return instead of failing.

    row = (
        supabase.table("users")
        .select("id, username, full_name, training_credits, classroom_id")
        .eq("id", student_id)
        .single()
        .execute()
    ).data
    return _student_summary(row)


@router.patch("/students/{student_id}", response_model=StudentSummary)
async def patch_student(
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
        raise HTTPException(status_code=400, detail="Keine Aenderungen")

    try:
        supabase.table("users").update(updates).eq("id", student_id).execute()
    except Exception as e:
        msg = str(e).lower()
        if "p0010" in msg or "kapazitaet" in msg:
            raise HTTPException(status_code=409, detail="Ziel-Klassenzimmer voll (30 Schueler)")
        logger.error("patch_student failed: %s", e)
        raise HTTPException(status_code=500, detail="Aktualisierung fehlgeschlagen")

    row = (
        supabase.table("users")
        .select("id, username, full_name, training_credits, classroom_id")
        .eq("id", student_id)
        .single()
        .execute()
    ).data
    return _student_summary(row)


def _delete_student_hf_artifacts(student_id: str) -> None:
    """Best-effort deletion of HuggingFace repos owned by the student.

    GDPR Art. 17: when a student account is deleted, the personal data
    they generated must be erased. The Modal worker uploads trained
    models to ``EduBotics-Solutions/<...>`` and the recording stack
    pushes datasets to ``<student_username>/<robot_type>_<task>`` (or
    EduBotics-Solutions/, depending on the recording config) — both
    contain robot demonstrations that may include faces, classroom
    audio, or other identifying detail.

    Errors are logged but do not block the auth deletion; an HF Hub
    outage during a deletion request would otherwise leave the auth
    user dangling. A cron-style cleanup pass can reconcile later.
    """
    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        logger.warning(
            "HF_TOKEN not set; skipping HF artifact cleanup for %s", student_id
        )
        return
    supabase = get_supabase()
    rows = (
        supabase.table("trainings")
        .select("dataset_name, model_name")
        .eq("user_id", student_id)
        .execute()
    ).data or []
    if not rows:
        return
    api = HfApi(token=hf_token)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        for repo_id, repo_type in (
            (row.get("model_name"), "model"),
            (row.get("dataset_name"), "dataset"),
        ):
            if not repo_id or (repo_id, repo_type) in seen:
                continue
            seen.add((repo_id, repo_type))
            try:
                api.delete_repo(repo_id=repo_id, repo_type=repo_type, missing_ok=True)
                logger.info("Deleted HF %s repo %s", repo_type, repo_id)
            except RepositoryNotFoundError:
                continue
            except Exception as e:
                logger.warning(
                    "HF delete %s %s failed: %s", repo_type, repo_id, e
                )


@router.delete("/students/{student_id}")
async def delete_student(student_id: str, teacher=Depends(get_current_teacher)):
    _assert_student_owned(teacher["id"], student_id)
    # Erase HF datasets/models BEFORE the auth user is deleted — once
    # the student row is gone we'd have to keep a separate record of
    # what to clean up, which becomes a GDPR liability of its own.
    _delete_student_hf_artifacts(student_id)
    supabase = get_supabase()
    try:
        supabase.auth.admin.delete_user(student_id)
    except Exception as e:
        logger.error("delete_user failed: %s", e)
        raise HTTPException(status_code=500, detail="Konto konnte nicht geloescht werden")
    return {"ok": True}


@router.post("/students/{student_id}/password")
async def reset_student_password(
    student_id: str,
    req: PasswordReset,
    teacher=Depends(get_current_teacher),
):
    _assert_student_owned(teacher["id"], student_id)
    supabase = get_supabase()
    try:
        supabase.auth.admin.update_user_by_id(
            student_id, {"password": req.new_password}
        )
    except Exception as e:
        logger.error("reset_student_password failed: %s", e)
        raise HTTPException(status_code=500, detail="Passwort konnte nicht gesetzt werden")
    return {"ok": True}


@router.post("/students/{student_id}/credits", response_model=CreditsResponse)
async def adjust_credits(
    student_id: str,
    req: CreditsDelta,
    teacher=Depends(get_current_teacher),
):
    _assert_student_owned(teacher["id"], student_id)
    if req.delta == 0:
        raise HTTPException(status_code=400, detail="Delta darf nicht 0 sein")
    supabase = get_supabase()
    try:
        result = supabase.rpc(
            "adjust_student_credits",
            {
                "p_teacher_id": teacher["id"],
                "p_student_id": student_id,
                "p_delta": req.delta,
            },
        ).execute()
    except Exception as e:
        msg = str(e)
        if "P0011" in msg:
            raise HTTPException(status_code=403, detail="Schueler gehoert nicht zu diesem Lehrer")
        if "P0012" in msg:
            raise HTTPException(
                status_code=409,
                detail="Neuer Betrag waere kleiner als bereits verbrauchte Credits",
            )
        if "P0013" in msg:
            raise HTTPException(status_code=409, detail="Credits duerfen nicht negativ werden")
        if "P0014" in msg:
            raise HTTPException(status_code=409, detail="Lehrer hat nicht genug Credits im Pool")
        logger.error("adjust_student_credits failed: %s", e)
        raise HTTPException(status_code=500, detail="Credit-Anpassung fehlgeschlagen")
    row = (result.data or [{}])[0]
    return CreditsResponse(
        new_amount=int(row.get("new_amount", 0)),
        pool_available=int(row.get("pool_available", 0)),
    )


@router.get("/students/{student_id}/trainings", response_model=list[TrainingSummary])
async def list_student_trainings(
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
            "requested_at, terminated_at, error_message"
        )
        .eq("user_id", student_id)
        .order("requested_at", desc=True)
        .limit(100)
        .execute()
    )
    return [TrainingSummary(**t) for t in (result.data or [])]


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
    # Null / omitted -> class-wide entry. Otherwise a student in the classroom.
    student_id: str | None = None


class ProgressEntryPatch(BaseModel):
    note: str = Field(..., min_length=1, max_length=4000)


class ProgressEntrySummary(BaseModel):
    id: str
    classroom_id: str
    student_id: str | None = None
    entry_date: str
    note: str
    created_at: str
    updated_at: str


def _serialize_progress_entry(row: dict) -> ProgressEntrySummary:
    return ProgressEntrySummary(
        id=row["id"],
        classroom_id=row["classroom_id"],
        student_id=row.get("student_id"),
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


@router.get(
    "/classrooms/{classroom_id}/progress-entries",
    response_model=list[ProgressEntrySummary],
)
async def list_progress_entries(
    classroom_id: str,
    student_id: str | None = None,
    scope: str | None = None,  # "student" | "classroom" — filters to one or the other when set
    teacher=Depends(get_current_teacher),
):
    """List entries for a classroom.

    - no filter                  -> all entries (both classroom + student)
    - student_id=<uuid>          -> only that student's entries
    - scope=classroom            -> only class-wide entries (student_id IS NULL)
    - scope=student (no id)      -> only student-scoped entries (student_id IS NOT NULL)
    """
    _assert_classroom_owned(teacher["id"], classroom_id)
    supabase = get_supabase()
    q = supabase.table("progress_entries").select("*").eq("classroom_id", classroom_id)
    if student_id is not None:
        _assert_student_owned(teacher["id"], student_id)
        q = q.eq("student_id", student_id)
    elif scope == "classroom":
        q = q.is_("student_id", "null")
    elif scope == "student":
        q = q.not_.is_("student_id", "null")
    rows = q.order("entry_date", desc=True).order("updated_at", desc=True).execute().data or []
    return [_serialize_progress_entry(r) for r in rows]


@router.post(
    "/classrooms/{classroom_id}/progress-entries",
    response_model=ProgressEntrySummary,
)
async def create_progress_entry(
    classroom_id: str,
    req: ProgressEntryCreate,
    teacher=Depends(get_current_teacher),
):
    _assert_classroom_owned(teacher["id"], classroom_id)
    if req.student_id:
        student = _assert_student_owned(teacher["id"], req.student_id)
        if student.get("classroom_id") != classroom_id:
            raise HTTPException(
                status_code=400,
                detail="Schueler gehoert nicht zu dieser Klasse",
            )

    payload: dict = {
        "classroom_id": classroom_id,
        "student_id": req.student_id,
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
                detail="Fuer diesen Tag existiert bereits ein Eintrag - bearbeite ihn stattdessen",
            )
        logger.error("create_progress_entry failed: %s", e)
        raise HTTPException(status_code=500, detail="Eintrag konnte nicht erstellt werden")
    return _serialize_progress_entry(result.data[0])


@router.patch(
    "/progress-entries/{entry_id}", response_model=ProgressEntrySummary
)
async def patch_progress_entry(
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
async def delete_progress_entry(
    entry_id: str, teacher=Depends(get_current_teacher)
):
    _assert_entry_owned(teacher["id"], entry_id)
    supabase = get_supabase()
    supabase.table("progress_entries").delete().eq("id", entry_id).execute()
    return {"ok": True}



