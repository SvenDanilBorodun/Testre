"""GUI auto-update checker.

Checks the Railway API for a newer GUI version and downloads the installer
if an update is available.  Uses only stdlib to avoid adding PyInstaller
dependencies.
"""

import json
import os
import tempfile
import urllib.request
import urllib.error


def _parse_version(v: str) -> tuple:
    """Convert '2.1.0' → (2, 1, 0) for comparison."""
    return tuple(int(x) for x in v.strip().split("."))


def check_for_update(current_version: str, api_url: str) -> dict | None:
    """Check the cloud API for a newer GUI version.

    Returns {"version": "x.y.z", "download_url": "..."} if an update is
    available, or None if current / on any error.
    """
    url = f"{api_url.rstrip('/')}/version"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        remote_version = data.get("version", "")
        download_url = data.get("download_url", "")
        if not remote_version or not download_url:
            return None
        if _parse_version(remote_version) > _parse_version(current_version):
            return {"version": remote_version, "download_url": download_url}
    except Exception:
        return None
    return None


def download_installer(url: str, dest_dir: str = None,
                       progress_callback=None) -> str | None:
    """Download the installer .exe to a temporary directory.

    Args:
        url: Public URL of the installer.
        dest_dir: Directory to save the file (defaults to system temp).
        progress_callback: Optional callable(bytes_downloaded, total_bytes).

    Returns:
        Full path to the downloaded file, or None on failure.
    """
    if dest_dir is None:
        dest_dir = tempfile.gettempdir()
    dest_path = os.path.join(dest_dir, "EduBotics_Setup.exe")

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 64 * 1024  # 64 KB

            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        return dest_path
    except Exception:
        # Clean up partial download
        try:
            os.remove(dest_path)
        except OSError:
            pass
        return None
