"""WSL2 command execution bridge.

All Linux-side commands are executed via:
    subprocess.run(["wsl", "--", "bash", "-c", cmd])
"""

import subprocess
from typing import Optional


class WSLError(Exception):
    """Raised when a WSL command fails."""


def run(cmd: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    """Execute a command inside the default WSL2 distribution.

    Args:
        cmd: Bash command string to execute inside WSL2.
        timeout: Seconds before the command is killed.
        check: If True, raise WSLError on non-zero exit code.

    Returns:
        CompletedProcess with stdout/stderr as decoded strings.
    """
    try:
        result = subprocess.run(
            ["wsl", "--", "bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise WSLError("WSL is not installed or not in PATH.")
    except subprocess.TimeoutExpired:
        raise WSLError(f"WSL command timed out after {timeout}s: {cmd}")

    if check and result.returncode != 0:
        raise WSLError(
            f"WSL command failed (exit {result.returncode}):\n"
            f"  cmd: {cmd}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return result


def is_wsl_available() -> bool:
    """Check whether WSL2 is installed and a distribution is running."""
    try:
        result = subprocess.run(
            ["wsl", "--status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def list_serial_devices() -> list[str]:
    """List /dev/serial/by-id/ paths visible inside WSL2."""
    try:
        result = run("ls /dev/serial/by-id/ 2>/dev/null", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return [
            f"/dev/serial/by-id/{line.strip()}"
            for line in result.stdout.strip().splitlines()
            if line.strip()
        ]
    except WSLError:
        return []


def list_video_devices() -> list[dict]:
    """List /dev/video* capture devices with friendly names.

    Returns list of dicts: [{"path": "/dev/video0", "name": "Logitech C920"}, ...]
    """
    try:
        # Find devices that support Video Capture and extract their names
        cmd = r"""
for d in /dev/video*; do
    info=$(v4l2-ctl --device="$d" --info 2>/dev/null)
    if echo "$info" | grep -q "Video Capture"; then
        name=$(echo "$info" | grep "Card type" | sed 's/.*: //')
        echo "$d|$name"
    fi
done
"""
        result = run(cmd, timeout=15, check=False)
        if not result.stdout.strip():
            return []

        devices = []
        for line in result.stdout.strip().splitlines():
            if "|" in line:
                path, name = line.split("|", 1)
                devices.append({"path": path.strip(), "name": name.strip() or path.strip()})
        return devices
    except WSLError:
        return []


def get_docker_wsl_distro() -> Optional[str]:
    """Return the name of Docker Desktop's WSL2 distro, if found."""
    try:
        result = subprocess.run(
            ["wsl", "-l", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            name = line.strip().replace("\x00", "")
            if "docker-desktop" in name.lower():
                return name
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
