import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import get_current_profile, get_user_profile
from app.services import deletion_requests, hf_credentials
from app.services.hf_identity import is_denied_author, normalize_hf_username
from app.services.modal_client import cancel_training_job
from app.services.supabase_client import get_supabase
from app.services.workgroups import resolve_visible_workgroup_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])

# GDPR export (E16): every trainings column EXCEPT worker_token (a per-row
# credential; 047 hides it from the request roles, and the service-role
# export must not hand it out either). Migration 049's seven columns included,
# and 052's (§8.6) except idempotency_key (a client nonce, not data about anyone).
# No column of the row holds another person's id (stop_requested_by was dropped).
_EXPORT_TRAINING_COLUMNS = (
    "id, user_id, status, dataset_name, model_name, model_type, training_params, cloud_job_id, "
    "current_step, total_steps, current_loss, requested_at, terminated_at, error_message, "
    "last_progress_at, loss_history, workgroup_id, cancel_attempted_at, cancel_attempts, log_url, "
    "managed, current_attempt, train_seconds_used, status_note, step_advanced_at, dataset_fps, unplaced_since, "
    "capped_step, continuable_until, continued_from, "
    "credit_scheme, reserved_hours, charged_hours, settled_at, stop_requested_at, stop_requested_by_role, "
    "lost_seconds, lost_reasons, charge_waiver"
)
_PROVIDER_OF_BACKEND = {
    "runpod_community": ("runpod", "community"),
    "runpod_secure": ("runpod", "secure"),
    "modal": ("modal", None),
}


def _export_attempts(supabase, training_ids: list) -> dict:
    """Where each attempt of a managed training ran (E16): provider, cloud, data
    centre, GPU type, start and end — never the cloud_ref (pod / call id) or a
    token. An attempt that was never placed (no backend) ran nowhere."""
    if not training_ids:
        return {}
    rows = (
        supabase.table("training_attempts")
        .select("training_id, attempt_no, backend, data_center, allowed_dcs, gpu_type, started_at, ended_at")
        .in_("training_id", training_ids)
        .order("attempt_no", desc=False)
        .execute()
    ).data or []
    out: dict = {}
    for r in rows:
        provider = _PROVIDER_OF_BACKEND.get(r.get("backend"))
        if provider is None:
            continue
        out.setdefault(r["training_id"], []).append({
            "provider": provider[0],
            "cloud": provider[1],
            "data_center": r.get("data_center"),
            # the data centres the create asked for ([] = any region; owner E42/m8)
            "requested_data_centers": r.get("allowed_dcs") or [],
            "gpu_type": r.get("gpu_type"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
        })
    return out


class MyProfile(BaseModel):
    id: str
    role: str
    username: str | None
    full_name: str | None
    # Migration 030: the student's linked HuggingFace username (Benutzer-ID).
    # The anchor for dataset discovery. Since migration 042 it is set from the
    # stored token's whoami (PROVEN) when the student saves a token; PATCH /me
    # stays as the self-asserted path for an account without a stored token.
    hf_username: str | None = None
    classroom_id: str | None
    workgroup_id: str | None = None
    workgroup_name: str | None = None
    # 052: a student's is the quota's legacy training_credits (startable free +
    # reserved + spent); 0 for everybody else. Never the dead users column.
    training_credits: int
    # Only populated for teachers. None for students/admin. Read from
    # credit_teacher_summary with the pre-052 meaning (pool_available = free …).
    pool_total: int | None = None
    allocated_total: int | None = None
    pool_available: int | None = None
    student_count: int | None = None
    group_count: int | None = None
    group_credits_total: int | None = None
    # 052 (C's gap 1): the admin's compensations into this teacher's accounts, ever.
    compensated: int | None = None
    # GDPR Art. 17 (launch plan step 5): set while the caller's deletion request is
    # open (the student SPA shows it and offers the withdrawal); the deadline is
    # Art. 12(3)'s one month (services/deletion_requests.py).
    deletion_requested_at: str | None = None
    deletion_due_by: str | None = None


def _build_my_profile(profile: dict) -> MyProfile:
    """Assemble the MyProfile response from a public.users row.

    Shared by GET /me and PATCH /me so both return the identical shape.
    """
    data = {
        "id": profile["id"],
        "role": profile["role"],
        "username": profile.get("username"),
        "full_name": profile.get("full_name"),
        "hf_username": profile.get("hf_username"),
        "classroom_id": profile.get("classroom_id"),
        "workgroup_id": profile.get("workgroup_id"),
        "training_credits": 0,
        "deletion_requested_at": profile.get("deletion_requested_at"),
        "deletion_due_by": deletion_requests.due_by(profile.get("deletion_requested_at")),
    }
    supabase = get_supabase()

    # Look up the group name for any user that's in one (students
    # primarily; teachers query groups via the dedicated routes).
    if profile.get("workgroup_id"):
        try:
            g = (
                supabase.table("workgroups")
                .select("name")
                .eq("id", profile["workgroup_id"])
                .single()
                .execute()
            )
            if g.data:
                data["workgroup_name"] = g.data.get("name")
        except Exception as exc:
            logger.warning(
                "workgroup lookup failed for user=%s group=%s: %s",
                profile["id"],
                profile["workgroup_id"],
                exc,
            )

    if profile["role"] == "student":
        from app.routes.training import credit_quota_row, quota_view

        q = credit_quota_row(profile["id"], sb=supabase)
        if q is not None:
            data["training_credits"] = quota_view(q)["training_credits"]
    elif profile["role"] == "teacher":
        from app.routes.teacher import pool_fields, teacher_summary

        fields = pool_fields(teacher_summary(profile["id"], sb=supabase))
        data.update({k: fields[k] for k in ("pool_total", "allocated_total", "pool_available", "student_count",
                                             "group_count", "group_credits_total", "compensated")})

    return MyProfile(**data)


@router.get("", response_model=MyProfile)
def read_me(profile=Depends(get_current_profile)):
    return _build_my_profile(profile)


class HfUsernameUpdate(BaseModel):
    hf_username: str = Field(..., min_length=1, max_length=96)


def _proven_name_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            "Dein Hugging-Face-Konto ist über dein gespeichertes Token "
            "festgelegt. Entferne zuerst das Token auf der Startseite, wenn "
            "du ein anderes Konto verwenden möchtest."
        ),
    )


@router.patch("", response_model=MyProfile)
def update_me(
    body: HfUsernameUpdate, profile=Depends(get_current_profile)
):
    """Link (or update) the caller's HuggingFace username (Benutzer-ID).

    Self-only: the row is keyed to the JWT-verified profile id, never to a
    body field (Rule §4). Since migration 042 the React app no longer calls
    this automatically: the account name comes from the stored token's
    whoami (routes/hf_token.py). This route remains for old clients and for an
    account WITHOUT a stored token, and answers 409 once one exists (the name
    is proven then and must not drift from the token the robot uploads with).
    The `has_credential` check below is the friendly early answer; the
    guarantee is the database's (migration 044's users trigger raises P0044
    for any hf_username change that differs from a stored credential's name),
    so a token saved between the check and the UPDATE still ends in the 409.
    The value is the SOLE anchor the dataset_sweep + POST /datasets/sync
    use to discover this student's datasets, so it is format-validated and
    deny-listed against upstream/system namespaces.
    """
    cleaned = normalize_hf_username(body.hf_username)
    if cleaned is None:
        raise HTTPException(
            status_code=400, detail="Ungültige HuggingFace-Benutzer-ID."
        )
    if is_denied_author(cleaned):
        raise HTTPException(
            status_code=400,
            detail=(
                "Diese Benutzer-ID ist nicht zulässig (System- oder "
                "Beispielkonto)."
            ),
        )
    supabase = get_supabase()
    # Migration 042: once a stored token has PROVEN the account name, the
    # self-asserted PATCH must not overwrite it (the name and the token the
    # robot uploads with would drift apart). The student removes the token
    # first. `has_credential` reads only through `.select().eq().execute()`.
    try:
        has_token = hf_credentials.has_credential(supabase, profile["id"])
    except Exception as exc:
        logger.error(
            "hf credential lookup failed in PATCH /me for user=%s: %s",
            profile["id"], type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="Benutzer-ID konnte nicht gespeichert werden.",
        ) from None
    if has_token:
        raise _proven_name_conflict()
    try:
        result = (
            supabase.table("users")
            .update({"hf_username": cleaned})
            .eq("id", profile["id"])
            .execute()
        )
        if not result.data:
            raise RuntimeError("update returned no rows")
    except Exception as exc:
        if "P0044" in str(exc):
            # A token was stored between the check above and this UPDATE.
            raise _proven_name_conflict() from None
        logger.error(
            "hf_username update failed for user=%s: %s", profile["id"], exc
        )
        raise HTTPException(
            status_code=500,
            detail="Benutzer-ID konnte nicht gespeichert werden.",
        ) from exc
    # Re-fetch so the response reflects the persisted value alongside the rest
    # of the profile (group name, credits, quota).
    fresh = get_user_profile(str(profile["id"]))
    return _build_my_profile(fresh)


@router.get("/export")
def export_my_data(profile=Depends(get_current_profile)):
    """GDPR/DSGVO Art. 15 — download everything we have on this user.

    Every table that holds a row about the caller, each read with an explicit
    column list (never ``select("*")``: a later column is exported only once
    somebody decided it is about the caller and not a credential) and scoped to
    the caller's OWN rows by the JWT-verified id (Rule §4; the route takes no id):

      * ``profile`` — every column of the caller's ``public.users`` row;
      * ``auth_account`` — what Supabase Auth keeps (e-mail, creation, last
        sign-in, confirmation, MFA factor kinds), best effort with a German
        ``auth_account_note`` when it cannot be read;
      * ``credit_account`` + ``credit_ledger`` + ``training_start_refusals``
        (052) — the caller's own hour account and its history, no other
        person's id;
      * ``trainings`` (every column but ``worker_token``) with ``attempts``;
      * ``workgroup_memberships`` (current and past, migration 011);
      * ``workflows``, ``workflow_versions`` (``saved_by_me`` instead of a
        classmate's id), ``workflow_trajectories`` (034, the recorded
        „Bewegungen"), ``workflow_submissions`` (040, „Abgeben");
      * ``datasets`` (the registry; the recordings themselves live on Hugging
        Face, where the student owns them);
      * ``tutorial_progress``;
      * ``inference_sessions`` (048, Cloud-Inferenz — never the connect token's
        hash, the connection id, the Modal session id or the sealed edge token)
        and ``inference_runs`` (the teacher's log of them);
      * ``hf_credential`` (042) — the stored token's account name, kind and
        timestamps, never the ciphertext, its fingerprint or its hint;
      * the progress notes that apply to the caller (a teacher: the notes in
        their classrooms; a student: the notes about them, about their
        classroom and about every group they are or were in).

    Not here: checkpoint manifests and attempt internals (technical data about a
    model, not about a person), the Supabase Auth audit log (IP addresses; not
    reachable through the API — the operator answers such a request by hand),
    and the recordings on the robot's own disk (the Daten tab shows them).
    """
    supabase = get_supabase()
    uid = profile["id"]
    role = profile.get("role")
    own_row = (
        supabase.table("users").select(_EXPORT_PROFILE_COLUMNS).eq("id", uid).execute()
    ).data or []
    bundle: dict = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": own_row[0] if own_row else {k: profile.get(k) for k in _EXPORT_PROFILE_KEYS},
    }
    bundle["auth_account"], note = _export_auth_account(supabase, uid)
    if note:
        bundle["auth_account_note"] = note
    # 052 (§8.6, §13): the caller's view of the hour ledger and the refused starts.
    bundle["credit_account"] = _export_credit_account(supabase, uid, role)
    bundle["credit_ledger"] = _export_ledger(supabase, uid, role)
    bundle["training_start_refusals"] = (
        supabase.table("training_start_refusals").select("at, request_kind, code, model_type")
        .eq("user_id", uid).order("id", desc=True).execute()
    ).data or []

    trainings = (
        supabase.table("trainings").select(_EXPORT_TRAINING_COLUMNS).eq("user_id", uid).execute()
    )
    bundle["trainings"] = trainings.data or []
    managed_ids = [t["id"] for t in bundle["trainings"] if t.get("managed")]
    attempts = _export_attempts(supabase, managed_ids)
    for t in bundle["trainings"]:
        t["attempts"] = attempts.get(t["id"], []) if t.get("managed") else []

    # Workgroup memberships audit — current + past, per GDPR Art. 15.
    bundle["workgroup_memberships"] = (
        supabase.table("workgroup_memberships").select("workgroup_id, user_id, joined_at, left_at")
        .eq("user_id", uid).execute()
    ).data or []

    # Workflows the user authored. Stored in our DB even when the
    # Blockly JSON references upstream blocks; the JSON itself is the
    # personal-data part (a student's classroom assignment solution).
    bundle["workflows"] = (
        supabase.table("workflows").select(_EXPORT_WORKFLOW_COLUMNS).eq("owner_user_id", uid).execute()
    ).data or []

    # Dataset registry rows the user owns. We do NOT pull HF Hub
    # contents — the user has direct access via their HF account.
    bundle["datasets"] = (
        supabase.table("datasets").select(_EXPORT_DATASET_COLUMNS).eq("owner_user_id", uid).execute()
    ).data or []

    # Roboter Studio Phase-2/3 personal data added in migrations 015/016.
    # Both tables are personal data under GDPR Art. 15 the moment a
    # student authors a workflow or completes a tutorial step. Audit
    # round-3 §L — without these the export is incomplete.
    workflow_ids = [w.get("id") for w in bundle["workflows"] if w.get("id")]
    bundle["workflow_versions"] = []
    if workflow_ids:
        # A group sibling may have saved a version of the caller's workflow:
        # whose id that was is not the caller's data, so only "was it me".
        for v in (
            supabase.table("workflow_versions").select(_EXPORT_VERSION_COLUMNS)
            .in_("workflow_id", workflow_ids).execute()
        ).data or []:
            saved_by = v.pop("saved_by", None)
            v["saved_by_me"] = saved_by is not None and str(saved_by) == str(uid)
            bundle["workflow_versions"].append(v)

    bundle["tutorial_progress"] = (
        supabase.table("tutorial_progress").select("tutorial_id, current_step, completed_at, updated_at")
        .eq("user_id", uid).execute()
    ).data or []

    # Launch plan step 5 (F-56): the tables the export used to miss.
    bundle["workflow_trajectories"] = (
        supabase.table("workflow_trajectories").select(_EXPORT_TRAJECTORY_COLUMNS)
        .eq("owner_user_id", uid).execute()
    ).data or []
    bundle["workflow_submissions"] = (
        supabase.table("workflow_submissions").select(_EXPORT_SUBMISSION_COLUMNS)
        .eq("student_user_id", uid).execute()
    ).data or []
    bundle["inference_sessions"] = (
        supabase.table("inference_sessions").select(_EXPORT_INFERENCE_SESSION_COLUMNS)
        .eq("user_id", uid).execute()
    ).data or []
    bundle["inference_runs"] = (
        supabase.table("inference_runs").select(_EXPORT_INFERENCE_RUN_COLUMNS)
        .eq("student_user_id", uid).execute()
    ).data or []
    credential = (
        supabase.table("user_hf_credentials").select(_EXPORT_HF_CREDENTIAL_COLUMNS)
        .eq("user_id", uid).execute()
    ).data or []
    bundle["hf_credential"] = credential[0] if credential else None

    if role == "teacher":
        bundle["classrooms"] = (
            supabase.table("classrooms").select("id, name, created_at").eq("teacher_id", uid).execute()
        ).data or []
        bundle["progress_entries"] = (
            supabase.table("progress_entries").select(_EXPORT_PROGRESS_COLUMNS)
            .in_("classroom_id", [c["id"] for c in bundle["classrooms"]] or [""]).execute()
        ).data or []
    elif role == "student":
        # GDPR Art. 15: a student has the right to receive every piece of
        # personal data we hold about them — including the per-student
        # notes their teacher writes (student_id == uid) AND the
        # class-wide notes that apply to them by virtue of belonging to
        # the classroom (student_id IS NULL, classroom_id = theirs).
        bundle["progress_entries"] = (
            supabase.table("progress_entries").select(_EXPORT_PROGRESS_COLUMNS)
            .eq("student_id", uid).execute()
        ).data or []

        if profile.get("classroom_id"):
            bundle["classroom_progress_entries"] = (
                supabase.table("progress_entries").select(_EXPORT_PROGRESS_COLUMNS)
                .eq("classroom_id", profile["classroom_id"])
                .is_("student_id", "null")
                .is_("workgroup_id", "null")
                .execute()
            ).data or []

        # Group-scoped entries for any group the student is or was in.
        visible_groups = resolve_visible_workgroup_ids(uid)
        if visible_groups:
            bundle["workgroup_progress_entries"] = (
                supabase.table("progress_entries").select(_EXPORT_PROGRESS_COLUMNS)
                .in_("workgroup_id", visible_groups).execute()
            ).data or []

    filename = f"edubotics-export-{uid}.json"
    return JSONResponse(
        content=bundle,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# The export's column lists (launch plan step 5). Each names every column of its
# table EXCEPT a credential or another person's id; app/tests/test_me_export.py
# compares each with the table's columns from the migrations.
_EXPORT_PROFILE_KEYS = (
    "id", "email", "role", "username", "full_name", "classroom_id", "workgroup_id", "created_by",
    "created_at", "deletion_requested_at", "hf_username", "training_credits",
    "vision_quota_per_term", "vision_used_per_term",
)
_EXPORT_PROFILE_COLUMNS = ", ".join(_EXPORT_PROFILE_KEYS)
_EXPORT_WORKFLOW_COLUMNS = (
    "id, owner_user_id, classroom_id, workgroup_id, name, description, is_template, blockly_json, "
    "code_language, code_files, sim_scene, created_at, updated_at"
)
_EXPORT_VERSION_COLUMNS = "id, workflow_id, note, blockly_json, code_language, code_files, created_at, saved_by"
_EXPORT_DATASET_COLUMNS = (
    "id, owner_user_id, workgroup_id, hf_repo_id, name, description, episode_count, total_frames, fps, "
    "robot_type, created_at, updated_at, discovered_via_sweep"
)
_EXPORT_TRAJECTORY_COLUMNS = (
    "id, workflow_id, owner_user_id, name, robot_profile, samples, point_count, duration_s, fps, "
    "created_at, updated_at"
)
_EXPORT_SUBMISSION_COLUMNS = (
    "id, workflow_id, student_user_id, classroom_id, name, code_language, code_files, blockly_json, "
    "sim_scene, note, submitted_at"
)
# Never token_hash, conn_id, modal_session_id or edge_token_enc (048's credentials).
_EXPORT_INFERENCE_SESSION_COLUMNS = (
    "id, user_id, classroom_id, workgroup_id, training_id, model_repo, model_revision, policy_type, "
    "status, session_s, created_at, reserve_until, edge_attached_at, connected_at, client_heartbeat_at, "
    "last_server_heartbeat_at, max_end_at, ended_at, end_reason, active_seconds, stats, "
    "edge_terminated_at, run_logged_at"
)
_EXPORT_INFERENCE_RUN_COLUMNS = (
    "id, student_user_id, classroom_id, policy_repo, backend, started_at, duration_s, exit_reason, "
    "error_message_de, created_at"
)
# Never token_ciphertext, token_fp or token_hint (042: a credential and pieces of it).
_EXPORT_HF_CREDENTIAL_COLUMNS = "hf_username, token_role, validated_at, created_at, updated_at"
_EXPORT_PROGRESS_COLUMNS = "id, classroom_id, student_id, workgroup_id, entry_date, note, created_at, updated_at"
# Never parent_teacher_id (the funding teacher's id is not the caller's data).
_EXPORT_CREDIT_ACCOUNT_COLUMNS = "kind, free, reserved, spent, member_hour_limit, closed_at, created_at"
AUTH_ACCOUNT_UNREADABLE_DE = (
    "Die Anmeldedaten (Supabase Auth) konnten gerade nicht gelesen werden. Bitte den Export "
    "später erneut herunterladen."
)
_AUTH_ACCOUNT_FIELDS = ("email", "created_at", "updated_at", "last_sign_in_at", "email_confirmed_at",
                        "confirmed_at")


def _plain(value):
    """A JSON-safe copy of an auth field (datetimes as ISO text)."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def _export_auth_account(supabase, uid: str) -> tuple[dict | None, str | None]:
    """What Supabase Auth keeps about the caller (``auth.admin.get_user_by_id``,
    service role): the fields above plus each MFA factor's kind, name, status and
    creation — never a factor id or secret. Best effort: on any failure the
    export still answers, with a German note instead of the block."""
    try:
        user = getattr(supabase.auth.admin.get_user_by_id(uid), "user", None)
    except Exception as exc:  # noqa: BLE001 - the export must not fail on Auth
        logger.warning("export: auth account read failed for user=%s: %s", uid, type(exc).__name__)
        return None, AUTH_ACCOUNT_UNREADABLE_DE
    if user is None or str(getattr(user, "id", uid)) != str(uid):
        return None, AUTH_ACCOUNT_UNREADABLE_DE
    out = {f: _plain(getattr(user, f, None)) for f in _AUTH_ACCOUNT_FIELDS}
    out["mfa_factors"] = [
        {
            "factor_type": _plain(getattr(f, "factor_type", None)),
            "friendly_name": _plain(getattr(f, "friendly_name", None)),
            "status": _plain(getattr(f, "status", None)),
            "created_at": _plain(getattr(f, "created_at", None)),
        }
        for f in (getattr(user, "factors", None) or [])
    ]
    return out, None


def _export_credit_account(supabase, uid: str, role) -> dict | None:
    """The caller's own hour account (052): free, reserved, spent, its closing time;
    a grouped student's hours live in the group's account, which is not theirs."""
    kind = {"student": "student", "teacher": "teacher"}.get(role)
    if kind is None:
        return None
    rows = (
        supabase.table("credit_accounts").select(_EXPORT_CREDIT_ACCOUNT_COLUMNS)
        .eq("kind", kind).eq("owner_id", uid).execute()
    ).data or []
    return rows[0] if rows else None


def _export_ledger(supabase, uid: str, role) -> list:
    """The caller's ledger rows (scope ``student`` / ``teacher``) as ``{at, kind, hours,
    account_kind, training_id, actor_role}`` — no ids of other people, no member or actor
    ids. ``reason`` (the current text, when not erased) only on a teacher's own grant /
    revoke rows and on a compensation into the caller's own account (the admin wrote it
    about this person)."""
    scope = {"student": "student", "teacher": "teacher"}.get(role)
    if scope is None:
        return []
    from app.routes.teacher import _account_side

    out, before = [], None
    for _ in range(50):  # 200 a page; an export takes them all
        page = supabase.rpc("credit_history", {"p_scope": scope, "p_id": uid, "p_before": before,
                                               "p_limit": 200}).execute().data or []
        for r in page:
            kind = r.get("kind")
            account_kind, account_id = _account_side(r)
            entry = {"at": r.get("at"), "kind": kind, "hours": r.get("hours"), "account_kind": account_kind,
                     "training_id": r.get("training_id"), "actor_role": r.get("actor_role")}
            own = str(account_id) == str(uid)
            if r.get("reason") is not None and (
                    (scope == "teacher" and kind in ("grant", "revoke") and own)
                    or (kind == "compensate" and str(r.get("to_id")) == str(uid))):
                entry["reason"] = r.get("reason")
            out.append(entry)
        if len(page) < 200:
            break
        before = page[-1].get("id")
    return out


async def _cancel_managed_for_deletion(supabase, training_id: int, uid: str) -> bool:
    from app.services import training_dispatch

    try:
        data = (await asyncio.to_thread(
            supabase.rpc(
                "cancel_request_training", {"p_training_id": training_id, "p_user_id": uid,
                                            "p_actor_role": "student"}
            ).execute
        )).data
    except Exception as exc:
        code = training_dispatch.pg_code(exc)
        if code in ("P0050", "P0051", "P0002"):
            logger.info("/me/delete: managed training %s left as it is (%s)", training_id, code)
            return False
        logger.warning("/me/delete: cancel_request_training failed for %s: %s", training_id, type(exc).__name__)
        return False
    info = (data or [{}])[0] if isinstance(data, list) else (data or {})
    try:
        await training_dispatch.stop_for_cancel(
            info.get("o_backend"), info.get("o_attempt_state"), info.get("o_cloud_ref")
        )
    except Exception as exc:  # best effort: the reaper ends a pod no active row owns
        logger.warning("/me/delete: stopping training %s failed: %s", training_id, type(exc).__name__)
    try:
        await asyncio.to_thread(
            supabase.rpc(
                "complete_cancel_training",
                {"p_training_id": training_id, "p_final": "canceled", "p_message": None},
            ).execute
        )
    except Exception as exc:
        logger.warning("/me/delete: complete_cancel_training failed for %s: %s", training_id, type(exc).__name__)
        return False
    return True


@router.post("/delete")
async def delete_my_account(profile=Depends(get_current_profile)):
    """GDPR/DSGVO Art. 17 — account deletion request.

    Students and teachers can call this; admins can't self-delete (would
    lock out the platform). This records a REQUEST; nothing is deleted here.
    The teacher of the student's classroom lists the open requests and
    confirms one (``routes/teacher.py::confirm_deletion_request``), which runs
    the full deletion; the admin sees every open request (a teacher's own
    included) and may confirm too (``routes/admin.py``). The requester may
    withdraw an open request (``POST /me/delete/withdraw``).

    Side effects:
      1. Cancels any running/queued training the user owns so credits and
         GPU stop burning while the request sits in the admin queue.
      2. Records deletion_requested_at on the users row (the FIRST request's
         time: asking again keeps it, so the Art. 12(3) deadline does not
         move). If that write fails the endpoint returns 500 — silently
         returning success used to mean nobody would ever see the request.
      3. Deletes the caller's stored Hugging Face token (user_hf_credentials,
         migration 042), best effort: a failure is logged by class and never
         blocks the request.
    """
    if profile["role"] == "admin":
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "Admin-Konten können sich nicht selbst löschen. Bitte "
                    "einen anderen Admin darum bitten oder den "
                    "Plattform-Betreiber kontaktieren."
                )
            },
        )

    supabase = get_supabase()
    uid = profile["id"]

    # 1. Cancel any in-flight trainings. Best-effort: a Modal cancel
    #    failure shouldn't block the user's deletion request, but the row
    #    is still marked canceled locally so credits free up.
    # Migration 023: include 'cancel_requested' so a row stuck mid-cancel
    # at account-deletion time is also force-closed. Deletion is the user's
    # decision — we don't retry Modal here, we just close the row out.
    active = await asyncio.to_thread(
        supabase.table("trainings")
        .select("id, cloud_job_id, managed")
        .eq("user_id", uid)
        .in_("status", ["queued", "running", "cancel_requested"])
        .execute
    )
    cancelled_ids: list = []
    for row in active.data or []:
        if row.get("managed"):
            # FR-19: a managed row goes through the same compare-and-set as
            # Cancel; a row whose finished model is being published (P0051) is
            # left to finish, a row no longer active (P0050) needs nothing.
            if await _cancel_managed_for_deletion(supabase, row["id"], uid):
                cancelled_ids.append(row["id"])
            continue
        if row.get("cloud_job_id"):
            try:
                await cancel_training_job(row["cloud_job_id"])
            except Exception as exc:
                logger.warning(
                    "Modal cancel failed in /me/delete for training %s: %s",
                    row["id"], exc,
                )
        try:
            await asyncio.to_thread(
                supabase.table("trainings").update(
                    {
                        "status": "canceled",
                        "terminated_at": datetime.now(timezone.utc).isoformat(),
                        "error_message": "Automatisch abgebrochen: Kontolöschung angefordert.",
                    }
                ).eq("id", row["id"]).execute
            )
            cancelled_ids.append(row["id"])
        except Exception as exc:
            logger.warning(
                "Could not mark training %s canceled in /me/delete: %s",
                row["id"], exc,
            )

    # 2. Disengage from any current workgroup so the slot frees during
    #    the up-to-30-day admin processing window — otherwise a phantom
    #    member would block the teacher from filling the slot. The audit
    #    row is left in place with left_at = NOW() so siblings keep
    #    historical visibility on this user's group-shared content (the
    #    RLS policies query workgroup_memberships, not users.workgroup_id).
    if profile.get("workgroup_id"):
        wg_id = profile["workgroup_id"]
        try:
            await asyncio.to_thread(
                supabase.table("users").update({"workgroup_id": None}).eq(
                    "id", uid
                ).execute
            )
            await asyncio.to_thread(
                supabase.table("workgroup_memberships").update(
                    {"left_at": datetime.now(timezone.utc).isoformat()}
                ).eq("workgroup_id", wg_id).eq("user_id", uid).is_(
                    "left_at", "null"
                ).execute
            )
            logger.info(
                "Workgroup disengagement on /me/delete: user=%s group=%s",
                uid, wg_id,
            )
        except Exception as exc:
            logger.warning(
                "Workgroup disengagement failed in /me/delete for %s: %s",
                uid, exc,
            )

    # 2b. Migration 042: take the student's own Hugging Face token out of the
    #     cloud. Best-effort like the steps above: a failure here must never
    #     block the deletion request (the FK ON DELETE CASCADE removes the row
    #     for good when the account itself is erased), so only the exception
    #     CLASS is logged. `removed` feeds the message below so it names only
    #     what really took effect.
    hf_token_removed = False
    try:
        removed_rows = await asyncio.to_thread(
            supabase.table("user_hf_credentials").delete().eq("user_id", uid).execute
        )
        hf_token_removed = bool(getattr(removed_rows, "data", None))
    except Exception as exc:
        logger.warning(
            "Stored HF token removal failed in /me/delete for %s: %s",
            uid, type(exc).__name__,
        )

    # 3. Record the deletion request. Migration 007 guarantees the
    #    column exists; a failure here is a real DB / network error and
    #    must be surfaced — silently returning success used to mean an
    #    admin would never see the request.
    #    The FIRST request time is kept (launch plan step 5): Art. 12(3)'s
    #    one-month deadline runs from when the request arrived, so asking
    #    again must not move it. Hence the conditional write, then a re-read
    #    when it matched nothing (an earlier request is already open).
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        result = await asyncio.to_thread(
            supabase.table("users")
            .update({"deletion_requested_at": now_iso})
            .eq("id", uid)
            .is_("deletion_requested_at", "null")
            .execute
        )
        written = result.data or []
        if written:
            requested_at = written[0].get("deletion_requested_at") or now_iso
        else:
            rows = (await asyncio.to_thread(
                supabase.table("users").select("deletion_requested_at").eq("id", uid).execute
            )).data or []
            requested_at = rows[0].get("deletion_requested_at") if rows else None
        if not requested_at:
            raise RuntimeError("no request time recorded")
    except Exception as exc:
        logger.error("delete_my_account write failed for %s: %s", uid, exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    "Löschanfrage konnte nicht gespeichert werden — bitte "
                    "erneut versuchen oder den Administrator informieren."
                )
            },
        )

    logger.info(
        "Deletion request from user=%s role=%s (canceled %d active trainings)",
        uid, profile["role"], len(cancelled_ids),
    )
    # Named only when it really happened (see step 2b): the same honesty rule
    # as the rest of this message.
    token_note = (
        " Außerdem wurde dein gespeichertes Hugging-Face-Token vom Server entfernt."
        if hf_token_removed
        else ""
    )
    due_by = deletion_requests.due_by(requested_at)
    due_date = deletion_requests.parse_timestamp(due_by)
    due_note = (
        f" Nach der DSGVO muss das spätestens bis zum {due_date:%d.%m.%Y} geschehen."
        if due_date is not None
        else ""
    )
    return {
        "status": "requested",
        "canceled_trainings": cancelled_ids,
        "hf_token_removed": hf_token_removed,
        # The FIRST request's time and its Art. 12(3) deadline (launch plan step 5).
        "requested_at": requested_at,
        "due_by": due_by,
        # German (Rule §1 — this is read by a student) and HONEST about what
        # just happened. The previous English text said an administrator "will
        # process it within 30 days", which overstated the mechanism twice
        # over: nothing here deletes anything, and until 2026-11-11 nothing
        # drained this queue. Since launch plan step 5 the teacher of the
        # student's classroom lists the open requests and confirms one
        # (routes/teacher.py::confirm_deletion_request, the admin as the
        # backstop), which runs the full `DELETE /teacher/students/{id}`
        # deletion — and even that cannot reach repos in the student's own
        # HuggingFace namespace (the platform token does not own them; see
        # teacher.py::_delete_student_hf_artifacts). Telling a minor their data
        # is scheduled for erasure when it is not is the worst version of this
        # defect, so the wording describes a REQUEST, names who carries it out
        # and names the actions that actually took effect.
        "deletion_performed": False,
        "message": (
            "Die Löschanfrage wurde gespeichert. Wichtig: Es wurden noch "
            "KEINE Daten gelöscht — die Anfrage muss von einer Lehrkraft oder "
            "einem Administrator bestätigt und ausgeführt werden."
            f"{due_note} Sofort wirksam "
            f"sind: {len(cancelled_ids)} laufende Training(s) wurden "
            "abgebrochen und die Arbeitsgruppen-Zuordnung wurde aufgehoben."
            f"{token_note} "
            "Aufnahmen im eigenen HuggingFace-Konto müssen dort selbst "
            "gelöscht werden. Solange die Anfrage offen ist, kannst du sie "
            "zurückziehen."
        ),
    }


@router.post("/delete/withdraw")
def withdraw_deletion_request(profile=Depends(get_current_profile)):
    """Withdraw the caller's own OPEN deletion request (launch plan step 5).

    Self-only: the row is the JWT-verified caller's, never a body field
    (Rule §4). It clears ``users.deletion_requested_at`` through the service
    role (055 refuses that write to the request roles, so it goes through here
    and nowhere else) and changes nothing else: what took effect when the request
    was filed (canceled trainings, the workgroup left, the stored token removed)
    stays as it is, and the answer says so. No open request is an idempotent
    ``withdrawn: false``. A request a teacher already carried out has no account
    left to call this with (401)."""
    if profile["role"] == "admin":
        raise HTTPException(status_code=400, detail=deletion_requests.ADMIN_CANNOT_REQUEST_DE)
    uid = profile["id"]
    try:
        result = (
            get_supabase()
            .table("users")
            .update({"deletion_requested_at": None})
            .eq("id", uid)
            .not_.is_("deletion_requested_at", "null")
            .execute()
        )
    except Exception as exc:
        logger.error("deletion-request withdrawal failed for user=%s: %s", uid, type(exc).__name__)
        raise HTTPException(status_code=500, detail=deletion_requests.WITHDRAW_FAILED_DE) from None
    withdrawn = bool(result.data)
    if withdrawn:
        logger.info("deletion request withdrawn by user=%s role=%s", uid, profile["role"])
    return {
        "withdrawn": withdrawn,
        "message": deletion_requests.WITHDRAWN_DE if withdrawn else deletion_requests.NOTHING_TO_WITHDRAW_DE,
    }


# ---------- Phase-3: skillmap tutorial progress ----------


class TutorialProgress(BaseModel):
    tutorial_id: str
    current_step: int = 0
    completed_at: str | None = None
    updated_at: str | None = None


class TutorialProgressUpdate(BaseModel):
    current_step: int | None = None
    completed: bool | None = None


# Conservative ID validator. Tutorial IDs in the bundled set are short
# lowercase strings with underscores; reject anything that could let a
# crafted ID corrupt a JSON path or SQL value.
_TUTORIAL_ID_MAX = 64


def _valid_tutorial_id(tid: str) -> bool:
    if not isinstance(tid, str):
        return False
    if not tid or len(tid) > _TUTORIAL_ID_MAX:
        return False
    return all(c.isalnum() or c in "._-" for c in tid)


@router.get("/tutorial-progress", response_model=list[TutorialProgress])
def list_tutorial_progress(
    profile=Depends(get_current_profile),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Return tutorial progress rows for the calling user.

    Paginated: a student with hundreds of distinct tutorial rows
    (long-tail bug or replay churn) would otherwise serialise the
    whole table on every page load. supabase-py uses an inclusive
    0-indexed range, so the slice for (offset, limit) is
    [offset, offset+limit-1].
    """
    sb = get_supabase()
    rows = (
        sb.table("tutorial_progress")
        .select("tutorial_id, current_step, completed_at, updated_at")
        .eq("user_id", profile["id"])
        .range(offset, offset + limit - 1)
        .execute()
    )
    return [TutorialProgress(**r) for r in (rows.data or [])]


@router.patch("/tutorial-progress/{tutorial_id}", response_model=TutorialProgress)
def update_tutorial_progress_endpoint(
    tutorial_id: str,
    body: TutorialProgressUpdate,
    profile=Depends(get_current_profile),
):
    """Upsert progress for a single tutorial. The completed flag, when
    true, sets ``completed_at`` to NOW(); explicit ``false`` is a NO-OP
    (server-side override) so a buggy frontend that sends
    ``{completed: false}`` on every keystroke can't accidentally reset
    a completed tutorial's timestamp. To intentionally clear, an
    admin/teacher must DELETE the row.
    """
    if not _valid_tutorial_id(tutorial_id):
        raise HTTPException(status_code=400, detail="Tutorial-ID ist ungültig.")
    sb = get_supabase()
    payload: dict = {
        "user_id": profile["id"],
        "tutorial_id": tutorial_id,
    }
    if body.current_step is not None:
        if body.current_step < 0:
            raise HTTPException(status_code=400, detail="Schritt-Nummer muss >= 0 sein.")
        payload["current_step"] = int(body.current_step)
    # Server-side override: only `completed=True` sets the timestamp.
    # `completed=False` is treated as a no-op rather than clearing
    # `completed_at` (prevents accidental regression from a buggy UI).
    if body.completed is True:
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
    try:
        result = (
            sb.table("tutorial_progress")
            .upsert(payload, on_conflict="user_id,tutorial_id")
            .execute()
        )
    except Exception as exc:
        logger.exception("tutorial-progress upsert failed for user=%s id=%s", profile["id"], tutorial_id)
        raise HTTPException(status_code=500, detail="Fortschritt konnte nicht gespeichert werden.") from exc
    if not result.data:
        raise HTTPException(status_code=500, detail="Fortschritt konnte nicht gespeichert werden.")
    row = result.data[0]
    return TutorialProgress(
        tutorial_id=row.get("tutorial_id", tutorial_id),
        current_step=row.get("current_step", 0) or 0,
        completed_at=row.get("completed_at"),
        updated_at=row.get("updated_at"),
    )


# ---------------------------------------------------------------------------
# GET /me/group-members — whose datasets the Daten tab lists (spec §E6)
# ---------------------------------------------------------------------------

GROUP_MEMBERS_FAILED_DE = "Gruppenmitglieder konnten nicht geladen werden."


class GroupMember(BaseModel):
    # The display name and the Hugging Face account name ONLY: no login
    # username and no user id (a classmate's login is not the page's business).
    full_name: str | None = None
    hf_username: str | None = None
    is_me: bool = False


class GroupMembers(BaseModel):
    workgroup_id: str | None = None
    members: list[GroupMember] = []


def _group_member(row: dict, me_id: str) -> dict:
    return {
        "full_name": row.get("full_name"),
        "hf_username": row.get("hf_username"),
        "is_me": row.get("id") == me_id,
    }


@router.get("/group-members", response_model=GroupMembers)
def read_my_group_members(profile=Depends(get_current_profile)):
    """The members of the CALLER's current workgroup (any role).

    Rule §4: the workgroup comes from the caller's own profile (resolved from
    the JWT), never from the request, so there is no id to tamper with; the
    service-role query is scoped by it. A caller in no workgroup (every
    teacher, a student working alone) gets themself only. UI scoping for the
    Daten tab's library, not a security boundary.
    """
    me_id = profile["id"]
    self_row = _group_member(profile, me_id)
    workgroup_id = profile.get("workgroup_id")
    if not workgroup_id:
        return {"workgroup_id": None, "members": [self_row]}
    try:
        result = (
            get_supabase()
            .table("users")
            .select("id, full_name, hf_username")
            .eq("workgroup_id", workgroup_id)
            .order("full_name")
            .execute()
        )
    except Exception as exc:
        logger.warning(
            "group-members lookup failed for user=%s group=%s: %s",
            me_id, workgroup_id, type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail=GROUP_MEMBERS_FAILED_DE) from exc
    rows = [r for r in (result.data or []) if isinstance(r, dict)]
    members = [_group_member(r, me_id) for r in rows]
    if not any(m["is_me"] for m in members):
        # The caller's own row always belongs to the answer (a row the query
        # did not return — a membership change in flight — is still „me").
        members.insert(0, self_row)
    return {"workgroup_id": workgroup_id, "members": members}
