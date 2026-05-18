"""USB device scanning, attachment, and identification.

Handles:
  - Listing USB devices via usbipd on Windows
  - Attaching USB devices to WSL2 via usbipd
  - Identifying leader/follower arms via identify_arm.py inside Docker
  - Camera discovery via v4l2-ctl inside WSL2
  - Diagnostics that produce actionable German error messages instead of a
    silent "Nicht gefunden" when something between Windows and the arm fails.
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}

from . import wsl_bridge
from .constants import ROBOTIS_VID, WSL_DISTRO_NAME


def _diagnostics_log_path() -> str:
    """Where install/runtime diagnostics get appended."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "EduBotics", "install_diagnostics.log")


def _append_diag(section: str, body: str) -> None:
    """Append a timestamped block to the diagnostics log. Best-effort."""
    try:
        path = Path(_diagnostics_log_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} {section} ===\n")
            fh.write(body.rstrip() + "\n")
    except OSError:
        pass


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


# usbipd 5.x splits output into "Connected:" and "Persisted:" sections; older
# 4.x prints a flat table. We parse strict first, then fall back to a VID:PID-
# only relaxed parse so a tiny column-format change in a future usbipd doesn't
# silently turn into "Nicht gefunden" for the student.
_USBIPD_STRICT_RE = re.compile(
    r"\s*(\d+-\d+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.+?)\s+"
    r"(Not shared|Shared|Attached|Forced|Shared \(forced\))\s*$"
)
_USBIPD_RELAXED_RE = re.compile(
    r"\s*(\d+-\d+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.+?)\s*$"
)


class UsbipdMissingError(RuntimeError):
    """`usbipd` is not on PATH (installer did not run, or PATH not refreshed yet)."""


def _run_usbipd_list() -> Optional[str]:
    """Run `usbipd list` and return raw stdout (combined with stderr).

    Returns None if the binary is missing or the call times out. Logs raw
    output to the diagnostics file regardless of outcome so a maintainer
    can read what Windows actually said when the student hit "Nicht
    gefunden".
    """
    try:
        result = subprocess.run(
            ["usbipd", "list"],
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
    except FileNotFoundError:
        _append_diag("usbipd_list", "FileNotFoundError: usbipd not on PATH")
        raise UsbipdMissingError("usbipd is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        _append_diag("usbipd_list", "TimeoutExpired after 10s")
        return None

    raw = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    _append_diag(
        "usbipd_list",
        f"rc={result.returncode}\n{raw}",
    )
    if result.returncode != 0:
        return raw  # parse what we can — usbipd sometimes prints valid rows
                    # before a non-fatal stderr line
    return raw


def list_usb_devices() -> list[USBDevice]:
    """List all USB devices visible to usbipd.

    Returns empty list on missing/failing usbipd. Use `_run_usbipd_list()`
    + a UsbipdMissingError catch when you need to distinguish "binary
    missing" from "no devices visible".
    """
    try:
        raw = _run_usbipd_list()
    except UsbipdMissingError:
        return []
    if raw is None:
        return []

    devices: list[USBDevice] = []
    skip_section = False
    for line in raw.splitlines():
        stripped = line.strip()
        # usbipd 5.x section headers — table follows on subsequent lines.
        if stripped == "Connected:":
            skip_section = False
            continue
        if stripped == "Persisted:":
            # Persisted entries have no BUSID and cannot be attached. Skip.
            skip_section = True
            continue
        if skip_section:
            continue
        if not stripped or stripped.startswith("BUSID"):
            continue

        m = _USBIPD_STRICT_RE.match(line)
        if m:
            devices.append(USBDevice(
                busid=m.group(1),
                vid_pid=m.group(2),
                description=m.group(3).strip(),
                state=m.group(4),
            ))
            continue

        # Relaxed fallback — preserves the BUSID + VID:PID even when the
        # state column drifts. State stays "Unknown" so callers can still
        # decide to try attach.
        m2 = _USBIPD_RELAXED_RE.match(line)
        if m2:
            devices.append(USBDevice(
                busid=m2.group(1),
                vid_pid=m2.group(2),
                description=m2.group(3).strip(),
                state="Unknown",
            ))

    if not devices:
        _append_diag(
            "usbipd_list_parse",
            "No devices parsed. If usbipd output above is non-empty the "
            "regex needs an update."
        )
    return devices


def list_robotis_devices() -> list[USBDevice]:
    """Filter USB devices to only ROBOTIS ones (VID 2F5D)."""
    return [d for d in list_usb_devices() if d.vid_pid.upper().startswith(ROBOTIS_VID)]


def attach_usb_to_wsl(busid: str, retries: int = 3) -> bool:
    """Attach a USB device to the EduBotics WSL2 distro via usbipd, with retry.

    With usbipd 5.x AutoBind policy configured, this does not require admin.
    Retries on failure because usbipd can be busy if multiple attaches
    happen in quick succession.

    Pins the target distro so multi-distro dev machines attach deterministically.
    usbipd 5.x takes the distro as the value of --wsl (positional). The older
    `--wsl --distribution <name>` form is rejected by 5.x with
    "Unrecognized command or argument '<name>'" — keep --wsl <distro> adjacent.
    """
    import time
    last_stderr = ""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["usbipd", "attach", "--wsl", WSL_DISTRO_NAME, "--busid", busid],
                capture_output=True, text=True, timeout=15,
                **_SUBPROCESS_KWARGS,
            )
            if result.returncode == 0:
                return True
            last_stderr = (result.stderr or result.stdout or "").strip()
        except FileNotFoundError:
            _append_diag(
                "attach_usb_to_wsl",
                f"busid={busid}: usbipd.exe not on PATH",
            )
            return False
        except subprocess.TimeoutExpired:
            last_stderr = "subprocess timeout after 15s"
        if attempt < retries - 1:
            time.sleep(1)
    _append_diag(
        "attach_usb_to_wsl",
        f"busid={busid} distro={WSL_DISTRO_NAME} failed after {retries} attempts\n"
        f"stderr: {last_stderr}",
    )
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


_SELF_HEAL_SCRIPT = r"""#!/bin/bash
# EduBotics scan-time self-heal. Idempotent. Runs in two cases:
#   1. start-dockerd.sh already loaded modules + started udev — we re-trigger
#      udev to catch any device usbipd attached since the last trigger
#      (no-op cost is ~50ms).
#   2. The user is on a pre-2.3.1 rootfs where start-dockerd.sh did NOT
#      load the modules. Loading them here unblocks the scan that's about
#      to happen.
set +e
modprobe cdc_acm 2>/dev/null
modprobe ftdi_sio 2>/dev/null
modprobe cp210x 2>/dev/null
modprobe ch341 2>/dev/null
modprobe pl2303 2>/dev/null
modprobe uvcvideo 2>/dev/null   # webcams (Logitech C920 etc.) — same race as USB-Serial
if [ -x /lib/systemd/systemd-udevd ] && ! pgrep -x systemd-udevd >/dev/null 2>&1; then
    /lib/systemd/systemd-udevd --daemon 2>/dev/null
    sleep 1
fi
udevadm trigger --action=add --subsystem-match=tty 2>/dev/null
udevadm settle --timeout=3 2>/dev/null
# Fallback: if /dev/serial/by-id/ still missing, build the symlinks
# manually from sysfs. This handles pre-2.3.1 setups where systemd-udevd
# is missing from the rootfs entirely.
mkdir -p /dev/serial/by-id 2>/dev/null
for tty in /dev/ttyACM* /dev/ttyUSB*; do
    [ -e "$tty" ] || continue
    name=$(basename "$tty")
    if ls /dev/serial/by-id/ 2>/dev/null | grep -q "$name"; then
        continue  # udev already linked this one
    fi
    syspath=$(readlink -f "/sys/class/tty/$name/device" 2>/dev/null) || continue
    cur=$syspath
    serial=""
    manufacturer=""
    product=""
    while [ -n "$cur" ] && [ "$cur" != "/" ]; do
        if [ -f "$cur/serial" ]; then
            serial=$(cat "$cur/serial" 2>/dev/null)
            manufacturer=$(cat "$cur/manufacturer" 2>/dev/null | tr ' ' '_')
            product=$(cat "$cur/product" 2>/dev/null | tr ' ' '_')
            break
        fi
        cur=$(dirname "$cur")
    done
    [ -n "$serial" ] || continue
    link="/dev/serial/by-id/usb-${manufacturer}_${product}_${serial}-if00"
    ln -sf "../../$name" "$link" 2>/dev/null
done
"""


def self_heal_wsl_serial() -> dict:
    """Heal the EduBotics distro's USB-Serial state.

    Audit: this is the GUI-side counterpart to start-dockerd.sh's USB-
    serial bring-up. New installs (v2.3.1+) get the same logic on every
    distro boot, but classrooms running older bundles never picked up the
    fix and would silently fail with "/dev/ttyACM* missing" the moment
    `wsl --shutdown` ran (kernel modules unload, udev never started,
    by-id/ symlinks gone). This helper runs the same heal sequence from
    the GUI's scan path so an upgrade to the .exe alone is enough to
    rescue students on stale rootfs.

    Returns a small diagnostic dict for the install_diagnostics.log so
    a teacher can see *which* layer needed repair on each scan.
    """
    diag = {"ran": False, "stdout": "", "stderr": "", "rc": None}
    try:
        result = subprocess.run(
            ["wsl", "-d", WSL_DISTRO_NAME, "--", "bash", "-c", _SELF_HEAL_SCRIPT],
            capture_output=True, text=True, timeout=15,
            **_SUBPROCESS_KWARGS,
        )
        diag["ran"] = True
        diag["rc"] = result.returncode
        diag["stdout"] = (result.stdout or "")[-500:]
        diag["stderr"] = (result.stderr or "")[-500:]
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        diag["stderr"] = f"{type(exc).__name__}: {exc}"
    _append_diag(
        "self_heal_wsl_serial",
        f"rc={diag['rc']} stdout={diag['stdout']!r} stderr={diag['stderr']!r}",
    )
    return diag


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

    # 0. Self-heal the distro before we even look. On older rootfs (pre-2.3.1)
    # the kernel modules for USB-Serial don't auto-load and systemd-udevd
    # never starts. Without this call, the scan would race through attach +
    # by-id poll and time out on a distro that just needed `modprobe cdc_acm`.
    # No-op cost on healthy 2.3.1+ rootfs is ~50ms.
    self_heal_wsl_serial()

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
        # Last-ditch: maybe udev silently dropped the events. Run self-heal
        # again now that the USB devices ARE attached — this guarantees
        # symlinks get rebuilt from sysfs as the fallback path.
        self_heal_wsl_serial()
        serial_paths = find_serial_paths_for_robotis()
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


def _list_usb_camera_vid_pids() -> list[tuple[str, str]]:
    """Query Windows PnP for currently-connected USB cameras.

    Returns [(vid_pid, friendly_name), ...] where vid_pid is "vvvv:pppp"
    (lower-case hex). Used by scan_cameras() to figure out which usbipd
    BUSIDs to attach to WSL — without this, a student plugs in a webcam
    and the GUI's `Kameras scannen` button silently finds nothing
    (the camera sits in usbipd's `Not shared` state and never reaches
    /dev/video* inside the distro).
    """
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                # `Class Camera` covers Win10/11 modern UVC drivers; some
                # older webcams still register under `Image`. Both filters
                # are cheap so we run them together.
                "Get-PnpDevice -Class Camera, Image -PresentOnly | "
                "Where-Object Status -eq 'OK' | "
                "ForEach-Object { "
                "  if ($_.InstanceId -match 'USB\\\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})') { "
                "    '{0}:{1}|{2}' -f $matches[1].ToLower(), $matches[2].ToLower(), $_.FriendlyName "
                "  } "
                "}",
            ],
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    cameras = []
    seen = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        vid_pid, name = line.split("|", 1)
        if vid_pid in seen:
            continue
        seen.add(vid_pid)
        cameras.append((vid_pid, name.strip()))
    return cameras


def attach_cameras_to_wsl() -> list[USBDevice]:
    """Attach all USB cameras that Windows can see, via usbipd.

    Mirrors `attach_all_robotis_devices()` for cameras. Without this the
    GUI's camera scan would only find devices a student had manually
    `usbipd attach`ed beforehand — impossible UX. Only attaches devices
    that already have a usbipd policy (state `Shared` / `Allowed`);
    devices in `Not shared` state need admin bind first (added by
    configure_usbipd.ps1 at install time for currently-connected
    cameras, then policy=AutoBind takes over for future replugs).

    A `Not shared` camera is silently skipped — the diagnostic log
    captures it so support can see "this student plugged in a new
    webcam after install and we never bound its VID:PID".
    """
    camera_vid_pids = {vp for vp, _ in _list_usb_camera_vid_pids()}
    if not camera_vid_pids:
        return []
    attached: list[USBDevice] = []
    skipped_not_shared: list[USBDevice] = []
    for dev in list_usb_devices():
        if dev.vid_pid.lower() not in camera_vid_pids:
            continue
        if dev.state == "Attached":
            attached.append(dev)
            continue
        if dev.state == "Not shared":
            skipped_not_shared.append(dev)
            continue
        # State is Shared / Allowed / Persisted — attach is non-admin.
        if attach_usb_to_wsl(dev.busid):
            dev.state = "Attached"
            attached.append(dev)
    if skipped_not_shared:
        details = ", ".join(
            f"{d.busid} ({d.vid_pid}) {d.description}"
            for d in skipped_not_shared
        )
        _append_diag(
            "attach_cameras_to_wsl",
            f"skipped (need admin bind): {details}",
        )
    return attached


def scan_cameras() -> list[CameraDevice]:
    """Scan for video capture devices available in WSL2.

    Three-step auto-recovery:
      1. self_heal_wsl_serial() loads uvcvideo (+ udev + USB-Serial mods)
      2. attach_cameras_to_wsl() auto-attaches Windows-visible webcams
         that have a usbipd policy (set up by configure_usbipd.ps1 for
         devices plugged in at install time)
      3. wsl_bridge.list_video_devices() lists /dev/video* + by-id
    """
    self_heal_wsl_serial()
    attach_cameras_to_wsl()
    raw = wsl_bridge.list_video_devices()
    return [CameraDevice(path=d["path"], name=d["name"]) for d in raw]


# ── Diagnostics ─────────────────────────────────────────────────────────────
# When the GUI's arm scan returns (None, None), we want to tell the student
# *why* — not just "Nicht gefunden". The diagnosis below walks the same chain
# the scan walks and reports the FIRST link that broke.


def _windows_sees_robotis_vid() -> Optional[bool]:
    """Ask Windows itself (not usbipd) whether VID_2F5D is enumerated.

    PowerShell's Get-PnpDevice covers devices that exist on the USB bus
    even if usbipd doesn't recognize them yet. Returns True/False, or
    None if PowerShell isn't reachable.
    """
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-PnpDevice -PresentOnly | "
                "Where-Object { $_.InstanceId -like '*VID_2F5D*' } | "
                "Select-Object -ExpandProperty InstanceId",
            ],
            capture_output=True, text=True, timeout=15,
            **_SUBPROCESS_KWARGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    _append_diag("pnp_vid_2f5d", f"rc={result.returncode}\n{result.stdout}\n{result.stderr}")
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


@dataclass
class UsbDiagnosis:
    """Why arm scanning failed — populated by `diagnose_usb_environment`.

    Exactly one of the boolean reasons should be True. `message_de` is the
    student-facing German error; `details` is a short technical addendum
    safe to show in a log pane.
    """
    usbipd_missing: bool = False
    wsl_distro_missing: bool = False
    windows_sees_no_robotis: bool = False
    usbipd_sees_no_robotis: bool = False
    attach_failed: bool = False
    no_serial_after_attach: bool = False
    docker_image_missing: bool = False
    ok: bool = False
    message_de: str = ""
    details: str = ""


def diagnose_usb_environment(image: Optional[str] = None) -> UsbDiagnosis:
    """Walk the host → WSL → docker chain and return the first failure.

    Args:
        image: Open-manipulator image used by the scanner container. If
            None, the docker-image check is skipped (caller doesn't care).
    """
    diag = UsbDiagnosis()

    # 1. usbipd reachable?
    try:
        raw = _run_usbipd_list()
    except UsbipdMissingError:
        diag.usbipd_missing = True
        diag.message_de = (
            "usbipd ist nicht installiert oder nicht im Suchpfad. "
            "Bitte den EduBotics-Installer erneut ausführen und Windows neu starten."
        )
        diag.details = "usbipd.exe not on PATH"
        return diag

    if raw is None:
        diag.usbipd_missing = True
        diag.message_de = (
            "usbipd hat nicht geantwortet (Zeitüberschreitung). "
            "Bitte den PC neu starten und erneut versuchen."
        )
        diag.details = "usbipd list timed out"
        return diag

    # 2. WSL distro registered?
    if not wsl_bridge.is_edubotics_distro_registered():
        diag.wsl_distro_missing = True
        diag.message_de = (
            "Die EduBotics-WSL-Umgebung ist nicht registriert. "
            "Bitte den Installer erneut ausführen."
        )
        diag.details = f"wsl --list --quiet does not contain {WSL_DISTRO_NAME!r}"
        return diag

    # 2b. Self-heal: load USB-Serial kernel modules + start udev. If the
    # rootfs is older than v2.3.1 (no auto-load in start-dockerd.sh) the
    # scan failed because /dev/ttyACM* was never created — this diagnose
    # call should fix that BEFORE we report a misleading "kein Gerät"
    # error to the student.
    self_heal_wsl_serial()

    # 3. Does Windows itself see a VID_2F5D device?
    windows_sees = _windows_sees_robotis_vid()
    robotis = list_robotis_devices()

    if not robotis:
        if windows_sees is False:
            diag.windows_sees_no_robotis = True
            diag.message_de = (
                "Windows erkennt kein ROBOTIS-Gerät (VID 2F5D). "
                "Bitte folgendes prüfen:\n"
                "  • USB-Kabel ist eingesteckt UND ein Datenkabel (kein Ladekabel)\n"
                "  • OpenRB-150 ist mit Strom versorgt (Netzteil eingesteckt)\n"
                "  • Anderen USB-Port am PC versuchen (direkt am Mainboard, keinen Hub)\n"
                "  • Im Geräte-Manager nach 'OpenRB' oder gelben Warndreiecken suchen"
            )
            diag.details = "Get-PnpDevice found no InstanceId matching VID_2F5D"
            return diag

        # Windows might be unavailable (Get-PnpDevice failed) OR Windows sees
        # the device but usbipd doesn't yet — both end up here.
        diag.usbipd_sees_no_robotis = True
        diag.message_de = (
            "usbipd hat keine ROBOTIS-Geräte gefunden. "
            "Bitte USB-Kabel und Stromversorgung der Arme prüfen "
            "und es erneut versuchen."
            + (
                "\n(Hinweis: Windows erkennt das Gerät — vermutlich ein Treiber-Problem. "
                "Im Geräte-Manager nach gelben Warndreiecken suchen.)"
                if windows_sees is True else ""
            )
        )
        diag.details = (
            f"usbipd list returned no VID 2F5D rows; "
            f"Windows Get-PnpDevice saw VID_2F5D = {windows_sees}"
        )
        return diag

    # 4. Try to attach every visible ROBOTIS device.
    attached = []
    attach_errors = []
    for dev in robotis:
        if dev.state == "Attached":
            attached.append(dev)
            continue
        if attach_usb_to_wsl(dev.busid):
            attached.append(dev)
        else:
            attach_errors.append(f"{dev.busid} ({dev.vid_pid})")

    if not attached:
        diag.attach_failed = True
        diag.message_de = (
            "Geräte gefunden, aber das Anhängen an die EduBotics-Umgebung "
            "ist fehlgeschlagen. Bitte den Installer erneut ausführen, "
            "damit die USB-Richtlinie eingerichtet wird."
        )
        diag.details = f"usbipd attach failed for: {', '.join(attach_errors)}"
        return diag

    # 5. Did /dev/serial/by-id/ populate inside the distro?
    import time as _time
    serial_paths: list[str] = []
    for _ in range(10):
        serial_paths = find_serial_paths_for_robotis()
        if serial_paths:
            break
        _time.sleep(1)
    if not serial_paths:
        diag.no_serial_after_attach = True
        diag.message_de = (
            "Geräte sind angehängt, aber Linux sieht keinen seriellen Anschluss. "
            "Bitte USB-Kabel auswechseln (manche Kabel übertragen keine Daten) "
            "oder anderen USB-Port verwenden."
        )
        diag.details = (
            f"Attached {[d.busid for d in attached]} but "
            f"`ls /dev/serial/by-id/` empty after 10s poll"
        )
        return diag

    # 6. Docker image present?
    if image is not None:
        try:
            result = subprocess.run(
                ["wsl", "-d", WSL_DISTRO_NAME, "--", "docker", "image", "inspect", image],
                capture_output=True, text=True, timeout=15,
                **_SUBPROCESS_KWARGS,
            )
            if result.returncode != 0:
                diag.docker_image_missing = True
                diag.message_de = (
                    f"Docker-Image '{image}' ist noch nicht heruntergeladen. "
                    "Bitte Internetverbindung prüfen und es erneut versuchen — "
                    "EduBotics lädt das Image automatisch beim ersten Start."
                )
                diag.details = f"docker image inspect rc={result.returncode}"
                return diag
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            diag.details = f"docker image inspect raised: {e}"

    diag.ok = True
    diag.message_de = (
        "Alle Systeme einsatzbereit, aber identify_arm.py konnte die Arme nicht zuordnen. "
        "Bitte Servo-IDs prüfen (Leader: 1-6, Follower: 11-16) und erneut versuchen."
    )
    diag.details = (
        f"attached={[d.busid for d in attached]} serial_paths={serial_paths}"
    )
    return diag


def get_diagnostics_log_path() -> str:
    """Public accessor — GUI uses this to tell the student where to find the log."""
    return _diagnostics_log_path()
