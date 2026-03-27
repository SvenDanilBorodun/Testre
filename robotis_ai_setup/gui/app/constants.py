"""Shared constants for the ROBOTIS AI Setup GUI."""

import os

# Docker image registry — override with ROBOTIS_REGISTRY env var
REGISTRY = os.environ.get("ROBOTIS_REGISTRY", "nettername")

# Docker image names
IMAGE_OPEN_MANIPULATOR = f"{REGISTRY}/open-manipulator:latest"
IMAGE_PHYSICAL_AI_SERVER = f"{REGISTRY}/physical-ai-server:latest"
IMAGE_PHYSICAL_AI_MANAGER = f"{REGISTRY}/physical-ai-manager:latest"
ALL_IMAGES = [IMAGE_OPEN_MANIPULATOR, IMAGE_PHYSICAL_AI_SERVER, IMAGE_PHYSICAL_AI_MANAGER]

# Network ports
PORT_WEB_UI = 80
PORT_VIDEO_SERVER = 8080
PORT_ROSBRIDGE = 9090

# USB identifiers
ROBOTIS_VID = "2F5D"  # ROBOTIS USB Vendor ID (OpenRB-150 boards)

# Dynamixel servo config
BAUDRATE = 1_000_000
LEADER_SERVO_IDS = [1, 2, 3, 4, 5, 6]
FOLLOWER_SERVO_IDS = [11, 12, 13, 14, 15, 16]

# ROS2 config
ROS_DOMAIN_ID = 30

# Paths — auto-detect dev environment vs installed
def _resolve_install_dir() -> str:
    """Return the install dir: env override > dev tree > default installed path."""
    env_dir = os.environ.get("ROBOTIS_INSTALL_DIR")
    if env_dir:
        return env_dir
    # Check if we're running from the source tree (gui/app/constants.py)
    this_file = os.path.abspath(__file__)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(this_file)))
    dev_compose = os.path.join(project_root, "docker", "docker-compose.yml")
    if os.path.isfile(dev_compose):
        return project_root
    return r"C:\Program Files\ROBOTIS AI"

INSTALL_DIR = _resolve_install_dir()
DOCKER_DIR = os.path.join(INSTALL_DIR, "docker")
COMPOSE_FILE = os.path.join(DOCKER_DIR, "docker-compose.yml")
COMPOSE_GPU_FILE = os.path.join(DOCKER_DIR, "docker-compose.gpu.yml")
ENV_FILE = os.path.join(DOCKER_DIR, ".env")

# Timeouts (seconds)
DOCKER_STARTUP_TIMEOUT = 120
DEVICE_WAIT_TIMEOUT = 30
WEB_UI_POLL_TIMEOUT = 120
WEB_UI_POLL_INTERVAL = 2
