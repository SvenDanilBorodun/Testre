"""GUI auto-update checker.

Checks the Railway API for a newer GUI version and downloads the installer
if an update is available.  Uses only stdlib to avoid adding PyInstaller
dependencies.
"""

import glob
import json
import os
import tempfile
import time
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


def cleanup_stale_installers(max_age_hours: int = 24) -> int:
    """Delete leftover EduBotics_Setup.exe files from past updates.

    The installer is downloaded into %TEMP% before being launched, then the
    GUI exits. The installer itself can't delete its own file (it's running),
    so we sweep on the NEXT GUI launch. Anything older than max_age_hours gets
    removed so stale installers don't pile up in %TEMP%.

    Returns the number of files removed.
    """
    patterns = [
        os.path.join(tempfile.gettempdir(), "EduBotics_Setup.exe"),
        os.path.join(tempfile.gettempdir(), "EduBotics_Setup*.exe"),
    ]
    now = time.time()
    cutoff = now - (max_age_hours * 3600)
    removed = 0
    seen = set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            try:
                # Only remove if older than cutoff — avoid deleting a running installer
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    return removed


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
