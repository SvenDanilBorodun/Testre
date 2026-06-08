"""GUI auto-update checker.

Checks the Railway API for a newer GUI version and downloads the installer
if an update is available.  Uses only stdlib to avoid adding PyInstaller
dependencies.
"""

import glob
import json
import os
import re
import tempfile
import time
import urllib.request
import urllib.error


_NUM_RE = re.compile(r"\d+")


def _parse_version(v: str) -> tuple:
    """Best-effort numeric version tuple, padded to 3 parts, for comparison.

    Tolerant of a leading 'v', pre-release/build suffixes, and short forms:
    'v2.8.1' / '2.8.1-rc1' / '2.8.1+build' → (2, 8, 1); '2.8' → (2, 8, 0);
    non-numeric / empty → (0, 0, 0).

    The previous ``int(x) for x in v.split('.')`` raised ValueError on ANY
    non-purely-numeric segment, and the caller swallowed it as "no update" —
    so a single non-numeric release tag (e.g. an '-rc1' hotfix) would silently
    stop every student from EVER updating while reporting "GUI ist aktuell".
    Padding also fixes the (2,8) > (2,8,0) == False short-form asymmetry.
    """
    nums = _NUM_RE.findall(v or "")
    if not nums:
        return (0, 0, 0)
    t = tuple(int(n) for n in nums[:3])
    return t + (0,) * (3 - len(t))


def _asset_available(url: str) -> bool:
    """HEAD the installer asset. Returns False ONLY when the server
    DEFINITIVELY reports it gone (404) — so we never lock a student behind the
    forced, non-closable update modal for a release whose .exe asset is
    missing/deleted/not-yet-propagated. Any other outcome (200, redirect, 405
    "HEAD not allowed", or a transient network error) returns True: we can't
    prove absence, and the download attempt + the skip path handle the rest.
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except urllib.error.HTTPError as e:
        return e.code != 404
    except Exception:
        return True


def check_for_update(current_version: str, api_url: str) -> dict | None:
    """Check the cloud API for a newer GUI version.

    Returns {"version": "x.y.z", "download_url": "..."} if an update is
    available AND its installer asset is reachable, or None if current / on any
    error. The asset pre-check prevents the forced-modal lockout when /version
    advertises a version whose .exe asset 404s (deleted release, hand-bumped
    GUI_VERSION before the asset landed, etc.).
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
            if not _asset_available(download_url):
                return None
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
