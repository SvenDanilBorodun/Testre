"""Generate .env file from discovered hardware configuration."""

from .constants import ENV_FILE, ROS_DOMAIN_ID, REGISTRY
from .device_manager import HardwareConfig


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

    lines = []
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
