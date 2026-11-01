"""Native Windows USB camera enumeration + capture (native-bridge path).

On the Windows student PC the cameras are NOT forwarded into WSL via usbipd
(that bridge caps UVC at ~6-10 Hz and jitters the Dynamixel bus — see CLAUDE.md
"native camera capture bridge"). Instead the GUI opens them directly with
OpenCV's MSMF backend (phosphobot's native-capture model) and streams frames
into the container. This module owns the Windows-side enumeration and the
OpenCV VideoCapture configuration; the streaming lives in camera_bridge.py.

Backend (see `_capture_backend`): we use MSMF, not DirectShow. DSHOW was the
original choice (stable enumeration, topology monikers) but was measured to
hard-cap these cameras at ~14 fps and refuse to leave YUY2; MSMF sustains ~25.
Enumeration and capture share the backend so device indices stay consistent.
cv2 is imported lazily so importing this module never hard-fails if OpenCV is
somehow missing from a build — callers get a clear German error when they
actually try to use a camera.

Disambiguating two identical cameras (R1): the Innomaker U20CAM-720P pair both
report identical name/VID/PID/serial, so we cannot tell "gripper" from "scene"
by metadata. The student assigns roles visually (the GUI shows a preview of
each index); we persist the chosen OpenCV index per role. `device_key()`
returns a best-effort stable key (PnP LocationInfo = physical USB port) so the
GUI can warn when indices shuffle after a replug.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass

from . import constants

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROCESS_KWARGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# How many OpenCV indices to probe when enumerating. A classroom rig has 2
# cameras; 8 leaves head-room for a laptop's built-in cam + a couple of spares
# without making the probe (which opens each device briefly) too slow.
_MAX_PROBE = 8


def _capture_backend(cv2):
    """OpenCV capture backend for BOTH enumeration and capture.

    Measured on Sven's classroom rig 2026-05-24 (Innomaker U20CAM-720P,
    640x480): the **MSMF** backend sustains ~25 fps per camera (and ~26 each
    with both open concurrently — no contention penalty), whereas **DSHOW**
    hard-caps at ~14 fps for the same camera/format and silently refuses to
    leave YUY2 even when MJPG is requested. Decode is never the limiter (the
    grab-only rate equals the read rate; a separate decode runs at >400 fps).
    So 30 fps is physically unreachable here — ~25 is the camera/USB/MSMF
    ceiling — and MSMF nearly doubles the usable rate. We therefore default to
    MSMF. Enumeration AND capture MUST use the same backend, because the OpenCV
    device-index order can differ between backends (index N under DSHOW may be a
    different physical camera than index N under MSMF), which would scramble the
    student's gripper/scene role pick.

    Override with EDUBOTICS_CAMERA_BACKEND=dshow for rollback (e.g. if a future
    rig's MSMF driver is flaky — MSMF can emit a transient async ReadSample
    warning at open, harmless here).
    """
    if capture_backend_name() == "dshow":
        return cv2.CAP_DSHOW
    return cv2.CAP_MSMF


def capture_backend_name() -> str:
    """``"dshow"`` under the EDUBOTICS_CAMERA_BACKEND=dshow rollback, else
    ``"msmf"``. The one parsing rule `_capture_backend` uses, readable without
    OpenCV (the station enablement checks it before any camera is opened)."""
    sel = os.environ.get("EDUBOTICS_CAMERA_BACKEND", "msmf").strip().lower()
    return "dshow" if sel == "dshow" else "msmf"


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
    """Query Windows PnP for present USB cameras (name + location), in PnP order.

    Best-effort: returns [] if PowerShell is unavailable. The order is not
    guaranteed to match the OpenCV index order, so we only use these for
    display names / stability keys, never to *choose* which camera is which —
    that is the student's visual assignment.

    USB-only filter (the `InstanceId -like 'USB\\*'` guard, mirrored in Python):
    the `Camera,Image` PnP class set also returns non-capture imaging devices —
    a networked HP LaserJet MFP's WSD *scanner* enumerates as an `Image`-class
    device (`SWD\\DAFWSDPROVIDER\\...\\SCANNER1`, LocationInfo = an `http://` URL),
    and a phantom `ROOT\\IMAGE` SCSI scanner can appear too. Because such a
    device sorts BEFORE the real UVC camera, the positional name/location zip in
    `list_windows_cameras()` shifted by one and mislabelled the real Innomaker
    as the printer (and gave it the printer's network URL as its stability key);
    in a 2-camera classroom it scrambled the gripper/scene identity anchor.
    Real UVC/WIA webcams always enumerate under `USB\\VID_...`, so restricting to
    `USB\\`-rooted instance ids drops the scanners while keeping every webcam.
    """
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "Get-PnpDevice -Class Camera,Image -PresentOnly | "
                "Where-Object { $_.Status -eq 'OK' -and "
                "  $_.InstanceId -like 'USB\\*' } | ForEach-Object { "
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
        # Defense-in-depth for the USB-only filter above: skip any non-USB
        # imaging device (network WSD / SCSI scanners) that slips past the
        # PowerShell guard, so it can never shift the positional zip.
        if inst and not inst.upper().startswith("USB\\"):
            continue
        details.append({"name": name, "location": loc, "instance_id": inst})
    return details


def camera_privacy_blocked() -> "bool | None":
    """Best-effort: is Windows blocking desktop apps from the camera?

    Windows 10/11 has a global "Let desktop apps access your camera" toggle
    (Settings → Privacy & security → Camera). When OFF, OpenCV's
    ``VideoCapture.isOpened()`` silently returns False for EVERY camera, so the
    GUI's scan finds nothing even though Device Manager shows the camera as
    healthy. This is one of the most common "no cameras found" causes on a
    fresh student PC. We read the ConsentStore registry value so the GUI can
    surface a precise fix instead of a generic "keine Kameras".

    Returns True if access is denied, False if allowed, None if undeterminable
    (non-Windows, registry unreadable, key absent → treat as "unknown").
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-untyped]
    except ImportError:
        return None
    # The per-machine desktop-app (NonPackaged) consent governs OpenCV here.
    # HKCU wins for the logged-in user; "Deny" means blocked.
    for hive, sub in (
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam\NonPackaged"),
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam\NonPackaged"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as k:
                val, _ = winreg.QueryValueEx(k, "Value")
                if isinstance(val, str) and val.strip().lower() == "deny":
                    return True
        except OSError:
            continue
    return False


def list_windows_cameras(max_probe: int = _MAX_PROBE) -> list[CameraInfo]:
    """Enumerate openable OpenCV (DirectShow) camera indices.

    Probes indices 0..max_probe-1, keeping those that open. PnP details are
    zipped on in discovery order for display names + stability keys. Each probe
    opens and immediately releases the device, so do NOT call this while the
    capture bridge is running on the same camera.
    """
    cv2 = _import_cv2()
    backend = _capture_backend(cv2)
    pnp = _pnp_camera_details()
    cams: list[CameraInfo] = []
    for idx in range(max_probe):
        cap = cv2.VideoCapture(idx, backend)
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


def _readback(cap, cv2) -> dict:
    """Read back what the backend (DirectShow or MSMF) actually negotiated
    (NOT what we requested).

    `set()` returns True even when the driver silently clamps to its nearest
    supported descriptor, so the only way to know the real format/rate is to
    `get()` it back. (On MSMF the fourcc read-back is garbage — callers flag
    that via info["msmf"] and ignore it.)
    """
    raw_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    try:
        fourcc = (raw_fourcc.to_bytes(4, "little")
                  .decode("latin1", "replace").strip("\x00 "))
    except Exception:  # noqa: BLE001
        fourcc = str(raw_fourcc)
    return {
        "fourcc": fourcc,
        "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0.0),
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
    }


def open_capture(index: int, width: int, height: int, fps: float):
    """Open and configure a VideoCapture for streaming.

    Forces MJPG fourcc (camera-side JPEG — the only way 2×640×480@30 fits a
    USB 2.0 budget and matches phosphobot) and the target resolution/fps.
    Raises CameraUnavailableError if the device won't open.

    Returns ``(cap, info)`` where ``info`` is the *negotiated* format read back
    from the driver (``{"fourcc","fps","width","height"}``). DirectShow / UVC
    silently downgrade to the camera's nearest supported frame-interval
    descriptor — e.g. an Innomaker that exposes a 15 fps MJPG mode will quietly
    run at 15 even though we asked for 30, with no error. The caller MUST
    inspect ``info`` and surface a mismatch (a steady 15 Hz with no error in the
    logs is exactly this trap). We also retry the format push once, because
    some UVC cameras only expose the 30 fps interval under MJPG and a stale
    graph can otherwise pin the rate.
    """
    cv2 = _import_cv2()
    backend = _capture_backend(cv2)
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        raise CameraUnavailableError(
            f"Kamera mit Index {index} konnte nicht geöffnet werden."
        )

    is_msmf = backend == cv2.CAP_MSMF

    def _apply():
        if is_msmf:
            # MSMF: set ONLY the MJPG fourcc. Measured 2026-05-24 that setting
            # CAP_PROP_FRAME_WIDTH/HEIGHT/FPS on MSMF triggers a ~12 s open
            # re-negotiation AND pins the camera to a slower ~24 fps mode;
            # setting only the fourcc opens in ~3 s and runs the camera's native
            # mode — 640x480 MJPG @ ~30 fps on the Innomaker U20CAM-720P. We do
            # NOT request a resolution here; the capture worker normalises to
            # the target size with a cheap resize if a camera's native size
            # differs. (MSMF reports an empty fourcc read-back — harmless.)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        else:
            # DSHOW (rollback path): FOURCC must be set LAST or the camera stays
            # on YUY2 (uncompressed, ~14 fps). Size/fps first, fourcc last.
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            cap.set(cv2.CAP_PROP_FPS, fps)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

    _apply()
    info = _readback(cap, cv2)
    # DSHOW only: if it reported a non-MJPG fourcc (YUY2 fallback), push the
    # format once more and re-read. MSMF reports an empty fourcc, so this never
    # fires there.
    if not is_msmf and info["fourcc"] and info["fourcc"] != "MJPG":
        _apply()
        info = _readback(cap, cv2)
    # 1-deep driver buffer so read() returns the freshest frame, not a backlog.
    # (Best-effort; not all DirectShow drivers honour BUFFERSIZE.)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:  # noqa: BLE001
        pass
    # MSMF returns a garbage-but-non-empty CAP_PROP_FOURCC read-back; flag it so
    # callers don't false-warn about a "non-MJPG" format on the MSMF path.
    info["msmf"] = is_msmf
    return cap, info


def open_station_capture(index: int):
    """Open the station camera ONCE in its native 1920x1080 MJPEG mode.

    Used by the scene capture worker on a camera-station rig (and by the
    enrollment probe, which must see exactly what the worker will). The backend
    is the unchanged `_capture_backend` — the one the enumeration and the
    student's role pick used, so ``index`` means the camera the student picked.
    The station is native-MSMF-only; the GUI refuses to enable it under the
    DirectShow or usb_cam rollbacks.

    How the native mode is reached is one GUI constant,
    ``constants.STATION_OPEN_METHOD`` (bench gates B1/B5):

      * ``"fourcc_only"`` — set only the MJPG fourcc, exactly like today's MSMF
        path in `open_capture`, and no fps; for a camera whose MSMF default mode
        is already 1920x1080 at >= 29 fps.
      * ``"fourcc_size_fps"`` — the MJPG fourcc plus CAP_PROP_FRAME_WIDTH/HEIGHT
        1920x1080 and CAP_PROP_FPS 30.

    Returns ``(cap, info)`` like `open_capture` (``info`` is the read-back, plus
    ``"msmf"``). Raises CameraUnavailableError if the device does not open.
    The caller never trusts the read-back for the size: every frame it reads is
    checked against 1920x1080.
    """
    method = constants.STATION_OPEN_METHOD
    if method not in constants.STATION_OPEN_METHODS:
        raise CameraUnavailableError(
            f"Unbekannte Öffnungsart für die Stationskamera: {method!r}."
        )
    cv2 = _import_cv2()
    backend = _capture_backend(cv2)
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        raise CameraUnavailableError(
            f"Stationskamera mit Index {index} konnte nicht geöffnet werden."
        )
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    if method == "fourcc_size_fps":
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, constants.STATION_NATIVE_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, constants.STATION_NATIVE_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, 30)
    info = _readback(cap, cv2)
    # 1-deep driver buffer so read() returns the freshest frame (best-effort),
    # as in open_capture.
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:  # noqa: BLE001
        pass
    info["msmf"] = backend == cv2.CAP_MSMF
    return cap, info


@dataclass
class StationProbeResult:
    """Outcome of `probe_station_camera`. ``error_de`` is empty on success."""
    ok: bool
    frames: int = 0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    error_de: str = ""


# How long the probe reads frames after the camera opened before it gives up
# (60 frames at 30 fps take 2 s; the rest is slack for a slow first frame).
STATION_PROBE_TIMEOUT_S = 20.0


def probe_station_camera(index: int,
                         frames: int = constants.STATION_PROBE_FRAMES,
                         min_fps: float = constants.STATION_PROBE_MIN_FPS,
                         timeout_s: float = STATION_PROBE_TIMEOUT_S,
                         clock=time.monotonic) -> StationProbeResult:
    """Enrollment probe for the camera assigned the scene role.

    Opens it exactly as the bridge will (`open_station_capture`) and requires
    ``frames`` frames of exactly 1920x1080 at a rate of at least ``min_fps``
    (measured over the intervals between the first and the last of them). A
    720P camera cannot pass, so the probe also tells the station camera from
    the 720P gripper camera.

    Run it with the camera bridge and the GUI's previews STOPPED: a second
    MSMF open of the same device returns a frameless handle. Never raises; the
    camera is always released. Returns a StationProbeResult whose ``error_de``
    is the German refusal.
    """
    want_w = constants.STATION_NATIVE_WIDTH
    want_h = constants.STATION_NATIVE_HEIGHT
    try:
        cap, _info = open_station_capture(index)
    except CameraUnavailableError as exc:
        return StationProbeResult(ok=False, error_de=str(exc))
    got = 0
    t_first = t_last = None
    try:
        deadline = clock() + timeout_s
        while got < frames and clock() < deadline:
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            if (w, h) != (want_w, want_h):
                return StationProbeResult(
                    ok=False, frames=got, width=w, height=h,
                    error_de=(
                        f"Die Kamera auf der Rolle „Szene“ liefert {w}×{h} "
                        f"statt {want_w}×{want_h} Bildpunkten — das ist nicht "
                        "die Stationskamera (U20CAM-1080P)."
                    ),
                )
            now = clock()
            if t_first is None:
                t_first = now
            t_last = now
            got += 1
    except Exception as exc:  # noqa: BLE001 — a probe never raises
        return StationProbeResult(
            ok=False, frames=got,
            error_de=f"Die Stationskamera konnte nicht gelesen werden ({exc}).",
        )
    finally:
        try:
            cap.release()
        except Exception:  # noqa: BLE001
            pass
    if got < frames:
        return StationProbeResult(
            ok=False, frames=got, width=want_w if got else 0,
            height=want_h if got else 0,
            error_de=(
                f"Die Stationskamera hat nur {got} von {frames} Bildern "
                "geliefert."
            ),
        )
    span = (t_last - t_first) if (t_first is not None and t_last is not None) else 0.0
    fps = (frames - 1) / span if span > 0 else float("inf")
    if fps < min_fps:
        return StationProbeResult(
            ok=False, frames=got, fps=fps, width=want_w, height=want_h,
            error_de=(
                f"Die Stationskamera liefert nur {fps:.1f} statt mindestens "
                f"{min_fps:g} Bildern pro Sekunde."
            ),
        )
    return StationProbeResult(ok=True, frames=got, fps=fps,
                              width=want_w, height=want_h)
