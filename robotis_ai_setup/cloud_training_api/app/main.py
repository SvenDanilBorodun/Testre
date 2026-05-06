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
from app.routes.health import router as health_router
from app.routes.me import router as me_router
from app.routes.teacher import router as teacher_router
from app.routes.training import router as training_router
from app.routes.version import router as version_router
from app.routes.workflows import router as workflows_router

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
    # 10/min is plenty.
    ("POST", "/teacher/classrooms", 10, 60.0),
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
                client_ip = _client_ip(request)
                bucket_key = f"{rule_method}:{prefix}"
                if not _rate_limiter.check(bucket_key, client_ip, limit, window):
                    logger.warning("Rate limit hit on %s %s from %s", rule_method, prefix, client_ip)
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
app.include_router(admin_router)
app.include_router(workflows_router)
