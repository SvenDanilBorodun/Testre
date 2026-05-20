"""Version endpoint consumed by the in-tree GUI's update_checker.

Two changes from the v1 shape:

  1. Dropped `required: true`. The GUI's update_checker never reads it,
     so it was a dead field that suggested a forced-update gate that
     doesn't exist. Silent confusion when a maintainer reads the
     endpoint output and assumes mandatory upgrades work.

  2. When GUI_VERSION / GUI_DOWNLOAD_URL are unset, return 200 with
     explicit nulls rather than 503. The previous 503 made
     update_checker.py fail closed (no update prompt ever), masking the
     misconfiguration entirely. An explicit null payload is the same
     "no update available" signal to the client without hiding the fact
     that the env vars aren't set — the boot-time warning in
     `_warn_optional_secrets` covers the operator-side notice.

  3. Added `commit` so CI health-gates can verify the running image's
     SHA matches the just-pushed commit (catches stale-cache deploys).
"""

import os

from fastapi import APIRouter

router = APIRouter()


def _resolve_commit() -> str:
    """Mirror health.py's resolver — Railway SHA → BUILD_COMMIT → unknown."""
    for key in ("RAILWAY_GIT_COMMIT_SHA", "BUILD_COMMIT"):
        val = os.environ.get(key)
        if val:
            return val
    return "unknown"


@router.get("/version")
async def get_latest_version():
    """Return the latest GUI version and download URL.

    Configured via Railway environment variables:
      GUI_VERSION      — e.g. "2.1.0"
      GUI_DOWNLOAD_URL — public URL to the installer .exe

    When either is unset the response is still 200 with null values so
    the GUI's update_checker treats it as "no update available" instead
    of failing closed on a 503.
    """
    version = os.environ.get("GUI_VERSION") or None
    download_url = os.environ.get("GUI_DOWNLOAD_URL") or None

    return {
        "version": version,
        "download_url": download_url,
        "commit": _resolve_commit(),
    }
