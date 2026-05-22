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

    The script is fed to bash via stdin rather than `bash -c "<script>"`.
    Reason: wsl.exe + bash -c mishandles multi-line scripts whose `$(...)`
    command substitution captures output containing literal `(` and tab-
    indented lines (e.g. `v4l2-ctl --info`). The captured output ends up
    being parsed by bash itself, producing "bash: line N: Card: command
    not found" and an empty stdout — silently breaking camera discovery.
    Piping the script via stdin avoids the argv path entirely. CRLF is
    normalized so Windows-source-file line endings don't reach bash as
    `$'\\r'` tokens.

    Args:
        cmd: Bash command string to execute.
        timeout: Seconds before the command is killed.
        check: If True, raise WSLError on non-zero exit code.
        distro: Override the distro name (defaults to EduBotics).

    Returns:
        CompletedProcess with stdout/stderr as decoded strings.
    """
    target = distro or WSL_DISTRO_NAME
    # Send stdin as raw bytes so Python's text-mode \n→\r\n translation on
    # Windows doesn't reinsert the CRs we just stripped. We still want
    # str-typed stdout/stderr, so decode manually after.
    script_bytes = cmd.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    try:
        result = subprocess.run(
            ["wsl", "-d", target, "--", "bash"],
            input=script_bytes,
            capture_output=True,
            timeout=timeout,
            **_SUBPROCESS_KWARGS,
        )
    except FileNotFoundError:
        raise WSLError("WSL is not installed or not in PATH.")
    except subprocess.TimeoutExpired:
        raise WSLError(f"WSL command timed out after {timeout}s: {cmd}")

    result.stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    result.stderr = (result.stderr or b"").decode("utf-8", errors="replace")

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

    Two-cameras-same-VID:PID quirk: when a classroom plugs in two
    identical UVC cameras and only one of them exposes a USB serial
    string, udev generates a `usb-..._SN0001-...` symlink only for the
    serialed one — but `udevadm info -q symlink` returns that same
    by-id name for BOTH devices' v4l capture nodes. Without de-dup the
    function would return two CameraDevices pointing at the same path.
    We emit `$d` (the kernel-assigned /dev/videoN) as a third column so
    we can de-dup by real video node, not by stable path.
    """
    try:
        # Filter to real capture nodes only — UVC cameras expose
        # secondary /dev/videoN entries for metadata / extended-control
        # channels that also report `Type: Video Capture` but enumerate
        # zero image formats. Keep only the ones with at least one
        # `[0]: 'FOURCC' ...` format entry, otherwise the student sees
        # twice as many cameras as they plugged in.
        cmd = r"""
for d in /dev/video*; do
    formats=$(v4l2-ctl --device="$d" --list-formats 2>/dev/null)
    echo "$formats" | grep -qE '^[[:space:]]+\[0\]:' || continue
    info=$(v4l2-ctl --device="$d" --info 2>/dev/null)
    name=$(echo "$info" | grep 'Card type' | sed 's/.*Card type[[:space:]]*:[[:space:]]*//')
    bus=$(echo "$info" | grep 'Bus info' | sed 's/.*Bus info[[:space:]]*:[[:space:]]*//')
    stable=$(udevadm info -q symlink -n "$d" 2>/dev/null | tr ' ' '\n' | grep -m1 'v4l/by-id' || true)
    path="$d"
    if [ -n "$stable" ] && [ -e "/dev/$stable" ]; then
        # Only trust the by-id symlink if it actually resolves to the
        # current device — when two cameras share VID:PID and one lacks
        # a serial, udev hands the same symlink name to both but the
        # filesystem link points at only one of them.
        if [ "$(readlink -f "/dev/$stable")" = "$d" ]; then
            path="/dev/$stable"
        fi
    fi
    echo "$path|$name|$d|$bus"
done
"""
        result = run(cmd, timeout=15, check=False)
        if not result.stdout.strip():
            return []

        # Dedup by physical camera. `Bus info` from v4l2 is unique per
        # physical USB port (e.g. `usb-vhci_hcd.0-1` vs `usb-vhci_hcd.0-2`),
        # so we key on that to guarantee one entry per camera even when
        # both expose the same by-id symlink.
        devices: list[dict] = []
        seen_bus: set[str] = set()
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) < 3:
                continue
            path = parts[0].strip()
            name = parts[1].strip()
            real_path = parts[2].strip()
            bus = parts[3].strip() if len(parts) == 4 else ""
            key = bus or real_path
            if key in seen_bus:
                continue
            seen_bus.add(key)
            devices.append({"path": path, "name": name or path})
        return devices
    except WSLError:
        return []
