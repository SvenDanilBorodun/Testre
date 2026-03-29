"""Gemeinsame Konstanten für das EduBotics Setup."""

import os

# Docker image registry — override with EDUBOTICS_REGISTRY env var
REGISTRY = os.environ.get("EDUBOTICS_REGISTRY", "nettername")

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
ENV_FILE = os.path.join(DOCKER_DIR, ".env")

# Timeouts (seconds)
DOCKER_STARTUP_TIMEOUT = 120
DEVICE_WAIT_TIMEOUT = 30
WEB_UI_POLL_TIMEOUT = 120
WEB_UI_POLL_INTERVAL = 2
