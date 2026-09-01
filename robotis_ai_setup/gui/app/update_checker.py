"""GUI auto-update checker.

Checks the Railway API for a newer GUI version and downloads the installer
if an update is available.  Uses only stdlib to avoid adding PyInstaller
dependencies.
"""

import glob
import hashlib
import json
import os
import re
import socket
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
            return {
                "version": remote_version,
                "download_url": download_url,
                # Optional integrity hash (None/"" on old deploys → skip verify).
                "sha256": data.get("installer_sha256") or "",
            }
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


class DownloadVerificationError(IOError):
    """An integrity gate refused the download, with an AUTHORED German reason.

    A distinct type because `IOError` IS `OSError` in Python 3, so „is this
    message already German prose or is it `[Errno 28] No space left`?" cannot
    be answered by `isinstance(exc, IOError)` — the first draft of
    `_download_failure_reason_de` got that wrong and would have put an English
    errno string on the non-closable update modal.
    """


def download_installer(url: str, dest_dir: str = None,
                       progress_callback=None,
                       expected_sha256: str = None,
                       reason_callback=None) -> str | None:
    """Download the installer .exe to a temporary directory, verifying integrity.

    Args:
        url: Public URL of the installer.
        dest_dir: Directory to save the file (defaults to system temp).
        progress_callback: Optional callable(bytes_downloaded, total_bytes).
        expected_sha256: Optional lowercase-hex SHA-256 the download must match.
        reason_callback: Optional callable(str) invoked ONCE with a German
            reason when the download fails. The two authored diagnoses below —
            „unvollständiger Download (n/m Bytes)" and „Prüfsumme stimmt nicht
            (beschädigter Download)" — used to be raised as IOError and then
            swallowed by this function's own `except Exception`, so a 404, a
            truncated transfer, a checksum mismatch, a full disk and a timeout
            were all indistinguishable to the caller: `None`. On
            `_show_update_dialog`'s NON-CLOSABLE modal that is the difference
            between an actionable message and a dead end. The RETURN CONTRACT
            is deliberately unchanged (`str | None`) — a tuple would break
            every existing caller and test for no gain.

    Two integrity gates before the file is handed back to be launched as admin:
      (A) Content-Length — a short/truncated download (server closed the
          connection early; the read loop ends "successfully" with a partial
          file) is rejected instead of launching a broken installer.
      (B) SHA-256 — when the cloud /version advertised a hash, the bytes must
          match it (corruption guard). A None/empty hash skips this gate (old
          deploys), so it's backward-compatible.

    Returns:
        Full path to the verified file, or None on failure (partial file
        cleaned up).
    """
    if dest_dir is None:
        dest_dir = tempfile.gettempdir()
    dest_path = os.path.join(dest_dir, "EduBotics_Setup.exe")
    expected = (expected_sha256 or "").strip().lower() or None

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 64 * 1024  # 64 KB
            hasher = hashlib.sha256()

            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        # (A) reject a truncated download
        if total > 0 and downloaded != total:
            raise DownloadVerificationError(
                f"Unvollständiger Download ({downloaded}/{total} Bytes) — "
                f"die Verbindung wurde vorzeitig getrennt."
            )
        # (B) reject a corrupted download against the advertised hash
        if expected and hasher.hexdigest().lower() != expected:
            raise DownloadVerificationError(
                "Prüfsumme stimmt nicht — die heruntergeladene Datei ist "
                "beschädigt.")

        return dest_path
    except Exception as exc:  # noqa: BLE001 — every failure yields a clean None
        # Report BEFORE cleaning up, so a failure in the cleanup cannot swallow
        # the reason the caller is about to show the student.
        if reason_callback is not None:
            try:
                reason_callback(_download_failure_reason_de(exc))
            except Exception:  # noqa: BLE001 — a diagnostic must not mask the failure
                pass
        # Clean up partial / failed-verification download
        try:
            os.remove(dest_path)
        except OSError:
            pass
        return None


def _download_failure_reason_de(exc: BaseException) -> str:
    """A German sentence for a failed installer download.

    The two IOErrors raised above already carry an authored German reason, so
    they are passed through verbatim. Everything else is classified by type
    rather than by its (English, often stringified-URL) message, because that
    text goes on a modal a student cannot close.
    """
    if isinstance(exc, DownloadVerificationError):
        text = str(exc).strip()
        if text:
            return text
    if isinstance(exc, urllib.error.HTTPError):
        return (f"Der Server hat die Datei nicht geliefert (Fehler "
                f"{exc.code}). Bitte später erneut versuchen.")
    if isinstance(exc, urllib.error.URLError):
        return ("Keine Verbindung zum Update-Server. Bitte die "
                "Internetverbindung prüfen.")
    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout):
        return "Zeitüberschreitung beim Download. Bitte erneut versuchen."
    if isinstance(exc, OSError):
        return ("Die Datei konnte nicht gespeichert werden — bitte "
                "freien Speicherplatz prüfen.")
    return "Der Download ist fehlgeschlagen. Bitte erneut versuchen."
