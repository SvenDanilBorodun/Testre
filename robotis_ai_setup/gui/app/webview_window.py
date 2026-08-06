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
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from .constants import WEBVIEW_PROFILE_DIR, WEBVIEW_PROFILE_LEAF

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


def wipe_browser_profile(profile_dir: str = WEBVIEW_PROFILE_DIR) -> bool:
    """Delete the WebView2 profile so a new student inherits nothing.

    WHY AT SPAWN, not at close. `destroy_all` uses `Popen.terminate()`, which
    on Windows is `TerminateProcess` — it delivers neither WinForms'
    `FormClosed` nor the DOM's `pagehide`/`beforeunload`, so no close hook can
    be made to run. A close can also be a crash or a power cut. Spawn always
    runs, so that is where the guarantee has to live.

    WHAT IT FIXES. The profile is a persistent disk store (`private_mode=False`,
    deliberately kept — see run_in_process). It holds the student's Supabase
    session, which auth-js creates with BOTH `persistSession` and
    `autoRefreshToken` — a durable, self-renewing credential rather than an
    expiring JWT. On one shared Windows account, student A closing the window
    without clicking „Abmelden" (the path students actually take) left that
    credential for student B, authorising `/me/export`, `POST /me/delete` and
    `/trainings/start` against A's workgroup credits.

    SAFETY. This is an rmtree, and its sibling `%LOCALAPPDATA%\\EduBotics`
    holds `.env` — the HuggingFace token and the arm ports — plus
    `phone-cert/`. So it REFUSES any directory whose basename is not the
    dedicated leaf, and refuses a filesystem root outright. Best-effort by
    design: a locked file must not stop the student's browser from opening.

    Returns True when the profile is gone (or was never there).
    """
    try:
        target = os.path.abspath(profile_dir)
    except (OSError, ValueError):
        log.warning("WebView-Profilpfad ist ungültig — Wipe übersprungen.")
        return False

    # Refuse anything that is not our dedicated leaf. A misconfigured override
    # pointing at %LOCALAPPDATA%\EduBotics would otherwise delete the .env.
    if os.path.basename(target.rstrip("\\/")) != WEBVIEW_PROFILE_LEAF:
        log.error(
            "WebView-Profil wird NICHT geloescht: %r ist nicht der dafuer "
            "vorgesehene Ordner %r.", target, WEBVIEW_PROFILE_LEAF)
        return False
    if os.path.dirname(target.rstrip("\\/")) in ("", os.path.sep):
        log.error("WebView-Profil wird NICHT geloescht: Pfad liegt zu weit oben.")
        return False

    if not os.path.exists(target):
        return True
    try:
        shutil.rmtree(target, ignore_errors=False)
        log.info("WebView-Profil geloescht (Schuelerwechsel): %s", target)
        return True
    except Exception as exc:  # noqa: BLE001
        # Best-effort: never block the browser from opening. A partial wipe is
        # still better than none, and the next launch retries.
        shutil.rmtree(target, ignore_errors=True)
        log.warning("WebView-Profil konnte nicht vollstaendig geloescht werden: %s", exc)
        return not os.path.exists(target)


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

        # Student handover: wipe the persistent browser profile before the new
        # window exists. Placed immediately after the live-child short-circuit
        # so re-clicking „Web-Oberfläche öffnen" while a window is OPEN never
        # yanks the profile out from under the student currently using it —
        # that path returned above. See wipe_browser_profile for why this
        # cannot be a close-time hook.
        wipe_browser_profile()

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


def destroy_all() -> None:
    """Kill the webview subprocess if running. Safe to call from any thread."""
    global _process
    with _lock:
        if _process and _process.poll() is None:
            _deliberate_stop.set()
            try:
                _process.terminate()
                _process.wait(timeout=3)
            except Exception:
                try:
                    _process.kill()
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
            # the app's own reload/version-check flow depends on it). What
            # changed in 2026-08-06 is WHERE it persists and that it is wiped
            # BETWEEN students — see wipe_browser_profile.
            private_mode=False,
            # Explicit, and the SAME constant open_student_window wipes. The
            # default would be %APPDATA%\pywebview, which ROAMS: on a school PC
            # with roaming profiles / FSLogix / AppData redirection the
            # student's live Supabase session followed them to every other PC.
            # Drift between this argument and the wipe target is SILENT — a
            # wiped directory nobody uses — so it is fenced by an AST test
            # (tests/test_webview_profile_wipe.py).
            storage_path=WEBVIEW_PROFILE_DIR,
            icon=icon_path if icon_path else None,
        )
        return 0
    except Exception as exc:
        log.error("WebView2-Fenster konnte nicht gestartet werden: %s", exc)
        return 3
