"""Gemeinsame Konstanten für das EduBotics Setup."""

import os
from pathlib import Path


def _read_version_file() -> str:
    """Load the project version from the repo root `VERSION` file.

    Falls back to the baked-in default if the file is missing (e.g. when the
    GUI is running from a PyInstaller dist without the source tree beside
    it). This keeps the installer .iss, docker/versions.env, and GUI in
    sync without three manual bumps per release.
    """
    for candidate in (
        Path(__file__).resolve().parents[3] / "VERSION",  # monorepo layout
        Path(__file__).resolve().parents[2] / "VERSION",  # in-tree builds
    ):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
    return "2.3.0"


# GUI version — read from repo-root VERSION file (single source of truth).
APP_VERSION = _read_version_file()

# Cloud API URL for update checks.
UPDATE_API_URL = os.environ.get(
    "EDUBOTICS_UPDATE_API_URL",
    "https://scintillating-empathy-production-1068.up.railway.app",
)

# Docker image registry — override with EDUBOTICS_REGISTRY env var.
REGISTRY = os.environ.get("EDUBOTICS_REGISTRY", "nettername")


def _read_image_tag_from_versions_env() -> str:
    """Read IMAGE_TAG from docker/versions.env so the GUI references the
    SAME image build that docker-compose pulls.

    Resolution order:
      1. EDUBOTICS_IMAGE_TAG environment variable (escape hatch for ops)
      2. docker/versions.env next to the compose file
      3. Fallback to :latest (matches docker-compose.yml's ${IMAGE_TAG:-latest})

    Without this, the GUI's pull/health-check paths used :latest while compose
    pulled :GIT_SHA — same bytes today (build script tags both in lockstep)
    but a latent bug if a SHA build ever ships without :latest.
    """
    env_override = os.environ.get("EDUBOTICS_IMAGE_TAG")
    if env_override:
        return env_override

    # Resolve docker/versions.env via the same install-dir walk as below.
    # We import lazily to avoid a circular reference at module load time.
    from pathlib import Path
    import sys
    candidates = [
        Path(os.path.dirname(os.path.abspath(sys.executable))),
        Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ]
    for start in candidates:
        d = start
        for _ in range(6):
            versions_env = d / "docker" / "versions.env"
            if versions_env.is_file():
                try:
                    for line in versions_env.read_text().splitlines():
                        line = line.strip()
                        if line.startswith("IMAGE_TAG="):
                            return line.split("=", 1)[1].strip()
                except OSError:
                    pass
                break  # found the file, no IMAGE_TAG line — bail
            parent = d.parent
            if parent == d:
                break
            d = parent
    return "latest"


IMAGE_TAG = _read_image_tag_from_versions_env()

# Docker image names — use the SAME tag docker-compose resolves so the GUI
# never accidentally pulls a newer/older image than what compose runs.
IMAGE_OPEN_MANIPULATOR = f"{REGISTRY}/open-manipulator:{IMAGE_TAG}"
IMAGE_PHYSICAL_AI_SERVER = f"{REGISTRY}/physical-ai-server:{IMAGE_TAG}"
IMAGE_PHYSICAL_AI_MANAGER = f"{REGISTRY}/physical-ai-manager:{IMAGE_TAG}"
ALL_IMAGES = [IMAGE_OPEN_MANIPULATOR, IMAGE_PHYSICAL_AI_SERVER, IMAGE_PHYSICAL_AI_MANAGER]

# Network ports
PORT_WEB_UI = 80
PORT_VIDEO_SERVER = 8080
PORT_ROSBRIDGE = 9090

# USB identifiers
ROBOTIS_VID = "2F5D"  # ROBOTIS USB Vendor ID (OpenRB-150 boards, PIDs: 0103, 2202)

# Dynamixel servo config
BAUDRATE = 1_000_000
LEADER_SERVO_IDS = [1, 2, 3, 4, 5, 6]
FOLLOWER_SERVO_IDS = [11, 12, 13, 14, 15, 16]

# ROS2 config
ROS_DOMAIN_ID = 30

# Paths — auto-detect dev environment vs installed
def _resolve_install_dir() -> str:
    """Return the install dir: env override > dev tree > default installed path."""
    import sys
    env_dir = os.environ.get("EDUBOTICS_INSTALL_DIR")
    if env_dir:
        return env_dir

    # Walk up from both the exe location and the source file location
    # looking for a parent directory that contains docker/docker-compose.yml
    anchors = [
        os.path.dirname(os.path.abspath(sys.executable)),  # PyInstaller exe dir
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # gui/app -> gui
    ]
    for start in anchors:
        d = start
        for _ in range(6):  # Walk up at most 6 levels
            compose = os.path.join(d, "docker", "docker-compose.yml")
            if os.path.isfile(compose):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent

    return r"C:\Program Files\EduBotics"

INSTALL_DIR = _resolve_install_dir()
DOCKER_DIR = os.path.join(INSTALL_DIR, "docker")
COMPOSE_FILE = os.path.join(DOCKER_DIR, "docker-compose.yml")
COMPOSE_GPU_FILE = os.path.join(DOCKER_DIR, "docker-compose.gpu.yml")

# WSL2 distro that hosts the headless Docker Engine. Students never see the
# word "Docker" — this is the single knob the GUI uses to address the runtime.
# Override with EDUBOTICS_WSL_DISTRO for dev/testing against a different distro.
WSL_DISTRO_NAME = os.environ.get("EDUBOTICS_WSL_DISTRO", "EduBotics")


def _to_wsl_path(win_path: str) -> str:
    r"""Convert a Windows absolute path to its /mnt/<drive>/... WSL form.

    Examples:
        C:\Program Files\EduBotics\docker  →  /mnt/c/Program Files/EduBotics/docker
        C:/Users/x/.env                    →  /mnt/c/Users/x/.env
    """
    if not win_path:
        return win_path
    normalized = win_path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        rest = normalized[2:].lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return normalized


DOCKER_DIR_WSL = _to_wsl_path(DOCKER_DIR)

# User-writable .env location. We cannot write under Program Files without
# admin rights, so the .env lives in %LOCALAPPDATA%\EduBotics\.env and is
# passed to docker compose via --env-file. This avoids PermissionError when
# the GUI runs without elevation (the normal case).
def _resolve_env_file() -> str:
    override = os.environ.get("EDUBOTICS_ENV_FILE")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "EduBotics", ".env")

ENV_FILE = _resolve_env_file()

# Timeouts (seconds)
DOCKER_STARTUP_TIMEOUT = 120
DEVICE_WAIT_TIMEOUT = 30
WEB_UI_POLL_TIMEOUT = 120
WEB_UI_POLL_INTERVAL = 2

# Auto-pull / image-update behaviour. The GUI ALREADY pulls every start via
# docker_manager.check_for_updates (added in ec82916). 2.2.4 hardens that
# path with offline detection (skip the 12-min retry storm), a manifest-
# digest pre-check (skip layer pulls when local == remote), and persistent
# last-update visibility so teachers can spot stale classroom PCs.
#
# Override with EDUBOTICS_SKIP_AUTO_PULL=1 to opt out (offline classrooms
# that explicitly manage their own image cadence). Default: enabled.
NETWORK_PROBE_TIMEOUT = 5       # seconds — Docker Hub reachability check
MANIFEST_INSPECT_TIMEOUT = 10   # seconds — single remote manifest probe
IMAGE_FRESHNESS_WARN_DAYS = 14  # red banner when last successful pull is older
SKIP_AUTO_PULL = os.environ.get("EDUBOTICS_SKIP_AUTO_PULL", "").strip() in ("1", "true", "yes")


def _resolve_last_pull_file() -> str:
    """Persisted state for auto-pull: timestamp + per-image digests.

    Lives next to ENV_FILE in %LOCALAPPDATA%\\EduBotics so the GUI can write
    it without admin rights. Format: JSON {"timestamp": <unix>, "digests":
    {<image>: <sha256:abcdef...>}}.
    """
    override = os.environ.get("EDUBOTICS_LAST_PULL_FILE")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "EduBotics", ".last_image_pull.json")


LAST_PULL_FILE = _resolve_last_pull_file()
