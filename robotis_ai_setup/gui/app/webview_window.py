"""Eingebettetes WebView2-Fenster für die EduBotics Web-Oberfläche.

Ersetzt den Systembrowser durch ein natives Fenster auf Basis von
Microsoft Edge WebView2 (auf Windows 11 vorinstalliert). Wir verwenden
pywebview mit dem `edgechromium`-Backend.

## Architektur — Subprozess

pywebview 6.x erzwingt, dass `webview.start()` auf dem Haupt-Thread läuft.
Der Haupt-Thread unseres Prozesses gehört aber bereits dem tkinter-Mainloop.
Statt die Architektur zu invertieren, starten wir pywebview in einem
*Kindprozess* (`subprocess.Popen`). Der Kindprozess hat seinen eigenen
Haupt-Thread, auf dem pywebview glücklich ist.

Vorteile:
  - Saubere Trennung: Absturz im Web-Fenster bringt nicht die Setup-GUI mit.
  - Keine Thread-Konflikte (WinForms STA, COM Apartments, etc.).
  - Das Schließen des Web-Fensters ist vom tkinter-Fenster entkoppelt.

Der Kindprozess wird mit `sys.executable --webview --url <url> ...`
gestartet. `main.py` erkennt das Sentinel-Flag und dispatcht auf
`run_in_process()` (siehe unten).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from .constants import WEBVIEW_PROFILE_DIR

log = logging.getLogger(__name__)

_lock = threading.Lock()
_process: Optional[subprocess.Popen] = None
_runtime_missing = threading.Event()
_deliberate_stop = threading.Event()

# Sentinel argv flag that main.py listens for to run the webview loop in-process.
WEBVIEW_FLAG = "--webview"


def is_available() -> bool:
    """Return True if pywebview and pythonnet can be imported in THIS process.

    Even though pywebview runs in a subprocess, both modules live in the same
    site-packages / frozen bundle, so testing import here is sufficient.
    """
    try:
        import webview  # noqa: F401
        import clr  # noqa: F401  (pythonnet)
        return True
    except Exception as exc:
        log.warning("WebView2 nicht verfügbar: %s", exc)
        return False


def runtime_missing() -> bool:
    """Signal set when the webview subprocess crashed (missing runtime, etc.)."""
    return _runtime_missing.is_set()


def _build_launch_cmd(url: str, icon_path: Optional[str]) -> List[str]:
    """Build the command used to spawn the webview subprocess."""
    debug = "1" if os.environ.get("EDUBOTICS_DEBUG") else "0"
    icon = icon_path or ""

    if getattr(sys, "frozen", False):
        # PyInstaller: re-invoke the same EduBotics.exe with the sentinel flag.
        return [sys.executable, WEBVIEW_FLAG, "--url", url, "--icon", icon, "--debug", debug]

    # Source checkout: re-invoke python main.py so main.py's dispatcher picks it up.
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    return [
        sys.executable,
        str(main_py),
        WEBVIEW_FLAG,
        "--url", url,
        "--icon", icon,
        "--debug", debug,
    ]


def open_student_window(url: str, icon_path: Optional[Path] = None) -> bool:
    """Open the EduBotics web UI in an embedded WebView2 window.

    Returns True if a webview subprocess was launched successfully (or was
    already running). Returns False only if pywebview/pythonnet are missing
    from this environment — in that case the caller should fall back to the
    system browser.
    """
    global _process

    if not is_available():
        return False

    with _lock:
        if _process and _process.poll() is None:
            # Already running. We can't easily navigate the existing window
            # without an IPC channel; relaunching would stack windows.
            # Accept this limitation: the existing window stays foremost.
            log.info("WebView-Subprozess läuft bereits (PID %d).", _process.pid)
            return True

        _runtime_missing.clear()
        _deliberate_stop.clear()
        icon_str = str(icon_path) if icon_path else None
        cmd = _build_launch_cmd(url, icon_str)

        try:
            creationflags = 0
            if sys.platform == "win32":
                # CREATE_NO_WINDOW keeps a stray console from flashing when
                # running as a python interpreter (harmless in frozen EXE).
                creationflags = subprocess.CREATE_NO_WINDOW
            _process = subprocess.Popen(cmd, creationflags=creationflags)
            log.info("WebView-Subprozess gestartet (PID %d): %s", _process.pid, cmd)
        except Exception as exc:
            log.error("Konnte WebView-Subprozess nicht starten: %s", exc)
            _runtime_missing.set()
            _process = None
            return False

    # Watch the subprocess: a non-zero exit within ~3s usually means the
    # WebView2 Evergreen runtime is missing on the host machine.
    threading.Thread(
        target=_watch_subprocess,
        args=(_process,),
        daemon=True,
        name="edubotics-webview-watchdog",
    ).start()

    return True


def _watch_subprocess(proc: subprocess.Popen) -> None:
    rc = proc.wait()
    # Exit code 0   = user closed the window normally.
    # Non-zero      = either a real crash (e.g. missing WebView2 runtime) OR
    #                 we deliberately called destroy_all(). Only flag as
    #                 runtime-missing when the stop was NOT deliberate.
    if rc != 0 and not _deliberate_stop.is_set():
        log.warning("WebView-Subprozess endete unerwartet mit Code %d", rc)
        _runtime_missing.set()


# How long the child gets to close itself after WM_CLOSE before we terminate it.
# The work being waited for is short by construction — `navigator.sendBeacon`
# hands the Jetson release to the browser's own delivery queue and returns, and
# JogPanel's re-torque is a single rosbridge service call — but a WinForms
# FormClosed -> WebView2 teardown -> DOM pagehide chain is several hops. 2.5 s
# is a compromise: long enough for that chain on a busy classroom PC, short
# enough that closing EduBotics still feels immediate. The wait ENDS the moment
# the child exits, so a healthy close costs a few hundred milliseconds.
GRACEFUL_CLOSE_TIMEOUT_S = 2.5
_GRACEFUL_POLL_S = 0.05


def _post_close_to_pid(pid: int) -> int:
    """Post WM_CLOSE to every top-level window owned by `pid`. Windows only.

    MATCHED BY PID, NEVER BY TITLE, and that is the load-bearing part.
    `gui_app.py`'s tkinter root and this pywebview child are BOTH titled
    exactly "EduBotics" — `_focus_existing_window`'s `FindWindowW(None,
    "EduBotics")` is already ambiguous today — so a close-by-title could shut
    the setup GUI instead of the browser. `EnumWindows` yields only top-level
    windows and `GetWindowThreadProcessId` names their owner, so the parent's
    windows are structurally out of reach.

    Returns the number of windows posted to; 0 off Windows, and 0 when the
    child has no window yet (a crash during startup), which lets the caller
    skip the grace period entirely.
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

        hwnds = []

        def _collect(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                hwnds.append(hwnd)
            return True

        # The callback must stay referenced for the whole EnumWindows call.
        cb = WNDENUMPROC(_collect)
        user32.EnumWindows(cb, 0)

        WM_CLOSE = 0x0010
        for hwnd in hwnds:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return len(hwnds)
    except Exception as exc:  # noqa: BLE001
        # Best-effort by design: the terminate below is the backstop, so a
        # ctypes/user32 problem must never stop the GUI from closing.
        log.warning("WM_CLOSE an das WebView-Fenster fehlgeschlagen: %s", exc)
        return 0


def destroy_all() -> None:
    """Close the webview subprocess if running. Safe to call from any thread.

    GRACEFUL FIRST, then terminate. `Popen.terminate()` alone is
    `TerminateProcess` on Windows, which delivers neither WinForms'
    `FormClosed` nor the DOM's `pagehide`/`beforeunload` — so EVERY
    browser-side teardown hook was dead, with three shipped consequences:

      * `useJetsonConnection`'s release beacon never fired, so the exclusive
        claim leaked for the full 5-minute sweeper window and THE NEXT STUDENT
        WAS REFUSED THE JETSON;
      * `JogPanel`'s unmount re-torque never ran, leaving the follower limp
        until the 30 s `_manual_idle_watchdog` caught it;
      * `RecordPanel`'s teardown never ran.

    The handlers already existed and were correct — only delivery was broken.
    `create_window(confirm_close=False)` means WM_CLOSE is not answered by a
    dialog, so nothing can wedge on the grace window.

    The terminate path is KEPT, unchanged, as the backstop: off Windows there
    is no WM_CLOSE at all, a child that crashed before creating a window has
    nothing to post to, and a hung renderer must not keep the GUI open.
    """
    global _process
    with _lock:
        proc = _process
        if proc and proc.poll() is None:
            _deliberate_stop.set()
            try:
                if _post_close_to_pid(proc.pid) > 0:
                    deadline = time.monotonic() + GRACEFUL_CLOSE_TIMEOUT_S
                    while time.monotonic() < deadline:
                        if proc.poll() is not None:
                            break
                        time.sleep(_GRACEFUL_POLL_S)
            except Exception as exc:  # noqa: BLE001
                log.warning("Sanftes Schließen fehlgeschlagen: %s", exc)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _process = None


# ---------------------------------------------------------------------------
# Subprocess entry point — invoked via `python main.py --webview ...`.
# `main.py` detects the `--webview` sentinel in sys.argv and calls this.
# ---------------------------------------------------------------------------

def run_in_process(url: str, icon_path: str = "", debug: bool = False) -> int:
    """Blocking call — runs the webview event loop on THIS process's main thread.

    Returns an exit code suitable for sys.exit().
    """
    try:
        import webview
    except Exception as exc:
        log.error("pywebview konnte nicht geladen werden: %s", exc)
        return 2

    try:
        webview.create_window(
            title="EduBotics",
            url=url,
            width=1400,
            height=900,
            min_size=(1024, 700),
            maximized=True,
            confirm_close=False,
            text_select=True,
            frameless=False,
        )
        webview.start(
            gui="edgechromium",
            debug=bool(debug),
            # private_mode=False is DELIBERATELY kept: the profile must persist
            # WITHIN a session (WebView2 needs a writable user-data dir, and
            # the app's own reload/version-check flow depends on it).
            private_mode=False,
            # EXPLICIT and non-roaming, and that is the half of the 2026-08-06
            # handover fix that lives here. pywebview's default is
            # %APPDATA%\pywebview, which ROAMS: on a school PC with roaming
            # profiles / FSLogix / AppData redirection the student's live
            # Supabase session followed them to every other PC in the building.
            #
            # What is NOT here any more is an rmtree of this directory. It held
            # localStorage AND IndexedDB, so deleting it destroyed the Blockly
            # crash-recovery autosave and every machine-scoped key — including
            # `edubotics_robotType`, whose loss costs the next student an arm
            # re-scan — and it fired for a student who merely closed the window
            # and re-opened it mid-lesson. The handover scrub is now done by the
            # SPA at boot, against the STUDENT/MACHINE partition
            # `src/utils/sessionScope.js` exists to express: the GUI stamps
            # `?fresh=<nonce>` onto a freshly spawned window
            # (gui_app.py::_open_webview) and `src/utils/bootScrub.js` answers
            # it. See tests/test_webview_handover.py.
            storage_path=WEBVIEW_PROFILE_DIR,
            icon=icon_path if icon_path else None,
        )
        return 0
    except Exception as exc:
        log.error("WebView2-Fenster konnte nicht gestartet werden: %s", exc)
        return 3
