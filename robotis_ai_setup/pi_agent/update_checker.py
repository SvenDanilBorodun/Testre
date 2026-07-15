"""Pi-agent self-update checker.

Checks the Railway Cloud API's ``/version`` endpoint for a newer release and
downloads the agent tarball if an update is available. Uses only stdlib (no pip
dependency in the agent's self-update path).

Two update surfaces exist on the Pi and they are deliberately kept separate:

  * **Container images** (`*-opi:latest`) update through
    ``docker_manager``'s arm64 digest set-membership pre-check (the jetson
    twins of ``_get_remote_digest_candidates`` / ``_parse_digest_candidates``,
    ``jetson_agent/agent.py:210-283``) — NOT here. This module never inspects
    image digests.
  * **The agent itself** updates from a SHA-256-verified GitHub-Release
    tarball (``edubotics-pi-agent.tar.gz``), which is what this module
    handles. It mirrors the Windows GUI's ``update_checker.py`` download
    gates (HEAD-precheck, Content-Length truncation reject, SHA-256 verify)
    but reads the NEW, OPTIONAL ``pi_agent_download_url`` + ``pi_agent_sha256``
    fields from ``/version``.

The ``/version`` fields ``pi_agent_download_url`` + ``pi_agent_sha256`` are
added cloud-side in P4 (deploy plan §7). Until then they are simply absent,
which this module reports as "no agent update available" (never a crash) — the
same backward-compatible posture the GUI takes toward the pre-`installer_sha256`
deploys.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request


# Asset filename produced by release.yml (deploy plan §7). Used only for the
# stale-tarball sweep below; the download URL itself is served fully-formed by
# the cloud so the agent never has to construct it.
AGENT_ASSET_NAME = "edubotics-pi-agent.tar.gz"

_NUM_RE = re.compile(r"\d+")


def _parse_version(v: str) -> tuple:
    """Best-effort numeric version tuple, padded to 3 parts, for comparison.

    Tolerant of a leading 'v', pre-release/build suffixes, and short forms:
    'v2.8.1' / '2.8.1-rc1' / '2.8.1+build' → (2, 8, 1); '2.8' → (2, 8, 0);
    non-numeric / empty → (0, 0, 0).

    Ported verbatim from the GUI's update_checker: the previous
    ``int(x) for x in v.split('.')`` raised ValueError on ANY non-purely-
    numeric segment, and the caller swallowed it as "no update" — so a single
    non-numeric release tag (e.g. an '-rc1' hotfix) would silently stop every
    Pi from EVER self-updating. Padding also fixes the (2,8) > (2,8,0) == False
    short-form asymmetry.
    """
    nums = _NUM_RE.findall(v or "")
    if not nums:
        return (0, 0, 0)
    t = tuple(int(n) for n in nums[:3])
    return t + (0,) * (3 - len(t))


def _asset_available(url: str) -> bool:
    """HEAD the agent tarball asset. Returns False ONLY when the server
    DEFINITIVELY reports it gone (404) — so a release whose tarball asset is
    missing/deleted/not-yet-propagated never advertises a broken update. Any
    other outcome (200, redirect, 405 "HEAD not allowed", or a transient
    network error) returns True: we can't prove absence, and the download
    attempt handles the rest.
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except urllib.error.HTTPError as e:
        return e.code != 404
    except Exception:
        return True


def check_for_agent_update(current_version: str, api_url: str) -> dict | None:
    """Check the cloud API for a newer pi-agent tarball.

    Returns ``{"version": "x.y.z", "download_url": "...", "sha256": "..."}`` if
    a newer release advertises a reachable agent tarball WITH a verification
    hash, or ``None`` when the agent is current, when the cloud does not
    advertise the Pi fields yet (pre-P4 deploy), when the advertised tarball
    carries NO ``pi_agent_sha256``, or on ANY error.

    The version compared is the product ``version`` string (the agent tarball
    is versioned in lockstep with the release, same as the ``.exe`` — deploy
    plan §7). ``pi_agent_download_url`` / ``pi_agent_sha256`` are read
    defensively: their absence is the ordinary pre-P4 state, not an error.

    A MISSING/EMPTY ``pi_agent_sha256`` reports "no update" on purpose: a root
    process must never ``extractall`` unverified bytes, so ``agent.py``'s apply
    path REFUSES a hash-less tarball while still marking the job succeeded.
    Advertising availability the apply side can never honour produced a
    perpetual, unskippable forced-update modal (W6 publishes PI_AGENT_SHA256
    empty-on-failure). Aligning the AVAILABILITY criterion here with the APPLY
    criterion means a degraded release with no hash advertises no update instead.
    """
    url = f"{api_url.rstrip('/')}/version"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        remote_version = data.get("version", "")
        # OPTIONAL, additive fields (P4). Absent on today's deploy → no agent
        # update advertised. Never dereference blindly.
        download_url = data.get("pi_agent_download_url") or ""
        if not remote_version or not download_url:
            return None
        if _parse_version(remote_version) > _parse_version(current_version):
            # No verification hash → the apply path can never apply it; do not
            # advertise it as available (checked BEFORE the HEAD probe so a
            # hash-less advertisement costs no network round-trip).
            sha256 = (data.get("pi_agent_sha256") or "").strip()
            if not sha256:
                return None
            if not _asset_available(download_url):
                return None
            return {
                "version": remote_version,
                "download_url": download_url,
                "sha256": sha256,
            }
    except Exception:
        return None
    return None


def cleanup_stale_tarballs(max_age_hours: int = 24) -> int:
    """Delete leftover agent tarballs from past self-updates.

    The tarball is downloaded into a temp dir, unpacked over the install, then
    the agent restarts — so the download file is swept on the NEXT start.
    Anything older than ``max_age_hours`` is removed so stale tarballs don't
    pile up. Returns the number of files removed.
    """
    patterns = [
        os.path.join(tempfile.gettempdir(), AGENT_ASSET_NAME),
        os.path.join(tempfile.gettempdir(), "edubotics-pi-agent*.tar.gz"),
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
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    return removed


def download_agent_tarball(url: str, dest_dir: str | None = None,
                           progress_callback=None,
                           expected_sha256: str | None = None) -> str | None:
    """Download the agent tarball, verifying integrity before it is unpacked.

    Args:
        url: Public URL of the ``edubotics-pi-agent.tar.gz`` asset.
        dest_dir: Directory to save the file (defaults to system temp).
        progress_callback: Optional callable(bytes_downloaded, total_bytes).
        expected_sha256: Optional lowercase-hex SHA-256 the download must match.

    Two integrity gates before the tarball is handed back to be unpacked over
    the running agent (mirrors the GUI's ``download_installer``):
      (A) Content-Length — a short/truncated download (server closed the
          connection early; the read loop ends "successfully" with a partial
          file) is rejected instead of unpacking a broken agent.
      (B) SHA-256 — when the cloud /version advertised a hash, the bytes must
          match it (corruption / TLS-inspection-tamper guard). A None/empty
          hash skips this gate (old deploys), so it's backward-compatible.

    Returns:
        Full path to the verified file, or None on failure (partial file
        cleaned up).
    """
    if dest_dir is None:
        dest_dir = tempfile.gettempdir()
    dest_path = os.path.join(dest_dir, AGENT_ASSET_NAME)
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
            raise IOError(
                f"unvollständiger Download ({downloaded}/{total} Bytes)"
            )
        # (B) reject a corrupted download against the advertised hash
        if expected and hasher.hexdigest().lower() != expected:
            raise IOError("Prüfsumme stimmt nicht (beschädigter Download)")

        return dest_path
    except Exception:
        # Clean up partial / failed-verification download
        try:
            os.remove(dest_path)
        except OSError:
            pass
        return None
