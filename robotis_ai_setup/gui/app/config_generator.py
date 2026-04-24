"""Generate .env file from discovered hardware configuration."""

import hashlib
import os
import uuid

from .constants import ENV_FILE, ROS_DOMAIN_ID, REGISTRY
from .device_manager import HardwareConfig


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

    domain_id = _resolve_ros_domain_id()
    lines = []
    lines.append(f"FOLLOWER_PORT={_quote(config.follower.serial_path)}")
    lines.append(f"LEADER_PORT={_quote(config.leader.serial_path)}")

    if config.cameras:
        for i, cam in enumerate(config.cameras, 1):
            lines.append(f"CAMERA_DEVICE_{i}={_quote(cam.path)}")
            lines.append(f"CAMERA_NAME_{i}={_quote(cam.role or f'camera{i}')}")

    lines.append(f"ROS_DOMAIN_ID={domain_id}")
    lines.append(f"REGISTRY={REGISTRY}")
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
        "",
    ]
    content = "\n".join(lines)
    _atomic_write(output_path, content)
    return content
