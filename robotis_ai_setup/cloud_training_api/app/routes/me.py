import logging

from fastapi import APIRouter, Depends
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
