"""Haupt-GUI für EduBotics Setup.

Bietet eine schrittweise Oberfläche für:
  1. Erkennen und Identifizieren der Roboterarme (Leader/Follower)
  2. Kamera auswählen
  3. EduBotics-Umgebung starten/stoppen (Container in der EduBotics WSL2-Distro)
  4. EduBotics Web-Oberfläche in eingebettetem Fenster öffnen
"""

import base64
import os
import secrets
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import webbrowser


def _is_elevated() -> bool:
    """Return True if the current process already holds an elevated admin token.

    Non-Windows always returns False. Never raises — an unreadable token is
    treated as non-elevated (the safe assumption for the UAC-prompt path).
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _uac_enabled() -> bool:
    """Return True if UAC (EnableLUA) is enabled on this machine.

    With EnableLUA=0 the ``runas`` verb no longer shows a consent dialog and,
    from a non-admin token, silently fails — so we probe it up front to give
    the student a clear German instruction instead of a bare error code.
    Defaults to True (the Windows default) whenever the key can't be read.
    """
    if sys.platform != "win32":
        return True
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "EnableLUA")
            return int(val) != 0
    except (OSError, ValueError):
        return True


# ShellExecuteExW failure codes returned in hInstApp (a fake HINSTANCE <= 32).
# Mapped to German so the student sees a cause, not a raw number.
_SE_ERR_REASONS = {
    0:  "Nicht genügend Arbeitsspeicher",
    2:  "Datei nicht gefunden",
    3:  "Pfad nicht gefunden",
    5:  "Zugriff verweigert (UAC abgelehnt oder Richtlinie)",
    8:  "Nicht genügend Arbeitsspeicher",
    26: "Freigabeverletzung",
    27: "Unvollständige Dateizuordnung",
    28: "Zeitüberschreitung",
    29: "Start fehlgeschlagen",
    30: "Datei ist belegt",
    31: "Keine zugeordnete Anwendung",
    32: "DLL nicht gefunden",
}


def _elevate_and_wait(exe: str, args: str, show: int = 1):
    """Run `exe args` elevated via UAC and wait for exit.

    Uses ShellExecuteExW (Win32) directly instead of PowerShell's
    `Start-Process -Verb RunAs -Wait`, which has known parameter-set
    conflicts and unreliable wait semantics.

    Returns (exit_code, cancelled, error_message):
      - exit_code is an int ONLY when the elevated process actually ran and
        exited; it is None on any LAUNCH failure (UAC cancelled, ShellExecuteEx
        error, no process handle, or a wait/exit-code readback failure). Callers
        MUST treat ``exit_code is None`` as "never ran", distinct from a numeric
        non-zero "ran and failed".
      - cancelled is True only for the UAC-consent-denied case, so callers can
        offer a retry rather than a manual-command fallback.
    On non-Windows, returns (None, False, 'not supported').
    """
    if sys.platform != "win32":
        return None, False, "not supported"

    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC        = 0x00000100
    SEE_MASK_FLAG_NO_UI     = 0x00000400  # suppress the OS error dialog (e.g. the
                                          # policy-block "Einschränkungen" box) so our
                                          # own actionable German fallback is what shows
    ERROR_CANCELLED         = 1223
    INFINITE                = 0xFFFFFFFF
    WAIT_FAILED             = 0xFFFFFFFF

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize",       wintypes.DWORD),
            ("fMask",        wintypes.ULONG),
            ("hwnd",         wintypes.HWND),
            ("lpVerb",       wintypes.LPCWSTR),
            ("lpFile",       wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory",  wintypes.LPCWSTR),
            ("nShow",        ctypes.c_int),
            ("hInstApp",     wintypes.HINSTANCE),
            ("lpIDList",     ctypes.c_void_p),
            ("lpClass",      wintypes.LPCWSTR),
            ("hkeyClass",    wintypes.HANDLE),
            ("dwHotKey",     wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess",     wintypes.HANDLE),
        ]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = args
    info.nShow = show

    # Load with use_last_error=True so ctypes preserves the Win32 last-error
    # across the call boundary — ctypes.get_last_error() reads the *ctypes*
    # copy, which is only captured for functions from a use_last_error DLL.
    # The previous ctypes.windll.* handles did NOT set this, so get_last_error()
    # returned a stale/zero value and every failure was misreported.
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    # DWORD restype: ctypes' default signed c_int returns WAIT_FAILED as -1,
    # which never equals the unsigned 0xFFFFFFFF constant -> the failure branch
    # was dead and a genuine wait failure fell through misdiagnosed.
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    ok = shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        err = ctypes.get_last_error()
        # hInstApp carries the ShellExecute-specific reason (a fake HINSTANCE
        # <= 32) that classifies the failure more precisely than GetLastError.
        h_inst = ctypes.cast(info.hInstApp, ctypes.c_void_p).value or 0
        # The cancel text deliberately does NOT contain "abgebrochen": the
        # caller echoes `UAC-Fehler: {err}` into the log, and the routing
        # regression test asserts on "abgebrochen" — the echo must not be able
        # to satisfy it (UacCancelDetectionTest pins this).
        if err == ERROR_CANCELLED:
            return None, True, "UAC-Zustimmung verweigert"
        reason = _SE_ERR_REASONS.get(h_inst)
        if reason:
            return None, False, f"{reason} (Fehler {err}, hInstApp={h_inst})"
        return None, False, f"ShellExecuteEx Fehler {err} (hInstApp={h_inst})"

    if not info.hProcess:
        return None, False, "Kein Prozess-Handle erhalten"

    wait_rc = kernel32.WaitForSingleObject(info.hProcess, INFINITE)
    if wait_rc == WAIT_FAILED:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(info.hProcess)
        return None, False, f"Warten auf den Prozess fehlgeschlagen (Fehler {err})"
    exit_code = wintypes.DWORD(0)
    got = kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
    kernel32.CloseHandle(info.hProcess)
    if not got:
        err = ctypes.get_last_error()
        return None, False, f"Exit-Code konnte nicht gelesen werden (Fehler {err})"
    return int(exit_code.value), False, None


def _run_privileged(exe: str, args: str, show: int = 1):
    """Run `exe args` with admin rights, choosing the path automatically.

    Rationale — "no UAC prompt appears" on an admin account is NOT a failure:
    it means either UAC is set to "Never notify" (the admin's elevation happens
    silently) or UAC is off entirely (this GUI process already carries a FULL
    admin token). In both cases the process is ALREADY elevated, so re-invoking
    the ``runas`` verb is redundant and can misbehave. Instead, when we already
    hold an admin token we run the command DIRECTLY with no prompt — this is
    exactly what makes setup succeed unattended on those accounts. Only when we
    are NOT elevated do we route through ShellExecuteEx "runas" (which shows the
    consent dialog on a standard token).

    Returns the SAME (exit_code, cancelled, error_message) contract as
    _elevate_and_wait: exit_code is an int only when the process actually ran
    and exited, None on any launch failure; cancelled is True only for a UAC
    decline (impossible on the direct path). On non-Windows, ('not supported').
    """
    if sys.platform != "win32":
        return None, False, "not supported"

    if not _is_elevated():
        # Standard token → we need the OS to elevate us via the UAC prompt.
        # Exception-guarded so an unexpected ctypes failure surfaces as the
        # normal launch-failure tuple instead of killing the caller's worker
        # thread (which would also strand re-entrancy flags like
        # _finalize_in_progress).
        try:
            return _elevate_and_wait(exe, args, show)
        except Exception as e:  # noqa: BLE001
            return None, False, f"Elevation fehlgeschlagen: {e}"

    # Already elevated → run the (already-quoted) command line directly with no
    # UAC. Build `"exe" args` so powershell's own command-line parsing applies
    # to the pre-quoted -File/-LogPath/-MarkerPath tokens the callers construct.
    # Resolve powershell.exe to its System32 path: unlike ShellExecuteEx (App
    # Paths/System32), a plain CreateProcess search order includes the process
    # CWD — an ELEVATED spawn must never resolve the binary from a
    # user-writable directory.
    if exe.lower() == "powershell.exe":
        exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
    CREATE_NO_WINDOW = 0x08000000  # no console window flashes at the student
    cmdline = f'"{exe}" {args}'
    try:
        proc = subprocess.run(
            cmdline,
            creationflags=CREATE_NO_WINDOW,
            # NO timeout — deliberately matching the UAC path's INFINITE wait.
            # finalize (rootfs import + multi-GB image pull) can legitimately
            # exceed any "generous" cap on slow school internet, and a timeout
            # kill here is worse than useless: subprocess.run() kills only the
            # powershell.exe child (no job object), so the wsl.exe/msiexec
            # grandchildren keep running detached while the GUI reports a
            # failure — and killing `wsl --import` mid-copy can leave the
            # corrupt-VHDX state the import script's disk gate exists to
            # prevent. The child runs on the caller's daemon thread, so a
            # wedged child never blocks the UI; finalize is idempotent, so a
            # GUI restart simply resumes.
        )
        return proc.returncode, False, None
    except Exception as e:  # noqa: BLE001
        return None, False, f"Direkter Start fehlgeschlagen: {e}"


def _ps_single_quote(s: str) -> str:
    """Escape a string for safe embedding inside a single-quoted PowerShell token.

    A single quote inside a single-quoted PS string must be doubled (`''`).
    Without this, a path containing an apostrophe (e.g. a Windows username like
    `O'Brien`) would terminate the quoted token early and break — or reshape —
    the elevated command line.
    """
    return str(s).replace("'", "''")


def _edubotics_diag_dir() -> str:
    """Return (creating if needed) the EduBotics diagnostics directory.

    Delegates to the ONE shared resolver (``constants.diagnostics_dir`` ->
    ``%ProgramData%\\EduBotics\\logs``, with a writability fallback), which is
    also what ``device_manager._diagnostics_log_path`` uses. That sharing is the
    whole point: the elevated repair transcripts written here
    (edubotics_finalize.log / .marker, edubotics_repair_usbipd.log,
    edubotics_bind_devices.log) must sit NEXT TO install_diagnostics.log, or
    support asks for "the log", gets it, and it is missing exactly the
    failed-setup evidence they need. Elevated log/marker files go here rather
    than %TEMP% so the GUI and its elevated child agree on one path the GUI
    unambiguously knows; the child receives it as an explicit -LogPath argument,
    so a fallback resolution stays coherent across the privilege boundary.
    """
    return diagnostics_dir()


def _asset_path(name: str) -> str:
    """Return absolute path to an asset file; works in dev + PyInstaller frozen builds."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "assets", name)


def _apply_window_icon(root: tk.Tk) -> None:
    """Set the EduBotics icon on a Tk window. Silent on failure."""
    ico = _asset_path("icon.ico")
    png = _asset_path("icon.png")
    if sys.platform == "win32" and os.path.isfile(ico):
        try:
            root.iconbitmap(default=ico)
            return
        except tk.TclError:
            pass
    if os.path.isfile(png):
        try:
            root.iconphoto(True, tk.PhotoImage(file=png))
        except tk.TclError:
            pass


from . import device_manager, docker_manager, health_checker, config_generator, wsl_bridge, update_checker, webview_window
from .roboter_studio_control import RoboterStudioControlServer
from . import theme as theme_mod
from . import camera_bridge as camera_bridge_mod
from . import phone_camera as phone_camera_mod
from . import phone_cert as phone_cert_mod
from .constants import (
    APP_VERSION,
    UPDATE_API_URL,
    IMAGE_OPEN_MANIPULATOR,
    IMAGE_TAG,
    PORT_WEB_UI,
    PORT_PHONE_HTTPS,
    PHONE_CAM_ID,
    DOCKER_DIR,
    ENV_FILE,
    ROBOT_PROFILES,
    DEFAULT_ROBOT_PROFILE,
    cameras_use_native_bridge,
    diagnostics_dir,
)


def _frame_to_photo_data(frame_bgr):
    """Encode a BGR frame (numpy ndarray) to base64-PNG bytes for tk.PhotoImage(data=...).

    Tk 8.6 (the shipped Python 3.11 build) cannot reliably render raw binary PPM via
    the image -data option — NUL bytes truncate through the Tcl string bridge. base64
    PNG is binary-safe and renders on Tk 8.6 and 9.0. Returns ascii base64 bytes, or
    None if the frame can't be encoded.
    """
    try:
        import cv2
        # Downscale the preview for DISPLAY only — the capture/recording path
        # keeps the full 640x480 frame untouched. Two previews render
        # side-by-side (holder.pack(side=tk.LEFT)) in the 700px-wide setup
        # window, so each must stay well under ~330px or the second camera's
        # preview AND the gripper/scene role combos below get clipped
        # off-screen — which would defeat the whole point of the side-by-side
        # thumbnails (telling the two identical-serial Innomakers apart). We
        # area-downscale to 320 wide: clean, unlike the old [::2,::2]
        # nearest-decimation that looked aliased/"broken". Keep BGR (cv2
        # handles BGR->PNG colors).
        img = frame_bgr
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        if img.ndim != 3 or img.shape[2] != 3:
            return None
        preview_max_width = 320
        if img.shape[1] > preview_max_width:
            new_height = max(
                1, round(img.shape[0] * preview_max_width / img.shape[1]))
            img = cv2.resize(
                img, (preview_max_width, new_height),
                interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return None
        return base64.b64encode(buf.tobytes())
    except Exception:  # noqa: BLE001 — preview is best-effort
        return None


def _read_freshest_frame(cap):
    """Read the FRESHEST frame, draining the driver's backlog first.

    The preview polls slower than the camera captures (~30 fps), so MSMF queues
    frames and a plain ``cap.read()`` returns the OLDEST queued one — the
    preview then drifts further behind real time every tick (the "lags very
    much" symptom). We ``grab()`` (cheap — no decode) while frames are
    IMMEDIATELY available; the first grab that has to WAIT for a fresh capture
    (>~12 ms) means the backlog is drained, so we ``retrieve()`` (decode) just
    that newest frame. Bounded so a fast camera can't spin the loop. Returns
    ``(ok, frame)`` like ``cap.read()``; falls back to a plain read if a backend
    lacks grab/retrieve or anything raises.
    """
    try:
        drained = False
        for _ in range(12):  # bounded drain — never spin forever
            t0 = time.monotonic()
            if not cap.grab():
                break
            drained = True
            if (time.monotonic() - t0) > 0.012:
                # This grab waited for a fresh capture → backlog now empty.
                break
        if drained:
            ok, frame = cap.retrieve()
            if frame is not None:
                return ok, frame
    except Exception:  # noqa: BLE001 — fall back to a plain read
        pass
    return cap.read()


def _redact_secret_env_line(line: str) -> str:
    """Mask secret values (HF_TOKEN, *_SECRET, *_KEY, *_PASSWORD) when echoing
    the generated .env to the on-screen Protokoll, so a token can't leak into a
    screenshot / screen-share / support paste. Non-secret lines pass through
    unchanged. Generated .env lines have no leading indentation (the log call
    adds it), so returning ``key=***`` is safe."""
    stripped = line.strip()
    if "=" not in stripped or stripped.startswith("#"):
        return line
    key = stripped.split("=", 1)[0].strip()
    if any(marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "KEY")):
        return f"{key}=***"
    return line


def _primary_lan_ip() -> str:
    """Best-effort primary outbound IPv4 of this PC, for the phone-camera URL.

    Uses the classic UDP-connect trick: connecting a UDP socket to a public
    address makes the OS pick the primary egress interface WITHOUT sending any
    packet, so we can read its local address. Falls back to the hostname-
    resolved address, then 127.0.0.1. The teacher can edit the URL if a VPN /
    multi-NIC setup makes this wrong (the dialog has a copy button)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no traffic — just selects the route
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not host_ip.startswith("127."):
            return host_ip
    except OSError:
        pass
    return "127.0.0.1"


# ── finalize_install.ps1 exit codes — THE contract, mirrored here ────────────
# The script's exit code is the AUTHORITY on what happened; `.reboot_required`
# is NOT a reboot discriminator (it means "the deferred work is not finished",
# which is true of every non-zero code below) and must never be read as one.
# Keep in lockstep with the $EXIT_* constants in installer/scripts/
# finalize_install.ps1 — the whole reason 10 and 12 became dead code and every
# failure reported "Neustart erforderlich" is that these two halves were owned
# by different people and only one of them moved.
FINALIZE_EXIT_DONE = 0      # import + pull succeeded; the flag was cleared
FINALIZE_EXIT_REBOOT = 10   # host reboot still required; nothing installed yet
FINALIZE_EXIT_CONSENT = 12  # rootfs rebuild needs consent -> re-run the installer

# Crossing arm families invalidates a scan (see _hardware_ready). Two sentences
# because the two surfaces differ: the status bar carries one short line, the
# start warning has room to say what to do. Deliberately near-identical to the
# Pi's _ARM_FAMILY_MISMATCH_DE — the two wizards are twins and a student may
# meet either.
ARM_FAMILY_MISMATCH_DE = (
    "Die gescannten Arme gehören zu einem anderen Robotertyp. "
    "Bitte die Arme für den gewählten Robotertyp neu scannen."
)
ARM_FAMILY_MISMATCH_STATUS_DE = "Robotertyp gewechselt — bitte die Arme neu scannen."

# Protokoll buffer cap. Insert-only and unbounded before this, the pane grew for
# the whole classroom day. Generous enough that no real diagnosis is truncated.
LOG_MAX_LINES = 5000

# How often the GUI asks the camera bridge how it is doing. Read-only, and it
# logs only on a CHANGE — see `_poll_camera_health`.
CAMERA_HEALTH_POLL_MS = 3000

# Starting wrap width for the grey helper paragraphs, in PIXELS. `wraplength` is
# a fixed pixel value, so a 620 px paragraph inside a container that can be
# 501 px wide at `minsize` is CLIPPED — and the only scrollbar is vertical, so
# the cut-off text is unreachable. `_build_scrollable_container`'s canvas
# <Configure> handler re-wraps every registered label to the real width; this is
# only the value they are born with, before the first configure event.
HINT_WRAP_DEFAULT_PX = 620
# Never wrap narrower than this, however small the window gets.
HINT_WRAP_MIN_PX = 220
# Left inset assumed for a helper paragraph while the widget tree is not yet
# realized (the first <Configure>, where every winfo_* answer is meaningless).
# 20 px reproduces the flat `canvas.width - 40` this used to apply to every
# paragraph regardless of depth; once realized, the inset is MEASURED per label.
HINT_WRAP_FALLBACK_INSET_PX = 20

# The four severity markers, and the ONLY four. Same set
# installer/scripts/preflight_system.ps1::Emit writes, whose lines
# `_run_preflight_diagnostics` echoes straight into this pane — adopting them is
# convergence, not invention. (colour, tag name) per marker.
LOG_LEVEL_COLORS = {
    "fehler": "#c0392b",
    "warnung": "#b9770e",
    "ok": "#1e8449",
    "info": "#2471a3",
}


class EduBoticsApp:
    """Hauptfenster der Anwendung."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EduBotics")
        # Open at the preferred size, but never taller than the screen actually
        # allows — on a 1366x768 student laptop a fixed 830px window runs off
        # the bottom edge (below the taskbar), hiding "Umgebung starten" and the
        # Protokoll. We clamp the initial height to the usable screen height; the
        # scrollable container built in _build_ui() makes anything that still
        # doesn't fit reachable by scrolling.
        pref_w, pref_h = 700, 830
        try:
            screen_h = self.root.winfo_screenheight()
            # Reserve room for the Windows taskbar + the title bar + a margin.
            usable_h = max(480, screen_h - 80)
            init_h = min(pref_h, usable_h)
        except tk.TclError:
            init_h = pref_h
        self.root.geometry(f"{pref_w}x{init_h}")
        # Place the top edge near the top of the screen so the window grows
        # downward into the available space rather than off either edge.
        self.root.geometry("+60+20")
        self.root.minsize(640, 520)
        self.root.resizable(True, True)
        # Every grey helper paragraph, re-wrapped to the real container width by
        # `_build_scrollable_container`'s canvas <Configure> handler. Populated
        # by `_hint_label`; initialised HERE because `_build_ui` (and the
        # scroll container inside it) runs below.
        self._wrapping_labels = []
        # One ttk.Style for the whole window, installed BEFORE `_build_ui`
        # because every widget below names a style from it. There was no
        # `ttk.Style()` anywhere in this GUI before — fonts were inline
        # literals in three shapes and the destructive „Daten zurücksetzen"
        # was visually identical to „Umgebung starten".
        theme_mod.apply_theme(self.root)
        _apply_window_icon(self.root)

        # State
        self.hardware = device_manager.HardwareConfig()
        self.cameras: list[device_manager.CameraDevice] = []
        # Set once finalize_install.ps1 reports FINALIZE_EXIT_DONE, so a
        # .reboot_required it could not delete can't re-route the rest of the
        # session back into finalize. Session-scoped on purpose: a next launch
        # retries the (idempotent) finalize once, which retries the delete.
        self._finalize_completed = False
        # Native camera capture bridge (Windows student path). Created on
        # environment start, stopped on environment stop / app close.
        self.camera_bridge = None
        # Camera-health poll (see `_poll_camera_health`): the scheduled
        # `root.after` id, and the last error string seen per role so the log
        # fires on a CHANGE and never on every tick.
        self._camera_health_after_id = None
        self._camera_health_last = {}
        # Phone-as-3rd-camera HTTPS receiver (optional). Created on environment
        # start when the toggle is on; the slot is shared with the camera
        # bridge's external sender (cam_id 2).
        self.phone_server = None
        self.phone_frame_slot = None
        self.gpu_available = False
        self.running = False
        self._scanning = False
        # Set only while „Arme scannen"'s confirmation dialog is on screen.
        # `askyesno` pumps the Tk event loop, so `_scan_arms` can be re-entered
        # from a pending `root.after` while the student is still reading the
        # first dialog — and `_scanning` is not set yet at that point. See
        # `_confirm_arm_scan_closes_window`.
        self._scan_confirm_open = False
        self._prerequisites_done = False
        # Cloud-only mode: skip arm/camera scan, only start physical_ai_manager
        # so the user can open the Cloud tab without any robot hardware.
        self.cloud_only = tk.BooleanVar(value=False)
        # Optional: use a school phone (browser) as a 3rd camera (cam_id 2).
        self.phone_camera_enabled = tk.BooleanVar(value=False)
        # Roboter-Studio leader toggle bridge: a localhost HTTP server the React
        # Roboter Studio tab calls to switch the arm stack between follower-only
        # (leader off — autonomous picking) and both-arms (teleop). Created when
        # the environment starts; see _start_rs_control_server.
        self._rs_control = None
        self._rs_use_gpu = False
        self._rs_phone_enabled = False
        # Robot type (ArmProfile) — HARDSET at env start, baked into the MANAGED
        # .env key EDUBOTICS_ROBOT_TYPE and read by the server at boot. A full
        # restart changes it; the RS leader-toggle only flips FOLLOWER_ONLY
        # WITHIN the omx_full type. The saved value (last session's .env) seeds
        # the selector. _rs_robot_type mirrors the SELECTED profile id: it is set
        # from the selector at env start and reused by _rs_set_leader_mode so a
        # runtime regen keeps the type (MANAGED key → stripped-and-re-emitted).
        self._robot_id_to_label = {
            pid: prof["display_de"] for pid, prof in ROBOT_PROFILES.items()
        }
        self._robot_label_to_id = {
            label: pid for pid, label in self._robot_id_to_label.items()
        }
        _saved_profile = config_generator.read_env_var("EDUBOTICS_ROBOT_TYPE", ENV_FILE)
        if _saved_profile not in ROBOT_PROFILES:
            _saved_profile = DEFAULT_ROBOT_PROFILE
        self.robot_type_var = tk.StringVar(value=self._robot_id_to_label[_saved_profile])
        # Plain-Python mirror of the selector, updated ONLY on the main thread
        # (__init__ + the <<ComboboxSelected>> callback). Worker threads
        # (_do_scan/_run_prerequisite_checks/_do_start via _hardware_ready)
        # read THIS, never the tk StringVar — a cross-thread StringVar.get()
        # can raise "main thread is not in main loop" on stricter tk builds.
        self._robot_profile_selected = _saved_profile
        self._rs_robot_type = _saved_profile
        # True while THIS GUI is mid-leader-toggle (rewriting the .env +
        # recreating the arm container). The bridge refuses a second switch and
        # _rs_get_status reports the in-flight state instead of the optimistic
        # .env value, so the badge never claims "ready" during the restart.
        self._rs_switch_in_flight = False

        # BEFORE _build_ui, because Schritt D's status label is built inside it
        # and reads the .env: a token this PC can prove belongs to another one
        # has to be gone from the file by then, so the label says „Kein Token
        # gespeichert" and the student is re-prompted exactly as on a fresh
        # install. Safe to log from here — _log defers through root.after(0),
        # which cannot fire before mainloop() and therefore not before
        # self.log_text exists.
        self._bind_hf_token()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._check_prerequisites()

    # ── UI Construction ──────────────────────────────────────────────

    def _build_scrollable_container(self):
        """Return a frame whose content can exceed the window height and stay
        reachable via a vertical scrollbar / mouse wheel.

        The whole form (Modus + Schritte A-D + Start-Buttons + Protokoll) is
        taller than a small student laptop screen. Without this, the bottom
        (the Start button, the Protokoll) is clipped off-screen and unreachable.
        Everything is packed into the returned inner frame exactly as before."""
        outer = ttk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._scroll_canvas = canvas

        inner = ttk.Frame(canvas, padding=10)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_height():
            # When the window is taller than the content, stretch the inner
            # frame to fill the canvas so the Protokoll's expand=True still lets
            # it grow (matches the pre-scroll behaviour on large screens). When
            # the content is taller, leave it at its natural height so the
            # scrollbar engages. Guarded against a configure feedback loop.
            canvas_h = canvas.winfo_height()
            target_h = max(inner.winfo_reqheight(), canvas_h)
            if getattr(self, "_scroll_inner_h", None) != target_h:
                self._scroll_inner_h = target_h
                canvas.itemconfigure(window_id, height=target_h)
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_inner_configure(_event):
            _sync_height()

        def _on_canvas_configure(event):
            # Keep the inner frame as wide as the canvas (so wraplength/fill=X
            # widgets behave exactly as they did when packed straight into root).
            canvas.itemconfigure(window_id, width=event.width)
            # `wraplength` is a FIXED pixel value, so the helper paragraphs
            # asked for ~620 px inside a container that is 501 px wide at
            # `minsize` — and the only scrollbar here is VERTICAL, so the
            # clipped text had no way to be reached at all. Re-wrap to the
            # width the container actually has.
            #
            # PER LABEL, not one width for all of them. The paragraphs sit at
            # different NESTING DEPTHS: some directly in this scroll container,
            # others inside a `ttk.LabelFrame(padding=10)` that costs them its
            # padding and border on top of the inner frame's. A single flat
            # `event.width - 40` is therefore correct for the shallow ones and
            # too generous for the nested ones — measured at `minsize`, the
            # Schritt-D HuggingFace paragraph asked for 589 px in a 581 px
            # container, i.e. 8 px of text no scrollbar could reach. Tuning the
            # constant only moves which depth is wrong; measuring the actual
            # inset is right at every depth, including any added later.
            survivors = []
            for lbl in getattr(self, "_wrapping_labels", ()):
                try:
                    if not lbl.winfo_exists():
                        continue  # destroyed with a rebuilt frame
                    lbl.configure(
                        wraplength=self._hint_wrap_width(lbl, canvas, event.width))
                    survivors.append(lbl)
                except tk.TclError:
                    continue
            self._wrapping_labels = survivors
            _sync_height()

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scrolling, active only while the pointer is over the app.
        # When the pointer is over the Protokoll text box, let that widget keep
        # its own wheel handling instead of scrolling the whole form.
        def _on_mousewheel(event):
            widget = self.root.winfo_containing(event.x_root, event.y_root)
            log = getattr(self, "log_text", None)
            if widget is not None and log is not None:
                wpath, lpath = str(widget), str(log)
                if wpath == lpath or wpath.startswith(lpath + "."):
                    return  # over the Protokoll — let it scroll itself
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        return inner

    @staticmethod
    def _hint_wrap_width(label, canvas, canvas_width):
        """Text width available to one registered helper paragraph.

        The label's LEFT INSET relative to the canvas — its padding and borders,
        summed over however many frames it is nested in — is what a flat margin
        cannot know. Doubling it accounts for the symmetric inset on the right.

        Reading it from the CURRENT layout while handling the <Configure> for the
        NEW one is sound because the inset is WIDTH-INDEPENDENT: padding and
        border thicknesses do not change when the window resizes, and these
        labels are packed `anchor=W`, so the label's x is its container's left
        edge plus that padding either way. What the resize changes is the space
        to the RIGHT of the label, which is exactly what is being recomputed.

        Falls back to `HINT_WRAP_FALLBACK_INSET_PX` whenever the measurement
        cannot be trusted — an unrealized widget answers 0 or the parent's
        position, and an implausible inset (negative, or a third of the window)
        means the tree is mid-layout rather than genuinely deeply nested.
        """
        try:
            inset = label.winfo_rootx() - canvas.winfo_rootx()
        except tk.TclError:
            inset = -1
        if not 0 < inset < max(1, canvas_width // 3):
            inset = HINT_WRAP_FALLBACK_INSET_PX
        return max(HINT_WRAP_MIN_PX, canvas_width - 2 * inset)

    def _hint_label(self, parent, text=None, textvariable=None, pady=(2, 0)):
        """A grey helper paragraph that RE-WRAPS with the window.

        One place for the three literals these six paragraphs used to repeat
        (`foreground="gray"`, `font=(theme_mod.FONT_FAMILY, 8)`, `wraplength=620`), and the
        registration that makes the re-wrap work: a fixed `wraplength` clipped
        them at `minsize` with only a vertical scrollbar to reach the rest.
        """
        kwargs = {
            "style": "Hint.TLabel",
            "wraplength": HINT_WRAP_DEFAULT_PX,
            "justify": tk.LEFT,
        }
        if textvariable is not None:
            kwargs["textvariable"] = textvariable
        else:
            kwargs["text"] = text
        label = ttk.Label(parent, **kwargs)
        label.pack(anchor=tk.W, pady=pady)
        self._wrapping_labels.append(label)
        return label

    def _bind_step_state(self, var, label):
        """Colour a Schritt status label from the text its StringVar carries.

        A TRACE, not a call at each write site, and that is the load-bearing
        part: the three status vars are written from `_scan_arms._do_scan`,
        `_try_rehydrate_arms` and `_refresh_hf_token_status`, and the first two
        are SOURCE-EXTRACTED by the test suite against owners built from
        `types.SimpleNamespace` — a new `self.<anything>` reference in either is
        an AttributeError there, with nothing about the scan actually changed.
        Tracing the variable covers every writer, including future ones, and
        changes neither method by a byte.

        Schritt A-D were uniformly grey before this whether they said „Nicht
        gescannt", „Gefunden: …" or „Nicht gefunden" (17 greys, one green and
        one #0A6 in the whole file), so nothing on screen said which steps were
        done and which had failed."""
        def _apply(*_args):
            try:
                label.configure(style=theme_mod.step_style_for(var.get()))
            except tk.TclError:  # pragma: no cover — label already destroyed
                pass
        var.trace_add("write", _apply)
        _apply()
        return label

    def _build_ui(self):
        main = self._build_scrollable_container()

        # Title
        title = ttk.Label(main, text="EduBotics", style="Title.TLabel")
        title.pack(pady=(0, 2))

        # THE WINDOW TITLE STAYS „EduBotics" — `_focus_existing_window` matches
        # it with an EXACT `FindWindowW(None, "EduBotics")`, and the WebView2
        # child is titled „EduBotics" too, so a prefix match could focus the
        # wrong window. Support still needs the version, so it goes here.
        # `tests/test_gui_theme.py` ties the two literals together.
        ttk.Label(main, text=f"Version {APP_VERSION}",
                  style="Hint.TLabel").pack(pady=(0, 6))

        # Status bar
        self.status_var = tk.StringVar(value="System wird geprüft...")
        status_label = ttk.Label(main, textvariable=self.status_var,
                                 style="Status.TLabel")
        status_label.pack(pady=(0, 10))

        # Progress bar
        self.progress = ttk.Progressbar(main, mode="indeterminate", length=400)
        self.progress.pack(pady=(0, 10))

        # ── Modus-Auswahl ──
        mode_frame = ttk.LabelFrame(main, text="Modus", padding=10)
        mode_frame.pack(fill=tk.X, pady=5)

        self.cloud_only_check = ttk.Checkbutton(
            mode_frame,
            text="Nur Cloud-Training (kein Roboter angeschlossen)",
            variable=self.cloud_only,
            command=self._on_mode_changed,
        )
        self.cloud_only_check.pack(anchor=tk.W)
        self._hint_label(
            mode_frame,
            "Aktivieren, um nur die Cloud-Lehrer/Schüler-Anmeldung zu starten — "
            "Aufnahme und Inferenz benötigen weiterhin Roboter-Hardware.")

        # Robot-type (ArmProfile) selection. Hardset for this session and baked
        # into the .env before "Umgebung starten"; a full restart changes it.
        robot_row = ttk.Frame(mode_frame)
        robot_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(robot_row, text="Robotertyp:").pack(side=tk.LEFT)
        self.robot_type_combo = ttk.Combobox(
            robot_row,
            textvariable=self.robot_type_var,
            values=[self._robot_id_to_label[pid] for pid in ROBOT_PROFILES],
            state="readonly",
            width=38,
        )
        self.robot_type_combo.pack(side=tk.LEFT, padx=(6, 0))
        self.robot_type_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_robot_type_changed()
        )
        # ONE paragraph, driven by the SELECTED profile's `help_de`. The two
        # static paragraphs this replaced named „OMX – Voll" and „OMX – Roboter
        # Studio (nur Follower)" on every profile — including edu6_studio, a
        # different robot with neither name — and the second one described a
        # leader toggle that `_start_rs_control_server` never even constructs on
        # a follower-only profile.
        self.robot_help_var = tk.StringVar(value="")
        self._hint_label(mode_frame, textvariable=self.robot_help_var)

        # Profile-independent, so it stays static: true of every robot type.
        self._hint_label(
            mode_frame,
            "Der Robotertyp wird beim Start festgelegt — zum Wechseln die "
            "Umgebung stoppen und neu starten.",
            pady=(6, 0))

        # ── Schritt A: Leader-Arm ──
        leader_frame = ttk.LabelFrame(main, text="Schritt A: Leader-Arm", padding=10)
        leader_frame.pack(fill=tk.X, pady=5)
        self.leader_frame = leader_frame

        self.leader_hint_label = ttk.Label(
            leader_frame, text="Leader-Arm per USB anschließen, dann auf Scannen klicken.")
        self.leader_hint_label.pack(anchor=tk.W)
        leader_row = ttk.Frame(leader_frame)
        leader_row.pack(fill=tk.X, pady=5)

        self.btn_scan_leader = ttk.Button(leader_row, text="Arme scannen", command=self._scan_arms)
        self.btn_scan_leader.pack(side=tk.LEFT)

        self.leader_status_var = tk.StringVar(value="Nicht gescannt")
        self.leader_status_label = ttk.Label(
            leader_row, textvariable=self.leader_status_var)
        self.leader_status_label.pack(side=tk.LEFT, padx=10)
        self._bind_step_state(self.leader_status_var, self.leader_status_label)

        # ── Schritt B: Follower-Arm ──
        follower_frame = ttk.LabelFrame(main, text="Schritt B: Follower-Arm", padding=10)
        follower_frame.pack(fill=tk.X, pady=5)
        self.follower_frame = follower_frame

        self.follower_hint_label = ttk.Label(
            follower_frame, text="Follower-Arm per USB anschließen (wird zusammen mit Leader gescannt).")
        self.follower_hint_label.pack(anchor=tk.W)
        follower_row = ttk.Frame(follower_frame)
        follower_row.pack(fill=tk.X, pady=5)

        # The SECOND scan button, and it exists because a tkinter widget cannot
        # be re-parented: on a leader-less robot type Schritt A is hidden
        # entirely, so the one button the student MUST press cannot stay inside
        # it. Exactly one of the two is packed at a time (see
        # `_apply_robot_type_labels`), both run `_scan_arms`, and `_scan_arms`
        # disables/re-enables BOTH — a stale enabled twin would let a second
        # worker race the first onto the same /dev/serial ports. Singular
        # wording, matching the Pi's card („Arm scannen").
        self.btn_scan_arm = ttk.Button(
            follower_row, text="Arm scannen", command=self._scan_arms)

        self.follower_status_var = tk.StringVar(value="Nicht gescannt")
        self.follower_status_label = ttk.Label(
            follower_row, textvariable=self.follower_status_var)
        self.follower_status_label.pack(side=tk.LEFT, padx=10)
        self._bind_step_state(self.follower_status_var,
                              self.follower_status_label)

        # Guided repair area for the arms — populated by _show_arm_repair()
        # after a failed scan with the action button that matches the actual
        # failure (bind arms, repair usbipd, finish setup, cable/power hints).
        self.arm_repair_frame = ttk.Frame(follower_frame)
        self.arm_repair_frame.pack(fill=tk.X, pady=(4, 0))

        # ── Schritt C: Kameras ──
        camera_frame = ttk.LabelFrame(main, text="Schritt C: Kameras (bis zu 2)", padding=10)
        camera_frame.pack(fill=tk.X, pady=5)
        self.camera_frame = camera_frame

        self.camera_hint_label = ttk.Label(
            camera_frame,
            text="Kameras anschließen, scannen, und per Checkbox auswählen.")
        self.camera_hint_label.pack(anchor=tk.W)
        camera_row = ttk.Frame(camera_frame)
        camera_row.pack(fill=tk.X, pady=5)

        self.btn_scan_camera = ttk.Button(camera_row, text="Kameras scannen", command=self._scan_cameras)
        self.btn_scan_camera.pack(side=tk.LEFT)

        self.camera_checks_frame = ttk.Frame(camera_frame)
        self.camera_checks_frame.pack(fill=tk.X, pady=5)
        self.camera_check_vars: list[tk.BooleanVar] = []

        # Camera role assignment (visible after 2 cameras selected). Roles are
        # picked per-preview now (no separate dropdowns), so the old
        # gripper_cam_var/scene_cam_var StringVars are gone — the live image
        # under each "Kamera N" badge is the identifier (the two Innomakers are
        # identical-serial, so a name/index can't disambiguate them).
        self.camera_role_frame = ttk.Frame(camera_frame)
        self.camera_role_frame.pack(fill=tk.X, pady=5)
        self._preview_role_vars = []

        # Optional: phone as a 3rd camera. The phone's browser streams over
        # HTTPS to EduBotics.exe, which forwards the frames into the container
        # as /phone/image_raw/compressed (cam_id 2) — it never goes through WSL2
        # USB, so it bypasses the usbipd camera ceiling.
        self.phone_cam_check = ttk.Checkbutton(
            camera_frame,
            text="Handy als 3. Kamera verwenden (über WLAN, kein USB)",
            variable=self.phone_camera_enabled,
        )
        self.phone_cam_check.pack(anchor=tk.W, pady=(6, 0))
        self._hint_label(
            camera_frame,
            "Beim Start zeigt EduBotics eine Web-Adresse an, die du am Handy "
            "im Browser öffnest. Das Handybild erscheint dann als zusätzliche "
            "Kamera /phone (für Roboter-Studio und die Vorschau). Das Handy "
            "muss im selben WLAN sein.")

        # ── Schritt D: HuggingFace Token ──
        # One-time token entry. Stored on THIS PC in %LOCALAPPDATA%\EduBotics\.env
        # (config_generator.upsert_env_var) and forwarded into the container via
        # docker-compose --env-file, so Aufnahme/Training/Inferenz all pick it up
        # from $HF_TOKEN without the student re-entering it anywhere.
        hf_frame = ttk.LabelFrame(main, text="Schritt D: HuggingFace Token", padding=10)
        hf_frame.pack(fill=tk.X, pady=5)
        self.hf_frame = hf_frame
        self._hint_label(
            hf_frame,
            "Einmalig dein HuggingFace-Token eingeben. Es wird auf diesem PC "
            "gespeichert und für Aufnahme, Training und Inferenz verwendet — "
            "du musst es später nirgends erneut eingeben.",
            pady=(0, 0))
        hf_row = ttk.Frame(hf_frame)
        hf_row.pack(fill=tk.X, pady=5)
        self.hf_token_var = tk.StringVar()
        self.hf_token_entry = ttk.Entry(
            hf_row, textvariable=self.hf_token_var, show="•", width=42,
        )
        self.hf_token_entry.pack(side=tk.LEFT)
        self.btn_save_token = ttk.Button(
            hf_row, text="Token speichern", command=self._save_hf_token,
        )
        self.btn_save_token.pack(side=tk.LEFT, padx=8)
        self.hf_token_status_var = tk.StringVar()
        self.hf_token_status_label = ttk.Label(
            hf_row, textvariable=self.hf_token_status_var)
        self.hf_token_status_label.pack(side=tk.LEFT, padx=4)
        self._bind_step_state(self.hf_token_status_var,
                              self.hf_token_status_label)
        self._refresh_hf_token_status()

        # ── Start-Button ──
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=10)

        self.btn_start = ttk.Button(
            btn_frame, text="Umgebung starten",
            command=self._start_environment,
            style="Primary.TButton",
            state=tk.DISABLED,
        )
        self.btn_start.pack(side=tk.LEFT, padx=5)

        self.btn_stop = ttk.Button(
            btn_frame, text="Stoppen",
            command=self._stop_environment,
            state=tk.DISABLED,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        self.btn_open_browser = ttk.Button(
            btn_frame, text="Web-Oberfläche öffnen",
            command=self._open_webview,
            state=tk.DISABLED,
        )
        self.btn_open_browser.pack(side=tk.LEFT, padx=5)

        # Factory Reset (rechtsbündig, bewusst abgesetzt von Start/Stopp):
        # löscht die Daten-Volumes (Datensätze/Modelle/Kalibrierung), die
        # Distro bleibt. CLAUDE.md verweist seit v2.5.0 auf diesen Knopf.
        self.btn_factory_reset = ttk.Button(
            btn_frame, text="Daten zurücksetzen",
            command=self._factory_reset,
            style="Danger.TButton",
            state=tk.DISABLED,
        )
        self.btn_factory_reset.pack(side=tk.RIGHT, padx=5)

        # ── Protokoll-Ausgabe ──
        log_frame = ttk.LabelFrame(main, text="Protokoll", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, state=tk.DISABLED,
            font=theme_mod.LOG_FONT, wrap=tk.WORD,
        )
        # Severity colouring. `_tag_for_line` reads the marker out of the text,
        # so lines echoed verbatim from preflight_system.ps1 colour too.
        for tag, color in LOG_LEVEL_COLORS.items():
            self.log_text.tag_configure(tag, foreground=color)

        # Getting the log OUT of the pane. „Reparatur fehlgeschlagen — siehe
        # Protokoll" / „Siehe Protokoll oben." send the student here repeatedly,
        # and there was no copy, no save and no cap. The clipboard pattern is
        # the one `_show_manual_elevation_fallback` and `_show_phone_url_dialog`
        # already use in this file.
        #
        # PACKED BEFORE THE TEXT, and `side=BOTTOM`: the whole form is taller
        # than the window, so pack runs out of height inside this frame and
        # squeezes whatever it allocates LAST. Measured with the row packed
        # after the text: both buttons collapsed to 1x1 px and were
        # unreachable. Reserving the row first gives the text the remainder.
        log_btn_row = ttk.Frame(log_frame)
        log_btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        ttk.Button(log_btn_row, text="Protokoll kopieren",
                   command=self._copy_log).pack(side=tk.LEFT)
        ttk.Button(log_btn_row, text="Protokoll speichern",
                   command=self._save_log).pack(side=tk.LEFT, padx=(6, 0))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Apply the saved-default robot type's Schritt A/B wording once the
        # hint labels exist (they are built above in Schritt A/B).
        self._apply_robot_type_labels()

    # ── Robot type (ArmProfile) ──────────────────────────────────────

    def _selected_robot_profile(self) -> str:
        """Profile id currently chosen in the Modus selector.

        Reads the plain-Python mirror (main-thread-maintained), NOT the tk
        StringVar: this method runs on worker threads (scan / prereq / start
        paths through _hardware_ready), where a StringVar.get() is not safe."""
        return self._robot_profile_selected

    def _arms_conflict_with_family(self, profile: str) -> bool:
        """Whether any arm this profile USES is provably of another arm family.

        Follower always, leader only where the profile scans one: a leftover
        leader on a follower-only profile is never launched, so judging it
        would refuse a rig over a record nothing reads.

        Twin of ``pi_agent/agent.py::_arms_conflict_with``."""
        arms = [self.hardware.follower]
        if ROBOT_PROFILES.get(profile, {}).get("scan_requires_leader", True):
            arms.append(self.hardware.leader)
        family = ROBOT_PROFILES.get(profile, {}).get("arm_family", "omx")
        return any(device_manager.serial_path_family_conflict(a.serial_path, family)
                   for a in arms if a is not None)

    def _hardware_ready(self, profile: str | None = None) -> bool:
        """Whether the scanned hardware satisfies the (given/selected) robot type.

        A both-arms type needs leader+follower (HardwareConfig.is_complete); a
        follower-only type (Roboter Studio kit, no leader) needs only the
        follower (HardwareConfig.follower_present).

        PRESENCE IS NOT ENOUGH — the arms must also be of this profile's ARM
        FAMILY. Measured headless before this check existed: with an OMX pair
        scanned, `_hardware_ready('edu6_studio')` returned True, and with a
        single edu6 arm scanned `_hardware_ready('omx_follower')` returned True
        — so the Modus dropdown alone could arm „Umgebung starten" for a
        Feetech bringup over a Dynamixel bus, or the reverse. Same hole the Pi
        had (see ``pi_agent/agent.py::_hardware_ready`` for the measured
        consequence); crossing arm families INVALIDATES the scan on both
        platforms (user decision, 2026-07-29).

        The hole THIS check closes is in-session: scan, then change the
        dropdown. The rehydrate path restores only family-matching PORTS,
        because ``_try_rehydrate_arms`` passes ``arm_family=`` into
        ``fast_rehydrate_arms``, which filters.

        Do NOT read that as "the .env path is protected" — an earlier revision
        of this docstring did, and it is wrong in a way this very function
        causes. ``_try_rehydrate_arms`` derives BOTH its ``arm_family`` and its
        two "never overwrite a scan" guards from the profile in the **.env**
        (a previous session's), and each guard asks ``_hardware_ready(<.env
        profile>)``. After a cross-family switch a fresh scan can never satisfy
        that profile — precisely what the family check above guarantees — so
        the guard is structurally unable to fire in the one case it is for, and
        the rehydrate silently restores the OLD family's ports over the new
        scan. Never UNSAFE (``_update_start_button`` and ``_start_environment``
        both re-gate on the SELECTED profile, so the arm cannot start); the cost
        is a discarded scan the student is not told about. Logged as a known
        issue in CLAUDE.md; the fix is to key both guards on the selected
        profile rather than the .env one."""
        if profile is None:
            profile = self._selected_robot_profile()
        if ROBOT_PROFILES.get(profile, {}).get("scan_requires_leader", True):
            if not self.hardware.is_complete:
                return False
        elif not self.hardware.follower_present:
            return False
        return not self._arms_conflict_with_family(profile)

    def _apply_robot_type_labels(self):
        """Reword the whole hardware section for the selected robot type.

        A leader-less robot type gets a SINGULAR, single-step scan card: Schritt
        A is hidden outright and Schritt B becomes „Schritt A: Roboterarm" with
        the scan button in it. The frame used to stay on screen titled
        „Schritt A: Leader-Arm (für diesen Typ nicht nötig)" — wrapped around
        the only button the student must press — and it rendered identically on
        `edu6_studio`, a 6-axis Feetech arm with no leader CONCEPT, while naming
        `omx_follower`'s display label. A permanently empty „Leader —" makes a
        Roboter-Studio kit look half-broken; the Pi's card went singular for
        exactly that reason and Windows now agrees with it.

        `omx_full` is untouched: both frames, both plural wordings, the button
        in Schritt A.
        """
        profile = self._selected_robot_profile()
        row = ROBOT_PROFILES.get(profile, {})
        has_leader = row.get("scan_requires_leader", True)
        self.robot_help_var.set(row.get("help_de", ""))
        # Schritt C follows the profile's camera_roles — a kit that can carry
        # only a scene camera must not advertise „bis zu 2".
        max_cams = len(row.get("camera_roles", ("gripper", "scene"))) or 1
        self.camera_frame.configure(
            text=("Schritt C: Kamera" if max_cams == 1
                  else f"Schritt C: Kameras (bis zu {max_cams})"))
        self.camera_hint_label.configure(
            text=("Kamera anschließen, scannen, und per Checkbox auswählen."
                  if max_cams == 1 else
                  "Kameras anschließen, scannen, und per Checkbox auswählen."))
        self.btn_scan_camera.configure(
            text="Kamera scannen" if max_cams == 1 else "Kameras scannen")
        if has_leader:
            self.leader_frame.pack(fill=tk.X, pady=5, before=self.follower_frame)
            self.btn_scan_arm.pack_forget()
            self.btn_scan_leader.pack(side=tk.LEFT)
            self.leader_frame.configure(text="Schritt A: Leader-Arm")
            self.leader_hint_label.configure(
                text="Leader-Arm per USB anschließen, dann auf Scannen klicken.")
            self.follower_frame.configure(text="Schritt B: Follower-Arm")
            self.follower_hint_label.configure(
                text="Follower-Arm per USB anschließen (wird zusammen mit Leader gescannt).")
        else:
            self.leader_frame.pack_forget()
            self.btn_scan_leader.pack_forget()
            self.btn_scan_arm.pack(side=tk.LEFT, before=self.follower_status_label)
            self.follower_frame.configure(text="Schritt A: Roboterarm")
            self.follower_hint_label.configure(
                text="Roboterarm per USB anschließen, dann auf „Arm scannen“ klicken.")

    def _on_robot_type_changed(self):
        """Robot-type selector changed — reword Schritt A/B + refresh gating.

        Runs on the main thread (tk <<ComboboxSelected>>) — the ONE place the
        StringVar is read; everything else consumes the plain mirror."""
        self._robot_profile_selected = self._robot_label_to_id.get(
            self.robot_type_var.get(), DEFAULT_ROBOT_PROFILE)
        self._apply_robot_type_labels()
        # The profile decides the camera ROLES, so a type change must re-derive
        # them. Measured: pick a camera under omx_full (role „gripper", green
        # „Greifer-Kamera: Cam0"), switch the dropdown to edu6_studio — the role
        # stayed „gripper" and the label stayed „Greifer-Kamera", and „Umgebung
        # starten" then wrote CAMERA_NAME_1="gripper" on a rig whose server
        # subscribes only to /scene/…. That is the likeliest real-world route
        # into the silent misconfiguration, since changing your mind about the
        # robot type after picking a camera is an ordinary thing to do.
        self._on_cameras_changed()
        if not self.cloud_only.get():
            if self._hardware_ready():
                self._set_status("Bereit — Start klicken.")
            elif self._arms_conflict_with_family(self._robot_profile_selected):
                # A wrong-FAMILY scan is not the same as no scan, and this is
                # the moment it happens — the student changed the dropdown and
                # a green „Arme erkannt" went grey with the serial paths still
                # on screen. „Hardware scannen, um zu beginnen" would send them
                # looking at cables instead of at what they just clicked.
                self._set_status(ARM_FAMILY_MISMATCH_STATUS_DE)
            else:
                self._set_status("Bereit — Hardware scannen, um zu beginnen")
        self._update_start_button()

    # ── HuggingFace token ────────────────────────────────────────────

    def _bind_hf_token(self):
        """Judge the stored HuggingFace token against THIS PC, and say so.

        %LOCALAPPDATA%\\EduBotics\\.env travels — roaming profile, FSLogix
        container, AppData redirection, or a golden image captured after a first
        launch — and it carries HF_TOKEN, which compose forwards into
        physical_ai_server. So a copied profile would upload one student's
        recordings under another's HuggingFace account. The decision and the
        deletion live in ``config_generator.bind_hf_token_to_this_machine``; this
        method exists to put the outcome in the Protokoll, because removing a
        credential the student entered must not happen silently.

        Never raises: an unreadable/unwritable .env is reported and the launch
        continues. The regenerate-side filter in
        ``config_generator._read_unmanaged_lines`` still keeps a foreign token out
        of the .env compose reads, so a failure here costs the report and the
        early deletion, not the protection.
        """
        try:
            verdict = config_generator.bind_hf_token_to_this_machine(ENV_FILE)
        except Exception as e:  # noqa: BLE001 — a diagnostic must not block startup
            self._log(
                f"[WARNUNG] Das gespeicherte HuggingFace-Token konnte nicht "
                f"geprüft werden: {e}"
            )
            return
        if verdict == config_generator.HF_TOKEN_FOREIGN:
            self._log(
                "[WARNUNG] Das gespeicherte HuggingFace-Token stammt von einem "
                "anderen PC — die EduBotics-Konfiguration wurde offenbar "
                "mitkopiert. Das Token wurde von diesem PC gelöscht, damit "
                "Aufnahmen nicht unter einem fremden HuggingFace-Konto landen. "
                "Bitte in Schritt D dein eigenes Token eingeben."
            )
        elif verdict == config_generator.HF_TOKEN_ADOPTED:
            self._log(
                "HuggingFace-Token wurde diesem PC zugeordnet und bleibt "
                "gespeichert — eine erneute Eingabe ist nicht nötig."
            )

    def _refresh_hf_token_status(self):
        """Reflect whether a token is already saved on this PC — without
        revealing the secret. Read from the .env each time so it stays
        accurate after a save or an external edit."""
        try:
            existing = config_generator.read_env_var("HF_TOKEN", ENV_FILE)
        except Exception:
            existing = None
        if existing:
            self.hf_token_status_var.set("✓ Token gespeichert")
        else:
            self.hf_token_status_var.set("Kein Token gespeichert")

    def _save_hf_token(self):
        """Persist the entered HF token to %LOCALAPPDATA%\\EduBotics\\.env.

        The token is cleared from the widget afterwards so the secret isn't
        left on screen; the status label confirms it's saved. Takes effect in
        the container on the next 'Umgebung starten' (--force-recreate)."""
        token = self.hf_token_var.get().strip()
        if not token:
            messagebox.showwarning(
                "Kein Token",
                "Bitte gib dein HuggingFace-Token ein (beginnt mit 'hf_').",
            )
            return
        try:
            # write_hf_token, not upsert_env_var: it stamps HF_TOKEN_MACHINE in
            # the same breath, so a stored token can never exist unstamped and
            # be mistaken for a legacy one on a machine that copied this .env.
            config_generator.write_hf_token(token, ENV_FILE)
        except Exception as e:
            messagebox.showerror(
                "Fehler", f"Token konnte nicht gespeichert werden: {e}"
            )
            return
        self.hf_token_var.set("")
        self._refresh_hf_token_status()
        self._log("HuggingFace-Token auf diesem PC gespeichert.")
        messagebox.showinfo(
            "Gespeichert",
            "HuggingFace-Token wurde gespeichert. Es wird beim nächsten "
            "„Umgebung starten“ aktiv.",
        )

    # ── Logging ──────────────────────────────────────────────────────

    def _log(self, msg: str):
        """Append a line to the Protokoll (thread-safe).

        SEVERITY IS AN INLINE MARKER, deliberately, not a `level=` parameter:
        `[OK]` / `[INFO]` / `[WARNUNG]` / `[FEHLER]` — the SAME four
        `installer/scripts/preflight_system.ps1::Emit` writes, and those lines
        already land in this very pane via `_run_preflight_diagnostics`. So the
        two producers finally agree instead of the student reading `[OK]`
        interleaved with `WARNUNG:`, `ACHTUNG:`, `Tipp:` and `⚠️`.

        A `level=` keyword was considered and rejected: FIVE methods containing
        `_log` calls are source-extracted by the test suite against a
        `list.append` double, so a keyword argument there is a TypeError waiting
        for whoever adds the next log line. An inline marker has exactly one
        call convention everywhere, and `_tag_for_line` recovers the severity
        from the text — which also colours the PowerShell-produced lines, which
        no parameter ever could. `tests/test_gui_log_levels.py` is what keeps
        the vocabulary closed.
        """
        def _append():
            self.log_text.config(state=tk.NORMAL)
            # PER LINE, not per call: `_run_preflight_diagnostics` deliberately
            # emits its whole block in ONE call (so it cannot interleave with
            # the prerequisite chain running on another thread), and those are
            # exactly the lines that already carry their own markers.
            for line in msg.split("\n"):
                self.log_text.insert(
                    tk.END, line + "\n", self._tag_for_line(line) or ())
            # Bound the buffer. Insert-only and unbounded, this grew for the
            # whole classroom day (the prerequisite chain alone writes dozens
            # of lines per launch, and „Arme scannen" re-runs it).
            try:
                lines = int(self.log_text.index("end-1c").split(".")[0])
                if lines > LOG_MAX_LINES:
                    self.log_text.delete(
                        "1.0", f"{lines - LOG_MAX_LINES + 1}.0")
            except (tk.TclError, ValueError):
                pass
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _append)

    @staticmethod
    def _tag_for_line(line: str) -> str:
        """The colour tag for a Protokoll line, from its leading marker.

        Reads the marker out of the TEXT rather than taking it from the caller,
        so lines echoed verbatim from `preflight_system.ps1` colour identically
        to the ones this file writes. Anything else is untagged (default fg) —
        never guessed."""
        for marker, tag in (("[FEHLER]", "fehler"), ("[WARNUNG]", "warnung"),
                            ("[OK]", "ok"), ("[INFO]", "info")):
            if line.lstrip().startswith(marker):
                return tag
        return ""

    def _copy_log(self):
        """„Protokoll kopieren" — the pane the student is repeatedly told to
        consult („siehe Protokoll") had no way to get its text OUT of it."""
        try:
            text = self.log_text.get("1.0", tk.END)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
        except tk.TclError as exc:
            self._log(f"[WARNUNG] Das Protokoll konnte nicht kopiert werden: {exc}")
            return
        self._log("[OK] Protokoll in die Zwischenablage kopiert.")

    def _save_log(self):
        """„Protokoll speichern" — into the machine-wide diagnostics leaf
        support already asks for, so one folder holds every artefact."""
        try:
            target_dir = _edubotics_diag_dir()
            os.makedirs(target_dir, exist_ok=True)
            path = os.path.join(
                target_dir,
                time.strftime("edubotics_protokoll_%Y%m%d_%H%M%S.txt"))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.log_text.get("1.0", tk.END))
        except Exception as exc:  # noqa: BLE001 — a diagnostic must never raise
            self._log(f"[WARNUNG] Das Protokoll konnte nicht gespeichert "
                      f"werden: {exc}")
            return
        self._log(f"[OK] Protokoll gespeichert: {path}")

    def _set_status(self, msg: str):
        """Statusleiste aktualisieren (thread-sicher)."""
        self.root.after(0, lambda: self.status_var.set(msg))

    def _update_start_button(self):
        """Start-Button aktivieren wenn:
        - Cloud-only mode: prereqs ok, nicht laufend
        - Voller Modus: prereqs ok, beide Arme gefunden, nicht laufend
        """
        def _update():
            # The robot type is snapshotted at env start and is meaningless in
            # cloud-only mode — keep the selector in lockstep with the run/mode
            # state on every transition that routes through here (stop, factory
            # reset, failed start, hardware scan).
            self._update_robot_type_combo_state()
            if not self._prerequisites_done or self.running:
                self.btn_start.config(state=tk.DISABLED)
                self.btn_factory_reset.config(state=tk.DISABLED)
                return
            # Factory Reset needs only a ready distro, no hardware scan —
            # it must work in cloud-only mode and on a half-configured rig.
            self.btn_factory_reset.config(state=tk.NORMAL)
            if self.cloud_only.get():
                self.btn_start.config(state=tk.NORMAL)
            elif self._hardware_ready():
                self.btn_start.config(state=tk.NORMAL)
            else:
                self.btn_start.config(state=tk.DISABLED)
        self.root.after(0, _update)

    def _update_robot_type_combo_state(self):
        """Gray the Robotertyp selector whenever it must not change: while the
        environment RUNS (the type is hardset/snapshotted at "Umgebung starten")
        and in CLOUD-ONLY mode (no robot, so the type is irrelevant). Restores
        the readonly (dropdown) state otherwise. Main-thread only; tolerant of a
        not-yet-built widget so early prereq ticks can't raise."""
        disabled = self.running or self.cloud_only.get()
        try:
            self.robot_type_combo.config(
                state="disabled" if disabled else "readonly")
        except (tk.TclError, AttributeError):
            pass

    def _on_mode_changed(self):
        """Cloud-only checkbox toggled — show/hide hardware sections."""
        is_cloud_only = self.cloud_only.get()
        # Disable the hardware frames in cloud-only mode (visually grayed out).
        # The Robotertyp selector is grayed too (via _update_robot_type_combo_
        # state below), but NOT by adding the whole Modus frame to this loop —
        # that would also disable the cloud-only checkbox itself and trap the
        # student in cloud mode.
        new_state = tk.DISABLED if is_cloud_only else tk.NORMAL
        for frame in (self.leader_frame, self.follower_frame, self.camera_frame):
            self._set_frame_state(frame, new_state)
        self._update_robot_type_combo_state()
        if is_cloud_only:
            self._set_status("Cloud-Modus — Start klicken, um die Web-Oberfläche zu starten.")
        else:
            if self._hardware_ready():
                self._set_status("Bereit — Start klicken.")
            else:
                self._set_status("Bereit — Hardware scannen, um zu beginnen")
        self._update_start_button()

    def _set_frame_state(self, frame, state):
        """Recursively enable/disable widgets in a LabelFrame."""
        for child in frame.winfo_children():
            cls = child.winfo_class()
            try:
                # ttk widgets use state(['disabled']) or state(['!disabled'])
                if hasattr(child, "state") and cls in (
                    "TButton", "TCheckbutton", "TCombobox", "TEntry",
                ):
                    child.state(["disabled"] if state == tk.DISABLED else ["!disabled"])
                elif "state" in child.config():
                    child.config(state=state)
            except (tk.TclError, KeyError):
                pass
            # Recurse into nested frames
            if child.winfo_children():
                self._set_frame_state(child, state)

    # ── Prerequisites Check ──────────────────────────────────────────

    def _check_prerequisites(self):
        """Auf GUI-Update prüfen, dann EduBotics-Umgebung, WSL2, usbipd beim Start prüfen."""
        # Fire the informational system preflight once per launch, off on its own
        # daemon thread so it never blocks or gates the startup checks below.
        threading.Thread(target=self._run_preflight_diagnostics, daemon=True).start()

        def _check():
            self.root.after(0, lambda: self.progress.start(10))

            # Clean up leftover installer .exe from any previous update
            # (can't be deleted while it was running — sweep on next launch).
            try:
                removed = update_checker.cleanup_stale_installers()
                if removed > 0:
                    self._log(f"Aufräumen: {removed} alte Installer-Dateien gelöscht.")
            except Exception:
                pass

            # ── GUI update check (runs before everything else) ──
            self._set_status("Auf GUI-Update prüfen...")
            self._log("Prüfe auf GUI-Updates...")
            update_info = update_checker.check_for_update(APP_VERSION, UPDATE_API_URL)
            if update_info:
                self._log(f"Neue Version verfügbar: {update_info['version']} (aktuell: {APP_VERSION})")
                self.root.after(0, lambda: self.progress.stop())
                self.root.after(0, lambda: self._show_update_dialog(update_info))
                return  # Block all further startup until update is applied
            self._log(f"GUI ist aktuell (Version {APP_VERSION}).")

            # Continue with EduBotics-Umgebung / system checks
            self._run_prerequisite_checks()

        threading.Thread(target=_check, daemon=True).start()

    def _run_prerequisite_checks(self):
        """Re-entrancy guard around the prerequisite scan.

        Four call sites re-run the scan on a worker thread — the update-dialog
        skip, the usbipd repair, the finalize success path, and the "Einrichtung
        abschließen (Administrator)" button. The last two were moved onto worker
        threads to fix a UI freeze, which made a SECOND scan reachable while the
        first is still inside pull_images: pressing the finalize button again
        once _finalize_in_progress clears starts two concurrent 15-30 min image
        pulls AND races two prune_superseded_tags over the same tags (an untag
        the other run is mid-pull on). Same try/finally shape as
        _finalize_in_progress — the flag must survive any exception, or a single
        raise permanently disables re-checks until a GUI restart."""
        if getattr(self, "_prereq_in_progress", False):
            self._log("Systemprüfung läuft bereits — doppelter Start übersprungen.")
            return
        self._prereq_in_progress = True
        try:
            self._run_prerequisite_checks_body()
        finally:
            self._prereq_in_progress = False

    def _run_prerequisite_checks_body(self):
        """EduBotics-Umgebung, Images, GPU prüfen (called after update check passes)."""
        self.root.after(0, lambda: self.progress.start(10))
        self._log("Voraussetzungen werden geprüft...")

        # usbipd reachability — checked before WSL2 so the "Reparieren" UX
        # fires up-front instead of after a 30s "Arme scannen" diagnose loop.
        # The resolver looks in PATH, %ProgramFiles%\usbipd-win, the Uninstall
        # registry, and a registry-refreshed PATH. If it still can't find
        # usbipd we offer one-click repair (elevates + re-runs the prereq
        # + policy scripts), which makes the install bullet-proof against
        # the post-install PATH-propagation race.
        self._set_status("usbipd wird gesucht...")
        if not device_manager.usbipd_reachable():
            self._log("usbipd wurde nicht gefunden (auch nicht in Program Files).")
            self.root.after(0, lambda: self.progress.stop())
            self.root.after(0, self._prompt_repair_usbipd)
            return
        resolved = device_manager.usbipd_path()
        if resolved:
            self._log(f"usbipd: OK ({resolved})")

        # A pending-reboot install deferred the distro import + image pull to a
        # post-reboot finalize: install_prerequisites.ps1 wrote .reboot_required
        # (after a DISM rc=3010) and the .iss ShouldImportDistro/ShouldPullImages
        # gates skipped Steps 4+5. On a FRESH install the distro is missing and
        # the branch below already routes to finalize; but on an UPGRADE the old
        # distro survives, so without this the GUI would silently pull images
        # itself and never complete finalize / clear the flag / prune superseded
        # tags (the v2.13.0 pilot incident). Route BOTH cases through finalize,
        # which clears the flag, re-imports ONLY if the rootfs actually changed,
        # pulls, and prunes.
        #
        # The flag means "deferred work outstanding", not "reboot needed" — the
        # reason only ever comes from finalize's exit code. _finalize_completed
        # is the override: finalize already reported EXIT_DONE this session, so a
        # flag it merely failed to delete must not re-route us into an endless
        # finalize/UAC loop.
        if self._reboot_required_pending() and not self._finalize_completed:
            self._log("Ein ausstehender Windows-Neustart hat die Einrichtung unterbrochen.")
            self.root.after(0, lambda: self.progress.stop())
            self.root.after(0, self._prompt_finalize_install)
            return

        # Check the EduBotics WSL2 distro is installed and docker engine is up
        self._set_status("EduBotics-Umgebung wird geprüft...")
        if not docker_manager.is_distro_registered():
            self._log("EduBotics-Umgebung ist noch nicht eingerichtet.")
            self.root.after(0, lambda: self.progress.stop())
            # Auto-run finalize (finalize_install.ps1) — direct when already
            # elevated, else via one UAC prompt; re-entrancy-guarded inside.
            self.root.after(0, self._prompt_finalize_install)
            return
        # Pin the distro awake for the lifetime of this GUI session BEFORE the
        # rest of the prerequisite scan runs. Without this, WSL2's vmIdleTimeout
        # shuts the distro down ~60s after the last wsl.exe call, killing
        # dockerd and the manager container — the embedded WebView then loads
        # http://localhost:80/ into a dead port. Starting it here (not after the
        # docker-wait below) keeps the distro pinned through the potentially slow
        # docker start, image checks, and arm scan that follow. Idempotent, and
        # safe now that is_distro_registered() has confirmed the distro exists.
        docker_manager.start_keepalive()
        if not docker_manager.is_docker_running():
            self._log("EduBotics-Umgebung startet...")
            docker_manager.start_edubotics_distro()
            if not docker_manager.wait_for_docker(
                callback=lambda e, t: self._set_status(f"Warte auf EduBotics-Umgebung... {e}s/{t}s")
            ):
                self._log("[FEHLER] EduBotics-Umgebung konnte nicht gestartet werden.")
                self._set_status("EduBotics-Umgebung nicht bereit")
                self.root.after(0, lambda: self.progress.stop())
                return
        self._log("[OK] EduBotics-Umgebung bereit.")

        # ── Lifecycle: the GUI is the SOLE owner of the container lifecycle ──
        # The robot stack must come up ONLY after the student has scanned both
        # arms and clicked "Umgebung starten" — never before. Two paths can
        # leave it running prematurely: (1) dockerd resurrects containers on
        # distro boot when they carry a `restart` policy other than "no"
        # (pre-2.5.3 images used `unless-stopped`), and the distro boots the
        # moment this GUI first touched WSL above (the startup preflight thread
        # boots it on EVERY launch, before we get here); (2) a previous session
        # left the stack up. Either way the arm would be driven — and the
        # Dynamixel serial bus held — before any scan, which ALSO breaks the
        # scan itself (identify_arm.py collides with the live controller on the
        # same /dev/serial port → neither arm is identified). Tear it down now
        # so the student starts from a clean, deterministic state.
        #
        # This runs at the EARLIEST point dockerd is known reachable, and
        # crucially BEFORE the rootfs gate below: that gate `return`s, and a
        # rootfs mismatch means the distro is OLD and was never re-imported —
        # i.e. exactly the population whose persisted container configs still
        # carry `restart: unless-stopped`. Teardown-after-the-gate left the
        # follower torqued up and running its boot quintic while the GUI showed
        # a destructive-rebuild consent dialog over a live arm, and a consent
        # would then run `wsl --unregister` against a live VM.
        self._set_status("Vorherige Sitzung wird aufgeräumt...")
        # Close a browser window this GUI process left open, for the same
        # reason `_stop_environment` and the arm scan do: while the child is
        # alive, `open_student_window` short-circuits and DISCARDS the fresh
        # `?fresh=<nonce>` URL, so the SPA boot scrub never runs and the next
        # student inherits the previous one's Supabase session.
        #
        # NOT nested in the `if` below — a stale window outlives a stopped
        # environment — and BEFORE the container teardown, so the graceful
        # close still has a live rosbridge.
        #
        # SCOPE, deliberately understated: `destroy_all` is PROCESS-LOCAL, so
        # despite the status text above this does NOT reach a window left by a
        # PREVIOUS GUI process. On a re-check within one session it does. An
        # orphan from a crashed GUI needs a kill-by-cmdline mechanism that does
        # not exist; see docs/KNOWN-ISSUES.md.
        try:
            webview_window.destroy_all()
        except Exception:
            pass
        if docker_manager.ensure_environment_stopped(log=self._log):
            self._log("[INFO] Umgebung gestoppt — sie startet erst, wenn du auf "
                  "„Umgebung starten“ klickst.")

        # ── Rootfs ↔ image version handshake ──────────────────────────────
        # A rootfs bump ships new dockerd pins / modprobe list / udev rules that
        # a new image set may depend on. If the installer's re-import was skipped
        # or declined (ShouldImportDistro "Nein", or a reboot-pending dead-end),
        # the OLD distro survives and the GUI would otherwise pull the NEW
        # :X.Y.Z images straight onto the stale rootfs — the exact silent
        # version-skew CLAUDE.md warns about ("new images on old rootfs with no
        # handshake"). Compare the distro's baked /etc/edubotics-rootfs-version
        # to the shipped wsl_rootfs/ROOTFS_VERSION and, on a POSITIVE mismatch,
        # refuse to proceed: route to the CONSENTED re-import instead. Fails OPEN
        # on any unreadable/absent stamp so a hiccup never bricks a classroom.
        if self._rootfs_rebuild_required():
            self._log(
                "[WARNUNG] Die EduBotics-Umgebung muss neu aufgebaut werden — "
                "das System-Update passt nicht zur installierten Umgebung. "
                "Es werden KEINE neuen Images auf die alte Umgebung geladen."
            )
            self.root.after(0, lambda: self.progress.stop())
            self.root.after(
                0, lambda: self._prompt_finalize_install(reason="rootfs_mismatch")
            )
            return

        # A FROZEN (.exe) build should always resolve IMAGE_TAG from the baked
        # docker/versions.env. If it resolved to "latest", versions.env was
        # missing/unreadable and this install will silently track main-HEAD
        # :latest instead of its pinned release (the Windows analogue of the
        # documented "Pi tracks main HEAD" hazard) — surface it so a teacher can
        # spot a mis-baked / half-copied install.
        if getattr(sys, "frozen", False) and IMAGE_TAG == "latest":
            self._log(
                "[WARNUNG] Image-Version nicht bestimmbar (versions.env fehlt) — "
                "es wird 'latest' verwendet. Bitte EduBotics neu installieren."
            )

        # Check images
        self._set_status("Images werden geprüft...")
        img_status = docker_manager.images_exist()
        missing = [img for img, exists in img_status.items() if not exists]
        if missing:
            self._log(f"Fehlende Images: {', '.join(missing)}")
            self._log("Images werden heruntergeladen (kann beim ersten Mal 15-30 Min. dauern)...")
            self._set_status("Images werden heruntergeladen...")
            if not docker_manager.pull_images(
                callback=lambda img, i, t: self._set_status(f"Lade Image {i+1}/{t}: {img.split('/')[-1]}"),
                log=self._log,
            ):
                self._log("[FEHLER] Images konnten nicht heruntergeladen werden. Internetverbindung prüfen.")
                self._set_status("Image-Download fehlgeschlagen")
                self.root.after(0, lambda: self.progress.stop())
                return
            self._log("[OK] Alle Images erfolgreich heruntergeladen.")

        # Check for image updates. The function pre-checks network reachability
        # and per-image manifest digests so an offline classroom returns in ~5s
        # and an up-to-date one returns in ~3s — only stale or missing images
        # trigger a real layer pull.
        self._set_status("Auf Updates prüfen...")
        self._log("Prüfe auf Image-Updates...")
        if docker_manager.check_for_updates(log=self._log):
            self._log("[OK] Images auf neueste Version aktualisiert.")
        else:
            self._log("[OK] Images sind aktuell.")

        # Surface the persisted last-pull state so a teacher can see at a
        # glance which classroom PCs have been offline too long to refresh.
        # Red status banner only fires past IMAGE_FRESHNESS_WARN_DAYS (14 d
        # by default) — short enough to catch real drift, long enough to
        # avoid spurious banners after a one-week vacation week.
        try:
            pull_status = docker_manager.get_last_pull_status()
            age = pull_status.get("age_days")
            if age is None:
                self._log(
                    "  Image-Frische: kein vorheriger Update-Zeitstempel "
                    "vorhanden — heutiger Stand wird ab jetzt verfolgt."
                )
            else:
                age_int = int(age)
                if pull_status.get("is_stale"):
                    self._log(
                        f"[WARNUNG] Image-Frische: letzter erfolgreicher Update vor "
                        f"{age_int} Tagen — bitte Internetverbindung prüfen."
                    )
                else:
                    self._log(
                        f"  Image-Frische: letzter Update vor "
                        f"{age_int} Tag{'en' if age_int != 1 else ''}."
                    )
            # Always log the per-image digest so support can compare
            # classroom PCs and confirm they match.
            for img, digest in (pull_status.get("digests") or {}).items():
                short = img.split("/")[-1]
                short_digest = (digest or "")[7:19] if digest else "—"
                self._log(f"    {short}: {short_digest}")
        except Exception as exc:
            # Don't let a freshness-banner glitch block GUI startup. Log
            # and continue with the existing (working) flow.
            self._log(f"  (Frische-Status konnte nicht gelesen werden: {exc})")

        # NOTE: we intentionally do NOT auto-resume a stack found running here.
        # `ensure_environment_stopped()` above already tore down any leftover /
        # resurrected containers, so the student always starts from a clean
        # state and the stack comes up only when they click "Umgebung starten".

        # Check GPU
        self.gpu_available = docker_manager.has_gpu()
        self._log(f"NVIDIA GPU: {'erkannt' if self.gpu_available else 'nicht erkannt (CPU-Modus)'}")

        self._prerequisites_done = True
        self._update_start_button()
        self._set_status("Bereit — Hardware scannen, um zu beginnen")
        self.root.after(0, lambda: self.progress.stop())
        self._log("Systemprüfung abgeschlossen. Arme und Kamera anschließen, dann auf Scannen klicken.")

        # PR-4: try to rehydrate the previous session's arm mapping from the
        # persisted .env — saves the full scan (container + pings) on every
        # GUI launch when the same two arms are still plugged in. Scheduled
        # onto the MAIN thread: _run_prerequisite_checks runs in a worker,
        # and the cloud_only tk Var read inside must happen on main (same
        # convention as _start_environment's capture-first pattern).
        self.root.after(0, self._try_rehydrate_arms)

    # ── Repair: usbipd missing (driver-level prerequisite) ──────────

    def _resolve_repair_scripts(self):
        """Find install_prerequisites.ps1 + configure_usbipd.ps1.

        Returns (install_prereq_path_or_None, configure_usbipd_path_or_None).
        Supports both production install layout and dev-tree layout.
        """
        from .constants import INSTALL_DIR
        prereq_candidates = [
            os.path.join(INSTALL_DIR, "scripts", "install_prerequisites.ps1"),
            os.path.join(INSTALL_DIR, "installer", "scripts", "install_prerequisites.ps1"),
        ]
        configure_candidates = [
            os.path.join(INSTALL_DIR, "scripts", "configure_usbipd.ps1"),
            os.path.join(INSTALL_DIR, "installer", "scripts", "configure_usbipd.ps1"),
        ]
        prereq = next((p for p in prereq_candidates if os.path.isfile(p)), None)
        configure = next((p for p in configure_candidates if os.path.isfile(p)), None)
        return prereq, configure

    def _prompt_repair_usbipd(self):
        """Offer one-click repair when usbipd is missing.

        Launches install_prerequisites.ps1 + configure_usbipd.ps1 elevated.
        After the elevated child exits we invalidate the resolver cache
        and re-run the prerequisite chain — if the repair worked, the
        student lands on the normal "Bereit" state without restarting
        the GUI.
        """
        prereq, configure = self._resolve_repair_scripts()
        if prereq is None or configure is None:
            messagebox.showerror(
                "Reparatur nicht möglich",
                "usbipd-win konnte nicht gefunden werden und die "
                "Reparaturskripte fehlen ebenfalls. Bitte den EduBotics-"
                "Installer erneut ausführen.",
            )
            self._set_status("usbipd fehlt — Installer erneut ausführen")
            return

        wants_run = messagebox.askyesno(
            "usbipd-win reparieren",
            "Der USB-Treiber 'usbipd-win' fehlt oder ist noch nicht im "
            "Suchpfad. Ohne ihn können Roboterarme und Kameras nicht erkannt "
            "werden.\n\n"
            "Reparatur jetzt starten? Dies erfordert einmalig Administrator-"
            "Rechte und dauert 1-3 Minuten.",
        )
        if not wants_run:
            self._set_status("usbipd fehlt — Reparatur übersprungen")
            return

        # Already-elevated accounts run the repair directly with no UAC prompt;
        # word the status accordingly so it isn't misleading.
        if _is_elevated():
            self._set_status("Reparatur läuft (Administrator-Rechte vorhanden)...")
            self._log("usbipd-Reparatur wird automatisch ausgeführt (Administrator-Rechte vorhanden)...")
        else:
            self._set_status("Reparatur läuft (UAC-Zustimmung erforderlich)...")
            self._log("usbipd-Reparatur wird mit Administrator-Rechten gestartet...")

        def _run_elevated():
            # We chain both scripts in a single elevated shell so the
            # student sees one UAC prompt, not two. The combined script
            # writes its transcript so the GUI can show what happened.
            log_file = os.path.join(_edubotics_diag_dir(), "edubotics_repair_usbipd.log")
            try:
                if os.path.isfile(log_file):
                    os.remove(log_file)
            except OSError:
                pass

            # This repair is the one production flow GUARANTEED to download the
            # usbipd MSI (it fires precisely when usbipd is missing), so replay
            # the pin install_prerequisites.ps1 persisted next to itself at
            # install time (.usbipd_pin: URL line + SHA-256 line) — without it
            # the elevated MSI install silently skips its integrity check. Also
            # pass -PreserveExistingRebootFlag: this repair can run in the same
            # PRE-reboot session as migrate's dd-uninstall .reboot_required, and
            # prereq's Summary would otherwise delete that flag without any
            # boot-time proof the reboot happened. Pin content is a GitHub URL
            # + hex digest — both `$`-free, so the manual fallback below stays
            # paste-safe.
            prereq_extra = " -PreserveExistingRebootFlag"
            pin_path = os.path.join(os.path.dirname(prereq), ".usbipd_pin")
            try:
                with open(pin_path, "r", encoding="ascii") as fh:
                    pin_lines = [ln.strip() for ln in fh.read().splitlines()
                                 if ln.strip()]
                if len(pin_lines) >= 2 and "$" not in (pin_lines[0] + pin_lines[1]):
                    prereq_extra += (
                        f" -UsbipdMsiUrl '{_ps_single_quote(pin_lines[0])}'"
                        f" -UsbipdMsiSha256 '{_ps_single_quote(pin_lines[1])}'"
                    )
            except (OSError, UnicodeDecodeError):
                pass

            # Every path lands inside a single-quoted PowerShell token, so any
            # embedded apostrophe (a username like O'Brien reaches these paths)
            # must be doubled or it terminates the token and reshapes the command.
            ps_cmd = (
                f'-NoProfile -ExecutionPolicy Bypass -Command '
                f'"Start-Transcript -Path \'{_ps_single_quote(log_file)}\' -Force | Out-Null; '
                f'& \'{_ps_single_quote(prereq)}\'{prereq_extra}; '
                f'$prereq_rc = $LASTEXITCODE; '
                f'& \'{_ps_single_quote(configure)}\'; '
                f'$cfg_rc = $LASTEXITCODE; '
                f'Stop-Transcript | Out-Null; '
                f'if ($prereq_rc -ne 0) {{ exit $prereq_rc }} '
                f'elseif ($cfg_rc -ne 0) {{ exit $cfg_rc }} '
                f'else {{ exit 0 }}"'
            )
            # Paste-safe variant for the manual-fallback dialog. ps_cmd cannot
            # be pasted into a PowerShell prompt: its double-quoted -Command
            # string contains $prereq_rc/$cfg_rc/$LASTEXITCODE, which the
            # OUTER interactive PowerShell interpolates (to empty/stale values)
            # before the child ever sees them — the child then receives ` = ;`
            # fragments and fails to parse, so nothing runs. The manual variant
            # drops the transcript/exit-code plumbing (the student sees the
            # output directly in their console) and contains no `$` at all, so
            # it survives both an elevated PowerShell and cmd.exe verbatim. The
            # powershell.exe -ExecutionPolicy Bypass wrapper stays: a bare
            # `& 'script.ps1'` would be blocked by the default policy.
            manual_cmd = (
                f'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '
                f'"& \'{_ps_single_quote(prereq)}\'{prereq_extra}; '
                f'& \'{_ps_single_quote(configure)}\'"'
            )
            self._elevation_prewarn()
            exit_code, cancelled, err = _run_privileged(
                exe="powershell.exe",
                args=ps_cmd,
            )
            if err:
                self._log(f"UAC-Fehler: {err}")

            # Surface the transcript tail.
            try:
                if os.path.isfile(log_file) and os.path.getsize(log_file) > 0:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
                    tail = lines[-25:]
                    if tail:
                        self._log("── Reparatur-Protokoll ──")
                        for line in tail:
                            self._log(f"  {line}")
            except OSError:
                pass

            # Invalidate the resolver cache and re-probe. The repair scripts
            # may have just installed usbipd into a path PATH still doesn't
            # know about — the resolver looks in Program Files + the
            # Uninstall registry, so we should find it now.
            from .usbipd_resolver import find_usbipd, reset_cache_for_tests
            reset_cache_for_tests()
            new_path = find_usbipd()
            if new_path:
                self._log(f"usbipd gefunden: {new_path}")
                self._log("Reparatur erfolgreich. Systemprüfung wird fortgesetzt...")
                self.root.after(0, lambda: threading.Thread(
                    target=self._run_prerequisite_checks, daemon=True).start())
            else:
                if cancelled:
                    self._log("Reparatur abgebrochen (UAC-Zustimmung verweigert).")
                    self._set_status("Reparatur abgebrochen — erneut versuchen")
                    # A Group-Policy auto-deny also surfaces as ERROR_CANCELLED and
                    # would loop forever; after the 2nd cancel offer the manual path.
                    n = getattr(self, "_cancel_count_usbipd", 0) + 1
                    setattr(self, "_cancel_count_usbipd", n)
                    if n >= 2:
                        self._log("Mehrfach abgebrochen — manueller Befehl wird angeboten.")
                        self._show_manual_elevation_fallback(
                            "usbipd-Reparatur manuell ausführen",
                            manual_cmd,
                        )
                elif exit_code is None:
                    # Launch failure: the elevated child never ran (distinct
                    # from a numeric non-zero "ran and failed"). Offer the
                    # manual admin command as a fallback.
                    self._log(
                        "Reparatur konnte nicht gestartet werden "
                        "(kein Prozess). Manueller Befehl wird angeboten."
                    )
                    self._set_status("Reparatur nicht gestartet — manueller Befehl")
                    self._show_manual_elevation_fallback(
                        "usbipd-Reparatur manuell ausführen",
                        manual_cmd,
                    )
                else:
                    self._log(
                        f"Reparatur fehlgeschlagen (exit {exit_code}). "
                        "Bitte den EduBotics-Installer erneut ausführen."
                    )
                    self._set_status("Reparatur fehlgeschlagen — siehe Protokoll")

        threading.Thread(target=_run_elevated, daemon=True).start()

    # ── Finalize Install (post-reboot continuation) ─────────────────

    # ── ShellExecuteEx helper is defined at module scope below. ──────

    def _elevation_prewarn(self):
        """Log a German warning when UAC is disabled and we're NOT already admin.

        With EnableLUA=0 the `runas` verb won't show a consent dialog and, from a
        standard token, ShellExecuteEx just fails — this note tells the student
        why up front. Returns True if a warning was emitted (best-effort only).
        """
        if _is_elevated():
            return False
        if not _uac_enabled():
            self._log(
                "[WARNUNG] Die Benutzerkontensteuerung (UAC) ist deaktiviert. "
                "Die Administrator-Abfrage erscheint möglicherweise nicht — "
                "bitte EduBotics als Administrator ausführen, falls die "
                "Reparatur fehlschlägt."
            )
            return True
        return False

    def _show_manual_elevation_fallback(self, title: str, command: str):
        """Offer the student a manual admin command when auto-elevation is a dead end.

        Shown in two cases: a launch failure (exit_code is None — the elevated
        child never started, so re-clicking rarely helps) and a repeated UAC
        cancel (>= 2, which also covers a Group-Policy auto-deny that surfaces
        as ERROR_CANCELLED and would otherwise loop forever). We give the exact
        command to paste into an elevated PowerShell, with a copy-to-clipboard
        button. The command MUST be paste-safe for an interactive PowerShell:
        no `$` inside double quotes (the outer shell would interpolate it
        before the child sees it). Runs on the Tk main thread.
        """
        def _build():
            win = tk.Toplevel(self.root)
            win.title(title)
            win.transient(self.root)
            win.resizable(False, False)
            _apply_window_icon(win)
            try:
                win.grab_set()
            except tk.TclError:
                pass

            frame = ttk.Frame(win, padding=16)
            frame.pack(fill=tk.BOTH, expand=True)
            ttk.Label(
                frame,
                text="Die automatische Administrator-Abfrage konnte nicht "
                     "gestartet werden.",
                font=(theme_mod.FONT_FAMILY, 10, "bold"),
                wraplength=520,
            ).pack(anchor=tk.W)
            ttk.Label(
                frame,
                text="Bitte öffne 'Windows PowerShell' oder das Terminal als "
                     "Administrator (Rechtsklick → 'Als Administrator "
                     "ausführen') und führe folgenden Befehl aus:",
                font=(theme_mod.FONT_FAMILY, 9),
                wraplength=520,
                foreground="gray",
            ).pack(anchor=tk.W, pady=(4, 8))

            cmd_box = scrolledtext.ScrolledText(
                frame, height=5, width=68, wrap=tk.WORD, font=theme_mod.LOG_FONT
            )
            cmd_box.insert(tk.END, command)
            cmd_box.config(state=tk.DISABLED)
            cmd_box.pack(fill=tk.X, pady=(0, 8))

            copied_var = tk.StringVar()

            def _copy():
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(command)
                    copied_var.set("Kopiert!")
                except tk.TclError:
                    copied_var.set("Kopieren fehlgeschlagen")

            row = ttk.Frame(frame)
            row.pack(fill=tk.X)
            ttk.Button(row, text="Befehl kopieren", command=_copy).pack(side=tk.LEFT)
            ttk.Label(row, textvariable=copied_var, foreground="gray").pack(
                side=tk.LEFT, padx=8)
            ttk.Button(row, text="Schließen", command=win.destroy).pack(side=tk.RIGHT)

        self.root.after(0, _build)

    def _reboot_required_pending(self) -> bool:
        """True when install_prerequisites.ps1 left a `.reboot_required` flag.

        The installer writes this (under {app}\\scripts) when a Windows feature
        enable returned DISM rc=3010, and the .iss ShouldImportDistro /
        ShouldPullImages gates then skip the distro import + image pull until a
        reboot. On a FRESH install the distro is missing and the prereq check
        routes to finalize anyway; on an UPGRADE the old distro survives, so
        without reading this flag the GUI would silently pull images itself and
        never complete finalize / clear the flag / prune (the v2.13.0 pilot
        incident). Read-only (the flag lives under Program Files; only the
        elevated finalize_install.ps1 can clear it)."""
        from .constants import INSTALL_DIR
        for p in (
            os.path.join(INSTALL_DIR, "scripts", ".reboot_required"),              # production
            os.path.join(INSTALL_DIR, "installer", "scripts", ".reboot_required"),  # dev tree
        ):
            if os.path.isfile(p):
                return True
        return False

    def _rootfs_rebuild_required(self) -> bool:
        """True on a POSITIVE rootfs-version mismatch (both stamps known + differ).

        The distro bakes /etc/edubotics-rootfs-version; the shipped expectation
        is wsl_rootfs/ROOTFS_VERSION under INSTALL_DIR. Returns True ONLY when
        both are non-empty and unequal — the installer's re-import was skipped or
        declined and the NEW images would otherwise run on the OLD rootfs. Fails
        OPEN (returns False) whenever EITHER stamp is unreadable/absent (dev
        build without wsl_rootfs, a pre-2.6.1 distro predating the marker, or a
        transient wsl.exe error) so a read hiccup never blocks startup.

        THE CONTRACT, both halves: positive mismatch only. Any unreadable stamp
        — on EITHER side, in EITHER component — must PROCEED, never block. The
        .iss RootfsReimportNeeded() converges on this same fail-open rule; it
        previously treated an unreadable stamp as "needs re-import" (fail
        CLOSED), so the two halves disagreed on exactly the state a broken rig
        is in and one of them would offer a destructive rebuild the other says
        is unnecessary. Do not "harden" one side to fail closed without moving
        the other in the same change."""
        from .constants import INSTALL_DIR
        version_file = os.path.join(INSTALL_DIR, "wsl_rootfs", "ROOTFS_VERSION")
        try:
            with open(version_file, "r", encoding="utf-8") as fh:
                shipped = fh.read().strip()
        except (OSError, UnicodeDecodeError):
            shipped = ""
        if not shipped:
            return False  # can't determine what we ship — fail open
        distro = docker_manager.read_rootfs_version()
        if not distro:
            return False  # can't read the distro stamp — fail open
        return shipped != distro

    def _resolve_finalize_script(self):
        """Find finalize_install.ps1. Supports both production and dev layouts."""
        from .constants import INSTALL_DIR
        candidates = [
            os.path.join(INSTALL_DIR, "scripts", "finalize_install.ps1"),            # production install
            os.path.join(INSTALL_DIR, "installer", "scripts", "finalize_install.ps1"),  # dev tree
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None

    def _resolve_preflight_script(self):
        """Find preflight_system.ps1. Supports both production and dev layouts."""
        from .constants import INSTALL_DIR
        candidates = [
            os.path.join(INSTALL_DIR, "scripts", "preflight_system.ps1"),               # production install
            os.path.join(INSTALL_DIR, "installer", "scripts", "preflight_system.ps1"),  # dev tree
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None

    def _run_preflight_diagnostics(self):
        """Run preflight_system.ps1 once at startup and echo its German output.

        Purely informational: the script's findings land in the Protokoll so a
        student (or support) can see the system's diagnosis. It NEVER gates or
        blocks any flow — every failure mode (non-Windows, missing script,
        timeout, subprocess error) is swallowed. Runs in a background daemon
        thread so it can never block the UI or startup checks.
        """
        try:
            if sys.platform != "win32":
                return
            script = self._resolve_preflight_script()
            if script is None:
                return
            # CREATE_NO_WINDOW so the elevated-free preflight never flashes a
            # console window at the student (win32-only path — guarded above).
            creationflags = 0x08000000
            # Encoding: Windows PowerShell 5.1 writes REDIRECTED stdout in the
            # console's OEM codepage (cp850 on German Windows), while Python's
            # text=True decodes with the ANSI codepage (cp1252) — umlauts would
            # garble, and 'ü' (0x81 in cp850, undefined in cp1252) would raise
            # a UnicodeDecodeError that silently swallows the WHOLE output.
            # Force the child to emit UTF-8 via [Console]::OutputEncoding (a
            # -Command wrapper; try/catch-guarded so a console-less edge case
            # still runs the script) and decode as UTF-8 with errors="replace"
            # so one bad byte can never blank the diagnosis.
            ps_wrapper = (
                "try { [Console]::OutputEncoding = "
                "[System.Text.Encoding]::UTF8 } catch { }; "
                f"& '{_ps_single_quote(script)}'"
            )
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-Command", ps_wrapper,
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                # Above the script's own worst case (~35 s docker probe with
                # -NoFallback + the other checks): a timeout kill here discards
                # the ENTIRE captured diagnosis, i.e. exactly the broken-rig
                # evidence this feature exists to collect.
                timeout=150,
                creationflags=creationflags,
            )
            output = (proc.stdout or "")
            lines = [ln for ln in output.splitlines() if ln.strip()]
            if lines:
                # ONE `_log` call, not one per line. `_check_prerequisites`
                # starts this on a daemon thread and then runs the prerequisite
                # chain on ANOTHER, and `_log` marshals through
                # `root.after(0, …)` — so the lines were serialised but the
                # BLOCK was not atomic, and a multi-line diagnosis could land
                # split across the prerequisite chain's own output.
                self._log("\n".join(
                    ["[INFO] Systemdiagnose (Preflight):"]
                    + [f"  {line}" for line in lines]))
        except Exception:
            # Diagnostics are best-effort — never let them raise into startup.
            pass

    def _prompt_finalize_install(self, reason=None):
        """Finish setup AUTOMATICALLY after a post-reboot continuation.

        When WSL2 was installed fresh, the installer defers the rootfs import
        until after reboot. On first GUI launch the distro is missing, so we run
        finalize_install.ps1 elevated WITHOUT asking — the student never clicks a
        "finish setup" button or opens a terminal. On an admin-capable account the
        only OS-level interaction is the standard UAC consent; the setup itself
        (import + image pull) then runs unattended and is idempotent/resumable.

        ``reason="rootfs_mismatch"`` is the ONE exception to the no-dialog rule:
        the runtime rootfs↔image handshake found that the distro EXISTS but its
        rootfs is older than this release ships, so completing the upgrade
        re-imports the distro (DESTROYING its Docker volumes). We take the same
        German data-loss consent the .iss shows and thread
        ``-AllowDestructiveReimport`` into finalize so import proceeds (finalize
        forwards it to import_edubotics_wsl.ps1); a decline leaves the stack
        down rather than pulling new images onto the stale rootfs.

        Re-entrancy is guarded so a repeated prerequisite scan can't stack a
        second elevated launch on top of one already in flight.
        """
        if getattr(self, "_finalize_in_progress", False):
            return

        script = self._resolve_finalize_script()
        if script is None:
            messagebox.showerror(
                "EduBotics-Umgebung fehlt",
                "Die EduBotics-Umgebung ist nicht eingerichtet und das "
                "Einrichtungsskript wurde nicht gefunden. Bitte den Installer "
                "erneut ausführen.",
            )
            self._set_status("EduBotics-Umgebung fehlt")
            return

        self._finalize_in_progress = True
        self._log(f"Setup-Skript: {script}")
        # A rootfs rebuild re-imports the distro and DESTROYS its Docker
        # volumes, so it is the ONE path that still asks: explicit data-loss
        # consent gates the destructive-import flag. Every other path runs
        # automatically (see the docstring). The re-entrancy flag is already
        # set — askyesno pumps the Tk event loop, so a concurrent prerequisite
        # rescan during the modal must not stack a second finalize — which is
        # why the decline path clears it explicitly.
        allow_destructive = False
        if reason == "rootfs_mismatch":
            wants_run = messagebox.askyesno(
                "EduBotics-Umgebung neu aufbauen",
                "Die EduBotics-Umgebung muss neu aufgebaut werden (System-"
                "Update).\n\n"
                "Dabei werden alle lokal gespeicherten Daten gelöscht:\n"
                "  - aufgenommene Datensätze (falls nicht zu Hugging Face "
                "hochgeladen)\n"
                "  - heruntergeladene Modelle\n"
                "  - die Roboter-Studio-Kalibrierung\n\n"
                "Tipp: Datensätze vorher in der Web-Oberfläche zu Hugging Face "
                "hochladen.\n\n"
                "Jetzt neu aufbauen? Dies erfordert einmalig Administrator-"
                "Rechte.",
                default=messagebox.NO,
            )
            if not wants_run:
                self._finalize_in_progress = False
                self._set_status("EduBotics-Umgebung nicht eingerichtet")
                self._log("Einrichtung vom Benutzer verschoben.")
                return
            allow_destructive = True

        # When we already hold an admin token there is NO UAC prompt (UAC off or
        # "Never notify"), so word the status/log for the actual path taken.
        if _is_elevated():
            self._set_status("Einrichtung läuft automatisch (Administrator-Rechte vorhanden)...")
            self._log("Einrichtung wird automatisch ausgeführt (Administrator-Rechte vorhanden)...")
        else:
            self._set_status("Einrichtung läuft automatisch (UAC-Zustimmung erforderlich)...")
            self._log("Einrichtung wird automatisch mit Administrator-Rechten gestartet...")

        def _run_elevated():
            # Clear the re-entrancy guard only after ALL post-processing
            # (transcript echo, re-check, fallback dialogs) — clearing it right
            # after the child exited let a concurrent rescan stack a second
            # finalize while this one was still reporting. The finally also
            # guarantees an unexpected exception can't strand the flag, which
            # would permanently disable setup retries until a GUI restart.
            try:
                _finalize_worker()
            finally:
                self._finalize_in_progress = False

        def _finalize_worker():
            # The elevated finalize_install.ps1 writes:
            #   - a marker file on startup (proves it launched)
            #   - a transcript file with full stdout/stderr (Start-Transcript)
            # Using ShellExecuteEx directly (not nested Start-Process) so we
            # get a reliable process handle + deterministic wait.
            diag = _edubotics_diag_dir()
            log_file = os.path.join(diag, "edubotics_finalize.log")
            marker_file = os.path.join(diag, "edubotics_finalize.marker")
            for f in (log_file, marker_file):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except OSError:
                    pass

            ps_args = (
                f'-NoProfile -ExecutionPolicy Bypass -File "{script}" '
                f'-LogPath "{log_file}" -MarkerPath "{marker_file}"'
            )
            self._elevation_prewarn()
            # Only the rootfs-mismatch path consents to the destructive
            # re-import; finalize forwards the switch to import_edubotics_wsl.ps1,
            # whose guard otherwise REFUSES to wipe an existing distro's volumes.
            if allow_destructive:
                ps_args += " -AllowDestructiveReimport"
            exit_code, cancelled, err = _run_privileged(
                exe="powershell.exe",
                args=ps_args,
            )
            if err:
                self._log(f"UAC-Fehler: {err}")

            # Did the elevated script start at all?
            marker_seen = os.path.isfile(marker_file)
            if not marker_seen:
                self._log(
                    "Das Setup-Skript wurde nicht gestartet "
                    "(Marker-Datei fehlt)."
                )

            def _marker_user():
                """The Windows account finalize_install.ps1 ran AS, or "".

                finalize writes THREE marker shapes and two of them carry the
                field: the startup stamp ``started <iso> pid=<n> user=<name>``
                and the success stamp ``SUCCESS <iso> user=<name>
                distro=<name>``. The third, ``FAILED <iso>\\n<problem>\\n<next
                step>``, carries no ``user=`` and correctly yields "" — it is
                also unreachable here, because this branch only runs on
                FINALIZE_EXIT_DONE. %USERNAME% may
                contain spaces, so the value runs to the end of the line —
                except in the SUCCESS shape, where " distro=" follows it; cut
                there. Never raises and never guesses: a missing, truncated or
                garbage marker yields "", which routes to the generic message.

                ENCODING IS LOAD-BEARING, and this cost a wrong accusation.
                finalize_install.ps1 writes the marker ``-Encoding UTF8`` (PS
                5.1 emits a BOM for that), so the read is ``utf-8-sig``. Before
                that pairing the .ps1 used Set-Content's default — the system
                ANSI codepage — and a German „Müller" came back as
                „M�ller", which can never casefold-match %USERNAME%: the
                per-account branch below then fired on the SAME account and
                replaced correct reboot advice with advice that cannot help.
                A U+FFFD ANYWHERE in the parsed name is therefore treated as
                unparseable rather than as a different account — accusing the
                student of the wrong Windows login is worse than the generic
                message, so a mojibake marker must fall back, not route.
                """
                try:
                    with open(marker_file, "r", encoding="utf-8-sig",
                              errors="replace") as fh:
                        body = fh.read()
                except OSError:
                    return ""
                for line in body.splitlines():
                    _, sep, rest = line.partition("user=")
                    if not sep:
                        continue
                    cut = rest.find(" distro=")
                    name = (rest[:cut] if cut != -1 else rest).strip()
                    return "" if "�" in name else name
                return ""

            # Surface the elevated script's transcript (last ~30 lines).
            try:
                if os.path.isfile(log_file) and os.path.getsize(log_file) > 0:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
                    tail = lines[-30:]
                    if tail:
                        self._log("── Setup-Protokoll ──")
                        for line in tail:
                            self._log(f"  {line}")
            except OSError:
                pass

            # Route on finalize's EXIT CODE — it is the authority (see the
            # FINALIZE_EXIT_* block at module scope). Do NOT route on
            # .reboot_required: finalize keeps that flag set on EVERY unfinished
            # outcome (failed pull, failed import, refused consent), so reading
            # it first collapsed all of them into "Neustart erforderlich" and
            # looped the student through reboots that could never help.
            #
            # exit 0 + is_distro_registered() is still the success gate: on an
            # UPGRADE the old distro survives, so the registration check alone
            # would call a mid-pull failure a success.
            if cancelled:
                self._log("Einrichtung abgebrochen (UAC-Zustimmung verweigert).")
                self._set_status("Einrichtung abgebrochen — erneut versuchen")
                # Group-Policy auto-deny loops on ERROR_CANCELLED; escalate.
                n = getattr(self, "_cancel_count_finalize", 0) + 1
                setattr(self, "_cancel_count_finalize", n)
                if n >= 2:
                    self._log("Mehrfach abgebrochen — manueller Befehl wird angeboten.")
                    self._show_manual_elevation_fallback(
                        "Einrichtung manuell abschließen",
                        f"powershell.exe {ps_args}",
                    )
            elif exit_code == FINALIZE_EXIT_REBOOT:
                self._log(
                    "Neustart erforderlich: Bitte den PC neu starten und "
                    "EduBotics danach erneut öffnen, um die Einrichtung "
                    "abzuschließen."
                )
                self._set_status("Neustart erforderlich — danach EduBotics erneut öffnen")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Neustart erforderlich",
                    "Windows muss neu gestartet werden, um die EduBotics-"
                    "Installation abzuschließen.\n\n"
                    "Bitte starten Sie den PC neu und öffnen Sie EduBotics "
                    "danach erneut.",
                ))
            elif exit_code == FINALIZE_EXIT_CONSENT:
                # The rootfs must be rebuilt and nobody consented. Rebooting can
                # never fix this; the installer is the only path that asks.
                self._log(
                    "Die EduBotics-Umgebung muss neu aufgebaut werden. Bitte "
                    "den Installer erneut ausführen — er fragt vorher nach "
                    "Ihrer Zustimmung."
                )
                self._set_status("Neuaufbau erforderlich — bitte den Installer erneut ausführen")
                self.root.after(0, lambda: messagebox.showwarning(
                    "Neuaufbau erforderlich",
                    "Die EduBotics-Umgebung muss neu aufgebaut werden "
                    "(System-Update).\n\n"
                    "Bitte führen Sie den EduBotics-Installer erneut aus. Er "
                    "fragt vor dem Neuaufbau nach Ihrer Zustimmung.\n\n"
                    "Tipp: Laden Sie Ihre Datensätze vorher in der "
                    "Web-Oberfläche zu Hugging Face hoch.",
                ))
            elif exit_code == FINALIZE_EXIT_DONE and docker_manager.is_distro_registered():
                # Latch it: finalize said done, so a .reboot_required it could
                # not delete (it warns and still exits 0) must not send
                # _run_prerequisite_checks straight back here — that would loop
                # the student through UAC prompts for the rest of the session.
                self._finalize_completed = True
                self._log("Einrichtung abgeschlossen. Systemprüfung wird fortgesetzt...")
                self.root.after(0, lambda: threading.Thread(
                    target=self._run_prerequisite_checks, daemon=True).start())
            elif exit_code == FINALIZE_EXIT_DONE:
                # exit 0 but is_distro_registered() is False. Without this branch
                # the chain fell through to the generic else and printed
                # "Einrichtung fehlgeschlagen (exit 0)" — a self-contradiction
                # that tells the student nothing and reads like a GUI bug.
                #
                # There are TWO causes and they need different remedies, so
                # discriminate before reporting. FINALIZE_EXIT_DONE implies
                # finalize ran Test-DistroRegistered and it PASSED — every other
                # outcome leaves through Fail-WithNextAction — so "the elevated
                # side saw the distro, we cannot" plus "it ran as a DIFFERENT
                # Windows account" is an inference from the contract, not a
                # guess. WSL2 registers distros PER WINDOWS USER
                # (HKCU\...\Lxss) while the installer is
                # PrivilegesRequired=admin: on a managed school PC where a
                # different admin elevates, `wsl --import` lands the
                # registration in the ADMIN's hive and the student's
                # un-elevated GUI cannot see it. Rebooting can never fix that,
                # which is exactly what the generic message below advises.
                #
                # Same account, or no readable marker -> fall through to that
                # generic message BYTE-FOR-BYTE: its stated causes (a wsl.exe
                # that reported success while the import silently didn't land —
                # a stale WSL-service state, or an unregister racing the import)
                # remain correct for the same-account case.
                elevated_user = _marker_user()
                current_user = (os.environ.get("USERNAME") or "").strip()
                if (elevated_user and current_user
                        and elevated_user.casefold() != current_user.casefold()):
                    # Windows account names are case-insensitive, so compare
                    # casefolded — a case difference is the same account.
                    self._log(
                        f"Die Einrichtung wurde mit dem Windows-Konto "
                        f"„{elevated_user}“ abgeschlossen, angemeldet sind Sie "
                        f"aber als „{current_user}“. Eine WSL-Umgebung gehört "
                        f"immer genau dem Windows-Konto, unter dem sie "
                        f"eingerichtet wurde — deshalb ist sie hier nicht "
                        f"sichtbar. Bitte EduBotics mit dem Konto "
                        f"„{elevated_user}“ starten, oder den EduBotics-"
                        f"Installer erneut ausführen, während Sie mit Ihrem "
                        f"eigenen Konto angemeldet sind."
                    )
                    self._set_status(
                        "Umgebung gehört einem anderen Windows-Konto — mit "
                        "diesem Konto anmelden")
                else:
                    self._log(
                        "Die Einrichtung meldet Erfolg, aber die EduBotics-Umgebung "
                        "fehlt weiterhin. Bitte den PC neu starten und EduBotics "
                        "erneut öffnen — hilft das nicht, bitte den EduBotics-"
                        "Installer erneut ausführen."
                    )
                    self._set_status(
                        "Umgebung fehlt trotz erfolgreicher Einrichtung — PC neu starten")
            elif exit_code is None:
                # Launch failure (never ran) — distinct from a numeric
                # non-zero exit. The missing marker corroborates it. Offer
                # the manual admin command as a fallback.
                self._log(
                    "Einrichtung konnte nicht gestartet werden "
                    "(kein Prozess). Manueller Befehl wird angeboten."
                )
                self._set_status("Einrichtung nicht gestartet — manueller Befehl")
                self._show_manual_elevation_fallback(
                    "Einrichtung manuell abschließen",
                    f"powershell.exe {ps_args}",
                )
            else:
                self._log(
                    f"Einrichtung fehlgeschlagen (exit {exit_code}). "
                    "Siehe Protokoll oben."
                )
                self._set_status("Einrichtung fehlgeschlagen")

        threading.Thread(target=_run_elevated, daemon=True).start()

    # ── Update Dialog ───────────────────────────────────────────────

    def _show_update_dialog(self, update_info: dict):
        """Show a blocking modal that forces the student to update."""
        self._update_fail_count = 0

        dialog = tk.Toplevel(self.root)
        dialog.title("EduBotics Update")
        dialog.geometry("480x260")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        _apply_window_icon(dialog)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)  # Non-closable

        # Center over main window
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 480) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 260) // 2
        dialog.geometry(f"+{x}+{y}")

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="Ein Update ist verfügbar!",
            font=(theme_mod.FONT_FAMILY, 14, "bold"),
        ).pack(pady=(0, 5))

        ttk.Label(
            frame,
            text=f"Neue Version: {update_info['version']}  (aktuell: {APP_VERSION})",
            font=(theme_mod.FONT_FAMILY, 10),
        ).pack(pady=(0, 5))

        ttk.Label(
            frame,
            text="Bitte aktualisiere EduBotics, bevor du fortfährst.",
            font=(theme_mod.FONT_FAMILY, 9),
            foreground="gray",
        ).pack(pady=(0, 10))

        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(frame, variable=progress_var, maximum=100, length=400)
        progress_bar.pack(pady=(0, 5))

        status_var = tk.StringVar(value="")
        status_label = ttk.Label(frame, textvariable=status_var, font=(theme_mod.FONT_FAMILY, 8))
        status_label.pack(pady=(0, 10))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack()

        btn_update = ttk.Button(btn_frame, text="Jetzt aktualisieren")
        btn_update.pack(side=tk.LEFT, padx=5)

        btn_skip = ttk.Button(btn_frame, text="Ohne Update fortfahren", state=tk.DISABLED)
        btn_skip.pack(side=tk.LEFT, padx=5)

        def _do_download():
            def _progress(downloaded, total):
                if total > 0:
                    pct = (downloaded / total) * 100
                    mb_down = downloaded / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    self.root.after(0, lambda: progress_var.set(pct))
                    self.root.after(0, lambda: status_var.set(
                        f"{mb_down:.1f} / {mb_total:.1f} MB"
                    ))

            # A 404, a truncated transfer, a checksum mismatch, a full disk and
            # a timeout all returned a bare None, so this modal — which the
            # student CANNOT CLOSE until an attempt has failed — said „bitte
            # Internetverbindung prüfen" for every one of them, including the
            # two cases where the connection is fine and the release is broken.
            reasons = []
            path = update_checker.download_installer(
                update_info["download_url"],
                progress_callback=_progress,
                expected_sha256=update_info.get("sha256"),
                reason_callback=reasons.append,
            )

            if path:
                self.root.after(0, lambda: status_var.set("Download abgeschlossen. Installer wird gestartet..."))
                self.root.after(500, lambda: self._launch_installer_and_exit(path))
            else:
                self._update_fail_count += 1
                reason = reasons[0] if reasons else (
                    "Download fehlgeschlagen. Bitte Internetverbindung prüfen.")
                self._log(f"[FEHLER] Update-Download fehlgeschlagen: {reason}")
                self.root.after(0, lambda: progress_var.set(0))
                self.root.after(0, lambda r=reason: status_var.set(r))
                self.root.after(0, lambda: btn_update.config(
                    state=tk.NORMAL, text="Erneut versuchen"
                ))
                # Enable "skip" after the FIRST failed download (was 3) so a
                # student on a flaky link / behind a blocked asset URL is never
                # hard-locked behind the non-closable modal. The asset HEAD
                # pre-check in update_checker already prevents a 404 asset from
                # ever opening this modal; this covers a mid-download failure.
                if self._update_fail_count >= 1:
                    self.root.after(0, lambda: btn_skip.config(state=tk.NORMAL))

        def _on_update_click():
            btn_update.config(state=tk.DISABLED)
            status_var.set("Download läuft...")
            progress_var.set(0)
            threading.Thread(target=_do_download, daemon=True).start()

        def _on_skip_click():
            dialog.destroy()
            self._log("[WARNUNG] Update übersprungen. Einige Funktionen funktionieren möglicherweise nicht korrekt.")
            self._set_status("System wird geprüft...")
            threading.Thread(target=self._run_prerequisite_checks, daemon=True).start()

        btn_update.config(command=_on_update_click)
        btn_skip.config(command=_on_skip_click)

    def _launch_installer_and_exit(self, installer_path: str):
        """Launch the downloaded installer and exit the GUI deterministically.

        Tear down everything holding a handle on {app}\\gui\\EduBotics.exe or a
        live wsl.exe BEFORE exiting, then os._exit(0). The interactive installer
        runs `[InstallDelete] {app}\\gui`, which deletes the *running*
        EduBotics.exe — a lingering GUI process, keepalive wsl.exe, or webview
        child would cause a sharing violation and a half-updated gui/. The old
        `sys.exit(0)` only raised SystemExit, which is swallowed inside a Tk
        `after`-callback on the main thread (this runs via root.after); os._exit
        is an unconditional, deterministic process exit.
        """
        self._log("Installer wird gestartet...")
        try:
            os.startfile(installer_path)
        except Exception as e:
            self._log(f"[FEHLER] Installer konnte nicht gestartet werden: {e}")
            return
        # Release every handle on gui/ + the distro before the installer deletes
        # us. Each teardown is best-effort — a raise here must not stop the exit.
        for teardown in (
            self._stop_camera_previews,
            self._stop_camera_bridge,
            self._stop_phone_server,
            self._stop_rs_control_server,
            webview_window.destroy_all,
            docker_manager.stop_keepalive,
        ):
            try:
                teardown()
            except Exception:
                pass
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)

    # ── Arm Scanning ─────────────────────────────────────────────────

    def _confirm_arm_scan_closes_window(self) -> bool:
        """Ask before „Arme scannen" ends a session that is still running.

        Returns True when the scan may proceed — including, silently, in the
        case where there is nothing to warn about.

        ONLY ASKS WHEN THERE IS SOMETHING TO LOSE. `_scan_arms._do_scan`
        closes the student's EduBotics window unconditionally, and that stays:
        a stale window outlives a stopped environment, and leaving it alive
        hands the next student the previous one's window with their live,
        self-renewing Supabase session. With no window up — the handover case,
        and the common one — that close is invisible, so a dialog there would
        be pure noise in front of every fresh student's first scan. With a
        window up the same click is a silent logout plus lost unsaved Blockly
        work in the middle of a lesson, which is what this asks about.

        MAIN THREAD ONLY, and that is why it is called from `_scan_arms` and
        not from `_do_scan`: a Tk dialog must run on the thread that owns the
        mainloop. `_scan_arms` is the button's `command`, so Tk calls it there
        — it already reads tk StringVars for precisely that reason — while
        `_do_scan` runs on the worker `_scan_arms` spawns.

        RE-ENTRANCY, and it is not hypothetical. `askyesno` pumps the Tk event
        loop (the same hazard `_prompt_finalize_install` records), so a
        `root.after` callback scheduled elsewhere fires while this dialog is up —
        `_prompt_bind_arms` schedules `root.after(200, self._scan_arms)` after
        an elevated bind, and „Erneut scannen" is wired to the same method. At
        that moment `self._scanning` is still False and the button is still
        enabled, so nothing else would stop a SECOND dialog stacking on this
        one and, on two confirms, two workers racing each other onto the same
        /dev/serial ports. `_scan_confirm_open` is that guard, read by
        `_scan_arms` next to `_scanning`. The `finally` clears it so a DECLINE
        leaves every flag exactly as it found them — nothing to strand the
        button in a disabled or permanently-guarded state.

        THE ANSWER IS A SNAPSHOT and deliberately not re-checked: if the
        student closes the window themselves while the dialog is up, confirming
        runs a scan whose `destroy_all` is a harmless no-op, and declining does
        nothing at all. Both outcomes are already correct, so there is nothing
        to synchronise.
        """
        if not webview_window.has_live_window():
            return True

        self._scan_confirm_open = True
        try:
            return bool(messagebox.askyesno(
                "Arme scannen",
                "Beim Scannen wird das EduBotics-Fenster geschlossen und du "
                "wirst abgemeldet. Nicht gespeicherte Änderungen gehen "
                "verloren.\n\n"
                "Trotzdem scannen?",
                default=messagebox.NO,
            ))
        finally:
            self._scan_confirm_open = False

    def _scan_arms(self):
        """Nach Roboterarmen scannen — läuft im Hintergrund."""
        if self._scanning or self._scan_confirm_open:
            return
        # BEFORE any state changes: a declined scan must leave the GUI exactly
        # as it found it — button live, `_scanning` false, no status, no log,
        # no teardown. Everything below this line is the scan the student
        # confirmed (or never needed to confirm).
        if not self._confirm_arm_scan_closes_window():
            return
        self._scanning = True
        self.btn_scan_leader.config(state=tk.DISABLED)
        # BOTH scan buttons: only one is packed at a time, but the hidden twin
        # keeps its own state, and a re-pack (a robot-type switch mid-scan)
        # would put a live button back on screen over a running worker.
        self.btn_scan_arm.config(state=tk.DISABLED)
        # Capture the selected profile on the MAIN thread (tk StringVar reads
        # are not thread-safe) so the worker's success branch is type-aware.
        profile_row = ROBOT_PROFILES.get(self._selected_robot_profile(), {})
        scan_requires_leader = profile_row.get("scan_requires_leader", True)
        arm_family = profile_row.get("arm_family", "omx")

        def _do_scan():
            self._set_status("Roboterarme werden gesucht...")
            self.root.after(0, lambda: self.progress.start(10))
            self._log("USB-Geräte werden nach Roboterarmen durchsucht...")

            # Close the student's browser window BEFORE anything else in this
            # teardown. „Arme scannen" ends the previous student's session just
            # as surely as „Umgebung stoppen" does, and it left the WebView2
            # child alive: the next „Umgebung starten" then hit
            # `open_student_window`'s live-child short-circuit, which DISCARDS
            # the freshly minted `?fresh=<nonce>` URL and returns True — so no
            # document ever loaded, `bootScrub` never ran, and the next student
            # got the previous one's window with their live, self-refreshing
            # Supabase session and their Blockly program.
            #
            # DELIBERATELY NOT nested in the `if ensure_environment_stopped(…)`
            # below: a stale window outlives a stopped environment, so the case
            # that needs this most — a student who clicked „Umgebung stoppen"
            # and walked away with the window still up — is exactly the one
            # where that condition is False.
            #
            # FIRST, so the graceful close still has a live rosbridge for
            # JogPanel's unmount re-torque and the Jetson release beacon, the
            # same ordering `_stop_environment` keeps. Best-effort: a failure
            # here must never stop the scan the student asked for.
            #
            # KNOWN LIMIT: `destroy_all` is PROCESS-LOCAL (`webview_window.
            # _process` is set only by THIS process's `open_student_window`),
            # so it cannot reach a child orphaned by a crashed or
            # Task-Manager-killed GUI. It covers the same-process path, which
            # is the one this button is on.
            try:
                webview_window.destroy_all()
            except Exception:
                pass

            # The arm identification opens the SAME /dev/serial ports the
            # running stack uses. If open_manipulator is up it holds those
            # ports and drives the Dynamixel bus at 100 Hz — identify_arm.py's
            # pings then collide with the live controller traffic and BOTH
            # arms fail to identify (the original "Arme konnten nicht
            # zugeordnet werden" while teleop was visibly running). Free the
            # bus first: tear down any running environment before scanning.
            if docker_manager.ensure_environment_stopped(log=self._log):
                self._log("Laufende Umgebung wurde für den Scan gestoppt.")
                self._stop_camera_bridge()
                self._stop_rs_control_server()
                self.running = False
                self.root.after(0, lambda: self.btn_stop.config(state=tk.DISABLED))
                self.root.after(0, lambda: self.btn_open_browser.config(state=tk.DISABLED))
                self.root.after(0, self._update_start_button)

            # Clear any repair button from a previous failed scan.
            self.root.after(0, self._clear_arm_repair)

            leader, follower = device_manager.scan_and_identify_arms(
                IMAGE_OPEN_MANIPULATOR, arm_family=arm_family)

            if leader:
                self.hardware.leader = leader
                self.root.after(0, lambda: self.leader_status_var.set(
                    f"Gefunden: {leader.description} ({leader.serial_path})"
                ))
                self._log(f"Leader gefunden: {leader.serial_path}")
            elif scan_requires_leader:
                self.root.after(0, lambda: self.leader_status_var.set("Nicht gefunden"))
                self._log("Leader-Arm nicht gefunden.")
            else:
                # A follower-only robot type has no leader, so „not found" is
                # the correct outcome, not a fault — reporting it as one made a
                # fully successful scan read „Nicht gefunden" in Schritt A while
                # the status bar said „Follower-Arm gefunden!". No log line: the
                # follower's own line already reports the success.
                #
                # INLINE STRING ON PURPOSE: this method is source-extracted by
                # tests/test_gui_robot_type.py with a hand-built globals dict,
                # so a new module-level constant would raise NameError there.
                self.root.after(0, lambda: self.leader_status_var.set(
                    "Für diesen Robotertyp nicht nötig"))

            if follower:
                self.hardware.follower = follower
                self.root.after(0, lambda: self.follower_status_var.set(
                    f"Gefunden: {follower.description} ({follower.serial_path})"
                ))
                self._log(f"Follower gefunden: {follower.serial_path}")
            else:
                self.root.after(0, lambda: self.follower_status_var.set("Nicht gefunden"))
                self._log("Follower-Arm nicht gefunden.")

            # The flag/spinner/button reset lives in `_do_scan_guarded`'s
            # `finally` below — NOT here. `_do_scan` has early `return`s and
            # un-guarded calls, so a raise anywhere in it used to leave
            # `_scanning` True and the scan button disabled for the rest of the
            # session, with the spinner running.
            self._update_start_button()

            # A follower-only robot type (Roboter Studio kit) has no leader, so a
            # leader-less scan is SUCCESS — it must NOT route into the failure /
            # diagnose flow (whose terminal message names leader servo IDs).
            if follower and (leader or not scan_requires_leader):
                if scan_requires_leader:
                    self._set_status("Beide Arme gefunden! Kamera auswählen und auf Start klicken.")
                else:
                    # SINGULAR, matching the single-step scan card this profile
                    # renders („Schritt A: Roboterarm" / „Arm scannen"). The
                    # leader/follower distinction does not exist on a
                    # follower-only kit and is meaningless on edu6_studio.
                    self._set_status("Roboterarm gefunden! Kamera auswählen und auf Start klicken.")
                return

            # Cross-probe notice (edu6 §5.4): the most likely setup mistake —
            # the OTHER arm family is plugged in — becomes one clear sentence
            # instead of a wall of diagnosis.
            notice = getattr(device_manager, "LAST_SCAN_NOTICE", "")
            if notice:
                for line in notice.splitlines():
                    self._log(line)
                self._set_status(notice.splitlines()[0])
                return

            # Nothing (or the required arm) not found — run the host→WSL→docker
            # chain diagnosis so the student sees *why*, not just "Nicht gefunden".
            self._log("Diagnose wird ausgeführt — bitte einen Moment...")
            try:
                diag = device_manager.diagnose_usb_environment(
                    image=IMAGE_OPEN_MANIPULATOR, arm_family=arm_family)
            except Exception as exc:
                self._log(f"Diagnose fehlgeschlagen: {exc}")
                self._set_status("Einige Arme nicht gefunden. Verbindungen prüfen und erneut versuchen.")
                return

            # Log the German message line-by-line so the GUI log pane shows the
            # full bullet list, then a single short status-bar message.
            for line in diag.message_de.splitlines():
                self._log(line)
            if diag.details:
                # `UsbDiagnosis.details` is ENGLISH and often raw PowerShell
                # („Get-PnpDevice found no InstanceId matching …"). It is not
                # addressed to a 13-year-old, so it is labelled as what it is
                # rather than dressed up as a German sentence with „(Technisch:".
                self._log(f"[INFO] Technische Details für den Support: "
                          f"{diag.details}")
            self._log(f"Vollständiges Diagnose-Log: {device_manager.get_diagnostics_log_path()}")

            short_status = diag.message_de.splitlines()[0] if diag.message_de else (
                "Einige Arme nicht gefunden. Verbindungen prüfen und erneut versuchen."
            )
            self._set_status(short_status)

            # Turn the diagnosis into a one-click guided repair button that
            # matches the actual failure, instead of leaving the student to
            # parse a wall of log text. Carry the arm family so the "freigeben"
            # list scopes to the right VID (an edu6 CH343 never showed up under
            # the OMX-only 2F5D probe).
            self.root.after(
                0, lambda d=diag, f=arm_family: self._show_arm_repair(d, f))

        def _do_scan_guarded():
            # WRAP THE TARGET, NEVER `_do_scan`'s BODY, and keep the reset trio
            # HERE rather than duplicating it:
            #   * test_shutdown_teardown requires the webview close to be a
            #     TOP-LEVEL `ast.Try` of `_do_scan` WITH handlers, so
            #     re-indenting `_do_scan`'s body into a try pushes it one level
            #     down and drops it out of that check;
            #   * `test_confirming_scans_byte_for_byte_as_it_did_before` pins
            #     the scan button to exactly ['disabled', 'normal'], so a
            #     `finally` that re-enables it IN ADDITION to an in-body call
            #     produces a third transition.
            # Four other shapes were tried and all failed for one of those two
            # reasons; this one is the measured-green shape.
            try:
                _do_scan()
            except Exception as exc:  # noqa: BLE001 — a worker must never strand the UI
                self._log(f"[FEHLER] Der Arm-Scan wurde abgebrochen: {exc}")
                self._set_status("Arm-Scan fehlgeschlagen — siehe Protokoll")
            finally:
                self._scanning = False
                self.root.after(0, lambda: self.progress.stop())
                self.root.after(
                    0, lambda: self.btn_scan_leader.config(state=tk.NORMAL))
                self.root.after(
                    0, lambda: self.btn_scan_arm.config(state=tk.NORMAL))

        threading.Thread(target=_do_scan_guarded, daemon=True).start()

    # ── Guided arm repair ────────────────────────────────────────────────

    def _clear_arm_repair(self):
        """Remove any repair widgets from a previous failed arm scan."""
        for w in self.arm_repair_frame.winfo_children():
            w.destroy()

    def _show_arm_repair(self, diag: "device_manager.UsbDiagnosis",
                         arm_family: str = "omx"):
        """Show the action button that matches the arm-scan failure reason.

        Maps each UsbDiagnosis flag to the cheapest fix the student can do
        themselves:
          - usbipd_missing       → re-run prereq + policy elevated
          - wsl_distro_missing   → finalize install elevated
          - attach_failed /
            no_serial_after_attach → one elevated `usbipd bind` of the arms
          - everything else      → just a "scan again" button (cable/power
                                    checklist already printed to the log).
        """
        self._clear_arm_repair()

        if diag.usbipd_missing:
            ttk.Button(
                self.arm_repair_frame,
                text="usbipd reparieren (Administrator)",
                command=self._prompt_repair_usbipd,
            ).pack(anchor=tk.W, pady=(2, 0))
            return

        if diag.wsl_distro_missing:
            ttk.Button(
                self.arm_repair_frame,
                text="Einrichtung abschließen (Administrator)",
                command=self._prompt_finalize_install,
            ).pack(anchor=tk.W, pady=(2, 0))
            return

        # attach_failed / no_serial_after_attach: the arms ARE visible to
        # usbipd but never reached the Shared state — the install-time bind
        # didn't happen (post-install PATH race) or a stale policy is missing.
        # Offer the same one-UAC bind flow the cameras have.
        if diag.attach_failed or diag.no_serial_after_attach:
            try:
                unbound = device_manager.list_unbound_robotis(arm_family)
            except Exception:  # noqa: BLE001
                unbound = []
            if not unbound:
                # Fall back to every visible arm device of this family — binding
                # an already-bound one is a cheap no-op in bind_devices.ps1.
                try:
                    unbound = device_manager.list_arm_devices(arm_family)
                except Exception:  # noqa: BLE001
                    unbound = []
            if unbound:
                n = len(unbound)
                ttk.Label(
                    self.arm_repair_frame,
                    text="Die Arme werden erkannt, sind aber noch nicht für die "
                         "EduBotics-Umgebung freigegeben.",
                    foreground="gray", wraplength=620, justify=tk.LEFT,
                ).pack(anchor=tk.W)
                ttk.Button(
                    self.arm_repair_frame,
                    text=f"Arme freigeben (Administrator) ({n})",
                    command=lambda u=unbound: self._prompt_bind_arms(u),
                ).pack(anchor=tk.W, pady=(2, 0))
                return

        # Generic fallback — cable/power/driver problem (windows_sees_no_robotis
        # etc.). The detailed checklist is already in the log; give a quick
        # re-scan affordance.
        ttk.Button(
            self.arm_repair_frame,
            text="Erneut scannen",
            command=self._scan_arms,
        ).pack(anchor=tk.W, pady=(2, 0))

    def _prompt_bind_arms(self, unbound: "list[device_manager.USBDevice]"):
        """Bind the ROBOTIS arms via one elevated bind_devices.ps1 call.

        Mirror of _prompt_bind_cameras for the arms: one UAC prompt adds the
        AutoBind policy + `usbipd bind` for each arm's VID:PID, moving them
        into the Shared state so the next (non-admin) scan can attach them.
        """
        hw_ids = device_manager.hardware_ids_for_devices(unbound)
        if not hw_ids:
            return
        device_lines = "\n".join(
            f"  • {d.description or '(unbekannt)'} ({d.vid_pid})" for d in unbound
        )
        wants_run = messagebox.askyesno(
            "Arme freigeben",
            "Folgende Roboterarme sind angeschlossen, aber noch nicht für die "
            "EduBotics-Umgebung freigegeben:\n\n"
            f"{device_lines}\n\n"
            "Jetzt freigeben? Dies erfordert einmalig Administrator-Rechte. "
            "Danach wird automatisch erneut gescannt.",
        )
        if not wants_run:
            return
        self._run_bind_devices_elevated(
            hw_ids,
            status="Arme werden freigegeben (UAC-Zustimmung erforderlich)...",
            on_done=lambda: self.root.after(200, self._scan_arms),
        )

    # ── Camera Scanning ──────────────────────────────────────────────

    def _scan_cameras(self):
        """Nach Webcams scannen — läuft im Hintergrund."""
        if self._scanning:
            # `_scanning` is SHARED with the arm scan, but only that path's own
            # button is disabled — „Kameras scannen" stayed visibly enabled and
            # this `return` was completely silent: no status, no log, no cursor
            # change. The button looked alive and did nothing.
            #
            # Deliberately NOT mirrored into `_scan_arms`'s combined early
            # return: `test_declining_does_ABSOLUTELY_NOTHING` requires a
            # declined confirmation to write no status and no log, and that
            # decline shares the guard clause with `_scan_confirm_open`.
            self._set_status("Ein Scan läuft bereits — bitte warten.")
            return
        self._scanning = True
        # Rescanning probes the devices — release previews first (PR-4).
        self._stop_camera_previews()
        self.btn_scan_camera.config(state=tk.DISABLED)

        def _do_scan():
            self._set_status("Kameras werden gesucht...")
            # The arm scan spins the progress bar and the camera scan did not,
            # so a camera scan (which can take seconds per device) gave no
            # motion cue at all.
            self.root.after(0, lambda: self.progress.start(10))
            self._log("Video-Geräte werden gescannt...")

            self.cameras = device_manager.scan_cameras()
            # After the scan, also look for cameras Windows sees that usbipd
            # has NOT bound yet — i.e. webcams plugged in AFTER the installer
            # ran, whose VID:PID never got a policy. The student gets a one-
            # click "Freigeben" repair if any are found. Cheap call (no docker,
            # no WSL — just usbipd list + Get-PnpDevice).
            #
            # Skipped in native_bridge mode: cameras are captured natively on
            # Windows and never attached to WSL via usbipd, so a "not bound"
            # state is irrelevant there.
            if cameras_use_native_bridge():
                unbound = []
            else:
                try:
                    unbound = device_manager.list_unbound_cameras()
                except Exception as exc:  # never block the scan UI on this probe
                    self._log(f"Unfreigegebene Kameras konnten nicht ermittelt werden: {exc}")
                    unbound = []

            def _update_checkbuttons():
                # Clear old checkbuttons
                for w in self.camera_checks_frame.winfo_children():
                    w.destroy()
                self.camera_check_vars.clear()

                if self.cameras:
                    for cam in self.cameras:
                        var = tk.BooleanVar(value=True)
                        cb = ttk.Checkbutton(
                            self.camera_checks_frame,
                            text=f"{cam.name} ({cam.path})",
                            variable=var,
                            command=self._on_cameras_changed,
                        )
                        cb.pack(anchor=tk.W)
                        self.camera_check_vars.append(var)
                    self._on_cameras_changed()
                    self._log(f"{len(self.cameras)} Kamera(s) gefunden.")
                else:
                    ttk.Label(self.camera_checks_frame, text="Keine Kameras gefunden (optional)", foreground="gray").pack(anchor=tk.W)
                    self._log("Keine Kameras gefunden. Kameras sind optional — Start ohne Kamera möglich.")
                    # Native-bridge cameras are opened directly via OpenCV on
                    # Windows. The #1 reason a healthy camera yields 0 hits is
                    # the Windows privacy toggle blocking desktop apps — detect
                    # it and give a precise fix instead of a generic message.
                    if cameras_use_native_bridge():
                        blocked = None
                        try:
                            from . import win_camera
                            blocked = win_camera.camera_privacy_blocked()
                        except Exception:  # noqa: BLE001
                            blocked = None
                        # TRI-STATE: `camera_privacy_blocked()` answers True /
                        # False / None, where None means „could not determine"
                        # (registry unreadable, key absent, non-Windows). A
                        # truthiness test folded None into the False branch and
                        # handed the student a confident „Tipp: … Geräte-Manager"
                        # on a machine where the check simply failed.
                        if blocked is True:
                            self._log(
                                "[WARNUNG] Windows blockiert den Kamera-Zugriff für "
                                "Desktop-Apps. Einstellungen → Datenschutz und "
                                "Sicherheit → Kamera → 'Desktop-Apps den Zugriff "
                                "auf Ihre Kamera erlauben' EINSCHALTEN, dann erneut scannen."
                            )
                            ttk.Button(
                                self.camera_checks_frame,
                                text="Windows-Kamera-Einstellungen öffnen",
                                command=self._open_camera_privacy_settings,
                            ).pack(anchor=tk.W, pady=(4, 0))
                        elif blocked is False:
                            self._log(
                                "[INFO] Kamera per USB anschließen und prüfen, ob sie im "
                                "Windows Geräte-Manager als „Kamera“ erscheint. "
                                "Bei Bedarf USB-Port wechseln, dann erneut scannen."
                            )
                        else:
                            self._log(
                                "[INFO] Die Windows-Kamera-Einstellungen konnten "
                                "nicht geprüft werden. Bitte in den Einstellungen "
                                "kontrollieren, ob Desktop-Apps auf die Kamera "
                                "zugreifen dürfen, und die Kamera per USB "
                                "anschließen."
                            )

                # Unbound-cameras hint + repair button. Shows even when SOME
                # cameras were found, because the student may have plugged in
                # one bound + one unbound camera.
                if unbound:
                    summary = ", ".join(
                        f"{d.description or d.vid_pid}" for d in unbound
                    )
                    self._log(
                        f"[INFO] {len(unbound)} Kamera(s) sichtbar aber nicht freigegeben: {summary}"
                    )
                    btn = ttk.Button(
                        self.camera_checks_frame,
                        text=f"Kamera(s) freigeben ({len(unbound)})",
                        command=lambda u=unbound: self._prompt_bind_cameras(u),
                    )
                    btn.pack(anchor=tk.W, pady=(5, 0))

            # The button reset moved OUT of `_update_checkbuttons` and into
            # `_do_scan_guarded`'s `finally`: this closure runs only if
            # `_do_scan` reached its `root.after`, so any raise above it left
            # „Kameras scannen" disabled for the rest of the session.
            self.root.after(0, _update_checkbuttons)
            self._set_status("Kamera-Scan abgeschlossen.")

        def _do_scan_guarded():
            try:
                _do_scan()
            except Exception as exc:  # noqa: BLE001 — a worker must never strand the UI
                self._log(f"[FEHLER] Der Kamera-Scan wurde abgebrochen: {exc}")
                self._set_status("Kamera-Scan fehlgeschlagen — siehe Protokoll")
            finally:
                self._scanning = False
                self.root.after(0, lambda: self.progress.stop())
                self.root.after(
                    0, lambda: self.btn_scan_camera.config(state=tk.NORMAL))

        threading.Thread(target=_do_scan_guarded, daemon=True).start()

    def _resolve_bind_script(self):
        """Locate bind_devices.ps1 in either the production or dev layout."""
        from .constants import INSTALL_DIR
        candidates = [
            os.path.join(INSTALL_DIR, "scripts", "bind_devices.ps1"),
            os.path.join(INSTALL_DIR, "installer", "scripts", "bind_devices.ps1"),
        ]
        return next((p for p in candidates if os.path.isfile(p)), None)

    def _run_bind_devices_elevated(self, hw_ids, status: str, on_done):
        """Run bind_devices.ps1 elevated for the given VID:PIDs, then call on_done.

        Shared by the camera ("Kameras freigeben") and arm ("Arme freigeben")
        repair flows. One UAC prompt processes the whole batch — usbipd accepts
        the hardware-IDs in sequence inside the script. ``on_done`` runs after
        the elevated child exits (regardless of success) so the caller can
        re-scan; it is NOT called on UAC cancel.
        """
        script = self._resolve_bind_script()
        if script is None:
            messagebox.showerror(
                "Reparatur nicht möglich",
                "Das Skript bind_devices.ps1 wurde nicht gefunden. "
                "Bitte den EduBotics-Installer erneut ausführen.",
            )
            return
        if not hw_ids:
            return

        self._set_status(status)
        self._log("Freigabe wird mit Administrator-Rechten gestartet...")

        def _run_elevated():
            log_file = os.path.join(_edubotics_diag_dir(), "edubotics_bind_devices.log")
            try:
                if os.path.isfile(log_file):
                    os.remove(log_file)
            except OSError:
                pass

            # PowerShell parses comma-separated string arrays from -HardwareIds
            # natively when we quote each element. Building the literal as a
            # comma-joined list of double-quoted strings is the safest form
            # across PowerShell 5.1 and 7.x.
            hw_args = ",".join(f'"{h}"' for h in hw_ids)
            ps_args = (
                f'-NoProfile -ExecutionPolicy Bypass -File "{script}" '
                f'-HardwareIds {hw_args} '
                f'-LogPath "{log_file}"'
            )
            # Paste-safe variant for the manual-fallback dialog. ps_args cannot
            # be pasted into an interactive PowerShell: the OUTER shell parses
            # the bare `-HardwareIds "a","b"` comma list as ITS OWN array and
            # flattens it into separate child arguments — the second ID then
            # fails to bind (no positional params) and nothing runs. Wrapping
            # everything in one double-quoted -Command string survives both
            # PowerShell and cmd.exe verbatim: the inner tokens are
            # single-quoted (apostrophes doubled), and nothing in the SOURCE
            # here contains `$` (VID:PIDs are hex; the log path comes from
            # _edubotics_diag_dir(), i.e. %ProgramData%\EduBotics\logs, or its
            # %LOCALAPPDATA% fallback).
            manual_hw = ",".join(f"'{_ps_single_quote(h)}'" for h in hw_ids)
            manual_cmd = (
                f'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '
                f'"& \'{_ps_single_quote(script)}\' '
                f'-HardwareIds {manual_hw} '
                f'-LogPath \'{_ps_single_quote(log_file)}\'"'
            )
            self._elevation_prewarn()
            exit_code, cancelled, err = _run_privileged(
                exe="powershell.exe",
                args=ps_args,
            )
            if err:
                self._log(f"UAC-Fehler: {err}")

            # Show the tail of what happened.
            try:
                if os.path.isfile(log_file) and os.path.getsize(log_file) > 0:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
                    tail = lines[-20:]
                    if tail:
                        self._log("── Freigabe-Protokoll ──")
                        for line in tail:
                            self._log(f"  {line}")
            except OSError:
                pass

            if cancelled:
                self._log("Freigabe abgebrochen (UAC-Zustimmung verweigert).")
                self._set_status("Freigabe abgebrochen — erneut versuchen")
                # Group-Policy auto-deny loops on ERROR_CANCELLED; escalate.
                n = getattr(self, "_cancel_count_bind", 0) + 1
                setattr(self, "_cancel_count_bind", n)
                if n >= 2:
                    self._log("Mehrfach abgebrochen — manueller Befehl wird angeboten.")
                    self._show_manual_elevation_fallback(
                        "Geräte-Freigabe manuell ausführen",
                        manual_cmd,
                    )
                return
            if exit_code is None:
                # Launch failure (never ran): offer the manual admin command
                # instead of silently swallowing it with the numeric branch.
                self._log(
                    "Freigabe konnte nicht gestartet werden (kein Prozess). "
                    "Manueller Befehl wird angeboten."
                )
                self._set_status("Freigabe nicht gestartet — manueller Befehl")
                self._show_manual_elevation_fallback(
                    "Geräte-Freigabe manuell ausführen",
                    manual_cmd,
                )
                return
            if exit_code != 0:
                self._log(f"Freigabe-Skript exit={exit_code}. Siehe Protokoll oben.")

            if on_done is not None:
                on_done()

        threading.Thread(target=_run_elevated, daemon=True).start()

    def _prompt_bind_cameras(self, unbound: "list[device_manager.USBDevice]"):
        """Bind the listed UVC cameras via one elevated PowerShell call.

        Flow:
          1. Confirm with the student (German messagebox lists VID:PIDs).
          2. UAC prompt → elevated bind_devices.ps1 with the VID:PIDs.
          3. After exit, re-run _scan_cameras so the newly-bound camera
             attaches to WSL and shows up in the checkbox list.
        """
        hw_ids = device_manager.hardware_ids_for_devices(unbound)
        if not hw_ids:
            return

        device_lines = "\n".join(
            f"  • {d.description or '(unbekannt)'} ({d.vid_pid})"
            for d in unbound
        )
        wants_run = messagebox.askyesno(
            "Kameras freigeben",
            "Folgende Kameras sind angeschlossen, aber noch nicht für die "
            "EduBotics-Umgebung freigegeben:\n\n"
            f"{device_lines}\n\n"
            "Jetzt freigeben? Dies erfordert einmalig Administrator-Rechte.",
        )
        if not wants_run:
            return

        def _rescan_cameras():
            self._log("Kamera-Scan wird nach Freigabe wiederholt...")
            self.root.after(200, self._scan_cameras)

        self._run_bind_devices_elevated(
            hw_ids,
            status="Kameras werden freigegeben (UAC-Zustimmung erforderlich)...",
            on_done=_rescan_cameras,
        )

    def _open_camera_privacy_settings(self):
        """Open the Windows camera privacy settings page (ms-settings deep link)."""
        try:
            os.startfile("ms-settings:privacy-webcam")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            webbrowser.open("ms-settings:privacy-webcam")

    def _on_cameras_changed(self):
        """Ausgewählte Kameras in HardwareConfig speichern und Rollenzuweisung anzeigen."""
        # The PROFILE decides which roles exist and therefore how many cameras
        # this rig can use: edu6_studio declares camera_roles=('scene',) and its
        # server config subscribes to exactly one topic, so a second camera
        # would publish a topic nothing reads. The cap used to be a flat 2.
        roles = ROBOT_PROFILES.get(
            self._selected_robot_profile(), {}).get(
                "camera_roles", ("gripper", "scene")) or ("gripper", "scene")
        max_cams = min(2, len(roles))

        selected = []
        for i, var in enumerate(self.camera_check_vars):
            if var.get() and i < len(self.cameras):
                selected.append(self.cameras[i])
        dropped = len(selected) - max_cams
        selected = selected[:max_cams]

        # Release any running previews BEFORE the role frame (which hosts
        # them) is rebuilt — and before captures could double-open.
        self._stop_camera_previews()

        # Clear role assignment UI
        for w in self.camera_role_frame.winfo_children():
            w.destroy()

        if dropped > 0:
            # Say so rather than silently ignoring a ticked box.
            ttk.Label(
                self.camera_role_frame,
                text=("Dieser Robotertyp nutzt nur eine Kamera — die erste "
                      "ausgewählte wird verwendet."
                      if max_cams == 1 else
                      f"Es werden höchstens {max_cams} Kameras verwendet."),
                foreground="gray", wraplength=620, justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(0, 4))

        if len(selected) == 1:
            # Single camera — auto-assign the PROFILE's first role (edu6 §4.4):
            # a Roboter-Studio kit's lone camera is the SCENE camera, not the
            # gripper cam (perception + the omx_f/edu6 config topics hang off
            # the role name, so "gripper" here broke every follower-only kit).
            role = roles[0]
            selected[0].role = role
            self.hardware.cameras = selected
            label_de = "Szenen-Kamera" if role == "scene" else "Greifer-Kamera"
            ttk.Label(self.camera_role_frame,
                      text=f"{label_de}: {selected[0].name}",
                      foreground="green").pack(anchor=tk.W)
            self._start_camera_previews(selected)
        elif len(selected) == 2:
            # Two cameras — the student assigns Greifer/Szene by LOOKING at the
            # live preview under each (the two Innomakers are identical-serial,
            # so a name/index can't disambiguate them). The role selectors live
            # in the preview holders built by _start_camera_previews.
            ttk.Label(self.camera_role_frame,
                      text="Kamera-Rollen zuweisen:",
                      style="Bold.TLabel").pack(anchor=tk.W, pady=(5, 2))
            ttk.Label(self.camera_role_frame,
                      text="Schau in die beiden Live-Vorschauen und wähle "
                           "darunter die passende Rolle — Greifer = Kamera am "
                           "Greifer, Szene = Übersicht.",
                      foreground="gray", wraplength=620,
                      justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 2))
            # Sensible default (corrected via the per-preview selectors): first
            # camera = Greifer, second = Szene. Taken from the PROFILE now, but
            # filtered through the FIXED wire order („gripper" then „scene")
            # rather than the registry's own tuple order — `omx_follower` lists
            # ('scene', 'gripper'), and that ordering is there to pick a LONE
            # camera's role, not to reorder a pair. hardware.cameras must stay
            # gripper-first: config_generator writes CAMERA_NAME_1/_2 in list
            # order and camera_ingest_node maps gripper→cam_id 0, scene→1.
            pair = [r for r in ("gripper", "scene") if r in roles]
            selected[0].role = pair[0]
            selected[1].role = pair[1]
            self.hardware.cameras = [selected[0], selected[1]]
            self._start_camera_previews(selected)
        else:
            self.hardware.cameras = selected

    def _on_preview_role_changed(self, cameras, position):
        """Per-preview role pick — keep the two cameras mutually exclusive.

        The student assigns a role by LOOKING at the live image under each
        "Kamera N" badge, so there is no index→name mapping to get wrong (the
        two Innomakers are identical-serial). Picking a role on one preview
        flips the OTHER to the opposite role, and `self.hardware.cameras` is
        kept gripper-first (the order config_generator / _start_camera_bridge
        expect).
        """
        role_map = {"Greifer": "gripper", "Szene": "scene"}
        role_vars = getattr(self, "_preview_role_vars", [])
        if (len(cameras) < 2 or position >= len(role_vars)
                or role_vars[position] is None):
            return
        chosen = role_vars[position].get()
        if chosen not in role_map:
            return
        other = 1 - position
        other_display = "Szene" if chosen == "Greifer" else "Greifer"
        if other < len(role_vars) and role_vars[other] is not None:
            role_vars[other].set(other_display)
        cameras[position].role = role_map[chosen]
        cameras[other].role = role_map[other_display]
        gripper_cam = cameras[position] if chosen == "Greifer" else cameras[other]
        scene_cam = cameras[other] if chosen == "Greifer" else cameras[position]
        self.hardware.cameras = [gripper_cam, scene_cam]
        self._log(
            f"Kamera-Rolle gesetzt: Kamera {position + 1} = {chosen}, "
            f"Kamera {other + 1} = {other_display}.")

    # ── Arm-Konfiguration aus letzter Sitzung wiederherstellen (PR-4) ──

    def _try_rehydrate_arms(self):
        """Restore leader/follower from the persisted .env without a full scan.

        Light path only (attach + by-id presence check; no scanner container,
        no serial pings). Silent on any mismatch — the student simply scans
        as before. Never runs in cloud-only mode or over an existing scan.
        """
        # The robot type persisted in the .env drives whether the leader is
        # required. A follower-only (Roboter Studio) .env has no LEADER_PORT, so
        # requiring it here would permanently no-op fast rehydrate for that type.
        profile = config_generator.read_env_var("EDUBOTICS_ROBOT_TYPE", ENV_FILE) \
            or DEFAULT_ROBOT_PROFILE
        if profile not in ROBOT_PROFILES:
            profile = DEFAULT_ROBOT_PROFILE
        requires_leader = ROBOT_PROFILES[profile]["scan_requires_leader"]
        arm_family = ROBOT_PROFILES[profile].get("arm_family", "omx")

        if (self.cloud_only.get() or self._hardware_ready(profile)
                or self._scanning):
            return
        try:
            leader_port = config_generator.read_env_var("LEADER_PORT", ENV_FILE)
            follower_port = config_generator.read_env_var("FOLLOWER_PORT", ENV_FILE)
        except Exception:  # noqa: BLE001 — missing .env on first run
            return
        # Follower always required; leader only for both-arms types.
        if not follower_port or (requires_leader and not leader_port):
            return

        def _do_rehydrate():
            self._log("Prüfe letzte Arm-Konfiguration ...")
            try:
                leader, follower = device_manager.fast_rehydrate_arms(
                    leader_port or "", follower_port,
                    require_leader=requires_leader, arm_family=arm_family,
                )
            except Exception as exc:  # noqa: BLE001
                self._log(f"(Schnellprüfung übersprungen: {exc})")
                return
            if follower is None or (requires_leader and leader is None):
                self._log(
                    "Letzte Arm-Konfiguration nicht bestätigt — bitte "
                    "„Arme scannen“ klicken."
                )
                return
            # A manual scan may be running or completed meanwhile — never
            # race or overwrite it (the scan's identify result outranks
            # the rehydrated mapping).
            if self._scanning or self._hardware_ready(profile):
                return
            # leader may be None for a follower-only type — only update its
            # status line when present (guard against AttributeError).
            if leader is not None:
                self.hardware.leader = leader
                self.root.after(0, lambda: self.leader_status_var.set(
                    f"Wiederhergestellt: {leader.description} ({leader.serial_path})"
                ))
            self.hardware.follower = follower
            self.root.after(0, lambda: self.follower_status_var.set(
                f"Wiederhergestellt: {follower.description} ({follower.serial_path})"
            ))
            self._log(
                "Letzte Arm-Konfiguration wiederhergestellt — bei Probleme"
                "n mit den Armen bitte erneut „Arme scannen“ klicken."
            )
            self._update_start_button()

        threading.Thread(target=_do_rehydrate, daemon=True).start()

    # ── Live-Kameravorschau bei der Rollenzuweisung (PR-4) ─────────────

    def _stop_camera_previews(self):
        """Release every preview capture + cancel the poll loop.

        MUST run before anything else claims the devices (the capture
        bridge at environment start, a camera rescan) — concurrent opens of
        the same index are the exact contention class the native bridge
        exists to avoid.
        """
        # Invalidate any in-flight worker opens first (they release their
        # captures instead of installing into a torn-down preview row).
        self._preview_generation = getattr(self, "_preview_generation", 0) + 1
        existing_after_id = getattr(self, "_preview_after_id", None)
        if existing_after_id is not None:
            try:
                self.root.after_cancel(existing_after_id)
            except Exception:  # noqa: BLE001
                pass
        # Initialise UNCONDITIONALLY (like every other preview-state attr
        # below). This used to be set ONLY inside the `if` above, so on the
        # FIRST preview of the process the attribute never existed — and
        # `_install`'s `if self._preview_after_id is None` then raised
        # AttributeError into Tk's callback handler (silent in the packaged
        # .exe). The poll was never scheduled → an endless "Vorschau lädt ..."
        # with the cameras visibly free. Bug since PR-4 introduced previews.
        self._preview_after_id = None
        for cap in getattr(self, "_preview_caps", {}).values():
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
        self._preview_caps = {}
        self._preview_labels = {}
        self._preview_images = {}
        # Per-camera diagnostic state (keyed by win_index) — reset every
        # teardown so a re-open starts fresh. So the "Vorschau lädt ..."
        # stall can never again be silent: we count renders/empty reads and
        # log a one-time give-up per camera.
        self._preview_names = {}
        self._preview_frames_rendered = {}
        self._preview_empty_reads = {}
        self._preview_error_logged = {}

    def _start_camera_previews(self, cameras):
        """Open a small live preview per selected camera (pre-start only).

        leLab-comparison PR-4: two identical-serial Innomakers are
        indistinguishable by name — the student picked gripper/scene BLIND,
        and a swap silently corrupts every recorded dataset (the policy
        learns the wrong view). A live thumbnail per camera makes the
        mistake visually impossible. Rendering encodes each frame to
        base64-PNG and feeds it to tk.PhotoImage (the shipped Tk 8.6 cannot
        reliably render raw binary PPM — NUL bytes truncate it; cv2 is
        already in this path, no Pillow dependency). Open-once-poll-close at
        ~5 Hz; previews stop before the bridge claims the devices.
        """
        self._stop_camera_previews()
        if not cameras:
            return

        # Generation counter: a selection change mid-open bumps it, and the
        # worker discards (releases) captures that finish for a stale
        # generation instead of installing them after _stop ran.
        self._preview_generation = getattr(self, "_preview_generation", 0) + 1
        generation = self._preview_generation

        # Distinct, colour-coded identity per camera. The two Innomakers are
        # identical-serial (same name + serial), so a name can't tell them
        # apart — the LIVE IMAGE does. Each gets a coloured "Kamera N" badge +
        # border, and (with 2 cameras) the role selector sits directly under
        # its own preview, so the student assigns Greifer/Szene by LOOKING at
        # the picture, never by an opaque index. (UX redesign 2026-06-14.)
        palette = [("#1f6feb", "Kamera 1"), ("#e8770e", "Kamera 2")]
        two_cams = len(cameras) >= 2
        self._preview_role_vars = []

        # The role picker is an ALLOWLIST, not a hint: a role this robot type
        # has no topic for must not be OFFERABLE. Same rule the Pi's
        # SystemPage.js applies to its <option> set, and the display ORDER
        # comes from this fixed list rather than the registry tuple (which
        # orders for the lone-camera default).
        allowed_roles = ROBOT_PROFILES.get(
            self._selected_robot_profile(), {}).get(
                "camera_roles", ("gripper", "scene")) or ("gripper", "scene")
        role_values = [de for wire, de in (("gripper", "Greifer"),
                                           ("scene", "Szene"))
                       if wire in allowed_roles]

        preview_row = ttk.Frame(self.camera_role_frame)
        preview_row.pack(fill=tk.X, pady=(6, 2))
        targets = []
        for position, cam in enumerate(cameras):
            color, cam_label = (
                palette[position] if position < len(palette)
                else ("#555555", f"Kamera {position + 1}"))
            # tk.Frame (not ttk) so the coloured border renders via the
            # highlight ring — ttk.Frame has no highlightthickness option.
            holder = tk.Frame(
                preview_row, highlightbackground=color, highlightcolor=color,
                highlightthickness=3, bd=0)
            holder.pack(side=tk.LEFT, padx=8)
            tk.Label(
                holder, text=cam_label, bg=color, fg="white",
                font=theme_mod.BADGE_FONT, padx=8, pady=2).pack(fill=tk.X)
            label = ttk.Label(
                holder, text="Vorschau lädt ...", anchor=tk.CENTER)
            label.pack(padx=4, pady=4)
            if two_cams and len(role_values) > 1:
                role_var = tk.StringVar(
                    value="Szene" if getattr(cam, "role", "") == "scene"
                    else "Greifer")
                self._preview_role_vars.append(role_var)
                role_row = ttk.Frame(holder)
                role_row.pack(fill=tk.X, padx=4, pady=(0, 4))
                ttk.Label(role_row, text="Rolle:").pack(side=tk.LEFT)
                combo = ttk.Combobox(
                    role_row, textvariable=role_var,
                    values=role_values, state="readonly", width=10)
                combo.pack(side=tk.LEFT, padx=4)
                combo.bind(
                    "<<ComboboxSelected>>",
                    lambda e, p=position: self._on_preview_role_changed(
                        cameras, p))
            else:
                # No selector: either a single camera, or a robot type that
                # declares only ONE camera role. Report the role
                # `_on_cameras_changed` ACTUALLY assigned from the profile —
                # this used to be hardcoded „Rolle: Greifer" and sat three rows
                # under a green „Szenen-Kamera: <name>" saying the opposite.
                self._preview_role_vars.append(None)
                role_de = ("Szene" if getattr(cam, "role", "") == "scene"
                           else "Greifer")
                ttk.Label(
                    holder, text=f"Rolle: {role_de}", foreground="gray").pack(
                    padx=4, pady=(0, 4))
            if cam.win_index < 0:
                # usb_cam rollback path — no native index to open (the
                # bridge guards the same way); opening -1 would grab an
                # arbitrary device (e.g. a laptop's built-in camera).
                label.config(text="Keine Vorschau (usb_cam-Modus)")
                continue
            targets.append((cam.win_index, label, cam_label))

        # Contention guard: while the environment runs the capture bridge holds
        # the cameras EXCLUSIVELY. A second MSMF open returns a frameless handle
        # (an endless "Vorschau lädt ..."), so don't open them — but the badges
        # + role selectors above stay fully usable; just note it in each slot.
        if self.running or self.camera_bridge is not None:
            for _idx, paused_label, _name in targets:
                paused_label.config(
                    text="Vorschau pausiert,\nUmgebung läuft.")
            return

        if not targets:
            return

        try:
            from . import win_camera
        except Exception:  # noqa: BLE001
            return

        def _open_captures():
            # MSMF open costs ~1-3 s per camera — NEVER on the Tk main
            # thread (a 2-camera pick froze the GUI ~6 s). Open here, then
            # marshal the handles back; cap.read() at 5 Hz stays on main
            # (cheap, and Tk widgets are touched only there).
            for win_index, label, name in targets:
                try:
                    cap, _info = win_camera.open_capture(win_index, 640, 480, 15)
                except Exception as exc:  # noqa: BLE001
                    self.root.after(0, lambda lbl=label, e=exc: lbl.config(
                        text=f"Keine Vorschau ({e})"))
                    self.root.after(0, lambda i=win_index, e=exc: self._log(
                        f"Kamera-Vorschau konnte nicht geöffnet werden "
                        f"(Index {i}): {e}"))
                    continue

                def _install(idx=win_index, c=cap, lbl=label, nm=name):
                    if generation != getattr(self, "_preview_generation", -1):
                        # Selection changed while opening — stale handle.
                        try:
                            c.release()
                        except Exception:  # noqa: BLE001
                            pass
                        return
                    self._preview_caps[idx] = c
                    self._preview_labels[idx] = lbl
                    self._preview_names[idx] = nm
                    self._preview_frames_rendered.setdefault(idx, 0)
                    self._preview_empty_reads.setdefault(idx, 0)
                    self._preview_error_logged.setdefault(idx, False)
                    if self._preview_after_id is None:
                        self._preview_after_id = self.root.after(
                            50, self._poll_camera_previews)

                self.root.after(0, _install)

        threading.Thread(target=_open_captures, daemon=True).start()

    def _poll_camera_previews(self):
        self._preview_after_id = None
        if not self._preview_caps:
            return
        # ~30 ticks at the 100 ms cadence ≈ 3 s before we give up on a
        # camera that has never produced a frame.
        give_up_ticks = 30
        for win_index, cap in list(self._preview_caps.items()):
            label = self._preview_labels.get(win_index)
            if label is None:
                continue
            name = self._preview_names.get(win_index, f"Index {win_index}")

            def _log_once(message):
                # One log line per camera; subsequent failures stay quiet.
                if not self._preview_error_logged.get(win_index):
                    self._preview_error_logged[win_index] = True
                    self._log(message)

            try:
                ok, frame = _read_freshest_frame(cap)
            except Exception as exc:  # noqa: BLE001 — preview is best-effort
                label.config(
                    text="Kamera liefert kein Bild — evtl. belegt oder "
                         "Treiberproblem")
                _log_once(
                    f"Kamera-Vorschau Lesefehler ({name}, Index "
                    f"{win_index}): {exc}")
                continue

            if not ok or frame is None:
                # No frame this tick — count it, and give up loudly once a
                # camera has yielded ZERO frames after ~3 s so the student
                # never stares at an endless "Vorschau lädt ...".
                self._preview_empty_reads[win_index] = (
                    self._preview_empty_reads.get(win_index, 0) + 1)
                if (self._preview_frames_rendered.get(win_index, 0) == 0
                        and self._preview_empty_reads[win_index]
                        >= give_up_ticks):
                    label.config(
                        text="Kamera liefert kein Bild — evtl. belegt oder "
                             "Treiberproblem")
                    _log_once(
                        f"Kamera-Vorschau liefert kein Bild ({name}, Index "
                        f"{win_index}) — evtl. belegt oder Treiberproblem.")
                continue

            data = _frame_to_photo_data(frame)
            if data is None:
                label.config(
                    text="Kamera liefert kein Bild — evtl. belegt oder "
                         "Treiberproblem")
                _log_once(
                    f"Kamera-Vorschau konnte das Bild nicht kodieren "
                    f"({name}, Index {win_index}).")
                continue
            try:
                photo = tk.PhotoImage(data=data)
            except Exception as exc:  # noqa: BLE001 — preview is best-effort
                label.config(
                    text="Kamera liefert kein Bild — evtl. belegt oder "
                         "Treiberproblem")
                _log_once(
                    f"Kamera-Vorschau Anzeigefehler ({name}, Index "
                    f"{win_index}): {exc}")
                continue

            label.config(image=photo, text="")
            # Keep a reference — Tk drops the image otherwise.
            self._preview_images[win_index] = photo
            prev = self._preview_frames_rendered.get(win_index, 0)
            self._preview_frames_rendered[win_index] = prev + 1
            if prev == 0:
                self._log(f"Kamera-Vorschau aktiv: {name}")
        if self._preview_caps:
            self._preview_after_id = self.root.after(
                100, self._poll_camera_previews)

    # ── Start Environment ────────────────────────────────────────────

    def _start_environment(self):
        """EduBotics-Umgebung starten und Web-Oberfläche öffnen."""
        # The capture bridge claims the cameras exclusively — previews must
        # be released first (PR-4; the leLab 5-revert contention class).
        self._stop_camera_previews()
        is_cloud_only = self.cloud_only.get()
        # Capture on the main thread — tk StringVar reads from the worker
        # thread below are not thread-safe. Empty means "leave the saved
        # token untouched" (generate_env_file preserves the existing one).
        hf_token = self.hf_token_var.get().strip()
        # Phone-as-3rd-camera toggle — captured here for the same thread-safety
        # reason. Only meaningful on the native_bridge (Windows) path.
        phone_enabled = bool(self.phone_camera_enabled.get()) and not is_cloud_only
        # Robot type is HARDSET at start (MANAGED .env key EDUBOTICS_ROBOT_TYPE).
        # Read it on the main thread; the worker below, the .env regen, the
        # webview URL, and the RS leader-toggle all reuse self._rs_robot_type.
        self._rs_robot_type = self._selected_robot_profile()

        if not is_cloud_only and not self._hardware_ready(self._rs_robot_type):
            # „Fehlende Hardware" is the wrong title for a wrong-FAMILY scan:
            # the hardware is not missing, it belongs to the other robot type.
            if self._arms_conflict_with_family(self._rs_robot_type):
                messagebox.showwarning("Falscher Robotertyp", ARM_FAMILY_MISMATCH_DE)
            elif ROBOT_PROFILES.get(self._rs_robot_type, {}).get("scan_requires_leader", True):
                messagebox.showwarning("Fehlende Hardware", "Bitte beide Arme scannen und identifizieren, bevor du startest.")
            else:
                messagebox.showwarning("Fehlende Hardware", "Bitte den Follower-Arm scannen und identifizieren, bevor du startest.")
            return

        self.btn_start.config(state=tk.DISABLED)
        self.running = True
        # Freeze the Robotertyp selector for the life of the session — the type
        # is hardset here (the successful-start path never routes through
        # _update_start_button, so gray it directly). Re-enabled on stop /
        # failed start via _update_start_button → _update_robot_type_combo_state.
        self._update_robot_type_combo_state()

        def _do_start():
            # Guarantee that progress spinner / button state / running flag
            # are always reset on exit, regardless of which early-return path
            # the body takes. Previously, the exception-handler at the
            # bottom was the only place that ran on error paths and the
            # early `return` branches above left the UI locked.
            startup_ok = False
            try:
                self.root.after(0, lambda: self.progress.start(10))

                if is_cloud_only:
                    self._log("Cloud-Modus: nur die Web-Oberfläche wird gestartet (kein Roboter).")
                else:
                    # 0. Serielle Ports validieren — bei Bedarf USB neu verbinden
                    self._set_status("Hardware-Verbindungen werden geprüft...")
                    try:
                        serial_paths = wsl_bridge.list_serial_devices()
                        missing_arms = []
                        for arm_name, arm in [("Leader", self.hardware.leader), ("Follower", self.hardware.follower)]:
                            if arm and arm.serial_path not in serial_paths:
                                missing_arms.append((arm_name, arm))

                        if missing_arms:
                            self._log("USB-Geräte werden erneut verbunden...")
                            reattach_family = ROBOT_PROFILES.get(
                                self._rs_robot_type, {}).get("arm_family", "omx")
                            device_manager.attach_all_robotis_devices(
                                reattach_family)
                            import time
                            for _ in range(5):
                                serial_paths = wsl_bridge.list_serial_devices()
                                if all(arm.serial_path in serial_paths for _, arm in missing_arms):
                                    break
                                time.sleep(1)

                            for arm_name, arm in missing_arms:
                                if arm.serial_path not in serial_paths:
                                    self._log(f"[FEHLER] {arm_name}-Arm ({arm.serial_path}) nicht erreichbar!")
                                    self._log("USB-Verbindung prüfen und erneut scannen.")
                                    self._set_status(f"{arm_name}-Arm getrennt")
                                    return
                            self._log("USB-Geräte erfolgreich neu verbunden.")
                    except Exception as e:
                        self._log(f"[WARNUNG] Serielle Ports konnten nicht validiert werden: {e}")
                        self._log("Fahre trotzdem fort — Container versuchen erneut auf Geräte zuzugreifen.")

                # 0.5 Persist a freshly-typed HF token (if any) BEFORE the .env
                # is regenerated, so generate_env_file() carries it through as
                # an unmanaged key. An already-saved token (empty field) is
                # left untouched and preserved by the regenerate. write_hf_token
                # stamps HF_TOKEN_MACHINE with it, so the token the student just
                # typed is bound to THIS PC before the .env can be copied
                # anywhere — writing it here unstamped would make it look like a
                # legacy token on every clone.
                if hf_token:
                    try:
                        config_generator.write_hf_token(hf_token, ENV_FILE)
                        self._log("HuggingFace-Token gespeichert.")
                        self.root.after(0, lambda: self.hf_token_var.set(""))
                        self.root.after(0, self._refresh_hf_token_status)
                    except Exception as e:
                        self._log(f"[WARNUNG] HF-Token konnte nicht gespeichert werden: {e}")

                # 1. .env generieren
                self._set_status("Konfiguration wird erstellt...")
                self._log(".env-Datei wird erstellt...")
                if is_cloud_only:
                    env_content = config_generator.generate_cloud_only_env(
                        ENV_FILE, phone_camera=phone_enabled,
                        robot_type=self._rs_robot_type)
                else:
                    # The initial FOLLOWER_ONLY is DERIVED from the robot type
                    # (omx_full → both arms; omx_follower → follower only). On
                    # omx_full, Roboter Studio can still switch to follower-only
                    # at RUNTIME via the React leader toggle (_rs_set_leader_mode).
                    env_content = config_generator.generate_env_file(
                        self.hardware, ENV_FILE, phone_camera=phone_enabled,
                        robot_type=self._rs_robot_type)
                self._log(f"Konfiguration geschrieben: {ENV_FILE}")
                for line in env_content.strip().splitlines():
                    self._log(f"  {_redact_secret_env_line(line)}")

                # 2. Container starten
                use_gpu = self.gpu_available and not is_cloud_only
                self._set_status("Container werden gestartet...")
                if is_cloud_only:
                    self._log("Container werden gestartet (nur physical_ai_manager, ohne Roboter)...")
                    ok = docker_manager.start_cloud_only(log=self._log)
                else:
                    self._log(f"Container werden gestartet ({'GPU' if use_gpu else 'CPU'}-Modus)...")
                    ok = docker_manager.start_containers(gpu=use_gpu, log=self._log)

                if not ok:
                    self._log("[FEHLER] Container konnten nicht gestartet werden. EduBotics-Umgebung prüfen.")
                    self._set_status("Start fehlgeschlagen")
                    return

                self._log("Container gestartet. Warte auf Dienste...")

                # 3. Auf Web-Oberfläche warten
                self._set_status("Warte auf Web-Oberfläche...")
                if not health_checker.wait_for_web_ui(
                    callback=lambda e, t: self._set_status(f"Warte auf Web-Oberfläche... {e}s/{t}s")
                ):
                    self._log("[WARNUNG] Web-Oberfläche antwortet noch nicht. Container starten möglicherweise noch.")
                    self._log("Du kannst die Web-Oberfläche manuell über die Schaltfläche öffnen.")
                else:
                    self._log("Web-Oberfläche ist bereit!")

                # 4. Gesundheitsprüfung
                health = health_checker.full_health_check()
                for service, ok in health.items():
                    self._log(f"  {service}: {'OK' if ok else 'NICHT BEREIT'}")

                # 4.5 Kamera-Bridge starten (native Aufnahme auf Windows →
                # Stream in den Container). Nur im native_bridge-Modus und
                # wenn Kameras zugewiesen sind. Bei aktiviertem Handy-Modus wird
                # der Handy-Empfänger gestartet und als 3. Kamera registriert.
                if not is_cloud_only:
                    self._start_camera_bridge(phone_enabled=phone_enabled)
                    # 4.6 Roboter-Studio leader-toggle bridge: lets the React
                    # Roboter Studio tab switch the arm between follower-only and
                    # both-arms at runtime. Capture gpu/phone here so a later
                    # toggle regenerates a consistent .env off the main thread.
                    self._start_rs_control_server(use_gpu, phone_enabled)

                # 5. Web-Oberfläche öffnen
                self._log("Web-Oberfläche wird geöffnet...")
                self._open_webview()

                self._set_status("Aktiv — Umgebung läuft")
                self.root.after(0, lambda: self.btn_stop.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.btn_open_browser.config(state=tk.NORMAL))
                startup_ok = True

            except Exception as e:
                self._log(f"[FEHLER] Unerwarteter Fehler beim Starten: {e}")
                import traceback
                self._log(traceback.format_exc())
            finally:
                self.root.after(0, lambda: self.progress.stop())
                if not startup_ok:
                    # A failure after _start_rs_control_server (e.g. _open_webview
                    # raising) would otherwise leave the :8769 socket + daemon
                    # thread running with no environment behind it. Tear it down.
                    self._stop_rs_control_server()
                    self.running = False
                    self._update_start_button()

        threading.Thread(target=_do_start, daemon=True).start()

    # ── Native camera bridge ─────────────────────────────────────────

    def _start_camera_bridge(self, phone_enabled: bool = False):
        """Start native Windows camera capture → TCP stream into the container.

        No-op when cameras run via usb_cam (Jetson). Maps each assigned camera's
        OpenCV index to its role (gripper -> cam_id 0, scene -> cam_id 1,
        matching the container's EDUBOTICS_CAMERA_NAMES order). When
        ``phone_enabled`` is True, also starts the phone HTTPS receiver and
        registers its frame slot as cam_id 2 (/phone/image_raw/compressed).
        """
        if not cameras_use_native_bridge():
            if phone_enabled:
                self._log(
                    "Handy-Kamera benötigt den nativen Kamera-Modus (Windows) — "
                    "übersprungen.")
            return
        # Tear down any previous bridge + phone server (stop→start, no restart).
        self._stop_camera_bridge()
        self._stop_phone_server()
        role_to_index = {
            cam.role: cam.win_index
            for cam in self.hardware.cameras
            if cam.role in ("gripper", "scene") and cam.win_index >= 0
        }
        if not role_to_index and not phone_enabled:
            self._log("Keine Kamera zugewiesen — Kamera-Bridge wird nicht gestartet.")
            return

        # Start the phone receiver FIRST so its slot exists before we register
        # the external sender on the bridge. A failure here is non-fatal: the
        # USB cameras still run; only the phone is skipped.
        phone_slot = None
        if phone_enabled:
            phone_slot = self._start_phone_server()

        try:
            self.camera_bridge = camera_bridge_mod.CameraBridge(role_to_index)
            if phone_slot is not None:
                # register_external_source MUST precede start().
                self.camera_bridge.register_external_source(PHONE_CAM_ID, phone_slot)
            self.camera_bridge.start()
            # Now that the bridge exists, watch it: its per-camera German
            # diagnostics had no reader at all before this.
            self._stop_camera_health_poll()
            self._camera_health_after_id = self.root.after(
                CAMERA_HEALTH_POLL_MS, self._poll_camera_health)
            roles = ", ".join(sorted(role_to_index)) or "keine USB-Kamera"
            phone_note = " + Handy" if phone_slot is not None else ""
            self._log(f"Kamera-Bridge gestartet (nativ, {roles}{phone_note}) — streamt mit "
                      f"{int(camera_bridge_mod.constants.CAMERA_FRAMERATE)} fps in den Container.")
        except Exception as e:  # noqa: BLE001
            self.camera_bridge = None
            self._log(f"[WARNUNG] Kamera-Bridge konnte nicht gestartet werden: {e}")

    def _start_phone_server(self):
        """Ensure the cert, start the phone HTTPS receiver, show the URL.

        Returns the LatestFrameSlot to register on the bridge, or None on
        failure (the USB cameras still run without the phone).
        """
        try:
            cert_path, key_path = phone_cert_mod.ensure_cert()
        except phone_cert_mod.PhoneCertError as exc:
            self._log(f"[WARNUNG] Handy-Kamera konnte nicht aktiviert werden: {exc}")
            return None
        slot = phone_camera_mod.LatestFrameSlot()
        try:
            server = phone_camera_mod.PhoneCameraServer(
                cert_path, key_path, frame_slot=slot,
                port=PORT_PHONE_HTTPS, log=self._log,
            )
            server.start()
        except OSError as exc:
            self._log(
                f"[WARNUNG] Handy-Empfänger konnte Port {PORT_PHONE_HTTPS} nicht "
                f"öffnen ({exc}) — Handy-Kamera deaktiviert.")
            return None
        except Exception as exc:  # noqa: BLE001
            self._log(f"[WARNUNG] Handy-Empfänger konnte nicht starten: {exc}")
            return None
        self.phone_server = server
        self.phone_frame_slot = slot
        url = f"https://{_primary_lan_ip()}:{PORT_PHONE_HTTPS}"
        self._log(f"Handy-Kamera aktiv — am Handy im Browser öffnen: {url}")
        self._log(
            "[INFO] Beim ersten Öffnen zeigt das Handy eine Sicherheitswarnung "
            "(selbst signiertes Zertifikat) — „Erweitert“ → „Trotzdem fortfahren“.")
        # Windows-Firewall: do NOT auto-add a rule (needs elevation). Log the
        # exact manual command the teacher can run once if the phone can't reach
        # the PC. (Auto-firewall-rule via an elevated helper is a documented
        # non-goal — see docs/plans/2026-06-07-phone-camera.md.)
        self._log(
            "Falls das Handy keine Verbindung bekommt, einmalig als Administrator "
            "die Windows-Firewall öffnen:")
        self._log(
            f'  netsh advfirewall firewall add rule name="EduBotics Handy-Kamera" '
            f'dir=in action=allow protocol=TCP localport={PORT_PHONE_HTTPS}')
        self.root.after(0, lambda: self._show_phone_url_dialog(url))
        return slot

    def _show_phone_url_dialog(self, url: str):
        """Small modal showing the phone URL + a „URL kopieren" button (no QR —
        a QR dependency is a documented non-goal)."""
        try:
            win = tk.Toplevel(self.root)
            win.title("Handy als Kamera")
            win.transient(self.root)
            win.resizable(False, False)
            frame = ttk.Frame(win, padding=16)
            frame.pack(fill=tk.BOTH, expand=True)
            ttk.Label(
                frame,
                text="Diese Adresse am Handy im Browser öffnen:",
                font=(theme_mod.FONT_FAMILY, 10),
            ).pack(anchor=tk.W)
            ttk.Label(
                frame, text=url, font=(theme_mod.FONT_FAMILY, 14, "bold"), foreground="#0A6",
            ).pack(anchor=tk.W, pady=(6, 10))

            def _copy():
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(url)
                    copied_var.set("Kopiert!")
                except tk.TclError:
                    copied_var.set("Kopieren fehlgeschlagen")

            row = ttk.Frame(frame)
            row.pack(fill=tk.X)
            ttk.Button(row, text="URL kopieren", command=_copy).pack(side=tk.LEFT)
            copied_var = tk.StringVar()
            ttk.Label(row, textvariable=copied_var, foreground="gray").pack(
                side=tk.LEFT, padx=8)
            ttk.Button(row, text="Schließen", command=win.destroy).pack(side=tk.RIGHT)
            ttk.Label(
                frame,
                text=(
                    "Das Handy muss im selben WLAN sein. Beim ersten Öffnen die "
                    "Sicherheitswarnung bestätigen („Erweitert“ → „Trotzdem "
                    "fortfahren“)."
                ),
                foreground="gray", font=(theme_mod.FONT_FAMILY, 8), wraplength=360,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(10, 0))
        except tk.TclError:
            # Headless / teardown — the URL is already in the Protokoll.
            pass

    def _stop_phone_server(self):
        """Stop the phone HTTPS receiver if running. Best-effort + idempotent."""
        if self.phone_server is not None:
            try:
                self.phone_server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self.phone_server = None
        self.phone_frame_slot = None

    def _poll_camera_health(self):
        """Surface the camera bridge's own diagnostics — LOG-ONLY, READ-ONLY.

        `CameraBridge.status()`'s docstring says „Per-camera health for the GUI
        status line" and, until this existed, had ZERO production callers: five
        authored German diagnostics were computed every capture tick and
        displayed NOWHERE, so a camera that died mid-lesson was completely
        silent in the GUI and the student met it as a missing image in the
        browser minutes later.

        Deliberately the smallest possible thing that closes that:

          * it only READS `status()` — it never opens a device, never touches a
            container, never restarts anything (the bridge already re-opens a
            dead camera by itself);
          * it logs ONCE PER TRANSITION, never per tick. A camera that stays
            broken says so once, and says so once more when it recovers;
          * it tolerates `self.camera_bridge` being None mid-teardown, and any
            raise out of `status()` ends the loop rather than repeating into
            the Protokoll;
          * `_stop_camera_bridge` cancels it, so it cannot outlive the bridge.
        """
        self._camera_health_after_id = None
        bridge = self.camera_bridge
        if bridge is None:
            return
        try:
            status = bridge.status() or {}
            cameras = status.get("cameras") or {}
        except Exception as exc:  # noqa: BLE001 — a diagnostic must not loop on its own failure
            self._log(f"[WARNUNG] Kamera-Status konnte nicht gelesen werden: {exc}")
            return
        previous = getattr(self, "_camera_health_last", {})
        current = {}
        for role, info in cameras.items():
            current[role] = (info or {}).get("error", "") or ""
        for role, message in current.items():
            was = previous.get(role, "")
            if message and message != was:
                self._log(f"[WARNUNG] {message}")
            elif was and not message:
                self._log(f"[OK] Kamera „{role}“ liefert wieder Bilder.")
        self._camera_health_last = current
        self._camera_health_after_id = self.root.after(
            CAMERA_HEALTH_POLL_MS, self._poll_camera_health)

    def _stop_camera_health_poll(self):
        """Cancel the health loop. Idempotent; safe before it ever started."""
        after_id = getattr(self, "_camera_health_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:  # noqa: BLE001
                pass
        self._camera_health_after_id = None
        self._camera_health_last = {}

    def _stop_camera_bridge(self):
        """Stop the native camera bridge if running. Best-effort."""
        self._stop_camera_health_poll()
        if self.camera_bridge is not None:
            try:
                self.camera_bridge.stop()
            except Exception:  # noqa: BLE001
                pass
            self.camera_bridge = None

    # ── Stop Environment ─────────────────────────────────────────────

    def _open_webview(self):
        """Open the web UI in an embedded WebView2 window.

        In cloud-only mode, pass ?cloud=1 so the React app skips the ROS
        startup gate (no rosbridge is running). Falls back to the system
        browser with a German warning if WebView2 is unavailable.
        """
        # Debounce rapid double-clicks. Each click previously could spawn
        # a separate pywebview subprocess (~150 MB RSS each).
        now_ms = int(time.monotonic() * 1000)
        last_ms = getattr(self, "_last_webview_click_ms", 0)
        if now_ms - last_ms < 800:
            return
        self._last_webview_click_ms = now_ms

        # Cache-bust the top-level navigation with the running image tag. The
        # embedded WebView2 keeps a PERSISTENT disk cache (private_mode=False),
        # so a window opened after an image update could otherwise be served a
        # prior session's app shell. index.html is already `no-store` at nginx,
        # so this is belt-and-suspenders — but it also defeats bfcache/in-memory
        # reuse on a window reopened right after a --force-recreate. The `_v`
        # changes whenever the pinned IMAGE_TAG changes (i.e. on every GUI
        # update / image bump), forcing a fresh fetch of the shell.
        params = [f"_v={IMAGE_TAG}"]
        if self.cloud_only.get():
            params.insert(0, "cloud=1")
        # Instant robot identity/badge for the React app (authoritative
        # capabilities still arrive from the server via TaskStatus). Uses the
        # profile id captured at env start, so it stays correct on re-open.
        params.append(f"robot={self._rs_robot_type}")
        # STUDENT HANDOVER. One shared Windows account = one WebView2 profile =
        # ONE localStorage for every student who ever sits at this PC, and the
        # student who walks away without clicking „Abmelden" leaves a durable,
        # self-renewing Supabase session behind. This nonce tells the SPA that
        # the window it is booting in is FRESHLY SPAWNED, and
        # `src/utils/bootScrub.js` scrubs the previous student's identity out of
        # storage before Redux or supabase-js can read it.
        #
        # A NONCE and not a bare flag, for two independent reasons:
        #   * useVersionCheck reloads the page on every image update and
        #     `location.reload()` KEEPS the query string, so a constant flag
        #     would re-fire the scrub and sign a student out mid-lesson. The SPA
        #     latches the nonce in sessionStorage, which survives a reload;
        #   * that latch must not be able to fail CLOSED. A new spawn always
        #     carries a new nonce, so it scrubs even if the latch somehow
        #     outlived the window.
        # Not a secret — it identifies a window, it does not authorise anything.
        params.append(f"fresh={secrets.token_hex(8)}")
        url = f"http://localhost:{PORT_WEB_UI}/?{'&'.join(params)}"

        icon = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets", "icon.ico",
        )
        icon_path = icon if os.path.isfile(icon) else None

        # `open_student_window` SHORT-CIRCUITS `return True` when a child is
        # already alive — deliberately, because relaunching would destroy the
        # student's unsaved Blockly work. The behaviour is right; the message
        # was a claim about something that did not happen: nothing opened,
        # nothing came to the foreground, and the freshly minted
        # `?fresh=<nonce>` was discarded. `has_live_window()` is the sanctioned
        # read of that state (PROCESS-LOCAL, like `destroy_all`), so say the
        # true thing instead. The short-circuit inside `open_student_window`
        # STAYS — this only replaces a false success message with an honest one.
        if webview_window.has_live_window():
            self._log("[INFO] Das EduBotics-Fenster ist bereits geöffnet.")
            return

        if webview_window.open_student_window(url, icon_path=icon_path):
            self._log("Web-Oberfläche wird im EduBotics-Fenster geöffnet.")
            # Give the worker thread a moment to fail fast if WebView2
            # runtime is missing, then fall back gracefully.
            self.root.after(2000, self._check_webview_fallback, url)
            return

        self._webview_fallback(url)

    def _check_webview_fallback(self, url: str):
        """If the webview worker reported a missing runtime, fall back."""
        if webview_window.runtime_missing():
            self._webview_fallback(url)

    def _webview_fallback(self, url: str):
        """Open the system browser as a last-resort fallback."""
        self._log("[WARNUNG] WebView2 nicht verfügbar — System-Browser wird geöffnet.")
        messagebox.showwarning(
            "WebView2 nicht verfügbar",
            "Das Microsoft Edge WebView2-Runtime wurde nicht gefunden.\n"
            "Die Web-Oberfläche wird stattdessen im Standard-Browser geöffnet.",
        )
        webbrowser.open(url)

    # ── Roboter Studio leader toggle (React → GUI bridge) ────────────────────

    def _start_rs_control_server(self, use_gpu: bool, phone_enabled: bool):
        """Start the localhost control bridge the React Roboter Studio tab uses
        to switch the arm between follower-only and both-arms. Idempotent; a
        failure (e.g. port taken) just disables the toggle, never crashes."""
        self._rs_use_gpu = bool(use_gpu)
        self._rs_phone_enabled = bool(phone_enabled)
        # Follower-only robot types have no leader, so there is nothing to
        # toggle — the React LeaderToggle self-hides and the :8769 bridge is not
        # started at all (the endpoint stays closed).
        if ROBOT_PROFILES.get(self._rs_robot_type, {}).get("follower_only", False):
            self._log(
                "Roboter Studio (nur Follower): Leader-Umschaltung nicht nötig — "
                "Steuerbrücke wird nicht gestartet.")
            return
        if self._rs_control is not None:
            return
        try:
            self._rs_control = RoboterStudioControlServer(
                on_set_mode=self._rs_set_leader_mode,
                get_status=self._rs_get_status,
                get_task_busy=self._rs_get_task_busy,
                log=self._log,
            )
            if self._rs_control.start():
                self._log(
                    f"Roboter-Studio-Steuerung aktiv (Port {self._rs_control.port}).")
            else:
                self._rs_control = None
        except Exception as e:  # noqa: BLE001 — never let it block startup
            self._log(f"Roboter-Studio-Steuerung konnte nicht starten: {e}")
            self._rs_control = None

    def _stop_rs_control_server(self):
        if self._rs_control is not None:
            try:
                self._rs_control.stop()
            except Exception:  # noqa: BLE001
                pass
            self._rs_control = None

    def _rs_get_status(self) -> dict:
        """Current arm mode for the React toggle badge.

        Reports the TRUE running state, not just the optimistic .env: while a
        switch is in flight the .env already holds the target mode but the arm
        container is still restarting, so we surface ``ready: False`` and
        ``busy: True`` and never claim the new mode is live until the arm
        container is actually running again."""
        val = config_generator.read_env_var("EDUBOTICS_FOLLOWER_ONLY", ENV_FILE)
        follower_only = str(val).strip() == "1"
        if self._rs_switch_in_flight:
            return {"follower_only": follower_only, "ready": False, "busy": True}
        try:
            arm_status = docker_manager.get_container_status().get(
                "open_manipulator", "not found")
        except Exception:  # noqa: BLE001 — never let the probe break the badge
            arm_status = "error"
        return {"follower_only": follower_only,
                "ready": arm_status == "running"}

    def _rs_get_task_busy(self) -> bool:
        """Whether a recording / inference run is active (the bridge refuses a
        mode switch then — restarting the arm container mid-run corrupts the
        episode). The GUI does not subscribe to ROS task status, so this is the
        conservative GUI-side signal: a switch already in flight counts as busy
        so a re-entrant restart can't slip past the bridge's own busy lock."""
        return bool(self._rs_switch_in_flight)

    def _rs_set_leader_mode(self, follower_only: bool, log):
        """Switch the arm to follower-only (leader off) or both-arms (leader on)
        and recreate ONLY the open_manipulator container. Runs on the control
        server's thread; returns (ok, german_message).

        Reuses the scanned hardware + the gpu/phone settings captured at env
        start, so the regenerated .env is identical except FOLLOWER_ONLY /
        LEADER_PORT. physical_ai_server (rosbridge + the React app) stays up via
        --no-deps, so the student's session only sees the arm blip + re-home;
        the native camera bridge auto-reconnects to the recreated ingest node.

        On a restart FAILURE the .env is ROLLED BACK to the previous mode so the
        badge + a later "Umgebung starten" don't lie about a dead arm."""
        # Belt-and-suspenders: a follower-only robot type never starts the :8769
        # bridge (see _start_rs_control_server), so this should be unreachable —
        # refuse loudly rather than silently rewrite the type to omx_full.
        if ROBOT_PROFILES.get(self._rs_robot_type, {}).get("follower_only", False):
            return False, ("Umschalten ist für diesen Robotertyp nicht verfügbar — "
                           "Roboter Studio läuft bereits ohne Leader-Arm.")
        if self.hardware is None or self.hardware.follower is None:
            return False, "Kein Follower-Arm konfiguriert."
        if not follower_only and self.hardware.leader is None:
            return False, "Kein Leader-Arm konfiguriert — bitte beide Arme scannen."
        # Remember the mode we are leaving so we can restore it on failure.
        prev_val = config_generator.read_env_var("EDUBOTICS_FOLLOWER_ONLY", ENV_FILE)
        prev_follower_only = str(prev_val).strip() == "1"
        try:
            # EDUBOTICS_ROBOT_TYPE is MANAGED (stripped + re-emitted) — pass the
            # session's type on BOTH the forward regen AND the rollback below, or
            # a switch would silently rewrite it to the default (omx_full).
            config_generator.generate_env_file(
                self.hardware, ENV_FILE,
                phone_camera=self._rs_phone_enabled,
                robot_type=self._rs_robot_type,
                follower_only=follower_only,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"Konfiguration konnte nicht erstellt werden: {e}"
        if follower_only:
            log("Leader-Arm wird abgeschaltet — Roboter Studio wird vorbereitet …")
        else:
            log("Leader-Arm wird wieder verbunden — Teleoperation wird vorbereitet …")
        self._rs_switch_in_flight = True
        try:
            ok = docker_manager.restart_open_manipulator(gpu=self._rs_use_gpu, log=log)
        finally:
            self._rs_switch_in_flight = False
        if not ok:
            # Roll the .env back to the mode that was actually running before, so
            # the status badge and the next env-start reflect reality and the
            # student can retry from a consistent state.
            try:
                config_generator.generate_env_file(
                    self.hardware, ENV_FILE,
                    phone_camera=self._rs_phone_enabled,
                    robot_type=self._rs_robot_type,
                    follower_only=prev_follower_only,
                )
                log("Moduswechsel fehlgeschlagen — Konfiguration zurückgesetzt.")
            except Exception as e:  # noqa: BLE001
                log(f"Rücksetzen der Konfiguration fehlgeschlagen: {e}")
            return False, ("Der Arm-Container konnte nicht neu gestartet werden — "
                           "der vorherige Modus bleibt aktiv.")
        msg = ("Roboter Studio bereit — der Leader-Arm ist abgeschaltet."
               if follower_only else
               "Leader-Arm verbunden — Teleoperation ist wieder verfügbar.")
        return True, msg

    def _stop_environment(self):
        """Alle Container in der EduBotics-Umgebung stoppen."""
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_open_browser.config(state=tk.DISABLED)

        def _do_stop():
            self._set_status("Container werden gestoppt...")
            self._log("Container werden gestoppt...")
            self.root.after(0, lambda: self.progress.start(10))

            self._stop_camera_bridge()
            self._stop_phone_server()
            self._stop_rs_control_server()
            # CLOSE THE BROWSER WINDOW TOO. Two reasons, and the second is the
            # load-bearing one:
            #
            #   * it is pointing at a stack that is about to be torn down —
            #     rosbridge dies with the containers, so the window can only
            #     show „Getrennt" from here on;
            #   * STUDENT HANDOVER. „Umgebung stoppen" then „Umgebung starten"
            #     is the primary documented lifecycle — the two buttons the
            #     product is operated with — and `_do_start` calls
            #     `_open_webview()` at the end of it. With the old window still
            #     alive, `webview_window.open_student_window` hits its
            #     live-child short-circuit, DISCARDS the freshly minted
            #     `?fresh=<nonce>` URL and returns True, so the GUI logs
            #     „Web-Oberfläche wird im EduBotics-Fenster geöffnet." while the
            #     next student is looking at the previous one's window — with
            #     their live, self-refreshing Supabase session. The boot scrub
            #     never runs, because no document ever loads.
            #
            # This was equally true of the profile `rmtree` that preceded the
            # scrub: it sat BELOW the same short-circuit, so it never ran on
            # this path either. Closing the child here is what makes the next
            # „Umgebung starten" spawn a real new window.
            webview_window.destroy_all()
            if self.cloud_only.get():
                docker_manager.stop_cloud_only(log=self._log)
            else:
                docker_manager.stop_containers(gpu=self.gpu_available)
            docker_manager.stop_keepalive()

            self._log("Alle Container gestoppt.")
            self._set_status("Gestoppt")
            # `self.running`, the spinner and the button refresh are reset in
            # `_do_stop_guarded`'s `finally`.

        def _do_stop_guarded():
            # `_do_stop`'s BODY is deliberately untouched: test_shutdown_
            # teardown pins `webview_window.destroy_all()` inside it, ordered
            # before the container stop, under the qualified name
            # `_stop_environment._do_stop`. A raise in any of the six teardown
            # calls used to leave `self.running` True with „Stoppen" and
            # „Umgebung starten" both dead for the rest of the session.
            try:
                _do_stop()
            except Exception as exc:  # noqa: BLE001 — a worker must never strand the UI
                self._log(f"[FEHLER] Das Stoppen der Umgebung wurde "
                          f"abgebrochen: {exc}")
                self._set_status("Stoppen fehlgeschlagen — siehe Protokoll")
            finally:
                self.running = False
                self._update_start_button()
                self.root.after(0, lambda: self.progress.stop())

        threading.Thread(target=_do_stop_guarded, daemon=True).start()

    def _factory_reset(self):
        """Daten-Volumes löschen (Datensätze, Modelle, Kalibrierung).

        Die Distro und das Programm bleiben installiert — gelöscht werden
        nur die persistenten Docker-Volumes. Der Uninstaller hat seinen
        eigenen (Check:-gesteuerten) Dialog für die komplette Distro.
        """
        confirmed = messagebox.askyesno(
            "Daten zurücksetzen",
            "Sollen wirklich ALLE EduBotics-Daten gelöscht werden?\n\n"
            "Unwiderruflich entfernt werden:\n"
            "  - aufgenommene Datensätze (falls nicht zu Hugging Face hochgeladen)\n"
            "  - heruntergeladene Modelle\n"
            "  - die Roboter-Studio-Kalibrierung\n\n"
            "Tipp: Datensätze vorher in der Web-Oberfläche zu Hugging Face hochladen.\n\n"
            "Das Programm und die EduBotics-Umgebung bleiben installiert.",
            default=messagebox.NO,
        )
        if not confirmed:
            self._log("Daten zurücksetzen abgebrochen — nichts gelöscht.")
            return

        self.btn_factory_reset.config(state=tk.DISABLED)
        self.btn_start.config(state=tk.DISABLED)

        def _do_reset():
            self._set_status("Daten werden zurückgesetzt...")
            self.root.after(0, lambda: self.progress.start(10))
            ok, msg = docker_manager.factory_reset(log=self._log)
            self._log(("" if ok else "[FEHLER] ") + msg)
            if ok:
                self._log(
                    "Beim nächsten „Umgebung starten“ werden die Daten-Volumes "
                    "leer neu angelegt."
                )
                self._set_status("Daten zurückgesetzt")
            else:
                self._set_status("Daten zurücksetzen fehlgeschlagen")
            # Spinner + button refresh live in `_do_reset_guarded`'s `finally`.

        def _do_reset_guarded():
            # `_factory_reset` disables BOTH „Daten zurücksetzen" and
            # „Umgebung starten" before spawning this, and only
            # `_update_start_button` re-enables them — so a raise out of
            # `docker_manager.factory_reset` left the student with no way to
            # start the environment again short of restarting the GUI.
            try:
                _do_reset()
            except Exception as exc:  # noqa: BLE001 — a worker must never strand the UI
                self._log(f"[FEHLER] Das Zurücksetzen der Daten wurde "
                          f"abgebrochen: {exc}")
                self._set_status("Daten zurücksetzen fehlgeschlagen — siehe Protokoll")
            finally:
                self.root.after(0, lambda: self.progress.stop())
                self._update_start_button()

        threading.Thread(target=_do_reset_guarded, daemon=True).start()

    def _on_close(self):
        """Fenster-Schließen abfangen — Container stoppen, wenn nötig.

        `self.running` IS NOT A RELIABLE "the stack is up" SIGNAL. `_do_start`'s
        `finally` clears it on any failure occurring AFTER `start_containers`
        succeeded, so the flag reads False over a live stack — and the old
        `running == False` branch then skipped the container stop, the camera
        bridge and the phone server, leaving all three behind. So the close ASKS
        DOCKER instead, and only falls back to the flag as the fast path.

        The probe is one bounded `docker ps` and fails CLOSED (see
        `docker_manager.any_container_running`): a broken probe must never turn
        closing the GUI into a modal the student cannot get past.
        """
        self._stop_camera_previews()

        # Fast path first: if the flag already says yes there is nothing to
        # probe, and we keep the close instant in the common case.
        stack_up = bool(self.running) or docker_manager.any_container_running()

        if stack_up:
            if not messagebox.askyesno(
                "EduBotics beenden",
                "Die Umgebung läuft noch.\nContainer stoppen und beenden?",
            ):
                return  # user clicked No — don't close
            self._log("Beende — Container werden gestoppt...")

        # UNCONDITIONAL from here. Every one of these is a resource THIS GUI
        # process owns, and each is idempotent and independently guarded, so
        # there was never a reason to tie them to the container state:
        #   * the camera bridge holds both USB cameras via MSMF,
        #   * the phone receiver holds 0.0.0.0:8444,
        #   * the Roboter-Studio bridge holds 127.0.0.1:8769 and a thread — it
        #     starts BEFORE _open_webview, so a partial start leaves it up,
        #   * the webview child is now closed GRACEFULLY (WM_CLOSE first) so the
        #     browser's teardown hooks — the Jetson release beacon, JogPanel's
        #     re-torque — actually run.
        self._stop_camera_bridge()
        self._stop_phone_server()
        self._stop_rs_control_server()
        webview_window.destroy_all()

        if stack_up:
            if self.cloud_only.get():
                docker_manager.stop_cloud_only()
            else:
                docker_manager.stop_containers(gpu=self.gpu_available)

        docker_manager.stop_keepalive()
        self.root.destroy()


_SINGLE_INSTANCE_MUTEX = "Local\\EduBotics_GUI_SingleInstance"


def _acquire_single_instance():
    """Windows named-mutex single-instance guard for the MAIN GUI.

    Returns a handle (keep it referenced for the process lifetime) when this is
    the first main instance, or None when another main GUI already runs. Prevents
    a double-launch (desktop shortcut + an auto-update relaunch) from BOTH
    hitting the forced-update modal and downloading into the SAME fixed installer
    path — interleaved writes corrupt the .exe that is then launched elevated —
    or dual-running the elevated installer.

    The `--webview` child is a second EduBotics.exe and must NOT be blocked: it
    never calls run(), and we also guard on argv so a future direct call can't
    self-block. Non-Windows or any ctypes failure returns a truthy sentinel — the
    guard is a convenience, never a correctness dependency, so it fails OPEN.
    """
    if sys.platform != "win32":
        return True
    if "--webview" in sys.argv[1:]:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
        last_err = ctypes.get_last_error()
        if not handle:
            return True  # couldn't create the mutex — fail open
        if last_err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return True


def _focus_existing_window():
    """Best-effort: bring an already-running EduBotics GUI to the foreground."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "EduBotics")
        if hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def run():
    """GUI-Anwendung starten."""
    instance_lock = _acquire_single_instance()
    if instance_lock is None:
        # Another EduBotics GUI is already running — focus it and exit instead
        # of racing the update download / dual-launching the elevated installer.
        _focus_existing_window()
        return
    root = tk.Tk()
    # Match Tk's layout scale to the monitor DPI, but ONLY on a process Windows
    # is already treating as DPI-aware — see `theme.apply_scaling`. A no-op
    # everywhere else, including every non-Windows host.
    theme_mod.apply_scaling(root)
    app = EduBoticsApp(root)
    # Pin the mutex handle to the app for the process lifetime — letting it get
    # GC'd (and its handle closed) would release the guard while the GUI is up.
    app._instance_lock = instance_lock
    root.mainloop()
