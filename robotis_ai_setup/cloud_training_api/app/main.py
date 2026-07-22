import logging
import os
import pathlib
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.routes.admin import router as admin_router
from app.routes.datasets import router as datasets_router
from app.routes.health import router as health_router
from app.routes.jetson import router as jetson_router
from app.routes.jetson import teacher_router as jetson_teacher_router
from app.routes.me import router as me_router
from app.routes.policies import router as policies_router
from app.routes.teacher import router as teacher_router
from app.routes.training import router as training_router
from app.routes.version import router as version_router
from app.routes.workflows import router as workflows_router
from app.routes.workgroups import router as workgroups_router
from app.services.dataset_sweep import sweep_loop as _dataset_sweep_loop
from app.services.jetson_sweep import sweep_loop as _jetson_sweep_loop
from app.services.rate_limiter import RateLimiter
from app.services.training_sweep import sweep_loop as _training_cancel_sweep_loop

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="EduBotics Cloud Training API")


def _validate_required_secrets() -> None:
    """Fail-fast at startup if any required env var is missing or empty.

    Without this check, the FastAPI app boots fine and `/health` returns
    200, so Railway marks the deploy as healthy. The first authenticated
    student request then hits a KeyError inside get_supabase() (or the
    Modal SDK rejects our token) → bare 500 with no hint that the deploy
    is missing a secret. This raises at import time so Railway aborts the
    deploy instead — exactly what already happens for ALLOWED_ORIGINS.

    SUPABASE_SERVICE_ROLE_KEY is the privileged key used server-side to
    bypass RLS for admin/teacher operations; SUPABASE_URL is the project
    URL; MODAL_TOKEN_ID + MODAL_TOKEN_SECRET let the Modal SDK dispatch
    training jobs (Modal picks them up automatically via os.environ).
    """
    required = (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
    )
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Cloud API cannot start — required Railway service variables "
            f"missing or empty: {', '.join(missing)}."
        )


_validate_required_secrets()


def _warn_optional_secrets() -> None:
    """Log WARNING at boot for env vars that aren't strictly required but
    silently disable specific routes when missing. Migration 019 added
    EDUBOTICS_JETSON_HF_TOKEN — without it /jetson/register returns 503
    and no Jetson can be set up. Operators deploying a classroom-Jetson
    feature flag should see a clear log line during Railway boot rather
    than discover the misconfiguration when a teacher tries to pair a
    Jetson.
    """
    optional_warns = (
        (
            "EDUBOTICS_JETSON_HF_TOKEN",
            "/jetson/register will return 503 — classrooms cannot pair Jetsons. "
            "Set the read-only HF org token on Railway to enable.",
        ),
        (
            "HF_TOKEN",
            "dataset reconciliation sweep + GDPR Art. 17 cleanup disabled.",
        ),
        # GUI_VERSION / GUI_DOWNLOAD_URL feed /version which the student
        # installer polls on every launch. When missing, the update gate
        # silently disables — existing installs never learn about new
        # releases. These are normally set automatically by release.yml's
        # W6 publish-gui-version job after the installer .exe is attached
        # to the GH Release; a warning here means that job didn't run (or a
        # manual deploy bypassed it).
        (
            "GUI_VERSION",
            "/version returns version=null — student installs cannot detect updates. "
            "Normally set by release.yml W6; matches the in-tree VERSION file.",
        ),
        # GUI_DOWNLOAD_URL is OPTIONAL now: /version derives the GH Release
        # asset URL from GUI_VERSION + GUI_RELEASE_REPO when it's unset. We
        # warn on GUI_RELEASE_REPO instead — without it (and without an
        # explicit GUI_DOWNLOAD_URL) the derive yields null.
        (
            "GUI_RELEASE_REPO",
            "/version may return download_url=null — set to 'owner/repo' (or set "
            "GUI_DOWNLOAD_URL explicitly) so the update gate can locate the .exe.",
        ),
    )
    for key, msg in optional_warns:
        if not os.environ.get(key):
            logger.warning("env var %s not set — %s", key, msg)

    # Drift guard: GUI_VERSION (Railway env) vs VERSION file (in-tree).
    # Rule §6 calls out the 4-place agreement (VERSION file, installer
    # .iss, gui constants, Railway env). The first three are caught by
    # `git grep` at PR time; the Railway env can only be checked at
    # boot. A mismatch means existing student installs poll /version,
    # see a stale version string, and either never update or update to
    # an installer URL that 404s. Log at WARNING — don't refuse boot;
    # a maintainer rolling out a hotfix may legitimately update Railway
    # before pushing the file change.
    gui_version_env = os.environ.get("GUI_VERSION")
    if gui_version_env:
        here = pathlib.Path(__file__).resolve()
        # Build candidates LAZILY — in production the file resolves to
        # /app/app/main.py whose .parents has only 3 entries (parents[3]
        # would raise IndexError, crashing module import BEFORE uvicorn
        # binds, which kills every deploy with "1/1 replicas never became
        # healthy"). Use a helper that returns None for missing indices
        # and filter them out before joining the VERSION segment.
        def _safe_parent(p: pathlib.Path, n: int) -> pathlib.Path | None:
            try:
                return p.parents[n]
            except IndexError:
                return None

        version_candidates: list[pathlib.Path] = [
            pathlib.Path("/app/VERSION"),  # image root (production layout)
        ]
        for n in (4, 3, 2):  # 4=repo root w/ extra nesting, 3=repo root, 2=cloud_training_api
            parent = _safe_parent(here, n)
            if parent is not None:
                version_candidates.append(parent / "VERSION")
        in_tree_version: str | None = None
        for path in version_candidates:
            try:
                if path.is_file():
                    in_tree_version = path.read_text(encoding="utf-8").strip()
                    break
            except OSError:
                continue
        if in_tree_version and in_tree_version != gui_version_env:
            logger.warning(
                "version drift: GUI_VERSION=%r on Railway but in-tree VERSION=%r. "
                "Update the Railway env var (and gh release asset) to match.",
                gui_version_env,
                in_tree_version,
            )

    # Conditional: when SUPABASE_JWT_ALGORITHM=HS256 (legacy Supabase
    # auth, pre-2024), the Jetson rosbridge proxy needs the shared HMAC
    # secret to verify student JWTs. Without it, every WebSocket
    # connection closes 4401 and no inference works.
    # Modern Supabase projects (post-2024) default to ES256 with a
    # JWKS endpoint, which needs NO shared secret — only SUPABASE_URL
    # for the JWKS lookup. Default left as ES256 to match Supabase's
    # current default; legacy projects need to set the env var.
    if os.environ.get("SUPABASE_JWT_ALGORITHM") == "HS256" and not os.environ.get(
        "SUPABASE_JWT_SECRET"
    ):
        logger.warning(
            "env var SUPABASE_JWT_SECRET not set but SUPABASE_JWT_ALGORITHM=HS256 — "
            "/jetson/register will return 503 because Jetson agents cannot verify "
            "student JWTs without the shared HMAC secret. "
            "Set SUPABASE_JWT_SECRET on Railway (Supabase Dashboard → Settings → API → JWT Secret) "
            "OR switch to ES256/RS256 (the default for modern Supabase projects)."
        )


_warn_optional_secrets()


def _sanitize_probe_error(target: str, exc: Exception) -> RuntimeError:
    """Wrap a schema-probe failure without leaking the service-role key.

    Why this exists: supabase-py builds on httpx/postgrest-py. When a
    request fails, those libraries can chain the original Request /
    Response objects into the exception (`exc.__cause__`,
    `exc.response.request.headers`, or str(exc) embedding the request).
    The `Authorization: Bearer <SUPABASE_SERVICE_ROLE_KEY>` header we
    set in services/supabase_client.py would then surface in the
    Railway/Actions stderr log when boot fails — a service-role key in
    public CI logs is an immediate-rotation incident.

    Two protections:
      1. We construct a NEW RuntimeError WITHOUT `raise ... from exc`,
         so the original exception chain (which may hold the request
         object and its headers) is dropped before Python prints the
         traceback.
      2. We log the original str(exc) at ERROR level with a regex scrub
         that replaces any `Bearer <token>` substring — so a maintainer
         debugging a real PGRST/network error still gets the cause,
         minus the secret.
    """
    exc_class = type(exc).__name__
    scrubbed = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer <REDACTED>", str(exc))
    # Also scrub `apikey: ...` headers if the SDK ever echoes them.
    scrubbed = re.sub(
        r"(apikey|api[-_]key)\s*[:=]\s*[A-Za-z0-9._\-]+",
        r"\1=<REDACTED>",
        scrubbed,
        flags=re.IGNORECASE,
    )
    logger.error(
        "Schema probe failure for %s — %s: %s",
        target, exc_class, scrubbed,
    )
    return RuntimeError(
        f"Schema probe failed for {target}: {exc_class} "
        "(details suppressed for secret safety — see ERROR log above)"
    )


def _validate_required_schema() -> None:
    """Fail-fast at startup if any required Supabase schema object is missing.

    Catches a class of regression that bit us once already: a migration
    file lands in `robotis_ai_setup/supabase/` but the live database is
    behind (migration applied via SQL Editor with an earlier file
    content, or never applied at all). The Cloud API then boots, /health
    returns 200, and the first workflow PATCH that triggers the
    snapshot_workflow_version trigger gets a bare 500 with no signal
    that the DB is the cause.
    By probing every schema object the code depends on at startup, the
    Railway deploy aborts with a named cause instead.
    Skip via EDUBOTICS_SKIP_SCHEMA_CHECK=1 (unit-test escape hatch).
    """
    if os.environ.get("EDUBOTICS_SKIP_SCHEMA_CHECK") == "1":
        return
    from app.services.supabase_client import get_supabase

    sb = get_supabase()
    # 1) Tables referenced from Python (.table('X').select/insert).
    #    `select('id').limit(0)` proves the table is reachable without
    #    actually reading rows.
    required_tables = (
        "users",
        "trainings",
        "classrooms",
        "workgroups",
        "workgroup_memberships",
        "workflows",
        "workflow_versions",  # 015
        "workflow_trajectories",  # 034 — Roboter Studio Batch-2b replay trajectories
        "tutorial_progress",  # 016
        "datasets",
        "progress_entries",
        "jetsons",  # 019 — classroom Jetson Orin Nano support
    )
    missing_tables: list[str] = []
    for table in required_tables:
        try:
            sb.table(table).select("*").limit(0).execute()
        except Exception as exc:  # supabase-py wraps PostgREST errors as APIError
            # PostgREST returns code PGRST205 ("Could not find the table")
            # when the relation is absent. Other failures (network, RLS)
            # are not what we're guarding against here, so re-raise.
            msg = str(exc)
            if "PGRST205" in msg or "Could not find the table" in msg or (
                "does not exist" in msg.lower() and table in msg
            ):
                missing_tables.append(table)
            else:
                raise _sanitize_probe_error(f"table {table}", exc) from None
    # Audit A3: probe specific columns the routes read/write that a
    # partial migration could leave absent even when the table exists.
    required_columns: tuple[tuple[str, str], ...] = (
        # Migration 030 — the HF-identity anchor read by auth.get_user_profile,
        # GET/PATCH /me, POST /datasets/sync, and the rewritten dataset_sweep.
        # A deploy that lands the new code before the ALTER TABLE would 500 on
        # every profile read; fail the deploy fast instead.
        ("users", "hf_username"),
        # Migration 022 — jetsons pair-intent columns (Fix #1 of the
        # 2026-05 audit). Without these the new two-step pair flow 500s.
        ("jetsons", "intent_token, intent_teacher_id"),
        # Migration 023 — trainings cancel-sweep columns (Fix #3 of the
        # 2026-05 audit). The status CHECK constraint expansion can't be
        # probed via PostgREST, but the new columns can.
        ("trainings", "cancel_attempted_at, cancel_attempts"),
        # 028: full-log pointer written by the extended
        # update_training_progress RPC on terminal transitions.
        ("trainings", "log_url"),
        # 029: compact Jetson inference run records (student-self write,
        # teacher read).
        ("inference_runs", "id, student_user_id, policy_repo, exit_reason"),
        # Migration 033 — Roboter Studio Phase-3 Sim-Szene. WorkflowResponse
        # round-trips this column on every read; routes/workflows.py writes it
        # on create/update/clone. A deploy that lands the new code before the
        # ALTER TABLE would 500 on the sim write — fail the deploy fast (golden
        # order: W1 migration → W2 cloud-API).
        ("workflows", "sim_scene"),
        # Migration 035 — edu6 arm-family tag on recorded trajectories. Every
        # trajectory SELECT list + the insert now name this column; a deploy
        # landing the code before the ALTER TABLE would 500 the whole
        # record/replay surface.
        ("workflow_trajectories", "robot_profile"),
    )
    for table, cols in required_columns:
        try:
            sb.table(table).select(cols).limit(0).execute()
        except Exception as exc:
            msg = str(exc)
            if "PGRST204" in msg or (
                "column" in msg.lower() and "does not exist" in msg.lower()
            ):
                missing_tables.append(f"{table}.{cols}")
            else:
                raise _sanitize_probe_error(
                    f"columns {table}.{cols}", exc
                ) from None
    # 2) RPCs the code calls directly. Probe with the actual argument
    #    shape; any "user not found" / "no rows" response proves the
    #    RPC exists. We fail only on PostgREST's PGRST202 (function
    #    not found in the schema cache).
    #
    #    Probes are scoped to RPCs that take a single p_*_id UUID arg
    #    so the probe shape is uniform. RPCs with complex required
    #    args (start_training_safe, adjust_student_credits, etc.) are
    #    NOT probed because PostgREST resolves functions by argument
    #    signature: `rpc('start_training_safe', {})` legitimately
    #    returns "function does not exist" for the zero-arg overload,
    #    which is indistinguishable from the function being absent.
    #    Those RPCs are covered by table-existence: if the migration
    #    that added them ran, the tables they touch exist.
    dummy = "00000000-0000-0000-0000-000000000000"
    required_rpcs = (
        ("get_remaining_credits", {"p_user_id": dummy}),      # base
        ("get_teacher_credit_summary", {"p_teacher_id": dummy}),  # 002
        # Audit A3: previously skipped on the theory that "if 011 ran
        # the workgroup tables exist". That's true today but doesn't
        # cover the case where 011 was applied partially via SQL Editor
        # copy-paste and the function body was missed. The probes below
        # pass concrete UUIDs to bypass the zero-arg/overload ambiguity
        # — PostgREST resolves on (name, arg-name set), so passing
        # p_teacher_id/p_workgroup_id/p_delta together picks exactly
        # one overload. A P0011/P0022 (ownership) or P0002 (not found)
        # response proves the RPC exists; PGRST202 means it doesn't.
        ("adjust_student_credits",
            {"p_teacher_id": dummy, "p_student_id": dummy, "p_delta": 0}),
        ("adjust_workgroup_credits",
            {"p_teacher_id": dummy, "p_workgroup_id": dummy, "p_delta": 0}),
        ("start_training_safe", {
            "p_user_id": dummy,
            "p_dataset_name": "_probe",
            "p_model_name": "_probe",
            "p_model_type": "act",
            "p_training_params": {},
            "p_total_steps": 0,
            "p_worker_token": dummy,
        }),
        # Migration 018 — the SECURITY DEFINER wrappers that set
        # app.user_id GUC inside the transaction so the
        # workflow_versions trigger writes `saved_by` correctly.
        # routes/workflows.py:277 calls update_workflow_blockly on
        # every PATCH that changes blockly_json; :433 calls
        # restore_workflow_version on POST .../versions/{id}/restore.
        # Without these probes, a Railway deploy of the workflows
        # routes against a DB missing migration 018 passes /health
        # and 500s on first student save (c56c012 class).
        ("update_workflow_blockly", {
            "p_workflow_id": dummy,
            "p_user_id": dummy,
            "p_blockly_json": {},
            "p_name": None,
            "p_description": None,
        }),
        ("restore_workflow_version", {
            "p_workflow_id": dummy,
            "p_version_id": dummy,
            "p_user_id": dummy,
        }),
        # Migration 019 — classroom Jetson lifecycle RPCs. Each probe
        # passes the signature the routes use; an existence-check P0002
        # ("nicht gefunden") response proves the RPC is callable, while
        # PGRST202 means the migration hasn't been applied yet.
        ("claim_jetson", {"p_jetson_id": dummy, "p_user_id": dummy}),
        ("release_jetson", {"p_jetson_id": dummy, "p_user_id": dummy}),
        ("heartbeat_jetson", {"p_jetson_id": dummy, "p_user_id": dummy}),
        ("agent_heartbeat_jetson", {
            "p_jetson_id": dummy,
            "p_agent_token": dummy,
            "p_lan_ip": "",
            "p_agent_version": "",
        }),
        # Migration 022 — pair_jetson now requires p_intent_token. The
        # 4-arg overload is DROP'd by 022 so the probe MUST send the
        # 5-arg shape, otherwise PostgREST reports the function as
        # missing and the deploy aborts. claim_pair_intent is the new
        # /pair-intent RPC.
        ("pair_jetson", {
            "p_classroom_id": dummy,
            "p_pairing_code": "_probe",
            "p_teacher_id": dummy,
            "p_mdns_name": "_probe",
            "p_intent_token": dummy,
        }),
        ("claim_pair_intent", {
            "p_pairing_code": "_probe",
            "p_teacher_id": dummy,
        }),
        ("force_release_jetson", {"p_jetson_id": dummy, "p_teacher_id": dummy}),
        ("sweep_jetson_locks", {}),
        # Migration 020 — v2.3.0 follow-up RPCs (agent-side claim-fail
        # release, teacher pairing-code regeneration, teacher unpair).
        # Same fingerprint pattern: PGRST202 means the migration hasn't
        # been applied yet to the live DB and Railway should abort the
        # deploy instead of returning 200 from /health and 500'ing the
        # first teacher who tries to rotate a code.
        ("agent_release_jetson", {"p_jetson_id": dummy, "p_agent_token": dummy}),
        ("regenerate_pairing_code", {
            "p_jetson_id": dummy,
            "p_teacher_id": dummy,
            "p_new_code": "_probe",
            "p_expires_at": "1970-01-01T00:00:00+00:00",
        }),
        ("unpair_jetson", {"p_jetson_id": dummy, "p_teacher_id": dummy}),
        # Migration 026 — atomic register_dataset upsert + HF-author
        # anchor check. Without this RPC, /datasets POST 500s on the
        # supabase-py rpc('register_dataset_safe') call (PGRST202).
        # Probe with a non-existent UUID; the RPC's first action is
        # SELECT id FROM users FOR UPDATE which raises P0002 ("Benutzer
        # nicht gefunden") for any unknown user — that proves the
        # function is registered without requiring a real row.
        ("register_dataset_safe", {
            "p_user_id": dummy,
            "p_hf_repo_id": "_probe/_probe",
            "p_hf_author": "_probe",
            "p_workgroup_id": None,
            "p_name": "_probe",
            "p_description": "",
            "p_episode_count": None,
            "p_total_frames": None,
            "p_fps": None,
            "p_robot_type": None,
        }),
    )
    missing_rpcs: list[str] = []
    for name, args in required_rpcs:
        try:
            sb.rpc(name, args).execute()
        except Exception as exc:
            msg = str(exc)
            # PostgREST PGRST202: function not found in the schema
            # cache. The "function X does not exist" string is also
            # what Postgres raises for a missing overload, but since
            # we probe with the correct signature here, that response
            # only happens when the function is truly absent.
            if "PGRST202" in msg or (
                "function" in msg.lower() and "does not exist" in msg.lower()
            ):
                missing_rpcs.append(name)
            # Everything else (P0001 token mismatch, P0002 user not
            # found, P0003 no credits, P0011/P0022 ownership, etc.)
            # proves the RPC exists.
    if missing_tables or missing_rpcs:
        details = []
        if missing_tables:
            details.append(f"tables: {', '.join(missing_tables)}")
        if missing_rpcs:
            details.append(f"RPCs: {', '.join(missing_rpcs)}")
        raise RuntimeError(
            "Cloud API cannot start — Supabase schema is behind the deployed "
            f"code. Missing {' and '.join(details)}. Apply the on-disk "
            "migrations under robotis_ai_setup/supabase/ to the live "
            "database and redeploy. Override with "
            "EDUBOTICS_SKIP_SCHEMA_CHECK=1 only for local unit-test runs."
        )


def _parse_and_validate_origins() -> list[str]:
    """Parse ALLOWED_ORIGINS and refuse to start with dangerous values.

    Three classes of mistake we've seen in real deployments and want to
    catch at startup rather than in production: (1) a literal `*` with
    `allow_credentials=True` (browsers reject the combo, but FastAPI
    doesn't — confusing silent failures), (2) a typo like
    `https://*.vercel.app` which CORSMiddleware treats as a literal
    string and silently blocks every origin, and (3) URLs without a
    scheme that parse as if the whole string were a path.
    """
    raw = os.environ.get("ALLOWED_ORIGINS", "http://localhost")
    origins: list[str] = []
    for part in raw.split(","):
        origin = part.strip()
        if not origin:
            continue
        if origin == "*":
            raise RuntimeError(
                "ALLOWED_ORIGINS='*' is incompatible with allow_credentials=True. "
                "List explicit origins instead."
            )
        if "*" in origin:
            raise RuntimeError(
                f"ALLOWED_ORIGINS entry {origin!r} contains a wildcard. "
                "CORSMiddleware treats this as a literal string and will "
                "block every real origin. Use explicit URLs."
            )
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(
                f"ALLOWED_ORIGINS entry {origin!r} is not a valid absolute URL "
                f"(expected http[s]://host[:port])."
            )
        origins.append(origin)
    if not origins:
        raise RuntimeError("ALLOWED_ORIGINS is empty.")
    return origins


allowed_origins = _parse_and_validate_origins()
logger.info("CORS allowed origins: %s", allowed_origins)

# ─── Simple in-process rate limiter ────────────────────────────────────
# Stops a student script from hammering /trainings/start or the auth
# endpoints. The RateLimiter implementation lives in
# app.services.rate_limiter (extracted so it is unit-testable without the
# full FastAPI app import, and so the LRU key-cap memory guard is
# documented in one place). Keyed by client IP from X-Forwarded-For for
# most rules, or a hashed bearer token for the per-user prefixes below.
# State lives in-process, so this only behaves correctly at
# uvicorn --workers 1 (Railway default). If we ever scale out, swap to
# Redis-backed slowapi.
_rate_limiter = RateLimiter()

# (method, path prefix, limit, window seconds). Method "*" matches any.
# Method-aware so we can rate-limit POST /workflows (creation) without
# also throttling PATCH /workflows/{id} which the Blockly editor calls
# on every debounced save.
_RATE_LIMIT_RULES: list[tuple[str, str, int, float]] = [
    ("*", "/trainings/start", 10, 60.0),
    ("*", "/trainings/cancel", 20, 60.0),
    ("POST", "/workflows", 10, 60.0),
    # Teacher classroom + template creation. Audit §2.1: the v1 ship
    # left teacher template POST unrate-limited, leaving a DOS hole
    # next to the student-side guarded path. The prefix covers both
    # "POST /teacher/classrooms" (new classroom) and
    # "POST /teacher/classrooms/{id}/workflow-templates" — both are
    # legitimately bursty when a teacher is setting up a classroom but
    # 10/min is plenty. Also covers
    # "POST /teacher/classrooms/{id}/workgroups" (new workgroup).
    ("POST", "/teacher/classrooms", 10, 60.0),
    # Workgroup detail-level POSTs: members + credit adjustments. A
    # teacher legitimately bursts a few member-adds back-to-back when
    # setting up a group but 20/min is comfortable.
    ("POST", "/teacher/workgroups", 20, 60.0),
    # Datasets registry: students POST after every recording upload —
    # 20/min is more than enough for any classroom while stopping a
    # broken recording loop from spamming.
    ("POST", "/datasets", 20, 60.0),
    # Jetson agent bootstrap (no auth). IP-keyed because the agent has
    # no JWT yet. Five registrations per minute per public IP is fine
    # for a teacher physically setting up a Jetson; a rogue actor on
    # the LAN would burn through the pool without getting credentials
    # bound to any classroom.
    #
    # MUST come BEFORE the broader "/jetson/" rule below — the
    # middleware's first-match-wins (break-on-match) ordering means a
    # less-specific prefix listed first shadows a more-specific one.
    # The original "/jetson/" rule was listed first and turned this
    # 5/60s into dead code.
    ("POST", "/jetson/register", 5, 60.0),
    # Classroom Jetson claim/heartbeat/release/agent-heartbeat/
    # agent-release/release-beacon — single prefix rule for everything
    # under /jetson/{id}/. Per-user (JWT sub keyed) when an
    # Authorization header is present; falls back to IP keying when
    # absent (agent endpoints + release-beacon body-token path).
    #
    # Why 30/60s and not 10/60s: a classroom of 30 students all closing
    # their browser tab at the end of a lesson would each fire one
    # navigator.sendBeacon('release-beacon') call. The beacon path has
    # no JWT header (sendBeacon can't set headers) so it falls back to
    # IP keying; 30 students from one NAT IP would exceed 10/60s and
    # some beacons would get 429'd, sending their locks back to the
    # 5-min sweeper — exactly what v2.3.0 fixed. 30/60s comfortably
    # absorbs that storm and still catches a tight-loop bug. For the
    # JWT-authenticated paths (claim, heartbeat, release) the
    # per-user-keying means 30/min/user is still very generous (claim
    # is 1x/session, heartbeat is 2/min, release is 1x/session).
    ("POST", "/jetson/", 30, 60.0),
    # Tutorial progress upsert — a Blockly editor that auto-saves on
    # every step transition can burst a few requests per minute per
    # student; 30/60s easily absorbs that while stopping a runaway
    # loop from spamming the row's history.
    ("PATCH", "/me/tutorial-progress", 30, 60.0),
    # GDPR export — large response (full per-user JSON bundle), no
    # reason to allow more than a couple per hour.
    ("GET", "/me/export", 3, 3600.0),
]

# Sort rules longest-prefix-first so a more-specific rule (e.g.
# /jetson/register) always wins over a broader one (/jetson/). Without
# this, a newly-added narrow rule listed AFTER a broad rule in the
# literal becomes dead code on the first-match-wins middleware path.
# Done in code so future additions can't regress by accidental ordering.
_RATE_LIMIT_RULES.sort(key=lambda rule: len(rule[1]), reverse=True)


def _client_ip(request: Request) -> str:
    """Resolve the real client IP behind Railway's proxy.

    request.client.host is the proxy address — every student's request
    would carry the same value, so a per-IP limiter would behave as one
    global bucket. Railway always sets X-Forwarded-For; the leftmost
    entry is the original client.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _user_key_from_jwt(request: Request) -> str | None:
    """Derive a stable per-token rate-limit bucket key.

    NOTE on the trade-off — the middleware runs BEFORE
    ``Depends(get_current_user)`` so the JWT signature is unverified at
    this point. We deliberately do NOT trust the JWT payload (a forged
    token can claim any ``sub``). Instead, we hash the full token bytes
    and use the truncated hex digest as the bucket key. Two
    consequences:

      1. Per-user bucketing still holds for legitimate users — every
         valid session reuses the same JWT until it expires, so the
         hash is stable.
      2. A forged-token attacker gets one bucket per distinct forged
         token rather than colliding into one bucket per claimed
         ``sub`` — slightly less aggressive limiting than trusting
         ``sub`` would be, but no attacker can starve a real user's
         bucket by spamming forgeries that claim the victim's id.

    The route's ``Depends(get_current_user)`` still authenticates the
    forged tokens away with a clean 401; the rate limiter only matters
    for the (legitimate or near-legitimate) traffic that reaches the
    inner handler.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    import hashlib
    return "jwt:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


# Routes that should rate-limit per AUTHENTICATED USER rather than per
# IP. Anything missing here keeps the IP-keyed legacy behavior. The
# /jetson/ prefix uses per-JWT-sub keying for claim/heartbeat/release
# so a NAT'd classroom of 30 students doesn't share one bucket. The
# /jetson/register sub-prefix has no JWT and falls back to IP keying
# (the _user_key_from_jwt helper returns None → IP path).
#
# /trainings/ (covers the rate-limited /trainings/start + /trainings/cancel
# rules) is per-user for the same NAT reason: both endpoints require
# Depends(get_current_user) so a JWT is always present, and IP keying
# meant 30 students behind one school NAT shared the 10/min start bucket —
# the 11th student got a spurious 429 even though nobody individually
# exceeded the limit. The IP fallback still covers any missing header.
#
# /workflows (no trailing slash so it covers the bare `POST /workflows`
# create path AS WELL AS `POST /workflows/{id}/{clone,versions/.../restore,
# trajectories}`) is per-user for the identical NAT reason: the single
# ("POST", "/workflows", 10, 60.0) rule matches by prefix, so all four
# workflow POSTs share one bucket — IP-keyed, a 30-student NAT classroom
# collectively blew the 10/min IP bucket → spurious 429. Every /workflows
# route requires Depends(get_current_user) (verified: create_workflow,
# clone_workflow, restore_workflow_version, create_trajectory), so a JWT is
# always present and there is no no-JWT/beacon fallback to regress. The IP
# fallback still covers any (unreachable) missing-header case.
_PER_USER_RATE_LIMIT_PREFIXES = ("/jetson/", "/trainings/", "/workflows")


# Audit A2: hard upper bound on workflow-write request bodies.
# validate_blockly_json's 256 KB cap fires AFTER json.dumps has already
# materialised the entire payload — useless as a DoS guard. Reject at the
# Content-Length header level for the affected paths so an attacker can't
# force a multi-MB allocation. Give 50% headroom over the validator cap
# so an oversized-but-legitimate payload still gets the German 413 from
# the validator (which includes the limit in the message) rather than
# the generic middleware 413.
_MAX_WRITE_BODY_BYTES = 384 * 1024
_BODY_SIZE_LIMITED_PREFIXES: tuple[tuple[str, str], ...] = (
    ("POST", "/workflows"),
    ("PATCH", "/workflows"),
    ("POST", "/teacher/classrooms"),  # covers workflow-templates path too
)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in {"POST", "PATCH", "PUT"}:
            for m, prefix in _BODY_SIZE_LIMITED_PREFIXES:
                if m == request.method and request.url.path.startswith(prefix):
                    # Reject Transfer-Encoding: chunked on these
                    # protected paths — without a Content-Length the
                    # size check below is useless and an attacker
                    # could stream multi-MB bodies past the guard.
                    # 411 Length Required is the standards-correct
                    # response when the server demands Content-Length.
                    if request.headers.get("transfer-encoding", "").lower() == "chunked":
                        logger.warning(
                            "Rejecting chunked %s %s — Content-Length required.",
                            request.method, request.url.path,
                        )
                        return JSONResponse(
                            status_code=411,
                            content={
                                "detail": (
                                    "Chunked-Übertragung wird auf diesem "
                                    "Endpunkt nicht unterstützt."
                                )
                            },
                        )
                    cl = request.headers.get("content-length")
                    if cl:
                        try:
                            size = int(cl)
                        except ValueError:
                            size = -1
                        if size > _MAX_WRITE_BODY_BYTES:
                            logger.warning(
                                "Rejecting oversized %s %s body: %s bytes",
                                request.method, request.url.path, cl,
                            )
                            return JSONResponse(
                                status_code=413,
                                content={
                                    "detail": (
                                        "Anfrage zu groß "
                                        f"(>{_MAX_WRITE_BODY_BYTES // 1024} KB)."
                                    )
                                },
                            )
                    break
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method
        for rule_method, prefix, limit, window in _RATE_LIMIT_RULES:
            if rule_method != "*" and rule_method != method:
                continue
            # Anchored prefix match: a rule for "/trainings/start" must
            # NOT match "/trainings/started". Either the path is exactly
            # the prefix or the prefix must be followed by "/" so the
            # next segment is fully delimited. Path prefixes that end
            # in "/" (e.g. "/jetson/") still match all sub-resources
            # because path == "/jetson/" or path startswith "/jetson/"
            # via the second branch.
            if prefix.endswith("/"):
                matched = path.startswith(prefix)
            else:
                matched = path == prefix or path.startswith(prefix + "/")
            if matched:
                # /workflows must NOT match /workflows/{id} for PATCH
                # — when the rule pins the method, allow the prefix
                # match to apply across path lengths; when it's "*",
                # the legacy behaviour is preserved. The POST-on-create
                # rule combined with method=POST ensures PATCH on the
                # same prefix is unaffected.
                use_user_key = any(
                    path.startswith(p) for p in _PER_USER_RATE_LIMIT_PREFIXES
                )
                key = None
                if use_user_key:
                    key = _user_key_from_jwt(request)
                if not key:
                    key = _client_ip(request)
                bucket_key = f"{rule_method}:{prefix}"
                if not _rate_limiter.check(bucket_key, key, limit, window):
                    logger.warning("Rate limit hit on %s %s from %s", rule_method, prefix, key)
                    # Return a Response directly. Raising HTTPException
                    # inside BaseHTTPMiddleware.dispatch is a footgun:
                    # Starlette doesn't route middleware exceptions
                    # through FastAPI's exception handlers, so the
                    # client would see a 500 instead of a clean 429.
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "Zu viele Anfragen — bitte einen Moment warten."},
                    )
                break
        return await call_next(request)


# Middleware ordering: Starlette wraps in reverse of add_middleware calls
# (last-added becomes outermost). We want CORS to be the OUTERMOST layer so
# a 429 from RateLimitMiddleware still carries Access-Control-* headers —
# otherwise the browser sees the response as a generic CORS failure instead
# of a structured 429. Therefore: add RateLimit first, then CORS.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(health_router)
app.include_router(version_router)
app.include_router(training_router)
app.include_router(me_router)
app.include_router(teacher_router)
app.include_router(workgroups_router)
app.include_router(datasets_router)
app.include_router(policies_router)
app.include_router(admin_router)
app.include_router(workflows_router)
app.include_router(jetson_router)
app.include_router(jetson_teacher_router)


# ─── Background tasks ───────────────────────────────────────────────────
# The dataset reconciliation sweep is the safety net for the rare case
# where a successful HF upload is followed by a failed POST /datasets
# from the React app — without it, group siblings would never see the
# upload. Single-tenant on purpose: Cloud API runs uvicorn --workers 1,
# so spawning the loop once at startup is correct. Disable by setting
# DATASET_SWEEP_DISABLED=1 (e.g. for unit tests).
@app.on_event("startup")
async def _start_dataset_sweep() -> None:
    import asyncio as _asyncio  # local import keeps the module-load graph clean

    if os.environ.get("DATASET_SWEEP_DISABLED") == "1":
        logger.info("dataset_sweep: disabled via env")
        return
    if not os.environ.get("HF_TOKEN"):
        # Sweep is a no-op without HF_TOKEN; don't even start the loop
        # so the Railway log doesn't show "skipping tick" forever.
        logger.info("dataset_sweep: HF_TOKEN not set, loop not started")
        return
    _asyncio.create_task(_dataset_sweep_loop())


@app.on_event("startup")
async def _start_jetson_sweep() -> None:
    """Migration 019: auto-release classroom-Jetson locks whose student
    heartbeat is older than 5 minutes. Pairs with the agent's transition
    watcher to recover from browser crashes / abandoned sessions."""
    import asyncio as _asyncio

    if os.environ.get("JETSON_SWEEPER_DISABLED") == "1":
        logger.info("jetson_sweep: disabled via env")
        return
    _asyncio.create_task(_jetson_sweep_loop())


@app.on_event("startup")
async def _start_training_cancel_sweep() -> None:
    """Migration 023: retry Modal cancel for rows stuck in
    `cancel_requested`. /trainings/cancel only flips to `canceled` on a
    successful Modal cancel — otherwise the row sits in cancel_requested
    and this sweep retries every 30 s until success or MAX_CANCEL_ATTEMPTS.

    Disable via TRAINING_CANCEL_SWEEPER_DISABLED=1 for unit tests."""
    import asyncio as _asyncio

    if os.environ.get("TRAINING_CANCEL_SWEEPER_DISABLED") == "1":
        logger.info("training_cancel_sweep: disabled via env")
        return
    _asyncio.create_task(_training_cancel_sweep_loop())


# Schema probe lives in a BACKGROUND task scheduled here, NOT at module
# import. Without this split, _validate_required_schema() (32+ sequential
# Supabase round-trips) blocks the FastAPI lifespan startup phase, which
# in turn blocks uvicorn from binding the socket — Railway's healthcheck
# then polls connection-refused for 5-15s and times out the deploy. By
# scheduling the probe as a fire-and-forget asyncio task, uvicorn binds
# immediately; /health serves 503 with status="starting" via the
# STARTUP_SCHEMA_OK gate until the probe flips it. Failure raises SIGTERM
# so Railway sees the process exit and marks the deploy failed (instead
# of "Healthcheck failed" masking a real schema-drift cause).
@app.on_event("startup")
async def _start_schema_probe() -> None:
    import asyncio as _asyncio

    async def _run_and_flip() -> None:
        try:
            loop = _asyncio.get_event_loop()
            await loop.run_in_executor(None, _validate_required_schema)
        except Exception:
            logger.exception(
                "Schema probe failed at startup — Cloud API exiting so Railway "
                "marks the deploy failed."
            )
            import signal
            os.kill(os.getpid(), signal.SIGTERM)
            return
        from app.routes import health as _health_route
        _health_route.STARTUP_SCHEMA_OK = True
        logger.info("Cloud API boot complete — /health will report status=ok")

    _asyncio.create_task(_run_and_flip())
