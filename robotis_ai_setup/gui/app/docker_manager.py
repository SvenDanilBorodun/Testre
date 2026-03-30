"""Docker Compose lifecycle management.

Handles:
  - Checking Docker Desktop is running
  - Pulling images if needed
  - Starting/stopping containers
  - GPU detection
"""

import os
import subprocess
import sys
import time
from typing import Optional

# On Windows, hide console windows spawned by subprocess
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}

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
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def start_docker_desktop() -> bool:
    """Try to launch Docker Desktop if it's installed but not running."""
    paths = [
        os.path.join(os.environ.get("ProgramFiles", ""), "Docker", "Docker", "Docker Desktop.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Docker", "Docker", "Docker Desktop.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Docker", "Docker Desktop.exe"),
    ]
    for path in paths:
        if os.path.isfile(path):
            try:
                subprocess.Popen([path], **_SUBPROCESS_KWARGS)
                return True
            except OSError:
                continue
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
            **_SUBPROCESS_KWARGS,
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
                **_SUBPROCESS_KWARGS,
            )
            status[image] = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            status[image] = False
    return status


def check_for_updates(log=None) -> bool:
    """Check if any images have newer versions on the registry.

    Compares local image digests against remote manifests without pulling.
    Returns True if updates were found and pulled.

    Args:
        log: Optional callable for status messages (e.g. gui._log).
    """
    updates_available = False
    registry_reachable = False

    for image in ALL_IMAGES:
        try:
            # Get local digest
            local = subprocess.run(
                ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image],
                capture_output=True, text=True, timeout=10,
                **_SUBPROCESS_KWARGS,
            )
            if local.returncode != 0:
                continue
            local_digest = local.stdout.strip()

            # Get remote digest via manifest inspect (no download)
            remote = subprocess.run(
                ["docker", "manifest", "inspect", "--verbose", image],
                capture_output=True, text=True, timeout=30,
                **_SUBPROCESS_KWARGS,
            )
            if remote.returncode != 0:
                continue

            registry_reachable = True

            # If local digest not found in remote manifest output, update available
            if local_digest and local_digest.split("@")[-1] not in remote.stdout:
                updates_available = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    if not registry_reachable:
        if log:
            log("WARNUNG: Docker Hub nicht erreichbar — Update-Prüfung übersprungen.")
        return False

    if updates_available:
        if log:
            log("Updates gefunden — Images werden aktualisiert...")
        for image in ALL_IMAGES:
            try:
                subprocess.run(
                    ["docker", "pull", "--quiet", image],
                    capture_output=True, text=True, timeout=600,
                    **_SUBPROCESS_KWARGS,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        # Remove dangling images to free disk space
        try:
            subprocess.run(
                ["docker", "image", "prune", "-f"],
                capture_output=True, text=True, timeout=30,
                **_SUBPROCESS_KWARGS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return True
    return False


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
                **_SUBPROCESS_KWARGS,
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


def start_containers(gpu: bool = False, log=None) -> bool:
    """Start all containers via docker compose up -d.

    Uses --force-recreate to handle stale containers cleanly.

    Args:
        gpu: If True, include the GPU override compose file.
        log: Optional callable for status messages.

    Returns:
        True if containers started successfully.
    """
    cmd = _compose_cmd(gpu) + ["up", "-d", "--force-recreate"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=180,
            cwd=DOCKER_DIR,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0 and log:
            log(f"Docker Compose Fehler: {result.stderr.strip()}")
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        if log:
            log(f"Docker Compose Fehler: {e}")
        return False


def stop_containers(gpu: bool = False) -> bool:
    """Stop all containers via docker compose down."""
    cmd = _compose_cmd(gpu) + ["down"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
            cwd=DOCKER_DIR,
            **_SUBPROCESS_KWARGS,
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
                **_SUBPROCESS_KWARGS,
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
            **_SUBPROCESS_KWARGS,
        )
        return result.stdout + result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
