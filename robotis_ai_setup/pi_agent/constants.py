"""Shared constants for the EduBotics Pi-Agent (Orange Pi 5 Pro, arm64).

Port of ``robotis_ai_setup/gui/app/constants.py`` with every Windows/WSL2
bit removed:

  - No ``%LOCALAPPDATA%`` path defaults — the agent runs as root under
    systemd, so state lives under fixed Linux paths (``/etc/edubotics``,
    ``/var/lib/edubotics``, ``/opt/edubotics``), NOT ``~/.config`` (root's
    ``~`` is ``/root``).
  - No ``sys.platform == "win32"`` camera fallback — the Pi is ALWAYS the
    native ``usb_cam`` path (there is no capture bridge on :5557).
  - No ``sys.executable``-relative ``versions.env`` walk (that covered a
    PyInstaller dist layout that does not exist here) and no WSL distro
    name / ``_to_wsl_path`` conversion.

What is KEPT (platform-neutral): the registry resolution order (env
override → ``docker/versions.env`` → default) shared by ``REGISTRY`` /
``REGISTRY_FALLBACK`` / ``IMAGE_TAG``, the image short-name → full-ref
derivation, the ROBOTIS USB ids and Dynamixel servo config, and the
network/auto-pull timing knobs.
"""

import os
from pathlib import Path


def _read_version_file() -> str:
    """Load the product version from a ``VERSION`` file.

    The FIRST candidate is a ``VERSION`` packaged BESIDE this module (the
    ``pi_agent/`` package dir). This matters for self-update: the agent tarball
    ships ``pi_agent/VERSION`` and the ``rsync --delete`` apply refreshes it, so
    preferring it means a self-updated agent reports the NEW version instead of
    re-downloading the update forever (the ``/opt/edubotics/VERSION`` copy
    setup.sh lays down for bench installs is never refreshed by self-update).
    Falls back to the in-tree repo-root layout, then the installed
    ``/opt/edubotics/VERSION``, then the baked-in default.
    """
    override = os.environ.get("EDUBOTICS_VERSION")
    if override:
        return override.strip()
    here = Path(__file__).resolve()
    for candidate in (
        here.parent / "VERSION",         # packaged beside the agent — self-update refreshes THIS
        here.parents[2] / "VERSION",     # repo root (in-tree dev checkout)
        Path("/opt/edubotics/VERSION"),  # installed bench layout (setup.sh copies repo VERSION here)
        here.parents[1] / "VERSION",     # robotis_ai_setup/VERSION (defensive)
    ):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
    return "2.13.0"


# Agent/product version — reported by the /status endpoint and used by the
# self-update check to decide whether a newer release is advertised.
APP_VERSION = _read_version_file()

# Cloud API URL for the update check (`/version`). Same host the Windows
# GUI + the Jetson agent use; override for dev/testing.
UPDATE_API_URL = os.environ.get(
    "EDUBOTICS_UPDATE_API_URL",
    "https://scintillating-empathy-production-1068.up.railway.app",
)


def _read_versions_env(key: str) -> str:
    """Return ``key`` from ``docker/versions.env`` (the CI-baked pin file
    beside the compose file), or ``""`` when the file/key is absent.

    Unlike the GUI reader this walks up ONLY from this module's directory
    (there is no PyInstaller ``sys.executable`` layout on the Pi). In-tree
    the file resolves at ``robotis_ai_setup/docker/versions.env``; on an
    installed Pi it is normally absent (CI-generated), so every reader
    falls back to the hardcoded default — exactly the documented behaviour.
    """
    prefix = f"{key}="
    d = Path(__file__).resolve().parent
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
            break  # found the file in this root, key absent — stop
        parent = d.parent
        if parent == d:
            break
        d = parent
    return ""


def _resolve_setting(env_var: str, versions_key: str, default: str) -> str:
    """Resolve a setting: env override → ``docker/versions.env`` → default.

    The SAME order ``IMAGE_TAG`` has always used; sharing it means a future
    registry change ships in the CI-baked ``versions.env`` with no agent
    rebuild.
    """
    override = os.environ.get(env_var)
    if override:
        return override
    from_file = _read_versions_env(versions_key)
    if from_file:
        return from_file
    return default


# Docker image registry. PRIMARY = GHCR (public packages have no anonymous
# pull rate limit); FALLBACK = Docker Hub (images are dual-pushed with
# byte-identical manifest digests). Both resolve env → versions.env →
# default. One-variable rollback to Hub as primary: EDUBOTICS_REGISTRY=nettername.
REGISTRY = _resolve_setting("EDUBOTICS_REGISTRY", "REGISTRY", "ghcr.io/svendanilborodun")
REGISTRY_FALLBACK = _resolve_setting(
    "EDUBOTICS_REGISTRY_FALLBACK", "REGISTRY_FALLBACK", "nettername"
)
IMAGE_TAG = _resolve_setting("EDUBOTICS_IMAGE_TAG", "IMAGE_TAG", "latest")

# Image SHORT names — the Orange Pi (`-opi`) flavour built by P1. Deriving
# both primary and fallback full refs from one list avoids split('/')
# munging, which breaks on a two-segment registry like ghcr.io/<owner>.
IMAGE_NAMES = [
    "open-manipulator-opi",
    "physical-ai-server-opi",
    "physical-ai-manager-opi",
]


def image_ref(name: str, registry: str = REGISTRY, tag: str = "") -> str:
    """Full image reference for a short ``name``. Defaults to the PRIMARY
    registry and the resolved ``IMAGE_TAG``; pass ``registry=REGISTRY_FALLBACK``
    for the Docker Hub twin."""
    return f"{registry}/{name}:{tag or IMAGE_TAG}"


# Docker image names — use the SAME tag the opi compose resolves so the
# agent never pulls a newer/older image than what compose runs.
IMAGE_OPEN_MANIPULATOR = image_ref("open-manipulator-opi")
IMAGE_PHYSICAL_AI_SERVER = image_ref("physical-ai-server-opi")
IMAGE_PHYSICAL_AI_MANAGER = image_ref("physical-ai-manager-opi")
ALL_IMAGES = [image_ref(n) for n in IMAGE_NAMES]

# --- Network ports (all native; no usbipd, no WSL localhost forwarder) ---
PORT_WEB_UI = 80            # physical_ai_manager (nginx, serves the SPA)
PORT_VIDEO_SERVER = 8080    # web_video_server (camera streams)
PORT_ROSBRIDGE = 9090       # rosbridge (unauthenticated — LAN-open by decision)
# camera-ingest TCP server (camera_ingest_node.py) — UNUSED on the Pi. The Pi
# runs the in-container usb_cam path, so there is no native capture bridge
# feeding :5557. Kept documented only so the port map is complete.
PORT_CAMERA_INGEST = 5557
# The management API HTTP server. Bound to 127.0.0.1 AND the docker ros_net
# gateway IP (never the LAN NIC); the browser reaches it only through the
# manager's same-origin /api/system reverse proxy.
PORT_AGENT = 8769
# Phone-as-3rd-camera HTTPS receiver. On the Pi this is a PREVIEW-ONLY backend
# (no ROS republish — the usb_cam path has no camera_ingest consumer; OPEN item,
# see ORANGE_PI_DEPLOY_PLAN.md §6). Kept so the receiver module can bind it.
PORT_PHONE_HTTPS = int(os.environ.get("EDUBOTICS_PHONE_HTTPS_PORT", "8444"))

# --- Phone camera (preview-only backend on the Pi) ---
PHONE_CAM_ID = 2
PHONE_CAMERA_NAME = "phone"
PHONE_FRAME_STALE_MAX_AGE_S = 2.0
PHONE_MAX_FRAME_BYTES = 2 * 1024 * 1024


def _resolve_phone_cert_dir() -> str:
    """Directory holding the once-generated self-signed cert (cert.pem/key.pem)
    for the phone HTTPS receiver. Under /etc/edubotics (root-writable, survives
    reboots); minted with ``openssl`` on the Pi (not New-SelfSignedCertificate)."""
    override = os.environ.get("EDUBOTICS_PHONE_CERT_DIR")
    if override:
        return override
    return "/etc/edubotics/phone-cert"


PHONE_CERT_DIR = _resolve_phone_cert_dir()

# cam_id index -> role, gripper=0 / scene=1 (usb_cam publishes /<role>/... via
# CAMERA_NAME_*). The Pi never adds "phone" to this list (no ROS wiring).
CAMERA_BRIDGE_ROLES = ("gripper", "scene")

# USB identifiers — both OpenMANIPULATOR arms are OpenRB-150 boards.
ROBOTIS_VID = "2F5D"  # ROBOTIS USB Vendor ID (OpenRB-150; PIDs 0103, 2202)

# Dynamixel servo config.
BAUDRATE = 1_000_000
LEADER_SERVO_IDS = [1, 2, 3, 4, 5, 6]
FOLLOWER_SERVO_IDS = [11, 12, 13, 14, 15, 16]

# ROS 2 config — legacy default domain, used only as the last-resort fallback
# in config_generator._resolve_ros_domain_id (the real value is derived from
# /etc/machine-id, see that function).
ROS_DOMAIN_ID = 30

# --- LAN exposure + docker network (Orange Pi specifics) ---
# EDUBOTICS_LAN_OPEN maps to EDUBOTICS_BIND_HOST: "1" → 0.0.0.0 (published
# ports reachable from the school LAN, the locked default), "0" → 127.0.0.1
# (kiosk mode with a local monitor). config_generator derives BIND_HOST.
# "0" binds the MANAGER's :80 to loopback too, so it is kiosk-only — never a
# hardening knob (see the docker-compose.opi.yml ports comment). No code path
# sets it: it is an operator hand-edit of the .env.
DEFAULT_LAN_OPEN = "1"
# ros_net IPAM subnet. Configurable because 172.16.0.0/12 is common
# institutional space and an overlap blackholes container→LAN routing (cloud
# API unreachable from the server container). setup.sh (P4) records a free
# range; the gateway is DERIVED (first host, e.g. .1) by config_generator.
#
# SCOPE (measured): the AUTOMATIC check is bench-only. setup.sh probes
# `ip -4 route` at PROVISIONING time, the choice freezes into the golden image,
# and every clone's first boot carries it forward verbatim (it cannot re-probe —
# it runs Before=network-online.target). So this dodges the BENCH's LAN, never
# the deployment school's.
#
# A MANUAL relocation on a deployed rig DOES work — compose re-IPAMs ros_net on
# `up -d --force-recreate --no-deps physical_ai_manager`, the derived gateway
# follows, and _gateway_binder_loop rebinds (it re-reads the gateway each tick).
# But ONLY with the robot tier STOPPED. With it running, that same command stops
# the manager, fails to remove the network ("has active endpoints"), exits
# non-zero and leaves the manager `exited` — restart:unless-stopped does not
# help (explicit stop), and a retry fails differently ("container is not
# connected to the network") and never recovers. The Pi's only UI is gone until
# someone intervenes by hand. Stop the robot tier first, or re-provision.
DEFAULT_ROS_NET_SUBNET = "172.28.0.0/24"
DEFAULT_ROS_NET_GATEWAY = "172.28.0.1"  # documentation only — derived from subnet

# --- Runtime paths (root systemd agent) ---
# Managed .env — the compose interface (`--env-file`). NOT ~/.config: the agent
# runs as root, and the systemd unit's EnvironmentFile points here too.
ENV_FILE = os.environ.get("EDUBOTICS_ENV_FILE", "/etc/edubotics/.env")

# The opi compose file the agent drives (native `docker compose -f`, no WSL
# wrapper). Relative bind-mounts in it (`./physical_ai_server/.s6-keep`)
# resolve against this file's directory, so setup.sh (P4) lays the tree out
# under /opt/edubotics accordingly.
COMPOSE_FILE = os.environ.get(
    "EDUBOTICS_OPI_COMPOSE", "/opt/edubotics/docker-compose.opi.yml"
)

# Persisted auto-pull state: timestamp + per-image RepoDigests, for the
# freshness banner ("Letzter Image-Update: vor X Tagen").
LAST_PULL_FILE = os.environ.get(
    "EDUBOTICS_LAST_PULL_FILE", "/var/lib/edubotics/.last_image_pull.json"
)

# Persisted per-machine ROS_DOMAIN_ID. On the Pi /etc/machine-id is stable, so
# this file is belt-and-suspenders (and the store setup.sh can seed), but it
# keeps the resolution logic identical to the GUI.
ROS_DOMAIN_FILE = os.environ.get(
    "EDUBOTICS_ROS_DOMAIN_FILE", "/var/lib/edubotics/.ros_domain_id"
)

# Source of the stable machine identifier for the ROS_DOMAIN_ID derivation
# (hash mod 233), matching the Jetson setup.sh convention. uuid.getnode() is
# the fallback when this is unreadable.
MACHINE_ID_FILE = os.environ.get("EDUBOTICS_MACHINE_ID_FILE", "/etc/machine-id")

# --- Timeouts (seconds) ---
DOCKER_STARTUP_TIMEOUT = 120
DEVICE_WAIT_TIMEOUT = 30
WEB_UI_POLL_TIMEOUT = 120
WEB_UI_POLL_INTERVAL = 2

# --- Auto-pull / image-update behaviour ---
# Override EDUBOTICS_SKIP_AUTO_PULL=1 to opt out (offline classrooms managing
# their own image cadence).
NETWORK_PROBE_TIMEOUT = 5       # seconds — registry reachability TCP probe
MANIFEST_INSPECT_TIMEOUT = 30   # seconds — single remote manifest probe (arm64)
IMAGE_FRESHNESS_WARN_DAYS = 14  # red banner when last successful pull is older
SKIP_AUTO_PULL = os.environ.get("EDUBOTICS_SKIP_AUTO_PULL", "").strip() in ("1", "true", "yes")
