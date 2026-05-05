"""Roboter Studio workflow CRUD.

Mirrors ``routes/training.py`` for the basic shape (Pydantic models,
service-role client, FastAPI dependency on ``get_current_user``) but is
much smaller — workflows live entirely in Postgres and are not dispatched
to a worker. Ownership is enforced by ``_assert_workflow_owned`` (Python
check + service-role write); RLS provides defence in depth on read paths
that bypass the API.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services.supabase_client import get_supabase
from app.validators.workflow import (
    MAX_NAME_LENGTH,
    validate_blockly_json,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])

# Pagination defaults match the rest of the API surface.
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500


# ---------- Models ----------


class WorkflowCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH)
    description: str = Field(default="", max_length=2000)
    blockly_json: dict
    classroom_id: str | None = None


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=2000)
    blockly_json: dict | None = None


class WorkflowResponse(BaseModel):
    id: str
    owner_user_id: str
    classroom_id: str | None
    name: str
    description: str
    blockly_json: dict
    is_template: bool
    created_at: str
    updated_at: str


# ---------- Helpers ----------


def _assert_workflow_owned(user_id: str, workflow_id: str) -> dict:
    """Return the workflow row if user_id owns it; 404 otherwise.

    Lives here rather than in auth.py because the helper is only used by
    this router (mirror of teacher.py:_assert_classroom_owned scoping).
    """
    supabase = get_supabase()
    result = (
        supabase.table("workflows")
        .select("*")
        .eq("id", workflow_id)
        .eq("owner_user_id", user_id)
        .execute()
    )
    if not result.data:
        # Use 404 not 403 to avoid existence leakage between users.
        raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
    return result.data[0]


def _get_user_classroom_id(user_id: str) -> str | None:
    supabase = get_supabase()
    result = supabase.table("users").select("classroom_id").eq("id", user_id).execute()
    if not result.data:
        return None
    return result.data[0].get("classroom_id")


# ---------- Endpoints ----------


@router.get("", response_model=list[WorkflowResponse])
def list_workflows(
    user=Depends(get_current_user),
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(0, ge=0),
) -> list[WorkflowResponse]:
    """List the caller's own workflows + their classroom's templates.

    Paginated since audit §2.4 — a classroom with thousands of saves
    would otherwise serialise the whole table on every Realtime
    refresh. The total result is capped at ``limit + MAX_LIST_LIMIT``
    rows: up to ``limit`` of the caller's own (paginated by the offset
    arg) plus up to ``MAX_LIST_LIMIT`` classroom templates concatenated
    after deduplication. Teachers with very many templates therefore
    still see the full template set without forcing students to re-
    paginate just to find a template the teacher pinned.
    """
    supabase = get_supabase()
    user_id = user.id
    classroom_id = _get_user_classroom_id(user_id)

    own = (
        supabase.table("workflows")
        .select("*")
        .eq("owner_user_id", user_id)
        .order("updated_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = list(own.data or [])

    # Templates are fetched in addition (not paginated together) so a
    # student who hasn't saved any of their own workflows still sees
    # the classroom set. A teacher with > MAX_LIST_LIMIT templates is
    # an unusual case; fall back to the same range.
    if classroom_id:
        templates = (
            supabase.table("workflows")
            .select("*")
            .eq("classroom_id", classroom_id)
            .eq("is_template", True)
            .order("updated_at", desc=True)
            .range(0, MAX_LIST_LIMIT - 1)
            .execute()
        )
        seen_ids = {r["id"] for r in rows}
        for r in templates.data or []:
            if r["id"] not in seen_ids:
                rows.append(r)

    return [WorkflowResponse(**r) for r in rows[: limit + MAX_LIST_LIMIT]]


@router.get("/{workflow_id}", response_model=WorkflowResponse)
def get_workflow(workflow_id: str, user=Depends(get_current_user)) -> WorkflowResponse:
    supabase = get_supabase()
    result = supabase.table("workflows").select("*").eq("id", workflow_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
    row = result.data[0]
    if row["owner_user_id"] == user.id:
        return WorkflowResponse(**row)
    if row.get("is_template"):
        classroom_id = _get_user_classroom_id(user.id)
        if classroom_id and row.get("classroom_id") == classroom_id:
            return WorkflowResponse(**row)
    raise HTTPException(status_code=404, detail="Workflow nicht gefunden")


@router.post("", response_model=WorkflowResponse)
def create_workflow(
    payload: WorkflowCreate,
    user=Depends(get_current_user),
) -> WorkflowResponse:
    validate_blockly_json(payload.blockly_json)
    supabase = get_supabase()
    insert_payload = {
        "owner_user_id": user.id,
        "classroom_id": payload.classroom_id or _get_user_classroom_id(user.id),
        "name": payload.name,
        "description": payload.description,
        "blockly_json": payload.blockly_json,
        "is_template": False,
    }
    result = supabase.table("workflows").insert(insert_payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Workflow konnte nicht gespeichert werden.")
    return WorkflowResponse(**result.data[0])


@router.patch("/{workflow_id}", response_model=WorkflowResponse)
def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdate,
    user=Depends(get_current_user),
) -> WorkflowResponse:
    _assert_workflow_owned(user.id, workflow_id)
    update_payload: dict[str, Any] = {}
    if payload.name is not None:
        update_payload["name"] = payload.name
    if payload.description is not None:
        update_payload["description"] = payload.description
    if payload.blockly_json is not None:
        validate_blockly_json(payload.blockly_json)
        update_payload["blockly_json"] = payload.blockly_json
    if not update_payload:
        raise HTTPException(status_code=400, detail="Keine Änderungen angegeben.")

    supabase = get_supabase()
    result = (
        supabase.table("workflows")
        .update(update_payload)
        .eq("id", workflow_id)
        .eq("owner_user_id", user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
    return WorkflowResponse(**result.data[0])


@router.delete("/{workflow_id}")
def delete_workflow(workflow_id: str, user=Depends(get_current_user)) -> dict:
    _assert_workflow_owned(user.id, workflow_id)
    supabase = get_supabase()
    supabase.table("workflows").delete().eq("id", workflow_id).eq("owner_user_id", user.id).execute()
    return {"ok": True}


@router.post("/{workflow_id}/clone", response_model=WorkflowResponse)
def clone_workflow(workflow_id: str, user=Depends(get_current_user)) -> WorkflowResponse:
    """Create a non-template copy of the workflow under the caller's
    ownership. Used by students when they pick a classroom template."""
    supabase = get_supabase()
    result = supabase.table("workflows").select("*").eq("id", workflow_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
    src = result.data[0]

    # Visibility: owner can clone own, classmate can clone classroom template.
    if src["owner_user_id"] != user.id:
        if not src.get("is_template"):
            raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
        classroom_id = _get_user_classroom_id(user.id)
        if not classroom_id or src.get("classroom_id") != classroom_id:
            raise HTTPException(status_code=404, detail="Workflow nicht gefunden")

    insert_payload = {
        "owner_user_id": user.id,
        "classroom_id": _get_user_classroom_id(user.id),
        "name": f"{src['name']} (Kopie)",
        "description": src.get("description", ""),
        "blockly_json": src["blockly_json"],
        "is_template": False,
    }
    inserted = supabase.table("workflows").insert(insert_payload).execute()
    if not inserted.data:
        raise HTTPException(status_code=500, detail="Klon konnte nicht erstellt werden.")
    return WorkflowResponse(**inserted.data[0])
