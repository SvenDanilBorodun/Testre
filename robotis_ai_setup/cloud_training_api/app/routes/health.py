"""Health endpoint exposing commit, schema-fingerprint, and boot state.

Why this is more than `{"status":"ok"}`: Railway's healthcheck routes
traffic to a pod as soon as the FastAPI app responds 200. Before the
schema-probe in `main.py::_validate_required_schema` completes, the
process may be importable but the database isn't guaranteed to match the
on-disk migrations. Returning 503 with `status: starting` until
`STARTUP_SCHEMA_OK` flips lets Railway hold traffic at the gate.

The `commit` field lets CI gates verify "after deploy, /health.commit
equals the SHA we just pushed" — catching a stale Railway cache that
serves the previous build under the new health endpoint.
"""

import datetime
import os
import pathlib

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

# Set by app.main after every import-time validator finishes (secrets +
# optional-warns + schema probe). Until then, /health returns 503 so
# Railway's healthcheck holds traffic at the gate. The schema probe
# itself raises if it can't reach Supabase or the schema is drift-state,
# so when this flips True we KNOW the live DB matches the on-disk
# migrations the code expects.
STARTUP_SCHEMA_OK: bool = False

# Captured the first time anyone reads /health AFTER STARTUP_SCHEMA_OK
# flips, so the timestamp reflects "boot completed" rather than "module
# imported" — the gap can be 5–15 s on cold Railway deploys while the
# schema probe walks every table + RPC.
_BOOT_COMPLETED_AT: str | None = None


def _resolve_commit() -> str:
    """Resolve the git SHA the running image was built from.

    Priority: Railway's auto-injected RAILWAY_GIT_COMMIT_SHA → an explicit
    BUILD_COMMIT env (set as a Docker ARG/ENV in CI) → "unknown".
    """
    for key in ("RAILWAY_GIT_COMMIT_SHA", "BUILD_COMMIT"):
        val = os.environ.get(key)
        if val:
            return val
    return "unknown"


def _resolve_version() -> str:
    """Read the on-disk VERSION file. Falls back to env GUI_VERSION → "unknown".

    The VERSION file ships INSIDE the image (Dockerfile COPY); GUI_VERSION
    is a Railway env var that should agree with it (see main.py drift
    warning). When the file is missing the env value is the next-best
    signal.
    """
    # The cloud_training_api directory layout is .../cloud_training_api/app/routes/health.py;
    # the VERSION file lives at repo root, which is 5 levels up in dev.
    # In production the file resolves to /app/app/routes/health.py whose
    # .parents has only 4 entries — parents[4] would raise IndexError on
    # every /health call, which is exactly what killed the b28d5a1
    # deploy (1/1 replicas never became healthy → Railway gave up after
    # 5 min retry loop). Build candidates LAZILY via _safe_parent so
    # absent indices simply drop out of the list.
    here = pathlib.Path(__file__).resolve()

    def _safe_parent(p: pathlib.Path, n: int) -> pathlib.Path | None:
        try:
            return p.parents[n]
        except IndexError:
            return None

    candidates: list[pathlib.Path] = [
        pathlib.Path("/app/VERSION"),  # image root — first in production
    ]
    for n in (4, 3):  # 4=repo root in dev, 3=cloud_training_api dir
        parent = _safe_parent(here, n)
        if parent is not None:
            candidates.append(parent / "VERSION")
    for path in candidates:
        try:
            if path.is_file():
                txt = path.read_text(encoding="utf-8").strip()
                if txt:
                    return txt
        except OSError:
            continue
    return os.environ.get("GUI_VERSION") or "unknown"


@router.get("/health")
async def health_check():
    """Liveness + readiness probe.

    Returns 200 when the import-time schema probe succeeded.
    Returns 503 with the same body shape but `status: "starting"` until
    then — Railway's healthcheck treats that as "not ready" and holds
    traffic at the load balancer.
    """
    global _BOOT_COMPLETED_AT
    if STARTUP_SCHEMA_OK and _BOOT_COMPLETED_AT is None:
        _BOOT_COMPLETED_AT = datetime.datetime.now(datetime.timezone.utc).isoformat()

    body = {
        "status": "ok" if STARTUP_SCHEMA_OK else "starting",
        "commit": _resolve_commit(),
        "schema_ok": STARTUP_SCHEMA_OK,
        "boot_completed_at": _BOOT_COMPLETED_AT,
        "version": _resolve_version(),
    }
    if not STARTUP_SCHEMA_OK:
        return JSONResponse(status_code=503, content=body)
    return body
