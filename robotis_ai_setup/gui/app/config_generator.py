"""Generate .env file from discovered hardware configuration."""

import hashlib
import os
import uuid

from .constants import ENV_FILE, ROS_DOMAIN_ID, REGISTRY
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
    # CAMERA_DEVICE_N / CAMERA_NAME_N are handled by prefix-match below
    # because the count varies with how many cameras are connected.
})
_MANAGED_PREFIXES = ("CAMERA_DEVICE_", "CAMERA_NAME_")


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


def _resolve_ros_domain_id() -> int:
    """Derive a per-machine ROS_DOMAIN_ID so two student laptops on the
    same school LAN don't share ROS topics.

    Hardcoded 30 across every install meant Student A's inference could
    drive Student B's arm on the same Wi-Fi. We hash the machine's UUID
    (stable across reboots, unique per install) to a value in the legal
    DDS domain range [0, 232]. Override via EDUBOTICS_ROS_DOMAIN env var
    if needed.
    """
    override = os.environ.get("EDUBOTICS_ROS_DOMAIN")
    if override and override.isdigit():
        return max(0, min(232, int(override)))
    try:
        node_id = uuid.getnode()  # 48-bit MAC-derived identifier
        digest = hashlib.sha256(str(node_id).encode()).digest()
        return int.from_bytes(digest[:2], "big") % 233
    except Exception:
        # Fall back to the legacy default if anything above fails.
        return int(ROS_DOMAIN_ID)


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
            # if anyone hand-edited it. Skipped only if duplicated below.
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
    return preserved


def generate_env_file(config: HardwareConfig, output_path: str = ENV_FILE) -> str:
    """Write .env file with hardware paths.

    Args:
        config: Discovered hardware configuration.
        output_path: Path to write the .env file.

    Returns:
        The content written to the file.
    """
    if config.leader is None or config.follower is None:
        raise ValueError("Both leader and follower arms must be configured before generating .env")

    from .constants import cameras_use_native_bridge
    native = cameras_use_native_bridge()

    domain_id = _resolve_ros_domain_id()
    preserved = _read_unmanaged_lines(output_path)
    lines: list[str] = []
    lines.append(f"FOLLOWER_PORT={_quote(config.follower.serial_path)}")
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
                    f"Kamera ohne gueltige Rolle (gripper/scene): {cam.path}"
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
    # Default camera source. Yields to an operator override already present in
    # the preserved (unmanaged) lines, so EDUBOTICS_CAMERA_SOURCE=usb_cam in a
    # hand-edited .env survives regeneration (one-variable rollback).
    if native and not _has_camera_source(preserved):
        lines.append("EDUBOTICS_CAMERA_SOURCE=native_bridge")
    if preserved:
        lines.append("")
        lines.append("# Operator overrides preserved across regeneration.")
        lines.extend(preserved)
    lines.append("")  # trailing newline

    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content


def generate_cloud_only_env(output_path: str = ENV_FILE) -> str:
    """Write a minimal .env for cloud-only mode (no robot hardware).

    Docker Compose still reads .env when starting any service, so we provide
    empty placeholders for the variables referenced by the open_manipulator
    service (which we don't start in this mode anyway). Without this, compose
    would emit warnings about unset variables.
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
    ]
    from .constants import cameras_use_native_bridge
    if cameras_use_native_bridge() and not _has_camera_source(preserved):
        lines.append("EDUBOTICS_CAMERA_SOURCE=native_bridge")
    if preserved:
        lines.append("")
        lines.append("# Operator overrides preserved across regeneration.")
        lines.extend(preserved)
    lines.append("")
    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content
