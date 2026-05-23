"""Native Windows USB camera enumeration + capture (native-bridge path).

On the Windows student PC the cameras are NOT forwarded into WSL via usbipd
(that bridge caps UVC at ~6-10 Hz and jitters the Dynamixel bus — see CLAUDE.md
"native camera capture bridge"). Instead the GUI opens them directly with
OpenCV's DirectShow backend, exactly like phosphobot does, and streams frames
into the container. This module owns the Windows-side enumeration and the
OpenCV VideoCapture configuration; the streaming lives in camera_bridge.py.

Why DirectShow (CAP_DSHOW) and not MSMF: DirectShow gives a stable enumeration
order that matches the OpenCV device index, MJPG fourcc works reliably, and the
device monikers expose the USB topology we use to keep role assignments stable
across restarts. cv2 is imported lazily so importing this module never hard-
fails if OpenCV is somehow missing from a build — callers get a clear German
error when they actually try to use a camera.

Disambiguating two identical cameras (R1): the Innomaker U20CAM-720P pair both
report identical name/VID/PID/serial, so we cannot tell "gripper" from "scene"
by metadata. The student assigns roles visually (the GUI shows a preview of
each index); we persist the chosen OpenCV index per role. `device_key()`
returns a best-effort stable key (PnP LocationInfo = physical USB port) so the
GUI can warn when indices shuffle after a replug.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# How many OpenCV indices to probe when enumerating. A classroom rig has 2
# cameras; 8 leaves head-room for a laptop's built-in cam + a couple of spares
# without making the probe (which opens each device briefly) too slow.
_MAX_PROBE = 8


class CameraUnavailableError(RuntimeError):
    """OpenCV is missing or a camera index could not be opened."""


def _import_cv2():
    try:
        import cv2  # noqa: PLC0415 — lazy by design
        return cv2
    except Exception as exc:  # noqa: BLE001
        raise CameraUnavailableError(
            "OpenCV (cv2) ist nicht verfügbar — die Kamera-Bridge kann nicht "
            "starten. Bitte EduBotics neu installieren."
        ) from exc


@dataclass
class CameraInfo:
    """A Windows-side capture device discovered by enumeration."""
    index: int          # OpenCV DirectShow device index
    name: str           # FriendlyName from PnP (or a generic fallback)
    location: str = ""  # PnP LocationInfo (physical USB port — stable per port)
    instance_id: str = ""

    def device_key(self) -> str:
        """Best-effort stable identity. LocationInfo encodes the physical USB
        port, which survives replug into the SAME port; falls back to the
        instance id, then the index."""
        return self.location or self.instance_id or f"index:{self.index}"


def _pnp_camera_details() -> list[dict]:
    """Query Windows PnP for present cameras (name + location), in PnP order.

    Best-effort: returns [] if PowerShell is unavailable. The order is not
    guaranteed to match the OpenCV index order, so we only use these for
    display names / stability keys, never to *choose* which camera is which —
    that is the student's visual assignment.
    """
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "Get-PnpDevice -Class Camera,Image -PresentOnly | "
                "Where-Object Status -eq 'OK' | ForEach-Object { "
                "  $loc = (Get-PnpDeviceProperty -InstanceId $_.InstanceId "
                "    -KeyName 'DEVPKEY_Device_LocationInfo' "
                "    -ErrorAction SilentlyContinue).Data; "
                "  '{0}|{1}|{2}' -f $_.FriendlyName, $loc, $_.InstanceId }",
            ],
            capture_output=True, text=True, timeout=10, **_SUBPROCESS_KWARGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    details = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        parts = line.split("|")
        name = parts[0].strip() if len(parts) > 0 else ""
        loc = parts[1].strip() if len(parts) > 1 else ""
        inst = parts[2].strip() if len(parts) > 2 else ""
        details.append({"name": name, "location": loc, "instance_id": inst})
    return details


def list_windows_cameras(max_probe: int = _MAX_PROBE) -> list[CameraInfo]:
    """Enumerate openable OpenCV (DirectShow) camera indices.

    Probes indices 0..max_probe-1, keeping those that open. PnP details are
    zipped on in discovery order for display names + stability keys. Each probe
    opens and immediately releases the device, so do NOT call this while the
    capture bridge is running on the same camera.
    """
    cv2 = _import_cv2()
    pnp = _pnp_camera_details()
    cams: list[CameraInfo] = []
    for idx in range(max_probe):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        try:
            opened = cap.isOpened()
        finally:
            cap.release()
        if not opened:
            continue
        meta = pnp[len(cams)] if len(cams) < len(pnp) else {}
        cams.append(CameraInfo(
            index=idx,
            name=meta.get("name") or f"Kamera {idx}",
            location=meta.get("location", ""),
            instance_id=meta.get("instance_id", ""),
        ))
    return cams


def open_capture(index: int, width: int, height: int, fps: float):
    """Open and configure a VideoCapture for streaming.

    Forces MJPG fourcc (camera-side JPEG — the only way 2×640×480@30 fits a
    USB 2.0 budget and matches phosphobot) and the target resolution/fps.
    Raises CameraUnavailableError if the device won't open.
    """
    cv2 = _import_cv2()
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap.release()
        raise CameraUnavailableError(
            f"Kamera mit Index {index} konnte nicht geöffnet werden."
        )
    # Order matters on DirectShow: set FOURCC before size/fps.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    # 1-deep driver buffer so read() returns the freshest frame, not a backlog.
    # (Best-effort; not all DirectShow drivers honour BUFFERSIZE.)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:  # noqa: BLE001
        pass
    return cap
