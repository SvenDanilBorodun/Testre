"""Native v4l2 camera enumeration for the Orange Pi agent.

Ported from ``gui/app/wsl_bridge.list_video_devices`` — same
by-id/by-path selection order and same identical-serial dedup — but run
NATIVELY on the Pi (plain ``bash`` via subprocess) instead of through
``wsl -d EduBotics -- bash``. The vhci_hcd USB bridge, the Windows capture
bridge (:5557) and ``win_camera`` (MSMF) do not exist on the Pi; cameras reach
the container through the in-container ``usb_cam`` node by ``/dev/video*`` path,
so the ONLY host-side job is to enumerate the capture nodes with the most
stable udev symlink and hand distinct paths to the two camera slots.

The shell pipeline is fed to ``bash`` over stdin rather than ``bash -c
"<script>"``. On WSL this was mandatory (wsl.exe + ``bash -c`` mangled the
``$(...)`` captures containing literal ``(`` / tabs from ``v4l2-ctl --info`` —
see the wsl_bridge docstring). Natively that mangling does not occur, but the
stdin path is still the robust choice and keeps this a faithful port; CRLF is
normalized so a checked-out-on-Windows source file never reaches bash as
``$'\\r'`` tokens.

All log/comment strings are English (Rule §1); this module produces no
student-facing text (role labels are assigned downstream).
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

# CameraDevice is defined in config_generator (the Pi has no device_manager) and
# the scanning modules BUILD it — the config_generator docstring pins this
# ownership. Importing it here (rather than redefining) keeps ONE dataclass shape
# so generate_env_file reads .path/.role off exactly the object we produced.
from .config_generator import CameraDevice

logger = logging.getLogger("edubotics-pi-agent")


# The enumeration pipeline. Byte-for-byte the same selection logic as
# ``wsl_bridge.list_video_devices``'s embedded script: keep only real capture
# nodes (a ``[0]: 'FOURCC'`` format entry — UVC metadata nodes report
# "Video Capture" with zero formats and would double the count), then pick the
# most stable path udev offers, in order: by-id (serial-anchored) → by-path
# (USB-topology-anchored) → /dev/videoN (kernel-assigned, reshuffles). The
# chosen symlink must resolve back to $d, else a name collision (no-serial /
# identical-serial case) would hand the same symlink to two cameras.
_ENUM_SCRIPT = r"""
for d in /dev/video*; do
    formats=$(v4l2-ctl --device="$d" --list-formats 2>/dev/null)
    echo "$formats" | grep -qE '^[[:space:]]+\[0\]:' || continue
    info=$(v4l2-ctl --device="$d" --info 2>/dev/null)
    name=$(echo "$info" | grep 'Card type' | sed 's/.*Card type[[:space:]]*:[[:space:]]*//')
    bus=$(echo "$info" | grep 'Bus info' | sed 's/.*Bus info[[:space:]]*:[[:space:]]*//')
    symlinks=$(udevadm info -q symlink -n "$d" 2>/dev/null | tr ' ' '\n')
    path="$d"
    chosen=""
    for candidate in $(echo "$symlinks" | grep 'v4l/by-id'); do
        if [ -e "/dev/$candidate" ] && [ "$(readlink -f "/dev/$candidate")" = "$d" ]; then
            chosen="/dev/$candidate"
            break
        fi
    done
    if [ -z "$chosen" ]; then
        for candidate in $(echo "$symlinks" | grep 'v4l/by-path'); do
            if [ -e "/dev/$candidate" ] && [ "$(readlink -f "/dev/$candidate")" = "$d" ]; then
                chosen="/dev/$candidate"
                break
            fi
        done
    fi
    if [ -n "$chosen" ]; then
        path="$chosen"
    fi
    echo "$path|$name|$d|$bus"
done
"""


def _run_bash(script: str, timeout: int) -> Optional[str]:
    """Run a bash script natively via stdin, returning stdout (str) or None.

    Mirrors ``wsl_bridge.run(..., check=False)`` + reading ``.stdout``: the
    stdin-bytes + CRLF-normalize approach minus the ``wsl -d`` wrapper. stdout
    is returned REGARDLESS of exit code (the GUI parses stdout unconditionally)
    so a per-device ``v4l2-ctl`` hiccup — already swallowed with ``2>/dev/null``
    + ``|| continue`` in the pipeline — can't nuke the whole enumeration. Only a
    missing ``bash`` / timeout returns None, so callers fall back to an empty
    enumeration rather than raising into the agent loop.
    """
    script_bytes = script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    try:
        result = subprocess.run(
            ["bash"],
            input=script_bytes,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.warning("bash not found — cannot enumerate cameras")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("camera enumeration timed out after %ds", timeout)
        return None
    return (result.stdout or b"").decode("utf-8", errors="replace")


def _resolve_to_by_path(real_path: str) -> Optional[str]:
    """Return a ``/dev/v4l/by-path/...`` symlink that resolves to ``real_path``.

    Fallback when two cameras share a colliding by-id symlink (the identical-
    serial Innomaker case). by-path is anchored to the USB topology, so even
    identical-serial cameras get distinct symlinks. Returns None if no by-path
    entry resolves back to the device.
    """
    if not real_path:
        return None
    cmd = (
        f"udevadm info -q symlink -n {real_path} 2>/dev/null | tr ' ' '\\n' | "
        f"grep 'v4l/by-path' | while read s; do "
        f"  if [ \"$(readlink -f /dev/$s 2>/dev/null)\" = \"{real_path}\" ]; then "
        f"    echo /dev/$s; break; "
        f"  fi; "
        f"done"
    )
    out = _run_bash(cmd, timeout=5)
    if out is None:
        return None
    line = out.strip().splitlines()
    if line and line[0].strip():
        return line[0].strip()
    return None


def list_video_devices() -> list[dict]:
    """List ``/dev/video*`` capture devices with friendly names.

    Returns a list of dicts: ``[{"path": "/dev/video0", "name": "Logitech
    C920"}, ...]`` — the identical contract the GUI's ``list_video_devices``
    returns, so the System-window camera step consumes it unchanged.

    Dedup by physical camera on the v4l2 ``Bus info`` (unique per USB port), and
    when TWO physical cameras report the IDENTICAL USB serial (the Innomaker
    U20CAM-720P pair both report SN0001, whose colliding by-id symlink would
    otherwise point both slots at the same ``/dev/videoN`` → silent
    gripper↔scene swap), downgrade the colliding by-id rows to their distinct
    by-path equivalents. Both defenses are byte-for-byte the GUI logic.
    """
    out = _run_bash(_ENUM_SCRIPT, timeout=15)
    if not out or not out.strip():
        return []

    # Dedup by physical camera. `Bus info` from v4l2 is unique per physical USB
    # port, so we key on that to guarantee one entry per camera even when both
    # expose the same by-id symlink.
    raw_rows: list[dict] = []
    seen_bus: set[str] = set()
    for line in out.strip().splitlines():
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
        raw_rows.append({
            "path": path,
            "name": name or path,
            "real_path": real_path,
            "bus": bus,
        })

    # Identical-serial by-id collision (Innomaker SN0001 pair): two de-duped
    # rows carry the SAME by-id `path` while differing in `bus`. Handing both to
    # .env opens the same /dev/videoN twice. Downgrade the colliding rows to
    # their by-path equivalent (USB-topology-anchored, distinct even on
    # identical serials).
    if len(raw_rows) > 1:
        path_counts: dict[str, int] = {}
        for row in raw_rows:
            if "/by-id/" in row["path"]:
                path_counts[row["path"]] = path_counts.get(row["path"], 0) + 1
        colliding = {p for p, c in path_counts.items() if c > 1}
        if colliding:
            for row in raw_rows:
                if row["path"] in colliding:
                    by_path = _resolve_to_by_path(row["real_path"])
                    if by_path:
                        row["path"] = by_path

    return [{"path": r["path"], "name": r["name"]} for r in raw_rows]


def scan_cameras() -> list[CameraDevice]:
    """Convenience wrapper: enumerate and wrap each device as a role-less
    :class:`CameraDevice`. Roles (``gripper``/``scene``) are assigned downstream
    (the System window's camera step / ``POST /cameras/roles``).
    """
    return [
        CameraDevice(path=d["path"], name=d["name"])
        for d in list_video_devices()
    ]
