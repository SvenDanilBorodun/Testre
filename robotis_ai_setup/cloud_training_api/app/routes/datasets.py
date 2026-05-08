"""Datasets registry — discovery layer for HF Hub datasets.

Why this exists: nothing in Supabase tracked HF datasets before migration
011. Without it, group siblings cannot discover datasets uploaded by other
group members because the React DatasetSelector queried only their own HF
namespace via the ROS service `/training/get_dataset_list`.

This registry is purely for *discovery*. The HF repo itself is owned by
the student's HF user (`<username>/<task>` or `EduBotics-Solutions/<...>`)
and access is governed by HF's own visibility rules. The registry stores
the repo_id + display metadata so a group sibling's React app can list
peer-uploaded datasets and pass the same `repo_id` to /trainings/start.

Auto-share with the owner's current group at registration time matches
the user's mental model ("everything I upload is shared with my group").
A student can leave the group and the row's workgroup_id stays so former
members keep historical visibility (lifecycle policy, see plan).
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from huggingface_hub import HfApi
from huggingface_hub.utils import RepositoryNotFoundError
from pydantic import BaseModel, Field, field_validator

from app.auth import get_current_user, get_user_profile
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/datasets", tags=["datasets"])

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500


# ---------- Models ----------


class DatasetRegister(BaseModel):
    hf_repo_id: str = Field(..., min_length=3, max_length=200)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    episode_count: int | None = Field(default=None, ge=0, le=10_000_000)
    total_frames: int | None = Field(default=None, ge=0, le=10_000_000_000)
    fps: int | None = Field(default=None, ge=1, le=240)
    robot_type: str | None = Field(default=None, max_length=64)

    @field_validator("hf_repo_id")
    @classmethod
    def _shape(cls, v: str) -> str:
        if "/" not in v or v.startswith("/") or v.endswith("/"):
            raise ValueError("hf_repo_id must be in 'owner/repo' form")
        return v


class DatasetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class DatasetResponse(BaseModel):
    id: str
    owner_user_id: str
    workgroup_id: str | None
    hf_repo_id: str
    name: str
    description: str
    episode_count: int | None = None
    total_frames: int | None = None
    fps: int | None = None
    robot_type: str | None = None
    created_at: str
    updated_at: str
    is_owned: bool
    is_group_shared: bool


# ---------- Helpers ----------


def _serialize(row: dict, *, viewer_id: str) -> DatasetResponse:
    return DatasetResponse(
        id=row["id"],
        owner_user_id=row["owner_user_id"],
        workgroup_id=row.get("workgroup_id"),
        hf_repo_id=row["hf_repo_id"],
        name=row["name"],
        description=row.get("description", ""),
        episode_count=row.get("episode_count"),
        total_frames=row.get("total_frames"),
        fps=row.get("fps"),
        robot_type=row.get("robot_type"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        is_owned=(row["owner_user_id"] == viewer_id),
        is_group_shared=bool(row.get("workgroup_id")),
    )


def _hf_repo_exists(repo_id: str) -> bool:
    """Best-effort HF dataset existence check.

    Returns True on a real 200, False on RepositoryNotFoundError.
    Transient errors (network, rate limit, 5xx) raise so the caller can
    surface them as 502 rather than silently registering a non-existent
    repo. Token is optional — public datasets resolve without auth.
    """
    api = HfApi(token=os.environ.get("HF_TOKEN", ""))
    try:
        api.dataset_info(repo_id)
        return True
    except RepositoryNotFoundError:
        return False


# ---------- Endpoints ----------


@router.get("", response_model=list[DatasetResponse])
def list_datasets(
    user=Depends(get_current_user),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
) -> list[DatasetResponse]:
    """Returns own datasets + datasets shared with the caller's group(s).

    Uses workgroup_memberships so former group members keep historical
    visibility (lifecycle policy: trainings/datasets stay readable after
    a student leaves the group).
    """
    supabase = get_supabase()
    profile = get_user_profile(str(user.id))

    own_rows = (
        supabase.table("datasets")
        .select("*")
        .eq("owner_user_id", user.id)
        .order("updated_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    ).data or []

    # Group-shared rows from any group the caller is/was a member of.
    member_rows = (
        supabase.table("workgroup_memberships")
        .select("workgroup_id")
        .eq("user_id", user.id)
        .execute()
    ).data or []
    group_ids = [m["workgroup_id"] for m in member_rows]
    # Fall back to current workgroup_id if the audit table is empty (covers
    # the corner case where a row was inserted before the migration ran in
    # an integration environment).
    if not group_ids and profile.get("workgroup_id"):
        group_ids = [profile["workgroup_id"]]

    group_rows: list[dict] = []
    if group_ids:
        group_rows = (
            supabase.table("datasets")
            .select("*")
            .in_("workgroup_id", group_ids)
            .order("updated_at", desc=True)
            .range(0, MAX_LIST_LIMIT - 1)
            .execute()
        ).data or []

    seen: set[str] = set()
    merged: list[dict] = []
    for r in own_rows + group_rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        merged.append(r)

    viewer_id = str(user.id)
    return [_serialize(r, viewer_id=viewer_id) for r in merged]


@router.post("", response_model=DatasetResponse)
def register_dataset(
    payload: DatasetRegister, user=Depends(get_current_user)
) -> DatasetResponse:
    """Register a freshly uploaded HF dataset.

    Idempotent: the UNIQUE (owner_user_id, hf_repo_id) constraint means
    re-registering the same repo updates instead of duplicating. We mirror
    that behavior in code rather than catching the constraint error.
    """
    # Validate the HF repo exists. Distinguish 404 (typo) from transient
    # errors so the React layer can show different messages.
    try:
        exists = _hf_repo_exists(payload.hf_repo_id)
    except Exception as e:
        logger.warning(
            "HF dataset existence check failed for %s: %s",
            payload.hf_repo_id, e,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "HuggingFace Hub ist gerade nicht erreichbar — bitte gleich "
                "noch einmal versuchen."
            ),
        )
    if not exists:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Datensatz '{payload.hf_repo_id}' wurde auf HuggingFace Hub "
                "nicht gefunden."
            ),
        )

    profile = get_user_profile(str(user.id))
    workgroup_id = profile.get("workgroup_id")

    supabase = get_supabase()
    # Upsert by (owner_user_id, hf_repo_id) — keep prior id when re-registered.
    existing = (
        supabase.table("datasets")
        .select("*")
        .eq("owner_user_id", user.id)
        .eq("hf_repo_id", payload.hf_repo_id)
        .execute()
    )
    update_fields: dict = {
        "name": payload.name,
        "description": payload.description,
        "episode_count": payload.episode_count,
        "total_frames": payload.total_frames,
        "fps": payload.fps,
        "robot_type": payload.robot_type,
        "workgroup_id": workgroup_id,
    }
    if existing.data:
        result = (
            supabase.table("datasets")
            .update(update_fields)
            .eq("id", existing.data[0]["id"])
            .execute()
        )
    else:
        insert_payload = {**update_fields, "owner_user_id": user.id, "hf_repo_id": payload.hf_repo_id}
        result = supabase.table("datasets").insert(insert_payload).execute()

    if not result.data:
        raise HTTPException(
            status_code=500, detail="Datensatz konnte nicht gespeichert werden."
        )
    return _serialize(result.data[0], viewer_id=str(user.id))


@router.patch("/{dataset_id}", response_model=DatasetResponse)
def update_dataset(
    dataset_id: str,
    payload: DatasetUpdate,
    user=Depends(get_current_user),
) -> DatasetResponse:
    supabase = get_supabase()
    existing = (
        supabase.table("datasets")
        .select("*")
        .eq("id", dataset_id)
        .eq("owner_user_id", user.id)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Datensatz nicht gefunden")

    update_fields: dict = {}
    if payload.name is not None:
        update_fields["name"] = payload.name
    if payload.description is not None:
        update_fields["description"] = payload.description
    if not update_fields:
        raise HTTPException(status_code=400, detail="Keine Aenderungen")

    result = (
        supabase.table("datasets")
        .update(update_fields)
        .eq("id", dataset_id)
        .eq("owner_user_id", user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Datensatz nicht gefunden")
    return _serialize(result.data[0], viewer_id=str(user.id))


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: str, user=Depends(get_current_user)) -> dict:
    """Delete the registry row only.

    The HF Hub repo itself is intentionally untouched — the student
    decides via their HF account whether to drop the actual dataset.
    """
    supabase = get_supabase()
    supabase.table("datasets").delete().eq("id", dataset_id).eq(
        "owner_user_id", user.id
    ).execute()
    return {"ok": True}
