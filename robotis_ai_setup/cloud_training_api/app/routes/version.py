"""Version endpoint consumed by the in-tree GUI's update_checker AND the
Orange Pi agent's update_checker.

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

  4. `download_url` auto-derives from GUI_VERSION + GUI_RELEASE_REPO when
     GUI_DOWNLOAD_URL is unset. The release pipeline (release.yml W6
     `publish-gui-version`) sets BOTH vars explicitly after the installer
     asset is uploaded, so this derivation is a defensive backstop — it
     removes GUI_DOWNLOAD_URL as a SEPARATELY-driftable surface. The GH
     Release asset URL is deterministic from the tag:
       https://github.com/<repo>/releases/download/v<version>/EduBotics_Setup.exe

  5. OPTIONAL, ADDITIVE Orange Pi fields `pi_agent_download_url` +
     `pi_agent_sha256` (deploy plan §7). The Pi agent's self-update
     (`pi_agent/update_checker.py`) reads these EXACT keys to fetch the
     `edubotics-pi-agent.tar.gz` release asset. They are derived the SAME
     way as the `.exe` pair: the download URL from GUI_VERSION +
     GUI_RELEASE_REPO + the fixed asset name; the hash from PI_AGENT_SHA256
     (set by release.yml W6 AFTER it hashes the exact attached tarball,
     empty-on-failure so it is never stale). Old GUIs simply ignore the
     extra keys — the response stays backward-compatible.
"""

import os

from fastapi import APIRouter

router = APIRouter()

# Asset filename produced by release-installer.yml's softprops upload.
_INSTALLER_ASSET = "EduBotics_Setup.exe"

# Asset filename produced by release.yml's pi-agent-tarball job (deploy plan
# §7). Must match pi_agent/update_checker.py::AGENT_ASSET_NAME byte-for-byte —
# the agent trusts the download URL the cloud serves and never reconstructs it.
_PI_AGENT_ASSET = "edubotics-pi-agent.tar.gz"


def _resolve_commit() -> str:
    """Mirror health.py's resolver — Railway SHA → BUILD_COMMIT → unknown."""
    for key in ("RAILWAY_GIT_COMMIT_SHA", "BUILD_COMMIT"):
        val = os.environ.get(key)
        if val:
            return val
    return "unknown"


def _resolve_installer_sha256() -> str | None:
    """The SHA-256 of the advertised EduBotics_Setup.exe, set by release.yml W6
    (publish-gui-version) after it hashes the just-attached GH-Release asset.

    Lets the GUI verify integrity of the downloaded installer before launching
    it (corruption/truncation guard). Optional + backward-compatible: when
    unset (old deploy, or W6 didn't run) the GUI simply skips the hash check
    and keeps the Content-Length truncation guard. Normalised to lowercase hex;
    a malformed value returns None so the GUI doesn't reject a valid download
    against garbage.
    """
    val = (os.environ.get("GUI_INSTALLER_SHA256") or "").strip().lower()
    if len(val) == 64 and all(c in "0123456789abcdef" for c in val):
        return val
    return None


def _resolve_download_url(version: str | None) -> str | None:
    """Explicit GUI_DOWNLOAD_URL wins; else derive the GH Release asset URL
    from GUI_VERSION + GUI_RELEASE_REPO (owner/repo). Returns None when
    neither is available so the GUI treats it as 'no update'.

    Deriving keeps the version string and its download URL from drifting
    apart: a single GUI_VERSION bump is enough for the update gate to point
    at the right `.exe`, instead of two env vars that can disagree.
    """
    explicit = os.environ.get("GUI_DOWNLOAD_URL")
    if explicit:
        return explicit
    repo = os.environ.get("GUI_RELEASE_REPO")
    if version and repo:
        return (
            f"https://github.com/{repo}/releases/download/"
            f"v{version}/{_INSTALLER_ASSET}"
        )
    return None


def _resolve_pi_agent_sha256() -> str | None:
    """The SHA-256 of the advertised edubotics-pi-agent.tar.gz, set by
    release.yml W6 (publish-gui-version) after it hashes the just-attached
    GH-Release asset.

    Lets pi_agent/update_checker.py verify the tarball before unpacking it over
    the running agent (corruption / TLS-inspection-tamper guard — the exact
    failure the §5 Netzwerk-Check names). Optional + backward-compatible: when
    unset (old deploy, or W6 didn't run) the agent skips the hash check and
    keeps its Content-Length truncation guard. Normalised to lowercase hex; a
    malformed value returns None so the agent doesn't reject a valid download
    against garbage. Mirrors _resolve_installer_sha256's contract exactly.
    """
    val = (os.environ.get("PI_AGENT_SHA256") or "").strip().lower()
    if len(val) == 64 and all(c in "0123456789abcdef" for c in val):
        return val
    return None


def _resolve_pi_agent_download_url(version: str | None) -> str | None:
    """Derive the GH Release asset URL for the pi-agent tarball from
    GUI_VERSION + GUI_RELEASE_REPO (owner/repo). Returns None when either is
    unavailable so the agent treats it as 'no update'.

    Unlike the .exe there is NO explicit override env var — the tarball's URL
    is ALWAYS derived (deploy plan §7: "derived the same way as the .exe pair,
    from release repo + version + fixed asset name"), so the advertised Pi
    version and its download asset can never disagree.
    """
    repo = os.environ.get("GUI_RELEASE_REPO")
    if version and repo:
        return (
            f"https://github.com/{repo}/releases/download/"
            f"v{version}/{_PI_AGENT_ASSET}"
        )
    return None


@router.get("/version")
async def get_latest_version():
    """Return the latest GUI version and download URL.

    Configured via Railway environment variables:
      GUI_VERSION      — e.g. "2.1.0" (the publish gate; advertise only
                         after the matching .exe asset exists)
      GUI_DOWNLOAD_URL — public URL to the installer .exe (optional; when
                         unset, derived from GUI_VERSION + GUI_RELEASE_REPO)
      GUI_RELEASE_REPO — "owner/repo" used to derive the download URL

    When GUI_VERSION is unset the response is still 200 with null values so
    the GUI's update_checker treats it as "no update available" instead
    of failing closed on a 503.
    """
    version = os.environ.get("GUI_VERSION") or None
    download_url = _resolve_download_url(version)

    return {
        "version": version,
        "download_url": download_url,
        "installer_sha256": _resolve_installer_sha256(),
        "commit": _resolve_commit(),
        # OPTIONAL, ADDITIVE (deploy plan §7) — the Orange Pi agent's
        # self-update reads these EXACT keys. Absent-as-None on today's
        # deploy → the agent reports "no agent update available". Old GUIs
        # ignore them, so the payload stays backward-compatible.
        "pi_agent_download_url": _resolve_pi_agent_download_url(version),
        "pi_agent_sha256": _resolve_pi_agent_sha256(),
    }
