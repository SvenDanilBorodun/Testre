"""Generate .env file from discovered hardware configuration."""

from __future__ import annotations

import hashlib
import os
import uuid

from .constants import (
    DEFAULT_ROBOT_PROFILE,
    ENV_FILE,
    IMAGE_TAG,
    REGISTRY,
    REGISTRY_FALLBACK,
    ROBOT_PROFILES,
    ROS_DOMAIN_FILE,
    ROS_DOMAIN_ID,
)
from .device_manager import HardwareConfig


# Keys this generator owns. Anything in the existing .env that is NOT
# in this set gets preserved verbatim across rewrites. The set must stay
# in sync with the lines emitted below; orphaning a managed key here
# would leak stale values into newly generated files.
MANAGED_KEYS = frozenset({
    "FOLLOWER_PORT",
    "LEADER_PORT",
    "ROS_DOMAIN_ID",
    "REGISTRY",
    # REGISTRY_FALLBACK records the Docker Hub twin so the .env documents both
    # registries. Compose runs only ${REGISTRY}; the GUI's pull fallback uses
    # constants.REGISTRY_FALLBACK directly. MANAGED so it tracks constants.
    "REGISTRY_FALLBACK",
    # IMAGE_TAG pins compose to the installer's image build (constants.py
    # resolves it: EDUBOTICS_IMAGE_TAG env > docker/versions.env > latest).
    # It is MANAGED so a stale hand-pinned tag is superseded on the next
    # regenerate instead of silently redirecting compose: a leftover
    # validation-only IMAGE_TAG=collision-validate in the preserved block
    # broke "Umgebung starten" with "manifest unknown" on 2026-06-05 after
    # an installer upgrade had wiped the local image it pointed at.
    "IMAGE_TAG",
    # EDUBOTICS_CAMERA_NAMES is MANAGED so the phone-as-3rd-camera toggle is
    # the single source of truth: enabling it emits gripper,scene,phone and
    # disabling it later (line absent → compose default gripper,scene) SUPERSEDES
    # a stale 3-name value instead of leaving the /phone publisher orphaned.
    "EDUBOTICS_CAMERA_NAMES",
    # EDUBOTICS_FOLLOWER_ONLY is MANAGED so the Roboter-Studio-vs-recording
    # session mode is the single source of truth: a Roboter Studio start emits
    # =1 (no leader launched); a recording start omits the line so compose's
    # default (0) SUPERSEDES a stale =1 instead of silently keeping the leader
    # off in a session that needs it.
    "EDUBOTICS_FOLLOWER_ONLY",
    # EDUBOTICS_ROBOT_TYPE is the GUI-hardset robot profile id (omx_full |
    # omx_follower). MANAGED so the selector is the single source of truth: a
    # stale hand-pinned value is superseded on every regenerate, and the initial
    # EDUBOTICS_FOLLOWER_ONLY is derived from it. The server reads it at boot to
    # resolve its ArmProfile (capabilities + kinematics seam).
    "EDUBOTICS_ROBOT_TYPE",
    # CAMERA_DEVICE_N / CAMERA_NAME_N are handled by prefix-match below
    # because the count varies with how many cameras are connected.
})
_MANAGED_PREFIXES = ("CAMERA_DEVICE_", "CAMERA_NAME_")

# Auto-added separator before the preserved operator-override block. Defined
# once so _read_unmanaged_lines can skip it on re-read (otherwise a fresh copy
# compounds on every regenerate) and the two emitters stay in sync.
_PRESERVE_MARKER = "# Operator overrides preserved across regeneration."


def _is_managed_key(key: str) -> bool:
    return key in MANAGED_KEYS or key.startswith(_MANAGED_PREFIXES)


def _has_camera_source(preserved: list[str]) -> bool:
    """True if a preserved (operator-set) line already defines the camera source."""
    return any(p.lstrip().startswith("EDUBOTICS_CAMERA_SOURCE=") for p in preserved)


def _quote(value: str) -> str:
    """Double-quote a value so docker-compose handles spaces.

    Paths like `/mnt/c/Users/Max Muster/...` would otherwise break env parsing
    (compose stops at the space and treats the remainder as another var).
    """
    if value is None:
        return '""'
    # Escape any embedded double-quotes and backslashes.
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _read_persisted_ros_domain_id() -> int | None:
    """Return the persisted per-machine ROS_DOMAIN_ID, or None when absent /
    unreadable / out of the legal DDS range [0, 232]."""
    try:
        with open(ROS_DOMAIN_FILE, encoding="utf-8") as f:
            raw = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw.isdigit():
        return None
    value = int(raw)
    if 0 <= value <= 232:
        return value
    return None


def _persist_ros_domain_id(value: int) -> None:
    """Best-effort write of the resolved domain id so it stays STABLE across
    sessions regardless of NIC/VPN changes. Never raises — a failed persist
    just means we re-derive next time (same value as long as getnode() agrees)."""
    try:
        os.makedirs(os.path.dirname(ROS_DOMAIN_FILE), exist_ok=True)
        tmp = ROS_DOMAIN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"{value}\n")
        os.replace(tmp, ROS_DOMAIN_FILE)
    except OSError:
        pass


def _resolve_ros_domain_id() -> int:
    """Resolve a STABLE per-machine ROS_DOMAIN_ID so two student laptops on the
    same school LAN don't share ROS topics — and so a single laptop keeps the
    SAME domain across sessions.

    Order: EDUBOTICS_ROS_DOMAIN env override → persisted file → derive from
    uuid.getnode() (then persist). Hardcoded 30 across every install meant
    Student A's inference could drive Student B's arm on the same Wi-Fi, so we
    hash the machine UUID to a value in the legal DDS range [0, 232]. But
    getnode() is NOT stable on multi-NIC / VPN / docking-station PCs (it may
    pick a different interface — or a random fallback — between runs), and the
    .env is regenerated on every "Umgebung starten"; a changed domain splits
    the DDS graph from any surviving container and the React app shows
    "disconnected". So the first-derived value is PERSISTED (constants.
    ROS_DOMAIN_FILE) and reused thereafter. The env override always wins and is
    intentionally NOT persisted (it's an explicit per-run knob).
    """
    override = os.environ.get("EDUBOTICS_ROS_DOMAIN")
    if override and override.isdigit():
        return max(0, min(232, int(override)))

    persisted = _read_persisted_ros_domain_id()
    if persisted is not None:
        return persisted

    try:
        node_id = uuid.getnode()  # 48-bit MAC-derived identifier
        digest = hashlib.sha256(str(node_id).encode()).digest()
        resolved = int.from_bytes(digest[:2], "big") % 233
    except Exception:
        # Fall back to the legacy default if anything above fails.
        resolved = int(ROS_DOMAIN_ID)

    _persist_ros_domain_id(resolved)
    return resolved


def _atomic_write(path: str, content: str) -> None:
    """Write via temp file + rename so a power loss mid-write can't leave
    a truncated .env that compose would fail to parse."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="\n") as f:
        f.write(content)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # fsync unsupported (e.g. some network filesystems)
    os.replace(tmp, path)


def _read_unmanaged_lines(path: str) -> list[str]:
    """Return non-managed lines (comments, blanks, unknown KEY=VALUE)
    from an existing .env so a regenerate doesn't wipe operator-added
    overrides like EDUBOTICS_CAMERA_PIXEL_FORMAT, EDUBOTICS_ROS_DOMAIN,
    EDUBOTICS_REGISTRY, or per-classroom HF_TOKEN.

    Returns an empty list when the file doesn't exist yet.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        return []

    preserved: list[str] = []
    for line in raw:
        stripped = line.strip()
        # Drop trailing newline; we re-add when emitting.
        text = line.rstrip("\r\n")
        if not stripped or stripped.startswith("#"):
            # Keep comments + blank lines so the file stays human-readable
            # if anyone hand-edited it. EXCEPT the auto-added section marker:
            # re-preserving it would compound a fresh copy on every regenerate
            # (one extra marker per hardware re-scan).
            if stripped == _PRESERVE_MARKER:
                continue
            preserved.append(text)
            continue
        if "=" not in stripped:
            # Malformed line — keep it. Compose will reject it, the
            # student will see the error, and they can fix it; better
            # than silently dropping their manual edit.
            preserved.append(text)
            continue
        key = stripped.split("=", 1)[0].strip()
        if _is_managed_key(key):
            continue
        preserved.append(text)
    # Strip leading blank lines: generate_env_file always re-adds a single
    # separating blank before the marker, so carrying leading blanks here
    # would compound them across regenerates.
    while preserved and not preserved[0].strip():
        preserved.pop(0)
    return preserved


def _unquote(value: str) -> str:
    """Inverse of _quote: strip one surrounding pair of double-quotes and
    unescape \\" / \\\\. Unquoted values pass through unchanged."""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return v


def read_env_var(key: str, path: str = ENV_FILE) -> str | None:
    """Return the value of ``key`` from the .env at ``path``, or None if absent.

    Tolerates the quoting written by _quote() and surrounding whitespace.
    The GUI uses this to show a "token already saved on this PC" state
    WITHOUT re-displaying the secret value. Returns None when the file is
    missing/unreadable.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in raw:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        existing_key, _, existing_value = stripped.partition("=")
        if existing_key.strip() == key:
            return _unquote(existing_value)
    return None


def upsert_env_var(key: str, value: str, path: str = ENV_FILE) -> None:
    """Insert or replace ``key=value`` in the .env at ``path``, preserving
    every other line (managed keys, comments, operator overrides) verbatim.

    This is the SOLE writer of HF_TOKEN. HF_TOKEN is deliberately NOT a
    MANAGED_KEY: generate_env_file() carries it across hardware-rescan
    rewrites via _read_unmanaged_lines(), and this helper is how the GUI
    sets it once at setup. An empty ``value`` removes the key (token clear).
    Value is quoted via _quote() so a token with shell-special chars is safe.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except (OSError, UnicodeDecodeError):
        raw = []

    new_line = f"{key}={_quote(value)}"
    out: list[str] = []
    replaced = False
    for line in raw:
        text = line.rstrip("\r\n")
        stripped = text.strip()
        if "=" in stripped and not stripped.startswith("#"):
            if stripped.split("=", 1)[0].strip() == key:
                # Replace the first occurrence; drop any duplicates. When
                # value is empty we drop the line entirely (removal).
                if value and not replaced:
                    out.append(new_line)
                    replaced = True
                continue
        out.append(text)

    if value and not replaced:
        out.append(new_line)

    content = "\n".join(out).rstrip("\n")
    if content:
        content += "\n"
    _atomic_write(path, content)


def _phone_camera_names_line() -> str:
    """The managed EDUBOTICS_CAMERA_NAMES line that adds the phone as cam_id 2.

    Built from CAMERA_BRIDGE_ROLES + the phone name so the order stays in lockstep
    with camera_bridge's cam_id mapping (gripper=0, scene=1, phone=2)."""
    from .constants import CAMERA_BRIDGE_ROLES, PHONE_CAMERA_NAME
    names = ",".join(list(CAMERA_BRIDGE_ROLES) + [PHONE_CAMERA_NAME])
    return f"EDUBOTICS_CAMERA_NAMES={names}"


def generate_env_file(config: HardwareConfig, output_path: str = ENV_FILE,
                      phone_camera: bool = False,
                      robot_type: str = DEFAULT_ROBOT_PROFILE,
                      follower_only: bool | None = None) -> str:
    """Write .env file with hardware paths.

    Args:
        config: Discovered hardware configuration.
        output_path: Path to write the .env file.
        phone_camera: When True, emit the managed
            ``EDUBOTICS_CAMERA_NAMES=gripper,scene,phone`` line so the ingest
            node publishes /phone/image_raw/compressed (cam_id 2). When False the
            line is omitted and compose's default (gripper,scene) wins — and a
            stale 3-name value is superseded because the key is MANAGED.
        robot_type: The GUI-hardset ArmProfile id (``omx_full``|``omx_follower``)
            emitted as the managed ``EDUBOTICS_ROBOT_TYPE`` line and read by the
            server at boot. Also DERIVES the initial ``follower_only`` when that
            argument is left as None (omx_follower ⇒ True).
        follower_only: When True (Roboter Studio mode), emit the managed
            ``EDUBOTICS_FOLLOWER_ONLY=1`` line and OMIT ``LEADER_PORT`` — the
            entrypoint then never launches the leader, so the follower's
            arm_controller is driven solely by the workflow/calibration
            trajectory publisher (no leader broadcaster to clobber it). A
            leader need not be scanned/configured in this mode. When False a
            recording/teleop session is configured and both arms are required;
            the follower-only line is omitted so compose's default (0)
            supersedes any stale =1 (the key is MANAGED). When None (the
            default) the value is DERIVED from ``robot_type`` (via the
            ``constants.ROBOT_PROFILES`` registry) — this keeps the RS runtime
            leader-toggle able to OVERRIDE it while an omx_follower rig (which
            never scans a leader) still derives True instead of tripping the
            leader-required guard below. Explicitly passing False for a
            follower-only profile is CONTRADICTORY (the profile has no leader
            to re-arm) and raises a German ValueError instead of silently
            emitting a both-arms .env.

    Returns:
        The content written to the file.
    """
    # Resolve the initial follower_only from the ArmProfile registry — NOT a
    # hardcoded id literal — so a new follower-only profile is honoured without
    # editing this line (single source of truth: constants.ROBOT_PROFILES).
    # Derive FIRST (before the leader-null guard): without this an omx_follower
    # rig — which has no leader — would hit `not follower_only and leader is
    # None` and raise on every start. An explicit follower_only= (the RS toggle)
    # still wins, EXCEPT that re-arming the leader on a leader-less profile is
    # contradictory (a follower-only .env has no LEADER_PORT to emit) and is
    # refused loudly rather than silently writing a both-arms .env for a rig
    # that never scanned a leader.
    profile_follower_only = ROBOT_PROFILES.get(robot_type, {}).get(
        "follower_only", False)
    if follower_only is None:
        follower_only = profile_follower_only
    elif profile_follower_only and not follower_only:
        raise ValueError(
            f'Robotertyp „{robot_type}" erlaubt keinen Leader-Betrieb '
            f'(follower_only=False).'
        )
    if config.follower is None:
        raise ValueError("Der Follower-Arm muss konfiguriert sein, bevor die .env erzeugt wird")
    if not follower_only and config.leader is None:
        raise ValueError("Leader- und Follower-Arm müssen konfiguriert sein, bevor die .env erzeugt wird")

    from .constants import cameras_use_native_bridge
    native = cameras_use_native_bridge()

    domain_id = _resolve_ros_domain_id()
    preserved = _read_unmanaged_lines(output_path)
    lines: list[str] = []
    lines.append(f"FOLLOWER_PORT={_quote(config.follower.serial_path)}")
    if follower_only:
        # Roboter Studio: no leader launched. The workflow/calibration publisher
        # is the sole writer on /leader/joint_trajectory — there is no leader
        # broadcaster to arbitrate against. LEADER_PORT is intentionally omitted
        # (MANAGED → a stale value is dropped, not preserved).
        lines.append("EDUBOTICS_FOLLOWER_ONLY=1")
    else:
        lines.append(f"LEADER_PORT={_quote(config.leader.serial_path)}")

    if config.cameras:
        for i, cam in enumerate(config.cameras, 1):
            # Audit F8: refuse to write a camera without a valid role
            # (gripper / scene). omx_f_config.yaml hard-codes those
            # topic names, so a `camera1`/`camera2` fallback would
            # make the subscriber wait forever. The GUI wizard always
            # sets a role; this guard catches programmatic misuse.
            if cam.role not in ('gripper', 'scene'):
                raise ValueError(
                    f"Kamera ohne gültige Rolle (gripper/scene): {cam.path}"
                )
            # In native_bridge mode the container does NOT capture from
            # /dev/video* (the Windows GUI streams JPEG frames into
            # camera_ingest_node.py). CAMERA_DEVICE stays empty so the
            # entrypoint/healthcheck never wait on a non-existent device;
            # the role still drives the published /<role>/image_raw/compressed
            # topic. The capture index lives in the GUI session, not the .env.
            device_value = "" if native else cam.path
            lines.append(f"CAMERA_DEVICE_{i}={_quote(device_value)}")
            lines.append(f"CAMERA_NAME_{i}={_quote(cam.role)}")

    lines.append(f"ROS_DOMAIN_ID={domain_id}")
    lines.append(f"REGISTRY={REGISTRY}")
    lines.append(f"REGISTRY_FALLBACK={REGISTRY_FALLBACK}")
    # Pin compose to the image build this GUI ships with. docker-compose.yml
    # resolves ${IMAGE_TAG:-latest} from this file (--env-file); without the
    # line, compose silently runs :latest — drifting past the installer's
    # pinned tag AND re-downloading ~9 GB the installer already pulled.
    lines.append(f"IMAGE_TAG={IMAGE_TAG}")
    # GUI-hardset robot type — the server resolves its ArmProfile from this at
    # boot. MANAGED so the selector is authoritative; a full restart changes it.
    lines.append(f"EDUBOTICS_ROBOT_TYPE={robot_type}")
    # Default camera source. Yields to an operator override already present in
    # the preserved (unmanaged) lines, so EDUBOTICS_CAMERA_SOURCE=usb_cam in a
    # hand-edited .env survives regeneration (one-variable rollback).
    if native and not _has_camera_source(preserved):
        lines.append("EDUBOTICS_CAMERA_SOURCE=native_bridge")
    # Phone-as-3rd-camera: add "phone" to the published camera names so
    # camera_ingest_node publishes /phone/image_raw/compressed (cam_id 2). Only
    # when the student enabled the toggle; otherwise omitted (compose default
    # gripper,scene). MANAGED key → unchecking later supersedes a stale value.
    if phone_camera:
        lines.append(_phone_camera_names_line())
    if preserved:
        lines.append("")
        lines.append(_PRESERVE_MARKER)
        lines.extend(preserved)
    lines.append("")  # trailing newline

    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content


def generate_cloud_only_env(output_path: str = ENV_FILE,
                            phone_camera: bool = False,
                            robot_type: str = DEFAULT_ROBOT_PROFILE) -> str:
    """Write a minimal .env for cloud-only mode (no robot hardware).

    Docker Compose still reads .env when starting any service, so we provide
    empty placeholders for the variables referenced by the open_manipulator
    service (which we don't start in this mode anyway). Without this, compose
    would emit warnings about unset variables.

    ``phone_camera`` is accepted for signature symmetry with generate_env_file
    but is effectively a no-op here: cloud-only never starts open_manipulator,
    so no camera ingest node consumes EDUBOTICS_CAMERA_NAMES. We still honour it
    so a toggled-on value round-trips rather than being dropped.

    ``robot_type`` is emitted for MANAGED-key symmetry (so a stale hand-pinned
    EDUBOTICS_ROBOT_TYPE is superseded here too, R3) even though cloud-only
    never starts physical_ai_server.
    """
    domain_id = _resolve_ros_domain_id()
    preserved = _read_unmanaged_lines(output_path)
    lines = [
        "# Cloud-only mode — no robot hardware connected.",
        'FOLLOWER_PORT=""',
        'LEADER_PORT=""',
        'CAMERA_DEVICE_1=""',
        'CAMERA_NAME_1="gripper"',
        'CAMERA_DEVICE_2=""',
        'CAMERA_NAME_2="scene"',
        f"ROS_DOMAIN_ID={domain_id}",
        f"REGISTRY={REGISTRY}",
        f"REGISTRY_FALLBACK={REGISTRY_FALLBACK}",
        f"IMAGE_TAG={IMAGE_TAG}",
        f"EDUBOTICS_ROBOT_TYPE={robot_type}",
    ]
    from .constants import cameras_use_native_bridge
    if cameras_use_native_bridge() and not _has_camera_source(preserved):
        lines.append("EDUBOTICS_CAMERA_SOURCE=native_bridge")
    if phone_camera:
        lines.append(_phone_camera_names_line())
    if preserved:
        lines.append("")
        lines.append(_PRESERVE_MARKER)
        lines.extend(preserved)
    lines.append("")
    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content
