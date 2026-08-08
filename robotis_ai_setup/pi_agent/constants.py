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


# Set True by ``_read_version_file`` when APP_VERSION came from an EXPLICIT
# source (a VERSION file on disk, or the EDUBOTICS_VERSION override) rather than
# the baked-in last-resort literal below. ``_default_image_tag`` consults it: a
# version we actually READ is a fact about this install, whereas the literal is
# only "whatever this source tree was cut from" and must not be promoted into an
# image pin.
_VERSION_IS_AUTHORITATIVE = False


def _read_version_file() -> str:
    """Load the product version from a ``VERSION`` file.

    The FIRST candidate is a ``VERSION`` packaged BESIDE this module (the
    ``pi_agent/`` package dir). This matters for self-update: the agent tarball
    ships ``pi_agent/VERSION`` and the apply swaps in a tree carrying it, so
    preferring it means a self-updated agent reports the NEW version instead of
    re-downloading the update forever (the ``/opt/edubotics/VERSION`` copy
    setup.sh lays down for bench installs is never refreshed by self-update).
    Falls back to the in-tree repo-root layout, then the installed
    ``/opt/edubotics/VERSION``, then the baked-in default.
    """
    global _VERSION_IS_AUTHORITATIVE
    override = os.environ.get("EDUBOTICS_VERSION")
    if override:
        _VERSION_IS_AUTHORITATIVE = True
        return override.strip()
    here = Path(__file__).resolve()
    for candidate in (
        here.parent / "VERSION",         # packaged beside the agent — self-update refreshes THIS
        here.parents[2] / "VERSION",     # repo root (in-tree dev checkout)
        Path("/opt/edubotics/VERSION"),  # installed bench layout (setup.sh copies repo VERSION here)
        here.parents[1] / "VERSION",     # robotis_ai_setup/VERSION (defensive)
    ):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        # An EMPTY/whitespace VERSION file (a truncated write, a half-finished
        # rsync) used to return "" verbatim and make APP_VERSION empty. Harmless
        # while the version was only reported, but _default_image_tag now derives
        # an image PIN from it — an empty tag would resolve `…-opi:` and fail
        # every pull. Treat a blank file as unreadable and keep walking.
        if value:
            _VERSION_IS_AUTHORITATIVE = True
            return value
    return "2.15.0"


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
    (there is no PyInstaller ``sys.executable`` layout on the Pi). The FIRST
    candidate is ``pi_agent/docker/versions.env`` — the path BOTH writers target
    (``release.yml::pi-agent-tarball`` bakes it into the self-update tarball,
    ``setup.sh`` writes it at provision time), so it must stay first in the walk.
    Further up, in-tree, the file would resolve at
    ``robotis_ai_setup/docker/versions.env``.

    NEAREST-WINS PER KEY, not per file: the walk continues past a versions.env
    that does not carry the requested key. Stopping at the first FILE looked
    equivalent and is not — ``setup.sh`` writes a ``pi_agent/docker/versions.env``
    containing ONLY ``IMAGE_TAG``, so a future ``REGISTRY`` /
    ``REGISTRY_FALLBACK`` pin shipped one level up would be permanently
    invisible on every provisioned Pi, quietly falsifying the "a future registry
    change ships in the CI-baked versions.env with no agent rebuild" promise in
    ``_resolve_setting``. A key that IS present still wins over anything farther
    up, which is the property the original break was reaching for.
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
            # Key absent (or blank) in THIS file — keep walking rather than
            # concluding it is unset everywhere.
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


def _default_image_tag() -> str:
    """Last-resort ``IMAGE_TAG`` when neither the env override nor a baked
    ``docker/versions.env`` supplies one.

    The Pi deliberately does NOT default to ``latest`` the way the GUI does.
    ``docker-publish.yml`` pushes ``:latest`` on every push to ``main``, so a Pi
    that resolves ``latest`` silently tracks main HEAD instead of its release —
    and there is no version handshake anywhere to catch it. That is exactly the
    hole ``release.yml::pi-agent-tarball`` closes by baking ``IMAGE_TAG=<VERSION>``
    into ``pi_agent/docker/versions.env``; this is the defense-in-depth behind
    it, for the day that file goes missing (a partial tarball, a hand-copied
    tree, an operator cleaning up "generated" files). Degrading to the agent's
    OWN product version keeps the pin honest: the images this release ships are
    tagged with exactly that, so a missing pin file resolves to the same tag the
    pin file would have named — and if those images were never published the Pi
    now fails LOUDLY (PULL_MISSING names the version) instead of quietly running
    main HEAD.

    Only an APP_VERSION we actually READ counts (``_VERSION_IS_AUTHORITATIVE``).
    Falling back to the baked-in literal would pin every version-file-less tree
    to whatever release this source was cut from — a confidently WRONG pin, which
    is worse than ``latest``'s honest "I don't know". With no version source at
    all, keep the old behaviour.
    """
    if _VERSION_IS_AUTHORITATIVE and APP_VERSION:
        return APP_VERSION
    return "latest"


IMAGE_TAG = _resolve_setting("EDUBOTICS_IMAGE_TAG", "IMAGE_TAG", _default_image_tag())

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

# Arm-family USB identity table — the Pi twin of `gui/app/constants.py`'s
# ARM_USB_IDS (edu6 §4.4), kept equal by tests/test_constants.py. Keys are the
# ARM FAMILY (what the scan hardware-matches), NOT the profile id — both OMX
# profiles share one family. Values: (VID, PID-or-None) tuples, None = any PID
# under that VID. The edu6 bridge is the WCH CH343P on the Waveshare Bus Servo
# Adapter; identity is still PROVEN by the SERVOS answering
# (`identify_arm.py --protocol=feetech`), never by the bridge chip.
#
# WHERE IT IS CONSUMED DIFFERS FROM WINDOWS, deliberately. There the table
# scopes the usbipd attach (`usbipd attach --busid ...`). A Pi has no attach
# step at all — every arm is already linked under /dev/serial/by-id by native
# udev — so the Pi's family scoping is purely the by-id substring filter in
# `identify_arm._ARM_MARKERS`. This table is therefore the shared IDENTITY
# statement of what hardware each family is: the authority the udev
# permission-floor rule mirrors (`udev/99-edubotics-robotis.rules`) and the
# anchor that stops the two platforms drifting apart.
ARM_USB_IDS = {
    "omx":  (("2F5D", None),),
    "edu6": (("1A86", "55D3"),),
}

# --- Robot type (ArmProfile) registry — the Pi's THIN descriptor ---
# The robot type is a wizard-time, HARDSET decision baked into the managed .env
# (MANAGED key EDUBOTICS_ROBOT_TYPE) before „Umgebung starten"; only a full
# restart of the robot tier changes it. This mirrors the Windows thin descriptor
# (`gui/app/constants.py::ROBOT_PROFILES`) row for row, and both mirror the
# AUTHORITATIVE server registry (physical_ai_server/robot_profiles.py). The
# cross-boundary contract is enforced by tests/test_robot_profile_lockstep.py,
# but NOT every field spans the same number of copies — do not say "all three"
# of a field that has only two:
#   THREE copies (gui ↔ server AND gui ↔ pi): the profile-id SET,
#     `follower_only`, `camera_roles`, and the default profile id.
#   TWO copies (the thin descriptors only): `arm_family` — the server has NO
#     such field (`grep -c arm_family robot_profiles.py` → 0), because it never
#     scans USB; the field scopes the usbipd VID/PID attach on Windows and the
#     /dev/serial/by-id filter + prober protocol here. Its test says so in its
#     own name: `test_arm_family_agrees_across_the_two_thin_descriptors`.
#     `display_de` is also two-copy, and only checked non-empty — the two
#     platforms are free to word a label differently.
# The server owns capabilities/kinematics; the Pi (like the GUI) needs only the
# display label plus the scan/leader/camera flags.
#
#   - omx_full     → both arms; Roboter Studio via the mid-session LeaderToggle.
#   - omx_follower → follower only; no leader, LeaderToggle hidden, RS native.
#   - edu6_studio  → the 6-DOF Feetech arm; follower-only, RS only.
#
# `display_de` is HARD-mandatory (it is direct-subscripted when the wizard's
# profile list is built — an absent key must fail loudly, not degrade to a blank
# label). `scan_requires_leader` drives the follower-only scan surface;
# `follower_only` is the INITIAL EDUBOTICS_FOLLOWER_ONLY the .env generator
# DERIVES from the type. `arm_family` scopes the /dev/serial/by-id filter and the
# in-container prober protocol (`identify_arm`).
#
# `camera_roles` is the profile's ALLOWLIST of camera roles, and on the Pi it is
# live in two places: it rides `/status.robot_profiles[].camera_roles` (which is
# how `SystemPage.js` knows which `<option>`s to offer) and it is the set
# `agent.handle_cameras_roles` validates an incoming role against. Both halves
# exist because either alone is bypassable — a stale cached bundle would still
# offer the wrong role, and a hand-crafted POST would still be accepted.
#
# Why it must be an allowlist and not a hint: the server's per-profile
# `config/<type>_config.yaml` declares the camera TOPICS by role name, and
# `edu6_studio` declares exactly one (`scene:/scene/image_raw/compressed`).
# Naming that camera `gripper` publishes `/gripper/image_raw/compressed`, which
# no consumer subscribes to — while the opi compose healthcheck greps the very
# topic the student just named, so it goes GREEN, „Umgebung starten" reports
# success in German, and Roboter Studio then shows nothing with no diagnosis
# pointing back here. It also feeds the GUI's lone-camera auto-assign
# (`gui_app._on_cameras_changed` reads `camera_roles[0]`), where „gripper" on a
# follower-only kit was the original scar (CLAUDE.md). The Pi has NO such
# auto-assign — a role-less camera is a German 400, never a guess (the two
# Innomakers are identical-serial, so only the live preview tells them apart).
#
# ORDER is meaningful to the GUI (`[0]` is the lone-camera default) and NOT to
# the Pi (which tests membership), which is why `omx_full` and `omx_follower`
# carry the same two roles in opposite order and both accept both.
# The three-way lockstep (server ArmProfile, GUI twin, this row) is fenced by
# tests/test_robot_profile_lockstep.py::test_camera_roles_agree_per_id.
#
# Values are COPIED VERBATIM from gui/app/constants.py — never re-derived.
ROBOT_PROFILES = {
    "omx_full":     {"display_de": "OMX – Voll",                          "follower_only": False, "scan_requires_leader": True,  "arm_family": "omx",  "camera_roles": ("gripper", "scene")},
    "omx_follower": {"display_de": "OMX – Roboter Studio (nur Follower)", "follower_only": True,  "scan_requires_leader": False, "arm_family": "omx",  "camera_roles": ("scene", "gripper")},
    "edu6_studio":  {"display_de": "EduBotics 6-Achs – Roboter Studio",   "follower_only": True,  "scan_requires_leader": False, "arm_family": "edu6", "camera_roles": ("scene",)},
}
DEFAULT_ROBOT_PROFILE = "omx_full"

# The German label for each camera role, in the words the wizard's own dropdown
# uses — so a refusal names the role the student just picked, not its wire id.
# Every profile's `camera_roles` must be a subset of these keys: a role outside
# them could be offered by the SPA yet never accepted by `handle_cameras_roles`,
# which rejects unknown roles before it ever consults the profile.
# Fenced by tests/test_lifecycle.py::TestCameraRoleMustSuitTheProfile
# ::test_every_profiles_allowlist_is_a_subset_of_the_known_roles. Cited by
# SYMBOL, not by line: an earlier revision of this comment cited a
# `tests/test_camera_role_allowlist.py` that has never existed in the tree, and
# a dead citation reads as "this invariant is unfenced" — strictly worse than
# no citation, because it stops the next reader looking for the real guard.
CAMERA_ROLE_LABELS_DE = {"gripper": "Greifer", "scene": "Szene"}

# DERIVED, not restated: the default profile's family is the family an
# unknown/absent EDUBOTICS_ROBOT_TYPE falls back to.
DEFAULT_ARM_FAMILY = ROBOT_PROFILES[DEFAULT_ROBOT_PROFILE]["arm_family"]


def resolve_robot_type(robot_type) -> str:
    """Validate a managed ``EDUBOTICS_ROBOT_TYPE`` value against the registry.

    Unknown / absent / blank / mis-cased → :data:`DEFAULT_ROBOT_PROFILE`,
    matching the server registry's ``robot_profiles.resolve``: a bad .env value
    must keep today's OMX behaviour rather than make the rig unusable. That
    fallback IS the documented one-variable rollback — it never raises.
    """
    key = (robot_type or "").strip()
    return key if key in ROBOT_PROFILES else DEFAULT_ROBOT_PROFILE


def robot_profile(robot_type) -> dict:
    """The registry row for a managed ``EDUBOTICS_ROBOT_TYPE`` value (validated
    through :func:`resolve_robot_type`, so this never raises KeyError)."""
    return ROBOT_PROFILES[resolve_robot_type(robot_type)]


def arm_family_for_robot_type(robot_type) -> str:
    """Arm family for a managed ``EDUBOTICS_ROBOT_TYPE`` value.

    Unknown / absent / blank → ``DEFAULT_ARM_FAMILY``, matching the server
    registry's ``robot_profiles.resolve`` fallback: an unrecognised .env value
    keeps today's OMX behaviour rather than making every arm unscannable.
    """
    return robot_profile(robot_type)["arm_family"]


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

# Version stamp of the on-disk SYSTEM files (the opi compose, the systemd units,
# `.s6-keep`) — written by setup.sh at PROVISIONING time, deliberately OUTSIDE
# `pi_agent/` so a self-update (which rsyncs only `pi_agent/`) cannot advance it.
#
# CONTEXT ONLY — it is NOT a drift signal. The release tarball carries agent code
# ONLY, so a release that adds a forwarded EDUBOTICS_* env, changes a
# healthcheck, or re-points an image ref updates the images and the agent but
# leaves the deployed Pi on the OLD compose (the `env-forwarding-guard` fail-open
# class, defeated because the guard's compose change never ships). It is tempting
# to read `stamp != APP_VERSION` as "drifted", but that inequality is the NORMAL
# state of every self-updated Pi — the stamp cannot advance by design and every
# release bumps APP_VERSION — so it fires on ~100 % of healthy Pis and proves
# nothing about the files' CONTENT. The agent instead compares the installed
# system files byte-for-byte against the ones it ships (see
# SYSTEMD_UNIT_DIR/SYSTEMD_UNIT_NAMES and SHIPPED_COMPOSE_RELPATH) and uses this
# stamp only to NAME the version the on-disk system files came from once real
# drift is proven.
#
# ABSENT means "provisioned before the stamp existed" — read backward-compatibly.
SYSTEM_FILES_VERSION_FILE = os.environ.get(
    "EDUBOTICS_SYSTEM_FILES_VERSION_FILE", "/opt/edubotics/.system-files-version"
)

# Where setup.sh installs the systemd units, and which units it installs. They
# ship INSIDE `pi_agent/systemd/` (so a self-update rsync refreshes the agent's
# copy) and setup.sh installs them VERBATIM (`install -m 0644`, no templating) to
# SYSTEMD_UNIT_DIR — which self-update never touches. A byte difference between
# the two is therefore hard evidence that this agent version expects a unit the
# Pi is not running, with no false positives to tune out.
SYSTEMD_UNIT_DIR = os.environ.get(
    "EDUBOTICS_SYSTEMD_UNIT_DIR", "/etc/systemd/system"
)
SYSTEMD_UNIT_NAMES = ("edubotics-pi.service", "edubotics-pi-firstboot.service")

# Where setup.sh installs the udev rule(s), and which ones it installs — the
# same shape as the systemd pair above, for the same reason.
#
# The rules ship INSIDE `pi_agent/udev/` (so the release tar and the self-update
# rsync both carry them, verified) and setup.sh::install_udev copies them
# VERBATIM (`install -m 0644`, no templating) into UDEV_RULES_DIR, which
# self-update never touches. Until 2026-07-28 nothing then compared the two, so
# the udev rule was structurally the SAME fail-open the compose just closed: a
# release that adds a VID/PID line (the CH343P/edu6 rule, SHIPPED since
# `2951b87`, was exactly that) would ship an agent that scans for an arm whose
# /dev node it has no permission to open, on 100 % of the fielded fleet, with
# the only remedy being a `setup.sh` re-run from a source checkout no
# classroom has.
UDEV_RULES_DIR = os.environ.get("EDUBOTICS_UDEV_RULES_DIR", "/etc/udev/rules.d")
UDEV_RULE_NAMES = ("99-edubotics-robotis.rules",)

# The opi compose file THIS agent version ships, relative to the `pi_agent`
# package directory — the compose's half of the same repair mechanism.
#
# It used to be true that the compose could not be in the repairable set,
# because it lived only in `robotis_ai_setup/docker/` and the release tarball
# (`-C robotis_ai_setup pi_agent`) therefore carried no reference copy: a
# release that added a forwarded EDUBOTICS_* env, changed a healthcheck or
# re-pointed an image ref updated the images and the agent but left every
# fielded Pi on the compose setup.sh laid down at provisioning time — silently.
# A byte-identical TWIN now ships inside the package at this path, fenced
# against the source of truth by `ci.yml::pi-compose-twin-guard` (`cmp -s`) and
# `docker compose config`-validated on both copies by `ci.yml::compose-validate`,
# so the agent holds the bytes it needs to prove drift and repair it (see
# agent.py::_compose_drifted / _renew_compose).
#
# `.s6-keep` is deliberately still NOT in the repairable set: it is a 0-byte
# marker whose content has never changed, so there is nothing for a byte
# compare to find.
SHIPPED_COMPOSE_RELPATH = os.path.join("docker", "docker-compose.opi.yml")

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
