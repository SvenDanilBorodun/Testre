"""Docker Compose lifecycle management.

Handles:
  - Checking Docker Desktop is running
  - Pulling images if needed
  - Starting/stopping containers
  - GPU detection
"""

import subprocess
import time
from typing import Optional

from .constants import (
    ALL_IMAGES,
    COMPOSE_FILE,
    COMPOSE_GPU_FILE,
    DOCKER_DIR,
    DOCKER_STARTUP_TIMEOUT,
)


class DockerError(Exception):
    """Raised when a Docker operation fails."""


def is_docker_running() -> bool:
    """Check if Docker daemon is accessible."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def wait_for_docker(timeout: int = DOCKER_STARTUP_TIMEOUT, callback=None) -> bool:
    """Wait for Docker Desktop to be ready.

    Args:
        timeout: Max seconds to wait.
        callback: Optional function called with (elapsed, timeout) for progress updates.

    Returns:
        True if Docker became available within timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        if is_docker_running():
            return True
        elapsed = int(time.time() - start)
        if callback:
            callback(elapsed, timeout)
        time.sleep(2)
    return False


def has_gpu() -> bool:
    """Detect if NVIDIA GPU is available on the Windows host."""
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def images_exist() -> dict[str, bool]:
    """Check which Docker images are already pulled.

    Returns dict of image_name -> exists.
    """
    status = {}
    for image in ALL_IMAGES:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True, text=True, timeout=10,
            )
            status[image] = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            status[image] = False
    return status


def check_for_updates() -> bool:
    """Check if any images have newer versions on the registry.

    Compares local image digests against remote manifests without pulling.
    Returns True if updates were found and pulled.
    """
    updates_available = False
    for image in ALL_IMAGES:
        try:
            # Get local digest
            local = subprocess.run(
                ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image],
                capture_output=True, text=True, timeout=10,
            )
            if local.returncode != 0:
                continue
            local_digest = local.stdout.strip()

            # Get remote digest via manifest inspect (no download)
            remote = subprocess.run(
                ["docker", "manifest", "inspect", "--verbose", image],
                capture_output=True, text=True, timeout=30,
            )
            if remote.returncode != 0:
                continue

            # If local digest not found in remote manifest output, update available
            if local_digest and local_digest.split("@")[-1] not in remote.stdout:
                updates_available = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    if updates_available:
        # Pull only when updates are actually available
        for image in ALL_IMAGES:
            try:
                subprocess.run(
                    ["docker", "pull", "--quiet", image],
                    capture_output=True, text=True, timeout=600,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return True
    return False


def pull_latest(callback=None) -> bool:
    """Pull latest versions of all images.

    Args:
        callback: Optional function called with (image_name, index, total) for progress.

    Returns:
        True if all images pulled successfully.
    """
    for i, image in enumerate(ALL_IMAGES):
        if callback:
            callback(image, i, len(ALL_IMAGES))
        try:
            result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return True


def pull_images(callback=None) -> bool:
    """Pull all required Docker images.

    Args:
        callback: Optional function called with (image_name, index, total) for progress.

    Returns:
        True if all images pulled successfully.
    """
    for i, image in enumerate(ALL_IMAGES):
        if callback:
            callback(image, i, len(ALL_IMAGES))
        try:
            result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return True


def _compose_cmd(gpu: bool = False) -> list[str]:
    """Build the docker compose command with appropriate files."""
    cmd = ["docker", "compose", "-f", COMPOSE_FILE]
    if gpu:
        cmd.extend(["-f", COMPOSE_GPU_FILE])
    return cmd


def start_containers(gpu: bool = False) -> bool:
    """Start all containers via docker compose up -d.

    Args:
        gpu: If True, include the GPU override compose file.

    Returns:
        True if containers started successfully.
    """
    cmd = _compose_cmd(gpu) + ["up", "-d"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=120,
            cwd=DOCKER_DIR,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def stop_containers(gpu: bool = False) -> bool:
    """Stop all containers via docker compose down."""
    cmd = _compose_cmd(gpu) + ["down"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
            cwd=DOCKER_DIR,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_container_status() -> dict[str, str]:
    """Get status of all project containers.

    Returns dict of container_name -> status (e.g. "running", "exited", "not found").
    """
    containers = ["open_manipulator", "physical_ai_server", "physical_ai_manager"]
    status = {}
    for name in containers:
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", name],
                capture_output=True, text=True, timeout=10,
            )
            status[name] = result.stdout.strip() if result.returncode == 0 else "not found"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            status[name] = "error"
    return status


def all_containers_running() -> bool:
    """Check if all 3 containers are in 'running' state."""
    status = get_container_status()
    return all(s == "running" for s in status.values())


def get_container_logs(container_name: str, lines: int = 50) -> str:
    """Get recent logs from a container."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), container_name],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout + result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
