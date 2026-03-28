"""USB device scanning, attachment, and identification.

Handles:
  - Listing USB devices via usbipd on Windows
  - Attaching USB devices to WSL2 via usbipd
  - Identifying leader/follower arms via identify_arm.py inside Docker
  - Camera discovery via v4l2-ctl inside WSL2
"""

import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from . import wsl_bridge
from .constants import ROBOTIS_VID


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


@dataclass
class HardwareConfig:
    """Complete hardware configuration ready for .env generation."""
    leader: Optional[ArmDevice] = None
    follower: Optional[ArmDevice] = None
    camera: Optional[CameraDevice] = None

    @property
    def is_complete(self) -> bool:
        return self.leader is not None and self.follower is not None


def list_usb_devices() -> list[USBDevice]:
    """List all USB devices visible to usbipd."""
    try:
        result = subprocess.run(
            ["usbipd", "list"],
            capture_output=True, text=True, timeout=10,
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


def attach_usb_to_wsl(busid: str) -> bool:
    """Attach a USB device to WSL2 via usbipd.

    With usbipd 4.x+ policy configured, this does not require admin.
    """
    try:
        result = subprocess.run(
            ["usbipd", "attach", "--wsl", "--busid", busid],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def detach_usb_from_wsl(busid: str) -> bool:
    """Detach a USB device from WSL2."""
    try:
        result = subprocess.run(
            ["usbipd", "detach", "--busid", busid],
            capture_output=True, text=True, timeout=10,
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


def identify_arm_via_docker(serial_path: str) -> str:
    """Run identify_arm.py inside the open_manipulator container.

    Returns: "leader", "follower", "unknown", or "error:..."
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", "robotis_arm_scanner",
                "python3", "/usr/local/bin/identify_arm.py", serial_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip() if result.returncode == 0 else f"error:{result.stderr.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"error:{e}"


def start_scanner_container(image: str) -> bool:
    """Start a temporary container for arm identification.

    Uses the open_manipulator image with a sleep command so we can
    docker exec identify_arm.py into it.
    """
    # Stop any existing scanner
    subprocess.run(
        ["docker", "rm", "-f", "robotis_arm_scanner"],
        capture_output=True, timeout=10,
    )
    try:
        result = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", "robotis_arm_scanner",
                "--privileged",
                "-v", "/dev:/dev",
                "--entrypoint", "sleep",
                image,
                "120",
            ],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def stop_scanner_container():
    """Remove the temporary scanner container."""
    subprocess.run(
        ["docker", "rm", "-f", "robotis_arm_scanner"],
        capture_output=True, timeout=10,
    )


def scan_and_identify_arms(image: str) -> tuple[Optional[ArmDevice], Optional[ArmDevice]]:
    """Full scan workflow: attach USB, start scanner, identify arms.

    Returns (leader, follower) — either may be None if not found.
    """
    leader = None
    follower = None

    # 1. Attach all EduBotics USB devices to WSL2
    attached = attach_all_robotis_devices()
    if not attached:
        return None, None

    # 2. Wait a moment for udev to create /dev/serial/by-id/ entries
    import time
    time.sleep(2)

    # 3. Find serial paths
    serial_paths = find_serial_paths_for_robotis()
    if not serial_paths:
        return None, None

    # 4. Start temporary container for identification
    if not start_scanner_container(image):
        return None, None

    try:
        # Give container a moment to start
        time.sleep(1)

        # 5. Identify each serial device
        for path in serial_paths:
            role = identify_arm_via_docker(path)
            # Find the matching USB device for description
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
