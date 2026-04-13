import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/version")
async def get_latest_version():
    """Return the latest GUI version and download URL.

    Configured via Railway environment variables:
      GUI_VERSION      — e.g. "2.1.0"
      GUI_DOWNLOAD_URL — public URL to the installer .exe
    """
    version = os.environ.get("GUI_VERSION")
    download_url = os.environ.get("GUI_DOWNLOAD_URL")

    if not version or not download_url:
        return JSONResponse(
            status_code=503,
            content={"error": "Version info not configured"},
        )

    return {
        "version": version,
        "download_url": download_url,
        "required": True,
    }
