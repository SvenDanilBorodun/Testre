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

from app.auth import get_current_user, get_user_profile
from app.services.supabase_client import get_supabase
from app.validators.workflow import (
    MAX_NAME_LENGTH,
    validate_blockly_json,
    validate_code_files,
    validate_sim_scene,
    validate_trajectory,
    validate_trajectory_metadata,
    validate_trajectory_name,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])

# Pagination defaults match the rest of the API surface.
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500

# One workflow is one language (decision D3): a Blockly program or a Python /
# Java program, never both on one row. The two German refusals below are the
# only way a create or PATCH can mix them.
_ONE_LANGUAGE_DE = "Ein Programm besteht entweder aus Blöcken oder aus Code, nicht aus beidem."
_LANGUAGE_IMMUTABLE_DE = "Die Programmiersprache kann nach dem Anlegen nicht geändert werden."
_CODE_ZIELE_NEED_FILES_DE = (
    "Die Ziele und Positionen eines Code-Programms werden nur zusammen mit "
    "seinen Dateien gespeichert."
)

# Migration 041 (owner decisions O1/O2): a code program keeps its Ziele and
# Positionen where a Blockly program keeps them — the workspace serializer key
# `edubotics-destinations` inside blockly_json — and NOTHING else there. The
# DB CHECK workflows_code_blockly_json_destinations_only is the floor; this is
# the gate that answers in German first.
_CODE_BLOCKLY_KEYS = frozenset({"edubotics-destinations"})


def _code_blockly_json_ok(doc: Any) -> bool:
    """True when ``doc`` may be a code program's blockly_json: an object whose
    keys are all in ``_CODE_BLOCKLY_KEYS`` (``{}`` included — no Ziele)."""
    return isinstance(doc, dict) and set(doc) <= _CODE_BLOCKLY_KEYS


# ---------- Models ----------


class WorkflowCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH)
    description: str = Field(default="", max_length=2000)
    blockly_json: dict
    classroom_id: str | None = None
    # When the caller is in a workgroup, default to sharing the workflow
    # with the group. Set False to keep it private to the author.
    share_with_group: bool = True
    # Roboter Studio Phase-3 Sim-Szene (placed virtual objects). Optional —
    # a non-sim workflow omits it and the column defaults to {}.
    sim_scene: dict | None = None
    # Roboter Studio code programs (migration 040). '' = a Blockly program
    # (the column default); 'python' | 'java' = a code program whose
    # code_files carry the project and whose blockly_json carries at most its
    # Ziele / Positionen (`edubotics-destinations`, migration 041).
    code_language: str = ""
    code_files: dict | None = None


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=2000)
    blockly_json: dict | None = None
    # Re-share / un-share an already-saved workflow.
    share_with_group: bool | None = None
    # Phase-3 Sim-Szene. A sim-only PATCH carries just this; a combined
    # blockly+sim PATCH carries both (the sim_scene write is applied via the
    # plain owner-scoped update BEFORE the snapshot RPC — see update_workflow).
    sim_scene: dict | None = None
    # Code programs. The language is IMMUTABLE after create: a client may echo
    # the document's own language, any other value is a 409 — never silently
    # ignored. code_files goes through the SECURITY DEFINER RPC
    # update_workflow_code (see update_workflow); on a code program the same
    # PATCH may carry blockly_json holding ONLY the Ziele (migration 041), and
    # both reach that one RPC call.
    code_language: str | None = None
    code_files: dict | None = None


class WorkflowResponse(BaseModel):
    id: str
    owner_user_id: str
    classroom_id: str | None
    workgroup_id: str | None = None
    name: str
    description: str
    blockly_json: dict
    is_template: bool
    created_at: str
    updated_at: str
    # Phase-3 Sim-Szene. Declared so the read round-trip carries the column
    # back to the client (Pydantic drops undeclared fields). Defaults to None
    # for tolerance of a pre-migration row that lacks the column.
    sim_scene: dict | None = None
    # Migration 040. The defaults are the column defaults, so a row read before
    # the migration (or a client that never sends them) is a Blockly program.
    code_language: str = ""
    code_files: dict = Field(default_factory=dict)


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


def _resolve_visible_workgroup_ids(user_id: str) -> list[str]:
    """Every workgroup the caller is or was a member of (per audit table).

    Mirrors the helper in routes/training.py — duplicated here rather than
    cross-imported to keep router boundaries clean. Falls back to the
    user's currently-set workgroup_id when the audit table is empty.
    """
    supabase = get_supabase()
    rows = (
        supabase.table("workgroup_memberships")
        .select("workgroup_id")
        .eq("user_id", user_id)
        .execute()
    ).data or []
    if rows:
        return [r["workgroup_id"] for r in rows if r.get("workgroup_id")]
    profile = get_user_profile(user_id)
    wg = profile.get("workgroup_id")
    return [wg] if wg else []


def _assert_workflow_visible(user_id: str, workflow_id: str) -> dict:
    """Return the workflow row if user_id may READ it; 404 otherwise.

    Read-visibility ladder (mirrors ``get_workflow`` / ``list_workflow_versions``):
    owner OR group sibling (via the ``workgroup_memberships`` audit table) OR a
    classroom member reading a template. Used by the child-table read endpoints
    (versions, trajectories) which key on the parent workflow's visibility.
    Returns the parent row (its owner/group/template/classroom columns) so a
    caller that needs them avoids a second SELECT.
    """
    supabase = get_supabase()
    workflow_row = (
        supabase.table("workflows")
        .select("owner_user_id, workgroup_id, is_template, classroom_id")
        .eq("id", workflow_id)
        .execute()
    )
    if not workflow_row.data:
        raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
    row = workflow_row.data[0]
    if row.get("owner_user_id") == user_id:
        return row
    if (
        row.get("workgroup_id")
        and row["workgroup_id"] in _resolve_visible_workgroup_ids(user_id)
    ):
        return row
    if row.get("is_template"):
        classroom_id = _get_user_classroom_id(user_id)
        if classroom_id and row.get("classroom_id") == classroom_id:
            return row
    raise HTTPException(status_code=404, detail="Workflow nicht gefunden")


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
    group_ids = _resolve_visible_workgroup_ids(user_id)

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

    # Group-shared workflows from peers (covers former group members too
    # via workgroup_memberships).
    if group_ids:
        group_rows = (
            supabase.table("workflows")
            .select("*")
            .in_("workgroup_id", group_ids)
            .order("updated_at", desc=True)
            .range(0, MAX_LIST_LIMIT - 1)
            .execute()
        )
        seen_ids = {r["id"] for r in rows}
        for r in group_rows.data or []:
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
    # Group-shared visibility (covers former members via the audit table).
    if row.get("workgroup_id") and row["workgroup_id"] in _resolve_visible_workgroup_ids(user.id):
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
    if payload.sim_scene is not None:
        validate_sim_scene(payload.sim_scene)
    # A code program (migration 040): files AND a language, and no blocks —
    # its blockly_json may carry only its Ziele (migration 041). Files without
    # a language or a language without files are refused (the validator
    # refuses an empty project); a Blockly program sends neither and its
    # insert payload is unchanged from before 040 (column defaults).
    code_language = payload.code_language or ""
    if payload.code_files and not code_language:
        raise HTTPException(
            status_code=400,
            detail="Für Programmdateien muss eine Programmiersprache angegeben werden.",
        )
    if code_language and not _code_blockly_json_ok(payload.blockly_json):
        raise HTTPException(status_code=400, detail=_ONE_LANGUAGE_DE)
    code_files = (
        validate_code_files(payload.code_files, code_language) if code_language else None
    )
    supabase = get_supabase()
    profile = get_user_profile(str(user.id))
    workgroup_id = (
        profile.get("workgroup_id")
        if payload.share_with_group and profile.get("workgroup_id")
        else None
    )
    insert_payload = {
        "owner_user_id": user.id,
        "classroom_id": payload.classroom_id or _get_user_classroom_id(user.id),
        "workgroup_id": workgroup_id,
        "name": payload.name,
        "description": payload.description,
        "blockly_json": payload.blockly_json,
        "sim_scene": payload.sim_scene or {},
        "is_template": False,
    }
    if code_files is not None:
        insert_payload["code_language"] = code_language
        insert_payload["code_files"] = code_files
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
    row = _assert_workflow_owned(user.id, workflow_id)
    # One workflow is one language (migration 040). A pre-040 row has no
    # code_language key at all: that is a Blockly program.
    stored_language = row.get("code_language") or ""
    if (
        payload.blockly_json is not None
        and payload.code_files is not None
        and not stored_language
    ):
        raise HTTPException(status_code=400, detail=_ONE_LANGUAGE_DE)
    if payload.code_language is not None and payload.code_language != stored_language:
        raise HTTPException(status_code=409, detail=_LANGUAGE_IMMUTABLE_DE)
    if payload.code_files is not None and not stored_language:
        # A Blockly program never becomes a code program through PATCH.
        raise HTTPException(status_code=409, detail=_LANGUAGE_IMMUTABLE_DE)
    if payload.blockly_json is not None and stored_language:
        # Migration 041: a code program's blockly_json is its Ziele and
        # nothing else, and it rides the SAME save as the files (one call to
        # update_workflow_code = one version snapshot). The client always
        # sends both; Ziele alone would need a second writer.
        if not _code_blockly_json_ok(payload.blockly_json):
            raise HTTPException(status_code=400, detail=_ONE_LANGUAGE_DE)
        if payload.code_files is None:
            raise HTTPException(status_code=400, detail=_CODE_ZIELE_NEED_FILES_DE)
    update_payload: dict[str, Any] = {}
    if payload.name is not None:
        update_payload["name"] = payload.name
    if payload.description is not None:
        update_payload["description"] = payload.description
    if payload.blockly_json is not None:
        validate_blockly_json(payload.blockly_json)
        update_payload["blockly_json"] = payload.blockly_json
    if payload.code_files is not None:
        update_payload["code_files"] = validate_code_files(payload.code_files, stored_language)
    if payload.sim_scene is not None:
        validate_sim_scene(payload.sim_scene)
        update_payload["sim_scene"] = payload.sim_scene
    if payload.share_with_group is not None:
        # Re-share or un-share. Sharing requires the caller currently be in
        # a group; un-sharing always allowed.
        if payload.share_with_group:
            profile = get_user_profile(str(user.id))
            update_payload["workgroup_id"] = profile.get("workgroup_id")
        else:
            update_payload["workgroup_id"] = None
    if not update_payload:
        raise HTTPException(status_code=400, detail="Keine Änderungen angegeben.")

    supabase = get_supabase()
    # Migration 040/041: a code save. Tested BEFORE the blockly branch below,
    # because on a code program the same PATCH carries the Ziele as
    # blockly_json — through update_workflow_blockly they would write
    # blockly_json and never the code, in a second snapshot. Every other
    # column of this PATCH goes through the plain owner-scoped update first
    # (the RPC takes only the code columns and the Ziele), then the SECURITY
    # DEFINER RPC update_workflow_code (owner-only, stamps saved_by on the
    # version snapshot, code + Ziele in ONE UPDATE), then a re-SELECT so the
    # response is the merged row. p_blockly_json is always sent, None when
    # this PATCH carries no Ziele (the RPC then keeps the stored ones).
    if "code_files" in update_payload:
        plain = {
            k: v for k, v in update_payload.items()
            if k not in ("code_files", "blockly_json")
        }
        if plain:
            supabase.table("workflows").update(plain).eq("id", workflow_id).eq(
                "owner_user_id", user.id
            ).execute()
        try:
            supabase.rpc(
                "update_workflow_code",
                {
                    "p_workflow_id": workflow_id,
                    "p_user_id": str(user.id),
                    "p_code_files": update_payload["code_files"],
                    "p_code_language": stored_language,
                    "p_blockly_json": update_payload.get("blockly_json"),
                },
            ).execute()
        except Exception as exc:
            msg = str(exc)
            if "P0002" in msg or "nicht gefunden" in msg:
                raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
            raise
        final = (
            supabase.table("workflows")
            .select("*")
            .eq("id", workflow_id)
            .execute()
        )
        if not final.data:
            raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
        return WorkflowResponse(**final.data[0])

    # Audit A1: when blockly_json changes, route through the SECURITY
    # DEFINER RPC update_workflow_blockly so the BEFORE-UPDATE snapshot
    # trigger sees `current_setting('app.user_id', true)` = the caller's
    # UUID and writes workflow_versions.saved_by correctly. The plain
    # .table().update() path cannot SET LOCAL across PostgREST calls.
    # Non-blockly-only patches (name/description/share toggle) still go
    # through .table().update() because no snapshot fires for them.
    if "blockly_json" in update_payload:
        # Apply the share-with-group toggle FIRST (separate, no
        # snapshot fires for the workgroup_id column). Then call the
        # snapshot RPC for the blockly_json change. Finally re-SELECT
        # the row so the response reflects the merged state from both
        # writes — a previous version of this code returned the RPC's
        # return value and then patched workgroup_id into the dict
        # in-memory, which left stale `updated_at` on the response.
        if "workgroup_id" in update_payload:
            supabase.table("workflows").update(
                {"workgroup_id": update_payload["workgroup_id"]}
            ).eq("id", workflow_id).eq("owner_user_id", user.id).execute()
        # Apply the Sim-Szene via the same plain owner-scoped update (the
        # snapshot RPC writes only blockly_json/name/description; it does not
        # touch sim_scene). Owner-scoped .eq("owner_user_id", user.id) keeps
        # this IDOR-safe under the service-role client (Rule §4).
        if "sim_scene" in update_payload:
            supabase.table("workflows").update(
                {"sim_scene": update_payload["sim_scene"]}
            ).eq("id", workflow_id).eq("owner_user_id", user.id).execute()
        try:
            supabase.rpc(
                "update_workflow_blockly",
                {
                    "p_workflow_id": workflow_id,
                    "p_user_id": str(user.id),
                    "p_blockly_json": update_payload["blockly_json"],
                    "p_name": update_payload.get("name"),
                    "p_description": update_payload.get("description"),
                },
            ).execute()
        except Exception as exc:
            msg = str(exc)
            if "P0002" in msg or "nicht gefunden" in msg:
                raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
            raise
        final = (
            supabase.table("workflows")
            .select("*")
            .eq("id", workflow_id)
            .execute()
        )
        if not final.data:
            raise HTTPException(status_code=404, detail="Workflow nicht gefunden")
        return WorkflowResponse(**final.data[0])

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

    # Visibility: owner clone own, classmate clones classroom template,
    # group sibling clones a group-shared workflow.
    if src["owner_user_id"] != user.id:
        is_template_in_classroom = False
        if src.get("is_template"):
            classroom_id = _get_user_classroom_id(user.id)
            if classroom_id and src.get("classroom_id") == classroom_id:
                is_template_in_classroom = True
        is_group_shared = (
            src.get("workgroup_id")
            and src["workgroup_id"] in _resolve_visible_workgroup_ids(user.id)
        )
        if not is_template_in_classroom and not is_group_shared:
            raise HTTPException(status_code=404, detail="Workflow nicht gefunden")

    profile = get_user_profile(str(user.id))
    insert_payload = {
        "owner_user_id": user.id,
        "classroom_id": _get_user_classroom_id(user.id),
        # New copies belong to the cloner's *current* group (not the source's).
        "workgroup_id": profile.get("workgroup_id"),
        "name": f"{src['name']} (Kopie)",
        "description": src.get("description", ""),
        "blockly_json": src["blockly_json"],
        # Carry the Sim-Szene into the clone (already validated when the
        # source was saved). Defaults to {} for a pre-Phase-3 source row.
        "sim_scene": src.get("sim_scene") or {},
        # Carry the code document too (migration 040): a clone of a Python
        # program is a Python program. A pre-040 source row lacks both keys.
        "code_language": src.get("code_language") or "",
        "code_files": src.get("code_files") or {},
        "is_template": False,
    }
    inserted = supabase.table("workflows").insert(insert_payload).execute()
    if not inserted.data:
        raise HTTPException(status_code=500, detail="Klon konnte nicht erstellt werden.")
    new_workflow = inserted.data[0]

    # Carry the source's recorded replay trajectories into the clone so the
    # cloned „spiele Bewegung ab <name>" blocks still resolve. Copied under the
    # CLONER's ownership + the NEW workflow_id (never the source's ids — Rule §4).
    # Bounded to the per-workflow prune cap (16). Best-effort: a clone with no
    # trajectories is still a valid workflow.
    src_trajectories = (
        supabase.table("workflow_trajectories")
        .select("name, samples, point_count, duration_s, fps, robot_profile")
        .eq("workflow_id", workflow_id)
        .order("created_at", desc=True)
        .limit(16)
        .execute()
    ).data or []
    if src_trajectories:
        copy_rows = [
            {
                "workflow_id": new_workflow["id"],
                "owner_user_id": user.id,
                "name": t.get("name"),
                "samples": t.get("samples"),
                "point_count": t.get("point_count"),
                "duration_s": t.get("duration_s"),
                "fps": t.get("fps"),
                "robot_profile": t.get("robot_profile"),
            }
            for t in src_trajectories
        ]
        supabase.table("workflow_trajectories").insert(copy_rows).execute()

    return WorkflowResponse(**new_workflow)


# ---------- Phase-2: server-side version history ----------


class WorkflowVersion(BaseModel):
    id: str
    workflow_id: str
    blockly_json: dict
    note: str = ""
    created_at: str


@router.get("/{workflow_id}/versions", response_model=list[WorkflowVersion])
def list_workflow_versions(
    workflow_id: str,
    user=Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[WorkflowVersion]:
    """List up to ``limit`` snapshots of this workflow's blockly_json,
    newest first. The trigger in migration 015 caps history at 20 per
    workflow; the limit query parameter only narrows that further.

    Visibility mirrors ``get_workflow``: owner OR group sibling
    (via ``workgroup_memberships`` audit table) OR classroom member
    reading a template's history. Previously this endpoint was
    owner-only, which broke the Verlauf history dropdown for group
    siblings collaborating on a shared workflow.
    """
    _assert_workflow_visible(user.id, workflow_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflow_versions")
        .select("id, workflow_id, blockly_json, note, created_at")
        .eq("workflow_id", workflow_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [WorkflowVersion(**row) for row in (result.data or [])]


@router.post("/{workflow_id}/versions/{version_id}/restore", response_model=WorkflowResponse)
def restore_workflow_version(
    workflow_id: str,
    version_id: str,
    user=Depends(get_current_user),
) -> WorkflowResponse:
    """Replace the workflow's current blockly_json with the snapshot
    pointed to by ``version_id``. The current state is automatically
    snapshotted by the BEFORE-UPDATE trigger so this restore is itself
    reversible (until the 20-cap rotates the older snapshot out).
    """
    _assert_workflow_owned(user.id, workflow_id)
    supabase = get_supabase()
    # Re-run the size/depth validator against the snapshot in case an
    # older save predates today's caps (the block-type allowlist no
    # longer exists; size and depth are the only invariants).
    version_row = (
        supabase.table("workflow_versions")
        .select("blockly_json")
        .eq("id", version_id)
        .eq("workflow_id", workflow_id)
        .execute()
    )
    if not version_row.data:
        raise HTTPException(status_code=404, detail="Version nicht gefunden")
    snapshot = version_row.data[0].get("blockly_json")
    if not isinstance(snapshot, dict):
        raise HTTPException(status_code=500, detail="Snapshot ist beschädigt.")
    validate_blockly_json(snapshot)
    # Audit A1: restore via the SECURITY DEFINER RPC so the
    # BEFORE-UPDATE trigger's snapshot of the pre-restore state ALSO
    # records saved_by = the user who clicked restore.
    try:
        rpc = supabase.rpc(
            "restore_workflow_version",
            {
                "p_workflow_id": workflow_id,
                "p_version_id": version_id,
                "p_user_id": str(user.id),
            },
        ).execute()
    except Exception as exc:
        msg = str(exc)
        if "P0002" in msg or "nicht gefunden" in msg:
            raise HTTPException(status_code=404, detail="Version nicht gefunden")
        raise
    if not rpc.data:
        raise HTTPException(status_code=500, detail="Wiederherstellen fehlgeschlagen.")
    row = rpc.data if isinstance(rpc.data, dict) else rpc.data[0]
    return WorkflowResponse(**row)


# ---------- Batch-2b: recorded replay trajectories ----------
#
# A student torque-offs the follower, hand-guides a motion, and the ROS backend
# streams the recorded CONTRACT-B samples ({"fps", "points"}) up here. A
# „spiele Bewegung ab <name>" block later fetches them back via the by-name
# endpoint and replays them. Storage mirrors the versions child-table (migration
# 034): FK to workflows ON DELETE CASCADE, owner on the row, per-workflow prune
# cap 16. Writes assert workflow ownership + set owner/workflow SERVER-SIDE
# (Rule §4 — service-role bypasses RLS); reads use the workflow read-visibility
# ladder (_assert_workflow_visible).
#
# Six routes: create (POST), list (GET), by-name (GET), single (GET), rename
# (PATCH) and delete (DELETE). The rename is owner-only and renames EVERY row
# sharing the target's current name — the re-recorded versions of a name are
# one recording to the student.


class TrajectoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH)
    fps: float = Field(..., gt=0)
    points: list
    # Optional denormalised metadata; the server falls back to len(points) for
    # point_count when the client omits it.
    point_count: int | None = None
    duration_s: float | None = None
    # ARM-FAMILY tag (migration 035, widened by 039):
    # 'omx_f' | 'edu6_studio' | 'edu1_studio'. Selects the per-point width the
    # validator enforces (7 | 8 | 7) and rides the row so a replay client can
    # refuse a cross-profile recording with a clean German message instead of a
    # bare width error. None = untagged (pre-edu6 client) = the 7-wide OMX
    # default. NOTE 'edu1_studio' and 'omx_f' SHARE a width, so from 039 on this
    # tag is the ONLY thing separating those two recordings — the width check
    # cannot, and was never meant to.
    robot_profile: str | None = None


class TrajectoryResponse(BaseModel):
    id: str
    workflow_id: str
    owner_user_id: str
    name: str
    point_count: int | None = None
    duration_s: float | None = None
    fps: float | None = None
    robot_profile: str | None = None
    created_at: str
    updated_at: str
    # Full CONTRACT-B samples ({"fps", "points"}). Present on the single-get
    # response, omitted (None) on the list response to keep listings light.
    samples: dict | None = None


class TrajectorySamples(BaseModel):
    """The run-payload sibling: exactly what „spiele Bewegung ab" needs to
    replay — CONTRACT B ({"fps", "points"}) + the arm-family tag so the client
    can refuse a cross-profile replay cleanly (None = untagged legacy row)."""

    fps: float
    points: list
    robot_profile: str | None = None


@router.post("/{workflow_id}/trajectories", response_model=TrajectoryResponse)
def create_trajectory(
    workflow_id: str,
    payload: TrajectoryCreate,
    user=Depends(get_current_user),
) -> TrajectoryResponse:
    """Persist one recorded hand-guided trajectory under this workflow.

    Owner-only (``_assert_workflow_owned``); owner_user_id + workflow_id are set
    server-side, never from the body (Rule §4)."""
    _assert_workflow_owned(user.id, workflow_id)
    # Mirror the ROS backend + Blockly frontend name rules (1–40 chars, charset).
    # The cloud must not store a name no block can reference (dead storage) or one
    # whose `/` breaks the by-name path route. Returns the trimmed name to store.
    name = validate_trajectory_name(payload.name)
    validate_trajectory(payload.points, payload.fps,
                        robot_profile=getattr(payload, 'robot_profile', None))
    # Guard the denormalised metadata too: point_count / duration_s are stored
    # verbatim from the body, so an out-of-int4-range count (500 on INSERT) or a
    # non-finite/negative duration (stored as Infinity) must be rejected here.
    validate_trajectory_metadata(payload.point_count, payload.duration_s)
    supabase = get_supabase()
    point_count = (
        payload.point_count if payload.point_count is not None else len(payload.points)
    )
    insert_payload = {
        "workflow_id": workflow_id,
        "owner_user_id": user.id,
        "name": name,
        "samples": {"fps": payload.fps, "points": payload.points},
        "point_count": point_count,
        "duration_s": payload.duration_s,
        "fps": payload.fps,
        "robot_profile": (getattr(payload, 'robot_profile', None) or '').strip() or None,
    }
    result = supabase.table("workflow_trajectories").insert(insert_payload).execute()
    if not result.data:
        raise HTTPException(
            status_code=500, detail="Bewegung konnte nicht gespeichert werden."
        )
    return TrajectoryResponse(**result.data[0])


@router.get("/{workflow_id}/trajectories", response_model=list[TrajectoryResponse])
def list_trajectories(
    workflow_id: str,
    user=Depends(get_current_user),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[TrajectoryResponse]:
    """List this workflow's recorded trajectories, newest first (metadata only —
    the samples blob is fetched on demand via the by-name / single-get routes).
    Visibility mirrors the versions ladder (owner / group / classroom template).
    """
    _assert_workflow_visible(user.id, workflow_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflow_trajectories")
        .select(
            "id, workflow_id, owner_user_id, name, point_count, duration_s, fps, "
            "robot_profile, created_at, updated_at"
        )
        .eq("workflow_id", workflow_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [TrajectoryResponse(**r) for r in (result.data or [])]


@router.get(
    "/{workflow_id}/trajectories/by-name/{name}", response_model=TrajectorySamples
)
def get_trajectory_by_name(
    workflow_id: str,
    name: str,
    user=Depends(get_current_user),
) -> TrajectorySamples:
    """Fetch the newest trajectory named ``name`` under this workflow as the
    run-payload shape ({"fps", "points"}). Used by the „spiele Bewegung ab"
    block. Newest-wins when a name was re-recorded (no uniqueness constraint).

    Declared BEFORE the ``/{trajectory_id}`` route so the two-segment
    ``by-name/<name>`` path can never be shadowed by the single-segment id route.
    """
    _assert_workflow_visible(user.id, workflow_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflow_trajectories")
        .select("fps, samples, robot_profile")
        .eq("workflow_id", workflow_id)
        .eq("name", name)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Bewegung nicht gefunden")
    row = result.data[0]
    samples = row.get("samples") if isinstance(row.get("samples"), dict) else {}
    points = samples.get("points", [])
    fps = row.get("fps")
    if fps is None:
        fps = samples.get("fps")
    if fps is None:
        # Defense-in-depth: a legacy/edge row with fps NULL in both the column and
        # the samples blob would otherwise 500 on TrajectorySamples(fps=None). Fall
        # back to the manual-record sampler default (25 Hz) so replay still works.
        fps = 25.0
    return TrajectorySamples(fps=fps, points=points,
                             robot_profile=row.get("robot_profile"))


@router.get(
    "/{workflow_id}/trajectories/{trajectory_id}", response_model=TrajectoryResponse
)
def get_trajectory(
    workflow_id: str,
    trajectory_id: str,
    user=Depends(get_current_user),
) -> TrajectoryResponse:
    """Fetch a single trajectory (including its full CONTRACT-B samples) by id."""
    _assert_workflow_visible(user.id, workflow_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflow_trajectories")
        .select("*")
        .eq("workflow_id", workflow_id)
        .eq("id", trajectory_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Bewegung nicht gefunden")
    return TrajectoryResponse(**result.data[0])


class TrajectoryRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH)


@router.patch(
    "/{workflow_id}/trajectories/{trajectory_id}", response_model=TrajectoryResponse
)
def rename_trajectory(
    workflow_id: str,
    trajectory_id: str,
    payload: TrajectoryRename,
    user=Depends(get_current_user),
) -> TrajectoryResponse:
    """Rename a recording. EVERY row sharing the target row's current name is
    renamed in one owner-scoped UPDATE — the versions of a name are one
    recording to the student, and a per-row PATCH would 409 the second version
    against the first. Owner-only (Rule §4): _assert_workflow_owned + the write
    re-scoped by owner_user_id. created_at is untouched, so the prune order and
    by-name newest-wins do not change; updated_at moves via
    trg_workflow_trajectories_touch.

    KNOWN, accepted: the clash check and the UPDATE are two statements, not one
    transaction, and no unique (workflow_id, name) constraint can back them
    (the versions of a name share it by design). Two concurrent renames onto one
    free name by the same owner could both pass the check; closing that needs a
    row-locking RPC."""
    _assert_workflow_owned(user.id, workflow_id)
    new_name = validate_trajectory_name(payload.name)
    supabase = get_supabase()
    target = (
        # Metadata only: the samples (up to 256 KB) are never returned here.
        supabase.table("workflow_trajectories").select(
            "id, workflow_id, owner_user_id, name, point_count, duration_s, fps, "
            "robot_profile, created_at, updated_at"
        )
        .eq("workflow_id", workflow_id).eq("id", trajectory_id)
        .eq("owner_user_id", user.id).execute()
    )
    if not target.data:
        raise HTTPException(status_code=404, detail="Bewegung nicht gefunden")
    row = target.data[0]
    old_name = row["name"]
    if new_name == old_name:
        return TrajectoryResponse(**{**row, "samples": None})
    # The clash check spans the whole workflow (not only the caller's rows): two
    # recordings with one name would silently merge under by-name newest-wins.
    clash = (
        supabase.table("workflow_trajectories").select("id")
        .eq("workflow_id", workflow_id).eq("name", new_name).limit(1).execute()
    )
    if clash.data:
        raise HTTPException(
            status_code=409,
            detail=f'Eine Bewegung mit dem Namen „{new_name}" gibt es schon — bitte einen anderen Namen wählen.',
        )
    updated = (
        supabase.table("workflow_trajectories").update({"name": new_name})
        .eq("workflow_id", workflow_id).eq("owner_user_id", user.id)
        .eq("name", old_name).execute()
    )
    renamed = next((r for r in (updated.data or []) if r.get("id") == trajectory_id), None)
    if renamed is None:
        raise HTTPException(status_code=404, detail="Bewegung nicht gefunden")
    return TrajectoryResponse(**{**renamed, "samples": None})


@router.delete("/{workflow_id}/trajectories/{trajectory_id}")
def delete_trajectory(
    workflow_id: str,
    trajectory_id: str,
    user=Depends(get_current_user),
) -> dict:
    """Delete one recorded trajectory. Owner-only (``_assert_workflow_owned``);
    the delete is additionally owner-scoped as belt-and-suspenders (Rule §4)."""
    _assert_workflow_owned(user.id, workflow_id)
    supabase = get_supabase()
    (
        supabase.table("workflow_trajectories")
        .delete()
        .eq("workflow_id", workflow_id)
        .eq("id", trajectory_id)
        .eq("owner_user_id", user.id)
        .execute()
    )
    return {"ok": True}


# ---------- „Abgeben": immutable submissions (migration 040) ----------
#
# A student deliberately hands the teacher a snapshot of the current document.
# The row copies name / language / code_files / blockly_json / sim_scene from
# the WORKFLOW ROW (never from the body) and stamps student_user_id +
# classroom_id SERVER-SIDE — classroom_id from users.classroom_id, never from
# workflows.classroom_id, which a client sets at create with no membership
# check (Rule §4). Postgres keeps the newest 8 per (student, workflow) and
# refuses every UPDATE. Owner-only both ways. POST .../submit sits under the
# existing per-user POST /workflows 10/min rule by prefix and under the 384 KB
# body middleware; the teacher's reads live in routes/teacher.py.

SUBMISSION_NOTE_MAX_LENGTH = 500


class SubmissionCreate(BaseModel):
    note: str = ""


class SubmissionResponse(BaseModel):
    id: str
    workflow_id: str
    student_user_id: str
    name: str
    code_language: str = ""
    note: str = ""
    submitted_at: str
    # The snapshot itself: present on the submit response, omitted (None) on
    # the list so a listing stays light.
    code_files: dict | None = None
    blockly_json: dict | None = None
    sim_scene: dict | None = None


@router.post("/{workflow_id}/submit", response_model=SubmissionResponse)
def submit_workflow(
    workflow_id: str,
    payload: SubmissionCreate | None = None,
    user=Depends(get_current_user),
) -> SubmissionResponse:
    """Snapshot the current document as an „Abgabe" for the teacher.

    Owner-only (``_assert_workflow_owned``). Everything but the note comes
    from the row and the caller's own users row — a body carrying ids is
    ignored (the model has no such fields)."""
    row = _assert_workflow_owned(user.id, workflow_id)
    note = (payload.note if payload is not None else "") or ""
    if len(note) > SUBMISSION_NOTE_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Die Notiz darf höchstens {SUBMISSION_NOTE_MAX_LENGTH} Zeichen lang sein.",
        )
    supabase = get_supabase()
    insert_payload = {
        "workflow_id": workflow_id,
        "student_user_id": user.id,
        "classroom_id": _get_user_classroom_id(user.id),
        "name": row["name"],
        "code_language": row.get("code_language") or "",
        "code_files": row.get("code_files") or {},
        "blockly_json": row.get("blockly_json") or {},
        "sim_scene": row.get("sim_scene") or {},
        "note": note,
    }
    result = supabase.table("workflow_submissions").insert(insert_payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Abgabe konnte nicht gespeichert werden.")
    return SubmissionResponse(**result.data[0])


@router.get("/{workflow_id}/submissions", response_model=list[SubmissionResponse])
def list_submissions(
    workflow_id: str,
    user=Depends(get_current_user),
) -> list[SubmissionResponse]:
    """The caller's own submissions of this workflow, newest first, without
    the documents. No limit parameter: Postgres keeps at most 8 rows per
    (student, workflow), so the listing is bounded by construction."""
    _assert_workflow_owned(user.id, workflow_id)
    supabase = get_supabase()
    result = (
        supabase.table("workflow_submissions")
        .select("id, workflow_id, student_user_id, name, code_language, note, submitted_at")
        .eq("workflow_id", workflow_id)
        .eq("student_user_id", user.id)
        .order("submitted_at", desc=True)
        .execute()
    )
    return [
        SubmissionResponse(**{**r, "code_files": None, "blockly_json": None, "sim_scene": None})
        for r in (result.data or [])
    ]
