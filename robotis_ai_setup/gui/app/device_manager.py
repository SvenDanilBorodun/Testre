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
from .usbipd_resolver import (
    UsbipdNotFoundError,
    find_usbipd,
    usbipd_cmd,
)


def _diagnostics_log_path() -> str:
    """Where install/runtime diagnostics get appended.

    Machine-wide %ProgramData%\\EduBotics so the support artifact is ONE file on
    managed (student != admin) machines. `EduBotics_Setup.exe` is
    PrivilegesRequired=admin, so the installer + every elevated-repair .ps1 run
    as the admin account — their %LOCALAPPDATA% differs from the student's, so a
    per-user path splits the install/prereq/usbipd/migrate evidence into the
    admin profile while the GUI's own scan-time entries land in the student
    profile (support then sees only half). ProgramData is the same root the WSL
    install already uses. The installer creates the dir with a permissive-append
    ACL (matching PS-side move) so the standard-user GUI can append here too.
    Falls back to %LOCALAPPDATA% then ~ when ProgramData is unset (dev / non-Windows)."""
    base = (
        os.environ.get("PROGRAMDATA")
        or os.environ.get("LOCALAPPDATA")
        or os.path.expanduser("~")
    )
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
    path: str  # /dev/video0 (usb_cam path) or a display string (native bridge)
    name: str  # Human-readable name like "Logitech C920"
    role: str = ""  # "gripper" or "scene" — assigned by student in GUI
    win_index: int = -1  # OpenCV device index for native-bridge capture; -1 on the usb_cam path


@dataclass
class HardwareConfig:
    """Complete hardware configuration ready for .env generation."""
    leader: Optional[ArmDevice] = None
    follower: Optional[ArmDevice] = None
    cameras: list = field(default_factory=list)  # list[CameraDevice], supports 0-N cameras

    @property
    def is_complete(self) -> bool:
        return self.leader is not None and self.follower is not None

    @property
    def follower_present(self) -> bool:
        """True when at least the follower is identified. This is the
        sufficient hardware condition for Roboter Studio (follower-only): the
        leader is not launched there, so it need not be scanned."""
        return self.follower is not None


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
    """`usbipd` cannot be located by usbipd_resolver.

    Distinct from a FileNotFoundError on a single subprocess.run call — this
    means the resolver walked PATH, %ProgramFiles%\\usbipd-win, and the
    Uninstall registry, and still found nothing. The caller should surface
    the German "bitte den Installer erneut ausfuehren" UX.
    """


def _run_usbipd_list() -> Optional[str]:
    """Run `usbipd list` and return raw stdout (combined with stderr).

    Returns None if the binary is missing or the call times out. Logs raw
    output to the diagnostics file regardless of outcome so a maintainer
    can read what Windows actually said when the student hit "Nicht
    gefunden".
    """
    try:
        cmd = usbipd_cmd("list")
    except UsbipdNotFoundError as exc:
        _append_diag("usbipd_list", f"UsbipdNotFoundError: {exc}")
        raise UsbipdMissingError(str(exc))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=10,
            **_SUBPROCESS_KWARGS,
        )
    except FileNotFoundError:
        # Race: resolver returned a path, but the file was removed (e.g.
        # mid-uninstall) between resolve and exec. Treat as missing.
        _append_diag("usbipd_list", "FileNotFoundError after resolve — uninstall race?")
        raise UsbipdMissingError("usbipd resolved but no longer present on disk")
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
    try:
        argv = usbipd_cmd("attach", "--wsl", WSL_DISTRO_NAME, "--busid", busid)
    except UsbipdNotFoundError as exc:
        _append_diag("attach_usb_to_wsl", f"busid={busid}: {exc}")
        return False
    last_stderr = ""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                argv,
                capture_output=True, text=True, timeout=15,
                **_SUBPROCESS_KWARGS,
            )
            if result.returncode == 0:
                return True
            last_stderr = (result.stderr or result.stdout or "").strip()
        except FileNotFoundError:
            _append_diag(
                "attach_usb_to_wsl",
                f"busid={busid}: usbipd resolved but missing at exec time",
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
        argv = usbipd_cmd("detach", "--busid", busid)
    except UsbipdNotFoundError:
        return False
    try:
        result = subprocess.run(
            argv,
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
    # Route through wsl_bridge.run so this share the c87bde5 stdin-bytes
    # path. Direct `bash -c "<multi-line script>"` previously truncated
    # scripts whose $(...) captures contain literal tabs or parentheses
    # — see wsl_bridge.run docstring for the full failure mode.
    try:
        result = wsl_bridge.run(_SELF_HEAL_SCRIPT, timeout=15, check=False)
        diag["ran"] = True
        diag["rc"] = result.returncode
        diag["stdout"] = (result.stdout or "")[-500:]
        diag["stderr"] = (result.stderr or "")[-500:]
    except wsl_bridge.WSLError as exc:
        diag["stderr"] = f"WSLError: {exc}"
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


def fast_rehydrate_arms(
    saved_leader_path: str, saved_follower_path: str,
    require_leader: bool = True,
) -> tuple[Optional[ArmDevice], Optional[ArmDevice]]:
    """Light revalidation of the previous session's arm mapping (PR-4).

    Skips the two SLOW stages of scan_and_identify_arms — the throwaway
    scanner container and the per-device serial pings — and only attaches
    the ROBOTIS USB devices (idempotent) and confirms the saved
    /dev/serial/by-id paths are present and distinct. The path↔role binding
    is trusted from the saved .env: ROBOTIS arms expose DISTINCT stable
    by-id serials (the identical-serial problem is camera-only, see
    CLAUDE.md), so a binding cannot silently swap between sessions — the
    path either reappears as the same physical arm or doesn't appear at
    all. ANY mismatch returns (None, None) and the caller falls back to
    the full scan.

    ``require_leader`` is False for a follower-only robot type (Roboter
    Studio kit): its .env has no LEADER_PORT, so an empty ``saved_leader_path``
    is legal and the result is ``(None, follower)``. The follower is ALWAYS
    required — a follower mismatch still returns ``(None, None)``.
    """
    import time

    # The follower is always mandatory.
    if not saved_follower_path:
        return None, None
    if require_leader:
        if not saved_leader_path or saved_leader_path == saved_follower_path:
            return None, None
    elif saved_leader_path and saved_leader_path == saved_follower_path:
        # A follower-only .env normally has no leader path; a stray one that
        # collides with the follower is a corrupt mapping — bail to full scan.
        return None, None

    # Whether we expect (and must confirm) a leader path this call. When the
    # caller does NOT require a leader (a follower-only robot type), a stray
    # distinct LEADER_PORT left in a hand-edited .env must be IGNORED — not
    # waited on. Keying want_leader off saved_leader_path here made such a stray
    # burn the whole 10×1 s presence-retry loop and then fall back to a full
    # scan even though the follower was present. The corrupt leader==follower
    # mapping is already bailed above.
    want_leader = require_leader

    self_heal_wsl_serial()
    attached = attach_all_robotis_devices()
    if not attached:
        return None, None

    def _present(paths: list[str]) -> bool:
        if saved_follower_path not in paths:
            return False
        if want_leader and saved_leader_path not in paths:
            return False
        return True

    serial_paths: list[str] = []
    for _ in range(10):
        serial_paths = find_serial_paths_for_robotis()
        if _present(serial_paths):
            break
        time.sleep(1)
    if not _present(serial_paths):
        return None, None

    def _rebuild(path: str, role: str) -> ArmDevice:
        desc = path.split("/")[-1]
        busid = ""
        for dev in attached:
            if any(part in path for part in dev.description.split()):
                busid = dev.busid
                desc = dev.description
                break
        return ArmDevice(busid=busid, serial_path=path, role=role, description=desc)

    leader = (
        _rebuild(saved_leader_path, "leader")
        if (want_leader and saved_leader_path in serial_paths)
        else None
    )
    return leader, _rebuild(saved_follower_path, "follower")


def _list_camera_vid_pids_from_wsl() -> set[str]:
    """Enumerate VID:PID of every UVC capture node currently inside the EduBotics distro.

    Fallback used when Windows PnP returns nothing — that case happens
    routinely once usbipd has forwarded the camera into WSL: Windows
    then sees only the `VID_80EE:CAFE` usbipd stub, not a Camera-class
    device, so `_list_usb_camera_vid_pids()` is empty even though the
    camera is healthy. We walk /sys/class/video4linux back to the USB
    parent and read idVendor / idProduct from sysfs.
    """
    if sys.platform != "win32":
        return set()
    script = r"""
for sysdev in /sys/class/video4linux/video*; do
    [ -e "$sysdev" ] || continue
    dev=$(readlink -f "$sysdev")
    while [ -n "$dev" ] && [ "$dev" != "/" ]; do
        if [ -f "$dev/idVendor" ] && [ -f "$dev/idProduct" ]; then
            v=$(cat "$dev/idVendor")
            p=$(cat "$dev/idProduct")
            echo "$v:$p"
            break
        fi
        dev=$(dirname "$dev")
    done
done | sort -u
"""
    # Route through wsl_bridge.run for the same stdin-bytes safety as
    # self_heal_wsl_serial — keeps the c87bde5 fix universal.
    try:
        result = wsl_bridge.run(script, timeout=10, check=False)
    except wsl_bridge.WSLError:
        return set()
    if result.returncode != 0:
        return set()
    # Defence-in-depth: the usbipd stub registers under VID 80EE:CAFE
    # when no real camera class device is bound. Drop it so callers
    # don't try to match the stub against a real-camera VID:PID.
    return {
        line.strip().lower()
        for line in result.stdout.splitlines()
        if line.strip() and line.strip().lower() != "80ee:cafe"
    }


def _list_usb_camera_vid_pids() -> list[tuple[str, str]]:
    """Query Windows PnP for currently-connected USB cameras.

    Returns [(vid_pid, friendly_name), ...] where vid_pid is "vvvv:pppp"
    (lower-case hex). Used by scan_cameras() to figure out which usbipd
    BUSIDs to attach to WSL — without this, a student plugs in a webcam
    and the GUI's `Kameras scannen` button silently finds nothing
    (the camera sits in usbipd's `Not shared` state and never reaches
    /dev/video* inside the distro).

    Returns an empty list when cameras are already forwarded to WSL —
    the usbipd stub registers under `VID_80EE:CAFE` (not Class Camera),
    so Windows PnP no longer sees the device. Callers must handle that
    case by falling back to `_list_camera_vid_pids_from_wsl()`.
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
        # Windows PnP is blind to cameras that are already forwarded to
        # WSL (only the usbipd stub VID_80EE:CAFE shows up there). Ask
        # the distro directly which UVC devices it currently exposes —
        # this lets us still count them as "attached" rather than
        # returning [] and making the GUI think no cameras were found.
        camera_vid_pids = _list_camera_vid_pids_from_wsl()
        if camera_vid_pids:
            _append_diag(
                "attach_cameras_to_wsl",
                f"Windows PnP empty; using WSL-side UVC enumeration: "
                f"{sorted(camera_vid_pids)}",
            )
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
    """Scan for video capture devices.

    native_bridge path (Windows student PC, the default): enumerate cameras
    directly on Windows via OpenCV/DirectShow. The cameras are NOT attached to
    WSL via usbipd — they are captured natively and streamed into the container
    (see camera_bridge.py + CLAUDE.md "native camera capture bridge").

    usb_cam path (Jetson / native Linux, or EDUBOTICS_CAMERA_SOURCE=usb_cam):
      1. self_heal_wsl_serial() loads uvcvideo (+ udev + USB-Serial mods)
      2. attach_cameras_to_wsl() auto-attaches Windows-visible webcams that
         have a usbipd policy (set up by configure_usbipd.ps1)
      3. wsl_bridge.list_video_devices() lists /dev/video* + by-id
    """
    from .constants import cameras_use_native_bridge
    if cameras_use_native_bridge():
        return _scan_cameras_native()
    self_heal_wsl_serial()
    attach_cameras_to_wsl()
    raw = wsl_bridge.list_video_devices()
    return [CameraDevice(path=d["path"], name=d["name"]) for d in raw]


def _scan_cameras_native() -> list[CameraDevice]:
    """Enumerate Windows cameras for native capture (no usbipd attach).

    `path` is a human-readable display string only — in native_bridge mode the
    container's CAMERA_DEVICE_* stays empty (it doesn't open /dev/video*); the
    `win_index` is what the capture bridge opens with OpenCV.
    """
    try:
        from . import win_camera
        cams = win_camera.list_windows_cameras()
    except Exception as exc:  # noqa: BLE001 — never let a probe failure crash the scan
        _append_diag("scan_cameras_native", f"enumeration failed: {exc}")
        return []
    return [
        CameraDevice(path=f"Index {c.index}", name=c.name, win_index=c.index)
        for c in cams
    ]


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
    # The resolver looks in PATH, %ProgramFiles%\usbipd-win, the Uninstall
    # registry, and a registry-refreshed PATH before giving up. If we still
    # land in the "missing" branch, usbipd genuinely is not installed on
    # this machine (or was uninstalled after our last successful call).
    try:
        raw = _run_usbipd_list()
    except UsbipdMissingError:
        diag.usbipd_missing = True
        diag.message_de = (
            "usbipd-win ist nicht installiert. "
            "Bitte die EduBotics-Reparatur ausführen (Menü → System reparieren) "
            "oder den EduBotics-Installer erneut starten."
        )
        diag.details = "usbipd.exe not found via PATH, Program Files, or Uninstall registry"
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


def usbipd_reachable() -> bool:
    """Cheap probe: can we locate usbipd.exe right now?

    Called by the GUI's startup prerequisite check so the "Reparieren"
    UX surfaces BEFORE the student clicks Arme scannen and sees a 30-second
    diagnose loop. Does not invoke usbipd — just resolves the path.
    """
    return find_usbipd() is not None


def usbipd_path() -> Optional[str]:
    """Return the absolute path the resolver settled on, or None."""
    return find_usbipd()


def list_unbound_cameras() -> list[USBDevice]:
    """Return UVC cameras Windows sees but usbipd has not bound yet.

    These are devices in `Not shared` state whose VID:PID matches a Windows
    Camera/Image class device. Used by the GUI's "Kameras freigeben" repair
    flow: the student plugged in a webcam AFTER install ran, so its VID:PID
    never got a usbipd policy, so attaching it would require admin every time.

    We deliberately do NOT auto-bind these from the GUI's non-admin scan path
    — many camera-vendor VIDs (Logitech, Microsoft, Lenovo) also ship mice
    and keyboards, and silently binding those would break input. The student
    confirms the bind via the explicit "Freigeben" button + UAC prompt.
    """
    camera_vid_pids = {vp for vp, _ in _list_usb_camera_vid_pids()}
    if not camera_vid_pids:
        return []
    unbound: list[USBDevice] = []
    for dev in list_usb_devices():
        if dev.vid_pid.lower() not in camera_vid_pids:
            continue
        if dev.state == "Not shared":
            unbound.append(dev)
    return unbound


def list_unbound_robotis() -> list[USBDevice]:
    """Return ROBOTIS arms Windows/usbipd sees but that are not yet attachable.

    Mirrors ``list_unbound_cameras()`` for the arms. A device that is in
    ``Not shared`` (or the relaxed-parse ``Unknown``) state cannot be attached
    by the GUI without admin, because non-admin ``usbipd attach`` only works
    once the device is ``Shared`` (bound) — see usbipd 5.x policy semantics.
    This is the load-bearing list behind the GUI's "Arme freigeben" repair:
    when ``configure_usbipd.ps1`` failed to bind at install time (the classic
    post-install PATH race), the arms sit here and the student needs one
    elevated ``bind`` to make them attachable.
    """
    unbound: list[USBDevice] = []
    for dev in list_robotis_devices():
        if dev.state in ("Not shared", "Unknown"):
            unbound.append(dev)
    return unbound


def hardware_ids_for_devices(devs: list[USBDevice]) -> list[str]:
    """Extract unique VID:PID hardware-ID strings (lowercased) from devices.

    The output is the input format `bind_devices.ps1` expects. De-duplicated
    so we don't pass `usbipd bind` two arguments for two devices sharing a
    VID:PID (rare for cameras but possible).
    """
    seen: set[str] = set()
    out: list[str] = []
    for d in devs:
        key = d.vid_pid.lower()
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out
