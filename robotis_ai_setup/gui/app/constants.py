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
    return "2.8.2"


# GUI version — read from repo-root VERSION file (single source of truth).
APP_VERSION = _read_version_file()

# Cloud API URL for update checks.
UPDATE_API_URL = os.environ.get(
    "EDUBOTICS_UPDATE_API_URL",
    "https://scintillating-empathy-production-1068.up.railway.app",
)

def _read_versions_env(key: str) -> str:
    """Return the value of ``key`` from ``docker/versions.env`` (the CI-baked
    pin file beside the compose file), or ``""`` if the file or key is absent.

    Resolves the file via the same install-dir walk for every key, so the GUI
    references the SAME image build (and registry) that docker-compose runs.
    The installed layout puts the GUI under ``<install>/gui/app/`` and
    versions.env under ``<install>/docker/versions.env``; a PyInstaller dist
    puts the exe under ``<install>/gui/``. We walk up from both the executable
    dir and this module's dir. ``import sys`` is lazy to avoid any module-load
    ordering surprise.
    """
    import sys
    candidates = [
        Path(os.path.dirname(os.path.abspath(sys.executable))),
        Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ]
    prefix = f"{key}="
    for start in candidates:
        d = start
        for _ in range(6):
            versions_env = d / "docker" / "versions.env"
            if versions_env.is_file():
                try:
                    for line in versions_env.read_text().splitlines():
                        line = line.strip()
                        if line.startswith(prefix):
                            val = line.split("=", 1)[1].strip()
                            if val:
                                return val
                except OSError:
                    pass
                break  # found the file in this root, key absent — try next root
            parent = d.parent
            if parent == d:
                break
            d = parent
    return ""


def _resolve_setting(env_var: str, versions_key: str, default: str) -> str:
    """Resolve a setting: env override → docker/versions.env → hardcoded default.

    This is the SAME order IMAGE_TAG has always used; sharing it means a future
    registry change ships in the CI-baked versions.env with NO GUI rebuild.
    """
    override = os.environ.get(env_var)
    if override:
        return override
    from_file = _read_versions_env(versions_key)
    if from_file:
        return from_file
    return default


# Docker image registry. PRIMARY = GHCR (public packages have no anonymous pull
# rate limit — Docker Hub's 100/6 h-per-IP 429s a 30-PC classroom on one NAT);
# FALLBACK = Docker Hub (the images are dual-pushed with byte-identical manifest
# digests), so a degraded GHCR/Fastly edge can't strand a classroom — the GUI
# pulls the fallback and re-tags it to the primary name. Both resolve
# env → versions.env → default (see _resolve_setting). Per-rig override:
# EDUBOTICS_REGISTRY / EDUBOTICS_REGISTRY_FALLBACK (e.g. =nettername to roll back
# to Docker Hub as primary with no rebuild).
REGISTRY = _resolve_setting("EDUBOTICS_REGISTRY", "REGISTRY", "ghcr.io/svendanilborodun")
REGISTRY_FALLBACK = _resolve_setting(
    "EDUBOTICS_REGISTRY_FALLBACK", "REGISTRY_FALLBACK", "nettername"
)
IMAGE_TAG = _resolve_setting("EDUBOTICS_IMAGE_TAG", "IMAGE_TAG", "latest")

# Image SHORT names (the registry-independent per-repo component). Deriving both
# the primary and fallback full refs from one list avoids split('/') munging,
# which breaks on a two-segment registry like ghcr.io/<owner>.
IMAGE_NAMES = ["open-manipulator", "physical-ai-server", "physical-ai-manager"]


def image_ref(name: str, registry: str = REGISTRY, tag: str = "") -> str:
    """Full image reference for a short ``name``. Defaults to the PRIMARY
    registry and the resolved IMAGE_TAG; pass ``registry=REGISTRY_FALLBACK`` for
    the Docker Hub twin."""
    return f"{registry}/{name}:{tag or IMAGE_TAG}"


# Docker image names — use the SAME tag docker-compose resolves so the GUI
# never accidentally pulls a newer/older image than what compose runs.
IMAGE_OPEN_MANIPULATOR = image_ref("open-manipulator")
IMAGE_PHYSICAL_AI_SERVER = image_ref("physical-ai-server")
IMAGE_PHYSICAL_AI_MANAGER = image_ref("physical-ai-manager")
ALL_IMAGES = [image_ref(n) for n in IMAGE_NAMES]

# Network ports
PORT_WEB_UI = 80
PORT_VIDEO_SERVER = 8080
PORT_ROSBRIDGE = 9090

# --- Phone-as-3rd-camera (optional, GUI-hosted HTTPS receiver) ---
# A school phone's browser captures via getUserMedia and POSTs JPEG frames over
# HTTPS to this port on the Windows host; the GUI forwards them over the EXISTING
# TCP multiplex (127.0.0.1:5557) as cam_id 2 so camera_ingest_node.py republishes
# /phone/image_raw/compressed. The phone NEVER touches WSL2/usbipd (bypasses the
# vhci_hcd USB ceiling). getUserMedia requires a secure context → HTTPS with a
# once-generated self-signed cert (Windows New-SelfSignedCertificate, user-scope).
PORT_PHONE_HTTPS = int(os.environ.get("EDUBOTICS_PHONE_HTTPS_PORT", "8444"))
# The phone is cam_id 2 — the 3rd entry, AFTER gripper(0)/scene(1). The role name
# "phone" must appear at index 2 of the container's EDUBOTICS_CAMERA_NAMES
# (gripper,scene,phone) so the published topic is /phone/image_raw/compressed.
PHONE_CAM_ID = 2
PHONE_CAMERA_NAME = "phone"
# Drop a stale phone frame rather than re-sending the last one forever: only
# forward the slot when its newest frame is younger than this (the phone posts at
# ~10 fps, so 2 s tolerates a few dropped POSTs without freezing on a dead feed).
PHONE_FRAME_STALE_MAX_AGE_S = 2.0
# Reject an oversized POST body early (a 640x480 JPEG q0.7 is ~30-80 KB; 2 MB is
# a generous ceiling that still stops a runaway upload from allocating gigabytes).
PHONE_MAX_FRAME_BYTES = 2 * 1024 * 1024


def _resolve_phone_cert_dir() -> str:
    """Directory holding the once-generated self-signed cert (cert.pem/key.pem)
    for the phone HTTPS receiver. Lives under %LOCALAPPDATA%\\EduBotics so the
    GUI can write it without admin rights; reused across runs."""
    override = os.environ.get("EDUBOTICS_PHONE_CERT_DIR")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "EduBotics", "phone-cert")


PHONE_CERT_DIR = _resolve_phone_cert_dir()

# --- Native camera bridge (WSL2/Windows student path) ---
# The two USB cameras are captured NATIVELY on Windows (the WSL2 usbipd bridge
# caps in-container UVC at ~6-10 Hz — see CLAUDE.md "native camera capture
# bridge") and streamed as JPEG over a localhost TCP socket into the
# open_manipulator container's camera_ingest_node.py, which republishes them as
# CompressedImage on /<role>/image_raw/compressed. usb_cam is used only on the
# native-Linux / Jetson path (no GUI).
CAMERA_INGEST_HOST = os.environ.get("EDUBOTICS_CAMERA_INGEST_HOST", "127.0.0.1")
PORT_CAMERA_INGEST = int(os.environ.get("EDUBOTICS_CAMERA_INGEST_PORT", "5557"))
# cam_id index -> role. MUST match the container's EDUBOTICS_CAMERA_NAMES order
# (default "gripper,scene") so cam_id 0 publishes /gripper/... and 1 /scene/...
CAMERA_BRIDGE_ROLES = ("gripper", "scene")
CAMERA_WIDTH = int(os.environ.get("EDUBOTICS_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT = int(os.environ.get("EDUBOTICS_CAMERA_HEIGHT", "480"))
# Native-bridge (Windows) nominal capture target. Measured 2026-05-24: with the
# MSMF backend + MJPG fourcc and WITHOUT requesting a resolution (which would
# force a ~12s open + a slower ~24fps mode), the Innomaker U20CAM-720P runs its
# native 640x480 MJPG mode at a true ~30 fps (both cameras concurrently). The
# event-driven sender forwards every captured frame (no rate cap), so delivery
# tracks the real capture rate (~30); the recorder subsamples to its own
# task_info.fps. NOTE: this value is now only the *nominal* target — it sets the
# DSHOW-rollback CAP_PROP_FPS request; on the MSMF path it is informational (the
# sender no longer throttles to it). To record at a LOWER fps, change the
# recorder/UI fps (task_info.fps), NOT this. The Jetson / usb_cam path uses the
# compose EDUBOTICS_CAMERA_FRAMERATE (real native USB host), independent of this.
CAMERA_FRAMERATE = float(os.environ.get("EDUBOTICS_CAMERA_FRAMERATE", "30.0"))
CAMERA_JPEG_QUALITY = int(os.environ.get("EDUBOTICS_CAMERA_JPEG_QUALITY", "80"))


def cameras_use_native_bridge() -> bool:
    """Whether cameras are captured natively on Windows (vs usb_cam-over-usbipd).

    The GUI only runs on Windows, where native capture is always the right
    choice. `EDUBOTICS_CAMERA_SOURCE=usb_cam` is the one-variable rollback to
    the old usbipd camera path (must also be set in the .env for the container).
    """
    import sys
    src = os.environ.get("EDUBOTICS_CAMERA_SOURCE", "").strip().lower()
    if src == "native_bridge":
        return True
    if src == "usb_cam":
        return False
    return sys.platform == "win32"

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


def _resolve_ros_domain_file() -> str:
    """Persisted ROS_DOMAIN_ID for this machine (plain integer, one line).

    Lives next to ENV_FILE in %LOCALAPPDATA%\\EduBotics so the GUI can write it
    without admin rights. The domain id was derived from uuid.getnode() on every
    start, but getnode() is NOT stable on multi-NIC / VPN / docking-station PCs
    (the chosen MAC, or a random fallback, can differ between sessions). A
    changed domain across sessions splits the DDS graph from any surviving
    container — the React app's rosbridge then sees no topics ("disconnected").
    Persisting the first-resolved value pins it for the life of the install.
    """
    override = os.environ.get("EDUBOTICS_ROS_DOMAIN_FILE")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "EduBotics", ".ros_domain_id")


ROS_DOMAIN_FILE = _resolve_ros_domain_file()
