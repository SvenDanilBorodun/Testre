"""Generate .env file from discovered hardware configuration."""

from .constants import ENV_FILE, ROS_DOMAIN_ID, REGISTRY
from .device_manager import HardwareConfig


def generate_env_file(config: HardwareConfig, output_path: str = ENV_FILE, offline_mode: bool = False) -> str:
    """Write .env file with hardware paths.

    Args:
        config: Discovered hardware configuration.
        output_path: Path to write the .env file.
        offline_mode: If True, generate config for offline testing without real hardware.

    Returns:
        The content written to the file.
    """
    if not offline_mode and (config.leader is None or config.follower is None):
        raise ValueError("Both leader and follower arms must be configured before generating .env")

    lines = []

    if offline_mode:
        lines.append("OFFLINE_MODE=true")
        lines.append("FOLLOWER_PORT=/dev/null")
        lines.append("LEADER_PORT=/dev/null")
    else:
        lines.append(f"FOLLOWER_PORT={config.follower.serial_path}")
        lines.append(f"LEADER_PORT={config.leader.serial_path}")

    if config.cameras:
        for i, cam in enumerate(config.cameras, 1):
            lines.append(f"CAMERA_DEVICE_{i}={cam.path}")
            lines.append(f"CAMERA_NAME_{i}={cam.role or f'camera{i}'}")

    lines.append(f"ROS_DOMAIN_ID={ROS_DOMAIN_ID}")
    lines.append(f"REGISTRY={REGISTRY}")
    lines.append("")  # trailing newline

    content = "\n".join(lines)

    # Ensure parent directory exists
    import os
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, "w", newline="\n") as f:
        f.write(content)

    return content
