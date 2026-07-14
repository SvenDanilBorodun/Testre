"""Host-side arm scanner + leader/follower disambiguation for the Orange Pi.

This is the HOST orchestrator, ported from ``gui/app/device_manager``'s
``scan_and_identify_arms`` / ``fast_rehydrate_arms`` — run NATIVELY (plain
``docker ...``, no ``wsl -d`` wrapper) and with all usbipd / WSL self-heal
stages removed (on the Pi the arms are directly present on
``/dev/serial/by-id/`` via native udev; there is nothing to attach or heal).

The actual leader-vs-follower ping happens INSIDE a throwaway scanner
container, in the in-image script ``/usr/local/bin/identify_arm.py`` (shipped
in ``open-manipulator-opi``, source: ``docker/open_manipulator/identify_arm.py``)
— it pings Dynamixel servo IDs 1-6 (leader) vs 11-16 (follower) and prints
``leader`` / ``follower`` / ``unknown``. This module NEVER opens a serial port
itself; that must stay in the container that carries ``dynamixel_sdk``.

**Two-identical-arms disambiguation (design decision).** Both OpenMANIPULATOR
arms are OpenRB-150 boards with the SAME VID ``2F5D`` / PIDs ``0103|2202``, so a
static udev VID/PID rule cannot tell leader from follower (unlike the Jetson,
which has ONE arm). We follow the camera precedent and disambiguate by SERVO-ID
PING at scan time, then persist each role to its STABLE
``/dev/serial/by-id/...`` path (the arms have DISTINCT stable serials —
identical-serial is a camera-only problem, per CLAUDE.md). The agent writes
``LEADER_PORT`` / ``FOLLOWER_PORT`` managed ``.env`` keys with those by-id paths.

Chosen over serial-keyed dynamic udev symlinks (``/dev/edubotics-{leader,
follower}``) because it needs NO extra udev machinery: the by-id path is already
stable per physical arm and survives replug, and the compose references
``${FOLLOWER_PORT}`` / ``${LEADER_PORT}`` (student-file style), NOT the Jetson's
hardcoded ``/dev/edubotics-follower``. The static ROBOTIS VID/PID udev rule
(``pi_agent/udev/99-edubotics-robotis.rules``, another stream) remains useful
only for group permissions / a last-resort by-id fallback, not for the role
mapping.

All log/comment strings are English (Rule §1); this module produces no
student-facing text.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Optional

# ArmDevice is defined in config_generator (the Pi has no device_manager) and the
# scanning modules BUILD it — the config_generator docstring pins this ownership.
# Importing it here (rather than redefining) keeps ONE dataclass shape so
# generate_env_file reads .serial_path off exactly the object we produced.
from .config_generator import ArmDevice

logger = logging.getLogger("edubotics-pi-agent")

# The scanner container name + the in-image script path (same as the GUI).
SCANNER_CONTAINER_NAME = "robotis_arm_scanner"
IN_CONTAINER_SCRIPT = "/usr/local/bin/identify_arm.py"

# by-id substrings that mark an EduBotics arm (OpenRB-150). Same filter the GUI
# uses in ``find_serial_paths_for_robotis``. The FTDI legacy path is only a
# udev fallback and is intentionally not matched here.
_ARM_MARKERS = ("ROBOTIS", "OPENRB")

# Serial-path polling: native udev usually links a plugged arm within ~1 s, but
# a replug right before a scan can lag. Poll a few times before giving up.
_SERIAL_POLL_ATTEMPTS = 10
_SERIAL_POLL_INTERVAL_S = 1.0
_SERIAL_BY_ID_DIR = "/dev/serial/by-id"


def _docker(*args: str) -> list[str]:
    """Build a native ``docker ...`` argv (no ``wsl -d`` wrapper on the Pi)."""
    return ["docker", *args]


def list_serial_by_id() -> list[str]:
    """List full ``/dev/serial/by-id/...`` paths present on the host.

    Native replacement for ``wsl_bridge.list_serial_devices`` — reads the udev
    by-id directory directly. Returns ``[]`` if the directory is absent (no
    serial devices linked yet).
    """
    try:
        entries = sorted(os.listdir(_SERIAL_BY_ID_DIR))
    except OSError:
        return []
    return [f"{_SERIAL_BY_ID_DIR}/{e}" for e in entries]


def find_robotis_serial_paths() -> list[str]:
    """Find ``/dev/serial/by-id/`` paths for EduBotics arms (ROBOTIS/OpenRB)."""
    return [
        p for p in list_serial_by_id()
        if any(m in p.upper() for m in _ARM_MARKERS)
    ]


def start_scanner_container(image: str) -> bool:
    """Start a throwaway privileged container that carries ``dynamixel_sdk`` +
    the in-image ``identify_arm.py``, so we can ping arms without opening the
    serial port on the host. ``--entrypoint sleep ... 120`` keeps it idle so we
    ``exec`` the ping per device (same shape as the GUI scanner)."""
    # Remove any stale scanner first (idempotent).
    subprocess.run(
        _docker("rm", "-f", SCANNER_CONTAINER_NAME),
        capture_output=True, timeout=10,
    )
    try:
        result = subprocess.run(
            _docker("run", "-d",
                    "--name", SCANNER_CONTAINER_NAME,
                    "--privileged",
                    "-v", "/dev:/dev",
                    "--entrypoint", "sleep",
                    image,
                    "120"),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("scanner container failed to start: %s",
                           (result.stderr or "").strip())
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("scanner container start error: %s", exc)
        return False


def stop_scanner_container() -> None:
    """Remove the throwaway scanner container (best-effort, idempotent)."""
    subprocess.run(
        _docker("rm", "-f", SCANNER_CONTAINER_NAME),
        capture_output=True, timeout=10,
    )


def identify_arm_via_docker(serial_path: str) -> str:
    """Run the in-image ``identify_arm.py`` against ``serial_path`` inside the
    running scanner container.

    Returns ``"leader"``, ``"follower"``, ``"unknown"``, or ``"error:..."`` —
    exactly the GUI contract, so the caller's retry/branch logic is unchanged.
    """
    try:
        result = subprocess.run(
            _docker("exec", SCANNER_CONTAINER_NAME,
                    "python3", IN_CONTAINER_SCRIPT, serial_path),
            capture_output=True, text=True, timeout=15,
        )
        return (result.stdout.strip() if result.returncode == 0
                else f"error:{result.stderr.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"error:{exc}"


def _poll_serial_paths(expected: Optional[set[str]] = None) -> list[str]:
    """Poll for ROBOTIS serial paths. If ``expected`` is given, stop as soon as
    every expected path is present; otherwise stop on the first non-empty list.
    """
    paths: list[str] = []
    for _ in range(_SERIAL_POLL_ATTEMPTS):
        paths = find_robotis_serial_paths()
        if expected is not None:
            if expected.issubset(set(paths)):
                return paths
        elif paths:
            return paths
        time.sleep(_SERIAL_POLL_INTERVAL_S)
    return paths


def scan_and_identify_arms(
    image: str,
) -> tuple[Optional[ArmDevice], Optional[ArmDevice]]:
    """Full scan workflow: enumerate serial ports, ping each in the scanner
    container, return ``(leader, follower)`` — either may be ``None`` if not
    found.

    Native port of ``device_manager.scan_and_identify_arms`` minus the WSL
    self-heal + usbipd attach stages (the Pi has neither). The Dynamixel serial
    bus MUST be free before this runs — the agent stops the robot tier first
    (``docker_manager.ensure_environment_stopped``), exactly as the GUI does,
    because a live 100 Hz controller holds the same ``/dev/serial`` ports the
    ping opens.
    """
    leader: Optional[ArmDevice] = None
    follower: Optional[ArmDevice] = None

    # 1. Enumerate the arm serial ports (poll — udev can lag on a fresh replug).
    serial_paths = _poll_serial_paths()
    if not serial_paths:
        logger.warning("no ROBOTIS serial ports found under %s", _SERIAL_BY_ID_DIR)
        return None, None

    # 2. Start the throwaway scanner container for the pings.
    if not start_scanner_container(image):
        return None, None

    try:
        time.sleep(1)  # let the container's python come up

        # 3. Identify each serial device (retry once on error/unknown).
        for i, path in enumerate(serial_paths):
            if i > 0:
                time.sleep(1)  # let the USB bus settle between devices
            role = identify_arm_via_docker(path)
            if role.startswith("error:") or role == "unknown":
                time.sleep(2)
                role = identify_arm_via_docker(path)

            desc = path.split("/")[-1]
            if role == "leader":
                leader = ArmDevice(serial_path=path, role="leader", description=desc)
            elif role == "follower":
                follower = ArmDevice(serial_path=path, role="follower", description=desc)
            else:
                logger.info("serial %s did not identify as an arm (%s)", desc, role)
    finally:
        stop_scanner_container()

    return leader, follower


def fast_rehydrate_arms(
    saved_leader_path: str, saved_follower_path: str,
) -> tuple[Optional[ArmDevice], Optional[ArmDevice]]:
    """Light revalidation of the previous session's arm mapping.

    Native port of ``device_manager.fast_rehydrate_arms``: skips the two SLOW
    stages (the throwaway scanner container and the per-device serial pings) and
    only confirms BOTH saved ``/dev/serial/by-id`` paths are present and
    distinct. The path↔role binding is trusted from the saved ``.env``: ROBOTIS
    arms expose DISTINCT stable by-id serials (identical-serial is camera-only,
    CLAUDE.md), so a binding cannot silently swap between sessions — the path
    either reappears as the same physical arm or does not appear at all. ANY
    mismatch returns ``(None, None)`` and the caller falls back to the full
    scan.
    """
    if (not saved_leader_path or not saved_follower_path
            or saved_leader_path == saved_follower_path):
        return None, None

    expected = {saved_leader_path, saved_follower_path}
    serial_paths = set(_poll_serial_paths(expected=expected))
    if not expected.issubset(serial_paths):
        return None, None

    def _rebuild(path: str, role: str) -> ArmDevice:
        return ArmDevice(serial_path=path, role=role, description=path.split("/")[-1])

    return (
        _rebuild(saved_leader_path, "leader"),
        _rebuild(saved_follower_path, "follower"),
    )
