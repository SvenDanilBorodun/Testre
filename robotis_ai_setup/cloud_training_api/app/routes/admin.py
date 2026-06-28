import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_admin
from app.services.supabase_client import get_supabase
from app.services.usernames import synthetic_email, validate_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------- Models ----------


class TeacherCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=100)
    credits: int = Field(default=0, ge=0, le=10000)


class TeacherCreditsSet(BaseModel):
    credits: int = Field(..., ge=0, le=10000)


class PasswordReset(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)


class TeacherSummary(BaseModel):
    id: str
    username: str | None
    full_name: str | None
    pool_total: int
    allocated_total: int
    pool_available: int
    student_count: int
    classroom_count: int


# ---------- Endpoints ----------


@router.get("/teachers", response_model=list[TeacherSummary])
async def list_teachers(admin=Depends(get_current_admin)):
    supabase = get_supabase()
    teachers = (
        supabase.table("users")
        .select("id, username, full_name, training_credits")
        .eq("role", "teacher")
        .order("full_name", desc=False)
        .execute()
    ).data or []

    out: list[TeacherSummary] = []
    for t in teachers:
        summary_res = supabase.rpc(
            "get_teacher_credit_summary", {"p_teacher_id": t["id"]}
        ).execute()
        row = (summary_res.data or [{}])[0]
        classroom_count_res = (
            supabase.table("classrooms")
            .select("id", count="exact")
            .eq("teacher_id", t["id"])
            .execute()
        )
        out.append(
            TeacherSummary(
                id=t["id"],
                username=t.get("username"),
                full_name=t.get("full_name"),
                pool_total=int(row.get("pool_total", t["training_credits"])),
                allocated_total=int(row.get("allocated_total", 0)),
                pool_available=int(
                    row.get("pool_available", t["training_credits"])
                ),
                student_count=int(row.get("student_count", 0)),
                classroom_count=int(classroom_count_res.count or 0),
            )
        )
    return out


@router.post("/teachers", response_model=TeacherSummary)
async def create_teacher(req: TeacherCreate, admin=Depends(get_current_admin)):
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
    teacher_id = auth_user.id

    try:
        supabase.table("users").update(
            {
                "role": "teacher",
                "username": username,
                "full_name": req.full_name.strip(),
                "training_credits": req.credits,
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

    return TeacherSummary(
        id=teacher_id,
        username=username,
        full_name=req.full_name.strip(),
        pool_total=req.credits,
        allocated_total=0,
        pool_available=req.credits,
        student_count=0,
        classroom_count=0,
    )


@router.patch("/teachers/{teacher_id}/credits", response_model=TeacherSummary)
async def set_teacher_credits(
    teacher_id: str,
    req: TeacherCreditsSet,
    admin=Depends(get_current_admin),
):
    supabase = get_supabase()
    existing = (
        supabase.table("users")
        .select("id, role, username, full_name")
        .eq("id", teacher_id)
        .execute()
    )
    if not existing.data or existing.data[0]["role"] != "teacher":
        raise HTTPException(status_code=404, detail="Lehrer nicht gefunden")

    # Compute allocated so we can prevent going below it
    summary_res = supabase.rpc(
        "get_teacher_credit_summary", {"p_teacher_id": teacher_id}
    ).execute()
    row = (summary_res.data or [{}])[0]
    allocated = int(row.get("allocated_total", 0))
    if req.credits < allocated:
        raise HTTPException(
            status_code=409,
            detail=f"Kann nicht unter bereits verteilte Credits ({allocated}) gesetzt werden",
        )

    supabase.table("users").update({"training_credits": req.credits}).eq(
        "id", teacher_id
    ).execute()

    summary_res = supabase.rpc(
        "get_teacher_credit_summary", {"p_teacher_id": teacher_id}
    ).execute()
    new_row = (summary_res.data or [{}])[0]
    classroom_count_res = (
        supabase.table("classrooms")
        .select("id", count="exact")
        .eq("teacher_id", teacher_id)
        .execute()
    )
    t = existing.data[0]
    return TeacherSummary(
        id=teacher_id,
        username=t.get("username"),
        full_name=t.get("full_name"),
        pool_total=int(new_row.get("pool_total", req.credits)),
        allocated_total=int(new_row.get("allocated_total", 0)),
        pool_available=int(new_row.get("pool_available", req.credits)),
        student_count=int(new_row.get("student_count", 0)),
        classroom_count=int(classroom_count_res.count or 0),
    )


@router.post("/teachers/{teacher_id}/password")
async def reset_teacher_password(
    teacher_id: str,
    req: PasswordReset,
    admin=Depends(get_current_admin),
):
    supabase = get_supabase()
    existing = (
        supabase.table("users").select("role").eq("id", teacher_id).execute()
    )
    if not existing.data or existing.data[0]["role"] != "teacher":
        raise HTTPException(status_code=404, detail="Lehrer nicht gefunden")
    try:
        supabase.auth.admin.update_user_by_id(teacher_id, {"password": req.new_password})
    except Exception as e:
        logger.error("reset_teacher_password failed: %s", e)
        raise HTTPException(status_code=500, detail="Passwort konnte nicht gesetzt werden")
    return {"ok": True}


@router.delete("/teachers/{teacher_id}")
async def delete_teacher(teacher_id: str, admin=Depends(get_current_admin)):
    supabase = get_supabase()
    existing = (
        supabase.table("users").select("id, role").eq("id", teacher_id).execute()
    )
    if not existing.data or existing.data[0]["role"] != "teacher":
        raise HTTPException(status_code=404, detail="Lehrer nicht gefunden")

    classroom_count_res = (
        supabase.table("classrooms")
        .select("id", count="exact")
        .eq("teacher_id", teacher_id)
        .execute()
    )
    if (classroom_count_res.count or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="Lehrer hat noch Klassenzimmer - erst loeschen",
        )
    try:
        supabase.auth.admin.delete_user(teacher_id)
    except Exception as e:
        logger.error("delete_user failed: %s", e)
        raise HTTPException(status_code=500, detail="Konto konnte nicht geloescht werden")
    return {"ok": True}
