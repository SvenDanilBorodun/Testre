import logging
import os
import time
from collections import defaultdict, deque
from threading import Lock
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.routes.admin import router as admin_router
from app.routes.health import router as health_router
from app.routes.me import router as me_router
from app.routes.teacher import router as teacher_router
from app.routes.training import router as training_router
from app.routes.version import router as version_router

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="EduBotics Cloud Training API")


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
# endpoints. Keyed by client IP; trades cross-instance accuracy for
# zero-dependency. For a multi-instance Railway deploy this is per-pod,
# which is still a large reduction vs unbounded.
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

# path prefix -> (limit, window seconds). None match = unlimited.
_RATE_LIMIT_RULES: list[tuple[str, int, float]] = [
    ("/trainings/start", 10, 60.0),  # 10 starts per minute per IP
    ("/trainings/cancel", 20, 60.0),
]


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        for prefix, limit, window in _RATE_LIMIT_RULES:
            if path.startswith(prefix):
                client_ip = request.client.host if request.client else "unknown"
                if not _rate_limiter.check(prefix, client_ip, limit, window):
                    logger.warning("Rate limit hit on %s from %s", prefix, client_ip)
                    raise HTTPException(
                        status_code=429,
                        detail="Too many requests — please wait a moment.",
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
