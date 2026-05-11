"""WSL2 command execution bridge — pinned to the EduBotics distro.

Every `wsl` invocation targets the EduBotics distro explicitly so the GUI
behaves the same way regardless of what other distros the user has installed.
"""

import subprocess
import sys
from typing import Optional

from .constants import WSL_DISTRO_NAME

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class WSLError(Exception):
    """Raised when a WSL command fails."""


def run(cmd: str, timeout: int = 30, check: bool = True, distro: Optional[str] = None) -> subprocess.CompletedProcess:
    """Execute a command inside the EduBotics WSL2 distribution.

    Args:
        cmd: Bash command string to execute.
        timeout: Seconds before the command is killed.
        check: If True, raise WSLError on non-zero exit code.
        distro: Override the distro name (defaults to EduBotics).

    Returns:
        CompletedProcess with stdout/stderr as decoded strings.
    """
    target = distro or WSL_DISTRO_NAME
    try:
        result = subprocess.run(
            ["wsl", "-d", target, "--", "bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            **_SUBPROCESS_KWARGS,
        )
    except FileNotFoundError:
        raise WSLError("WSL is not installed or not in PATH.")
    except subprocess.TimeoutExpired:
        raise WSLError(f"WSL command timed out after {timeout}s: {cmd}")

    if check and result.returncode != 0:
        raise WSLError(
            f"WSL command failed in distro {target!r} (exit {result.returncode}):\n"
            f"  cmd: {cmd}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return result


def is_wsl_available() -> bool:
    """Check whether WSL2 is installed on the host."""
    try:
        result = subprocess.run(
            ["wsl", "--status"],
            capture_output=True,
            text=True,
            timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def is_edubotics_distro_registered() -> bool:
    """Return True iff the EduBotics WSL2 distro is registered."""
    try:
        result = subprocess.run(
            ["wsl", "--list", "--quiet"],
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            if line.replace("\x00", "").strip() == WSL_DISTRO_NAME:
                return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def list_serial_devices() -> list[str]:
    """List /dev/serial/by-id/ paths visible inside the EduBotics distro."""
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

    Audit F20: `/dev/videoN` is NOT stable across hotplug — the kernel
    may reassign on replug (`/dev/video0` → `/dev/video2`). Resolve to
    the udev `/dev/v4l/by-id/...` symlink when available so the env
    file survives a replug. Mirrors the existing `/dev/serial/by-id/`
    pattern used for the arms.
    """
    try:
        # Find devices that support Video Capture, extract their friendly
        # names, then resolve to a stable /dev/v4l/by-id/... symlink so
        # the .env file survives a USB replug.
        cmd = r"""
for d in /dev/video*; do
    info=$(v4l2-ctl --device="$d" --info 2>/dev/null)
    if echo "$info" | grep -q "Video Capture"; then
        name=$(echo "$info" | grep "Card type" | sed 's/.*: //')
        stable=$(udevadm info -q symlink -n "$d" 2>/dev/null | tr ' ' '\n' | grep -m1 'v4l/by-id' || true)
        if [ -n "$stable" ]; then
            path="/dev/$stable"
        else
            path="$d"
        fi
        echo "$path|$name"
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
