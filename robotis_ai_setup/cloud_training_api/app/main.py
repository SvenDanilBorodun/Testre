import logging
import os
import time
from collections import defaultdict, deque
from threading import Lock
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.routes.admin import router as admin_router
from app.routes.datasets import router as datasets_router
from app.routes.health import router as health_router
from app.routes.me import router as me_router
from app.routes.teacher import router as teacher_router
from app.routes.training import router as training_router
from app.routes.version import router as version_router
from app.routes.vision import router as vision_router
from app.routes.workflows import router as workflows_router
from app.routes.workgroups import router as workgroups_router
from app.services.dataset_sweep import sweep_loop as _dataset_sweep_loop

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


def _validate_required_schema() -> None:
    """Fail-fast at startup if any required Supabase schema object is missing.

    Catches a class of regression that bit us once already: a migration
    file lands in `robotis_ai_setup/supabase/` but the live database is
    behind (migration applied via SQL Editor with an earlier file
    content, or never applied at all). The Cloud API then boots, /health
    returns 200, and the first student to hit /vision/detect — or the
    first workflow PATCH that triggers the snapshot_workflow_version
    trigger — gets a bare 500 with no signal that the DB is the cause.
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
        "tutorial_progress",  # 016
        "datasets",
        "progress_entries",
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
                raise
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
        ("consume_vision_quota", {"p_user_id": dummy}),       # 017
        ("refund_vision_quota", {"p_user_id": dummy}),        # 017 round-3 — the c56c012 incident hot spot
        ("get_remaining_credits", {"p_user_id": dummy}),      # base
        ("get_teacher_credit_summary", {"p_teacher_id": dummy}),  # 002
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
            # found, P0003 no credits, etc.) proves the RPC exists.
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


_validate_required_schema()


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
# endpoints. Keyed by client IP from X-Forwarded-For (Railway proxies
# everything; request.client.host alone would be the proxy IP and every
# student would share one bucket). Trades cross-instance accuracy for
# zero-dependency: state lives in-process, so this only behaves correctly
# at uvicorn --workers 1 (Railway default). If we ever scale out, swap
# to Redis-backed slowapi.
class RateLimiter:
    def __init__(self) -> None:
        # { bucket_name: { key: deque[timestamps] } }
        self._buckets: dict[str, dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
        self._lock = Lock()

    def check(self, bucket: str, key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        with self._lock:
            hist = self._buckets[bucket][key]
            while hist and now - hist[0] > window_s:
                hist.popleft()
            if len(hist) >= limit:
                return False
            hist.append(now)
            return True


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
    # Cloud-burst perception (Phase 3 OWLv2 on Modal). Each call costs
    # ~$0.0001 in compute but a runaway loop in a workflow could rack
    # up dollars per classroom, so cap at 5/60s/user — well above any
    # legitimate manual editing cadence.
    ("POST", "/vision/detect", 5, 60.0),
]


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
    """Extract a stable per-user key from the Authorization Bearer JWT
    without verifying the signature. Middleware runs before the FastAPI
    dependency that fully validates the token; we trust the route's
    ``Depends(get_current_user)`` to authenticate, and use this only as
    a rate-limit bucket key.

    Returns the JWT's ``sub`` (user id) when present, otherwise None so
    the caller can fall back to IP keying. Audit round-3 §BD — keying
    /vision/detect by IP causes 30 NAT'd students in a classroom to
    share a 5/60s bucket; per-user keying fixes that without needing a
    second auth path inside the middleware.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    parts = token.split(".")
    if len(parts) < 2:
        return None
    import base64
    import json
    try:
        # JWT payload base64url, no padding — pad to multiple of 4.
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    sub = data.get("sub")
    if isinstance(sub, str) and sub:
        return sub
    return None


# Routes that should rate-limit per AUTHENTICATED USER rather than per
# IP. Anything missing here keeps the IP-keyed legacy behavior.
_PER_USER_RATE_LIMIT_PREFIXES = ("/vision/detect",)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method
        for rule_method, prefix, limit, window in _RATE_LIMIT_RULES:
            if rule_method != "*" and rule_method != method:
                continue
            if path.startswith(prefix):
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
                        content={"detail": "Too many requests — please wait a moment."},
                    )
                break
        return await call_next(request)


# Middleware ordering: Starlette wraps in reverse of add_middleware calls
# (last-added becomes outermost). We want CORS to be the OUTERMOST layer so
# a 429 from RateLimitMiddleware still carries Access-Control-* headers —
# otherwise the browser sees the response as a generic CORS failure instead
# of a structured 429. Therefore: add RateLimit first, then CORS.
app.add_middleware(RateLimitMiddleware)
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
app.include_router(admin_router)
app.include_router(workflows_router)
app.include_router(vision_router)


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
