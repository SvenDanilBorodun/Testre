"""USB device scanning, attachment, and identification.

Handles:
  - Listing USB devices via usbipd on Windows
  - Attaching USB devices to WSL2 via usbipd
  - Identifying leader/follower arms via identify_arm.py inside Docker
  - Camera discovery via v4l2-ctl inside WSL2
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}

from . import wsl_bridge
from .constants import ROBOTIS_VID, WSL_DISTRO_NAME


@dataclass
class USBDevice:
    """A USB device visible to usbipd."""
    busid: str
    vid_pid: str
    description: str
    state: str  # "Not shared", "Shared", "Attached"


@dataclass
class ArmDevice:
    """An identified robot arm (leader or follower)."""
    busid: str
    serial_path: str  # /dev/serial/by-id/...
    role: str  # "leader" or "follower"
    description: str


@dataclass
class CameraDevice:
    """A video capture device."""
    path: str  # /dev/video0
    name: str  # Human-readable name like "Logitech C920"
    role: str = ""  # "gripper" or "scene" — assigned by student in GUI


@dataclass
class HardwareConfig:
    """Complete hardware configuration ready for .env generation."""
    leader: Optional[ArmDevice] = None
    follower: Optional[ArmDevice] = None
    cameras: list = field(default_factory=list)  # list[CameraDevice], supports 0-N cameras

    @property
    def is_complete(self) -> bool:
        return self.leader is not None and self.follower is not None


def list_usb_devices() -> list[USBDevice]:
    """List all USB devices visible to usbipd."""
    try:
        result = subprocess.run(
            ["usbipd", "list"],
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        if result.returncode != 0:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    devices = []
    for line in result.stdout.splitlines():
        # Parse usbipd list output: "1-3    2f5d:0103  OpenRB-150    Not shared"
        match = re.match(
            r"\s*(\d+-\d+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.+?)\s+(Not shared|Shared|Attached)\s*$",
            line,
        )
        if match:
            devices.append(USBDevice(
                busid=match.group(1),
                vid_pid=match.group(2),
                description=match.group(3).strip(),
                state=match.group(4),
            ))
    return devices


def list_robotis_devices() -> list[USBDevice]:
    """Filter USB devices to only ROBOTIS ones (VID 2F5D)."""
    return [d for d in list_usb_devices() if d.vid_pid.upper().startswith(ROBOTIS_VID)]


def attach_usb_to_wsl(busid: str, retries: int = 3) -> bool:
    """Attach a USB device to the EduBotics WSL2 distro via usbipd, with retry.

    With usbipd 4.x+ policy configured, this does not require admin.
    Retries on failure because usbipd can be busy if multiple attaches
    happen in quick succession.

    Pins the target distro so multi-distro dev machines attach deterministically.
    """
    import time
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["usbipd", "attach", "--wsl", "--distribution", WSL_DISTRO_NAME,
                 "--busid", busid],
                capture_output=True, text=True, timeout=15,
                **_SUBPROCESS_KWARGS,
            )
            if result.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False  # usbipd not installed — no point retrying
        if attempt < retries - 1:
            time.sleep(1)
    return False


def detach_usb_from_wsl(busid: str) -> bool:
    """Detach a USB device from WSL2."""
    try:
        result = subprocess.run(
            ["usbipd", "detach", "--busid", busid],
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def attach_all_robotis_devices() -> list[USBDevice]:
    """Attach all EduBotics USB devices to WSL2. Returns list of attached devices."""
    devices = list_robotis_devices()
    attached = []
    for dev in devices:
        if dev.state != "Attached":
            if attach_usb_to_wsl(dev.busid):
                dev.state = "Attached"
                attached.append(dev)
            # else: failed to attach, skip
        else:
            attached.append(dev)
    return attached


def find_serial_paths_for_robotis() -> list[str]:
    """Find /dev/serial/by-id/ paths for EduBotics devices inside WSL2."""
    all_serial = wsl_bridge.list_serial_devices()
    return [p for p in all_serial if "ROBOTIS" in p.upper() or "OPENRB" in p.upper()]


def _docker(*args: str) -> list[str]:
    """Build a `wsl -d EduBotics -- docker ...` command."""
    return ["wsl", "-d", WSL_DISTRO_NAME, "--", "docker", *args]


def identify_arm_via_docker(serial_path: str) -> str:
    """Run identify_arm.py inside the open_manipulator container.

    Returns: "leader", "follower", "unknown", or "error:..."
    """
    try:
        result = subprocess.run(
            _docker("exec", "robotis_arm_scanner",
                    "python3", "/usr/local/bin/identify_arm.py", serial_path),
            capture_output=True, text=True, timeout=15,
            **_SUBPROCESS_KWARGS,
        )
        return result.stdout.strip() if result.returncode == 0 else f"error:{result.stderr.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"error:{e}"


def start_scanner_container(image: str) -> bool:
    """Start a temporary container (inside the EduBotics distro) for arm identification."""
    # Stop any existing scanner
    subprocess.run(
        _docker("rm", "-f", "robotis_arm_scanner"),
        capture_output=True, timeout=10,
        **_SUBPROCESS_KWARGS,
    )
    try:
        result = subprocess.run(
            _docker("run", "-d",
                    "--name", "robotis_arm_scanner",
                    "--privileged",
                    "-v", "/dev:/dev",
                    "--entrypoint", "sleep",
                    image,
                    "120"),
            capture_output=True, text=True, timeout=30,
            **_SUBPROCESS_KWARGS,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def stop_scanner_container():
    """Remove the temporary scanner container."""
    subprocess.run(
        _docker("rm", "-f", "robotis_arm_scanner"),
        capture_output=True, timeout=10,
        **_SUBPROCESS_KWARGS,
    )


def scan_and_identify_arms(image: str) -> tuple[Optional[ArmDevice], Optional[ArmDevice]]:
    """Full scan workflow: attach USB, start scanner, identify arms.

    Returns (leader, follower) — either may be None if not found.
    """
    import time

    leader = None
    follower = None

    # 1. Attach all EduBotics USB devices to WSL2
    attached = attach_all_robotis_devices()
    if not attached:
        return None, None

    # 2. Poll for serial paths (udev can take 1-10s depending on machine)
    serial_paths = []
    for _ in range(10):
        serial_paths = find_serial_paths_for_robotis()
        if serial_paths:
            break
        time.sleep(1)
    if not serial_paths:
        return None, None

    # 3. Start temporary container for identification
    if not start_scanner_container(image):
        return None, None

    try:
        time.sleep(1)

        # 4. Identify each serial device (retry once on error/unknown)
        for i, path in enumerate(serial_paths):
            if i > 0:
                time.sleep(1)  # Let USB bus settle between devices
            role = identify_arm_via_docker(path)
            if role.startswith("error:") or role == "unknown":
                time.sleep(2)
                role = identify_arm_via_docker(path)

            desc = path.split("/")[-1]
            busid = ""
            for dev in attached:
                if any(part in path for part in dev.description.split()):
                    busid = dev.busid
                    desc = dev.description
                    break

            if role == "leader":
                leader = ArmDevice(busid=busid, serial_path=path, role="leader", description=desc)
            elif role == "follower":
                follower = ArmDevice(busid=busid, serial_path=path, role="follower", description=desc)
    finally:
        stop_scanner_container()

    return leader, follower


def scan_cameras() -> list[CameraDevice]:
    """Scan for video capture devices available in WSL2."""
    raw = wsl_bridge.list_video_devices()
    return [CameraDevice(path=d["path"], name=d["name"]) for d in raw]
