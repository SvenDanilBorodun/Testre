"""Haupt-GUI für EduBotics Setup.

Bietet eine schrittweise Oberfläche für:
  1. Erkennen und Identifizieren der Roboterarme (Leader/Follower)
  2. Kamera auswählen
  3. EduBotics-Umgebung starten/stoppen (Container in der EduBotics WSL2-Distro)
  4. EduBotics Web-Oberfläche in eingebettetem Fenster öffnen
"""

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import webbrowser


def _elevate_and_wait(exe: str, args: str, show: int = 1):
    """Run `exe args` elevated via UAC and wait for exit.

    Uses ShellExecuteExW (Win32) directly instead of PowerShell's
    `Start-Process -Verb RunAs -Wait`, which has known parameter-set
    conflicts and unreliable wait semantics.

    Returns (exit_code, cancelled, error_message). On non-Windows, returns
    (None, False, 'not supported').
    """
    if sys.platform != "win32":
        return None, False, "not supported"

    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC        = 0x00000100
    SEE_MASK_FLAG_NO_UI     = 0x00000400
    ERROR_CANCELLED         = 1223
    INFINITE                = 0xFFFFFFFF

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
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = args
    info.nShow = show

    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]

    ok = shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return None, True, "UAC abgebrochen"
        return None, False, f"ShellExecuteEx Fehler {err}"

    if not info.hProcess:
        return None, False, "Kein Prozess-Handle erhalten"

    kernel32.WaitForSingleObject(info.hProcess, INFINITE)
    exit_code = wintypes.DWORD(0)
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
    kernel32.CloseHandle(info.hProcess)
    return int(exit_code.value), False, None

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
from . import camera_bridge as camera_bridge_mod
from .constants import (
    APP_VERSION,
    UPDATE_API_URL,
    IMAGE_OPEN_MANIPULATOR,
    PORT_WEB_UI,
    DOCKER_DIR,
    ENV_FILE,
    cameras_use_native_bridge,
)


class EduBoticsApp:
    """Hauptfenster der Anwendung."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EduBotics")
        self.root.geometry("700x830")
        self.root.resizable(True, True)
        _apply_window_icon(self.root)

        # State
        self.hardware = device_manager.HardwareConfig()
        self.cameras: list[device_manager.CameraDevice] = []
        # Native camera capture bridge (Windows student path). Created on
        # environment start, stopped on environment stop / app close.
        self.camera_bridge = None
        self.gpu_available = False
        self.running = False
        self._scanning = False
        self._prerequisites_done = False
        # Cloud-only mode: skip arm/camera scan, only start physical_ai_manager
        # so the user can open the Cloud tab without any robot hardware.
        self.cloud_only = tk.BooleanVar(value=False)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._check_prerequisites()

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Title
        title = ttk.Label(main, text="EduBotics", font=("Segoe UI", 18, "bold"))
        title.pack(pady=(0, 5))

        # Status bar
        self.status_var = tk.StringVar(value="System wird geprüft...")
        status_label = ttk.Label(main, textvariable=self.status_var, font=("Segoe UI", 10))
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
        ttk.Label(
            mode_frame,
            text=(
                "Aktivieren, um nur die Cloud-Lehrer/Schüler-Anmeldung zu starten — "
                "Aufnahme und Inferenz benötigen weiterhin Roboter-Hardware."
            ),
            foreground="gray",
            font=("Segoe UI", 8),
            wraplength=620,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))

        # ── Schritt A: Leader-Arm ──
        leader_frame = ttk.LabelFrame(main, text="Schritt A: Leader-Arm", padding=10)
        leader_frame.pack(fill=tk.X, pady=5)
        self.leader_frame = leader_frame

        ttk.Label(leader_frame, text="Leader-Arm per USB anschließen, dann auf Scannen klicken.").pack(anchor=tk.W)
        leader_row = ttk.Frame(leader_frame)
        leader_row.pack(fill=tk.X, pady=5)

        self.btn_scan_leader = ttk.Button(leader_row, text="Arme scannen", command=self._scan_arms)
        self.btn_scan_leader.pack(side=tk.LEFT)

        self.leader_status_var = tk.StringVar(value="Nicht gescannt")
        ttk.Label(leader_row, textvariable=self.leader_status_var, foreground="gray").pack(side=tk.LEFT, padx=10)

        # ── Schritt B: Follower-Arm ──
        follower_frame = ttk.LabelFrame(main, text="Schritt B: Follower-Arm", padding=10)
        follower_frame.pack(fill=tk.X, pady=5)
        self.follower_frame = follower_frame

        ttk.Label(follower_frame, text="Follower-Arm per USB anschließen (wird zusammen mit Leader gescannt).").pack(anchor=tk.W)
        follower_row = ttk.Frame(follower_frame)
        follower_row.pack(fill=tk.X, pady=5)

        self.follower_status_var = tk.StringVar(value="Nicht gescannt")
        ttk.Label(follower_row, textvariable=self.follower_status_var, foreground="gray").pack(side=tk.LEFT, padx=10)

        # Guided repair area for the arms — populated by _show_arm_repair()
        # after a failed scan with the action button that matches the actual
        # failure (bind arms, repair usbipd, finish setup, cable/power hints).
        self.arm_repair_frame = ttk.Frame(follower_frame)
        self.arm_repair_frame.pack(fill=tk.X, pady=(4, 0))

        # ── Schritt C: Kameras ──
        camera_frame = ttk.LabelFrame(main, text="Schritt C: Kameras (bis zu 2)", padding=10)
        camera_frame.pack(fill=tk.X, pady=5)
        self.camera_frame = camera_frame

        ttk.Label(camera_frame, text="Kameras anschließen, scannen, und per Checkbox auswählen.").pack(anchor=tk.W)
        camera_row = ttk.Frame(camera_frame)
        camera_row.pack(fill=tk.X, pady=5)

        self.btn_scan_camera = ttk.Button(camera_row, text="Kameras scannen", command=self._scan_cameras)
        self.btn_scan_camera.pack(side=tk.LEFT)

        self.camera_checks_frame = ttk.Frame(camera_frame)
        self.camera_checks_frame.pack(fill=tk.X, pady=5)
        self.camera_check_vars: list[tk.BooleanVar] = []

        # Camera role assignment (visible after 2 cameras selected)
        self.camera_role_frame = ttk.Frame(camera_frame)
        self.camera_role_frame.pack(fill=tk.X, pady=5)
        self.gripper_cam_var = tk.StringVar()
        self.scene_cam_var = tk.StringVar()

        # ── Start-Button ──
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=15)

        self.btn_start = ttk.Button(
            btn_frame, text="Umgebung starten",
            command=self._start_environment,
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

        # ── Protokoll-Ausgabe ──
        log_frame = ttk.LabelFrame(main, text="Protokoll", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=12, state=tk.DISABLED,
            font=("Consolas", 9), wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ── Logging ──────────────────────────────────────────────────────

    def _log(self, msg: str):
        """Nachricht an die Protokoll-Ausgabe anhängen (thread-sicher)."""
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _append)

    def _set_status(self, msg: str):
        """Statusleiste aktualisieren (thread-sicher)."""
        self.root.after(0, lambda: self.status_var.set(msg))

    def _update_start_button(self):
        """Start-Button aktivieren wenn:
        - Cloud-only mode: prereqs ok, nicht laufend
        - Voller Modus: prereqs ok, beide Arme gefunden, nicht laufend
        """
        def _update():
            if not self._prerequisites_done or self.running:
                self.btn_start.config(state=tk.DISABLED)
                return
            if self.cloud_only.get():
                self.btn_start.config(state=tk.NORMAL)
            elif self.hardware.is_complete:
                self.btn_start.config(state=tk.NORMAL)
            else:
                self.btn_start.config(state=tk.DISABLED)
        self.root.after(0, _update)

    def _on_mode_changed(self):
        """Cloud-only checkbox toggled — show/hide hardware sections."""
        is_cloud_only = self.cloud_only.get()
        # Disable the hardware frames in cloud-only mode (visually grayed out).
        new_state = tk.DISABLED if is_cloud_only else tk.NORMAL
        for frame in (self.leader_frame, self.follower_frame, self.camera_frame):
            self._set_frame_state(frame, new_state)
        if is_cloud_only:
            self._set_status("Cloud-Modus — Start klicken, um die Web-Oberfläche zu starten.")
        else:
            if self.hardware.is_complete:
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
        def _check():
            self.root.after(0, lambda: self.progress.start(10))

            # Clean up leftover installer .exe from any previous update
            # (can't be deleted while it was running — sweep on next launch).
            try:
                removed = update_checker.cleanup_stale_installers()
                if removed > 0:
                    self._log(f"Aufraeumen: {removed} alte Installer-Dateien geloescht.")
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

        # Check the EduBotics WSL2 distro is installed and docker engine is up
        self._set_status("EduBotics-Umgebung wird geprüft...")
        if not docker_manager.is_distro_registered():
            self._log("EduBotics-Umgebung ist noch nicht eingerichtet.")
            self.root.after(0, lambda: self.progress.stop())
            # Offer one-click finalize (UAC prompt, runs finalize_install.ps1).
            self.root.after(0, self._prompt_finalize_install)
            return
        if not docker_manager.is_docker_running():
            self._log("EduBotics-Umgebung startet...")
            docker_manager.start_edubotics_distro()
            if not docker_manager.wait_for_docker(
                callback=lambda e, t: self._set_status(f"Warte auf EduBotics-Umgebung... {e}s/{t}s")
            ):
                self._log("FEHLER: EduBotics-Umgebung konnte nicht gestartet werden.")
                self._set_status("EduBotics-Umgebung nicht bereit")
                self.root.after(0, lambda: self.progress.stop())
                return
        # Pin the distro awake for the lifetime of this GUI session. Without
        # this, WSL2's vmIdleTimeout shuts the distro down ~60s after the
        # last wsl.exe call, killing dockerd and the manager container — the
        # embedded WebView then loads http://localhost:80/ into a dead port.
        docker_manager.start_keepalive()
        self._log("EduBotics-Umgebung: OK")

        # ── Lifecycle: the GUI is the SOLE owner of the container lifecycle ──
        # The robot stack must come up ONLY after the student has scanned both
        # arms and clicked "Umgebung starten" — never before. Two paths can
        # leave it running prematurely: (1) dockerd resurrects containers on
        # distro boot when they carry a `restart` policy other than "no"
        # (pre-2.5.3 images used `unless-stopped`), and the distro boots the
        # moment this GUI first touched WSL above; (2) a previous session left
        # the stack up. Either way the arm would be driven — and the Dynamixel
        # serial bus held — before any scan, which ALSO breaks the scan itself
        # (identify_arm.py collides with the live controller on the same
        # /dev/serial port → neither arm is identified). Tear it down now so
        # the student starts from a clean, deterministic state.
        self._set_status("Vorherige Sitzung wird aufgeräumt...")
        if docker_manager.ensure_environment_stopped(log=self._log):
            self._log("Umgebung gestoppt — sie startet erst, wenn du auf 'Umgebung starten' klickst.")

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
                self._log("FEHLER: Images konnten nicht heruntergeladen werden. Internetverbindung prüfen.")
                self._set_status("Image-Download fehlgeschlagen")
                self.root.after(0, lambda: self.progress.stop())
                return
            self._log("Alle Images erfolgreich heruntergeladen.")

        # Check for image updates. The function pre-checks network reachability
        # and per-image manifest digests so an offline classroom returns in ~5s
        # and an up-to-date one returns in ~3s — only stale or missing images
        # trigger a real layer pull.
        self._set_status("Auf Updates prüfen...")
        self._log("Prüfe auf Image-Updates...")
        if docker_manager.check_for_updates(log=self._log):
            self._log("Images auf neueste Version aktualisiert.")
        else:
            self._log("Images sind aktuell.")

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
                        f"  ⚠️ Image-Frische: letzter erfolgreicher Update vor "
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

        self._set_status("Reparatur läuft (UAC-Zustimmung erforderlich)...")
        self._log("usbipd-Reparatur wird mit Administrator-Rechten gestartet...")

        def _run_elevated():
            # We chain both scripts in a single elevated shell so the
            # student sees one UAC prompt, not two. The combined script
            # writes its transcript so the GUI can show what happened.
            import tempfile
            log_file = os.path.join(tempfile.gettempdir(), "edubotics_repair_usbipd.log")
            try:
                if os.path.isfile(log_file):
                    os.remove(log_file)
            except OSError:
                pass

            ps_cmd = (
                f'-NoProfile -ExecutionPolicy Bypass -Command '
                f'"Start-Transcript -Path \'{log_file}\' -Force | Out-Null; '
                f'& \'{prereq}\'; '
                f'$prereq_rc = $LASTEXITCODE; '
                f'& \'{configure}\'; '
                f'$cfg_rc = $LASTEXITCODE; '
                f'Stop-Transcript | Out-Null; '
                f'if ($prereq_rc -ne 0) {{ exit $prereq_rc }} '
                f'elseif ($cfg_rc -ne 0) {{ exit $cfg_rc }} '
                f'else {{ exit 0 }}"'
            )
            exit_code, cancelled, err = _elevate_and_wait(
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
                self.root.after(0, lambda: self._run_prerequisite_checks())
            else:
                if cancelled:
                    self._log("Reparatur abgebrochen (UAC-Zustimmung verweigert).")
                    self._set_status("Reparatur abgebrochen — erneut versuchen")
                else:
                    self._log(
                        f"Reparatur fehlgeschlagen (exit {exit_code}). "
                        "Bitte den EduBotics-Installer erneut ausführen."
                    )
                    self._set_status("Reparatur fehlgeschlagen — siehe Protokoll")

        threading.Thread(target=_run_elevated, daemon=True).start()

    # ── Finalize Install (post-reboot continuation) ─────────────────

    # ── ShellExecuteEx helper is defined at module scope below. ──────

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

    def _prompt_finalize_install(self):
        """Prompt the student to finalize setup after a post-reboot continuation.

        When WSL2 was installed fresh, the installer defers rootfs import until
        after reboot. On first GUI launch the distro is missing; we ask the
        student for admin consent and run finalize_install.ps1 with UAC.
        """
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

        self._log(f"Setup-Skript: {script}")

        wants_run = messagebox.askyesno(
            "Einrichtung abschließen",
            "Die EduBotics-Umgebung muss noch eingerichtet werden.\n\n"
            "Dies erfordert einmalig Administrator-Rechte und dauert "
            "3–10 Minuten (Rootfs-Import + Docker-Images).\n\n"
            "Jetzt einrichten?",
        )
        if not wants_run:
            self._set_status("EduBotics-Umgebung nicht eingerichtet")
            self._log("Einrichtung vom Benutzer verschoben.")
            return

        self._set_status("Einrichtung läuft (UAC-Zustimmung erforderlich)...")
        self._log("Einrichtung wird mit Administrator-Rechten gestartet...")

        def _run_elevated():
            import tempfile
            # The elevated finalize_install.ps1 writes:
            #   - a marker file on startup (proves it launched)
            #   - a transcript file with full stdout/stderr (Start-Transcript)
            # Using ShellExecuteEx directly (not nested Start-Process) so we
            # get a reliable process handle + deterministic wait.
            temp = tempfile.gettempdir()
            log_file = os.path.join(temp, "edubotics_finalize.log")
            marker_file = os.path.join(temp, "edubotics_finalize.marker")
            for f in (log_file, marker_file):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except OSError:
                    pass

            exit_code, cancelled, err = _elevate_and_wait(
                exe="powershell.exe",
                args=(
                    f'-NoProfile -ExecutionPolicy Bypass -File "{script}" '
                    f'-LogPath "{log_file}" -MarkerPath "{marker_file}"'
                ),
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

            # Re-check.
            if docker_manager.is_distro_registered():
                self._log("Einrichtung abgeschlossen. Systemprüfung wird fortgesetzt...")
                self.root.after(0, lambda: self._run_prerequisite_checks())
            else:
                if cancelled:
                    self._log("Einrichtung abgebrochen (UAC-Zustimmung verweigert).")
                    self._set_status("Einrichtung abgebrochen — erneut versuchen")
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
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(0, 5))

        ttk.Label(
            frame,
            text=f"Neue Version: {update_info['version']}  (aktuell: {APP_VERSION})",
            font=("Segoe UI", 10),
        ).pack(pady=(0, 5))

        ttk.Label(
            frame,
            text="Bitte aktualisiere EduBotics, bevor du fortfährst.",
            font=("Segoe UI", 9),
            foreground="gray",
        ).pack(pady=(0, 10))

        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(frame, variable=progress_var, maximum=100, length=400)
        progress_bar.pack(pady=(0, 5))

        status_var = tk.StringVar(value="")
        status_label = ttk.Label(frame, textvariable=status_var, font=("Segoe UI", 8))
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

            path = update_checker.download_installer(
                update_info["download_url"],
                progress_callback=_progress,
            )

            if path:
                self.root.after(0, lambda: status_var.set("Download abgeschlossen. Installer wird gestartet..."))
                self.root.after(500, lambda: self._launch_installer_and_exit(path))
            else:
                self._update_fail_count += 1
                self.root.after(0, lambda: progress_var.set(0))
                self.root.after(0, lambda: status_var.set(
                    "Download fehlgeschlagen. Bitte Internetverbindung prüfen."
                ))
                self.root.after(0, lambda: btn_update.config(
                    state=tk.NORMAL, text="Erneut versuchen"
                ))
                if self._update_fail_count >= 3:
                    self.root.after(0, lambda: btn_skip.config(state=tk.NORMAL))

        def _on_update_click():
            btn_update.config(state=tk.DISABLED)
            status_var.set("Download läuft...")
            progress_var.set(0)
            threading.Thread(target=_do_download, daemon=True).start()

        def _on_skip_click():
            dialog.destroy()
            self._log("WARNUNG: Update übersprungen. Einige Funktionen funktionieren möglicherweise nicht korrekt.")
            self._set_status("System wird geprüft...")
            threading.Thread(target=self._run_prerequisite_checks, daemon=True).start()

        btn_update.config(command=_on_update_click)
        btn_skip.config(command=_on_skip_click)

    def _launch_installer_and_exit(self, installer_path: str):
        """Launch the downloaded installer and exit the GUI."""
        self._log("Installer wird gestartet...")
        try:
            os.startfile(installer_path)
        except Exception as e:
            self._log(f"FEHLER: Installer konnte nicht gestartet werden: {e}")
            return
        import sys
        sys.exit(0)

    # ── Arm Scanning ─────────────────────────────────────────────────

    def _scan_arms(self):
        """Nach Roboterarmen scannen — läuft im Hintergrund."""
        if self._scanning:
            return
        self._scanning = True
        self.btn_scan_leader.config(state=tk.DISABLED)

        def _do_scan():
            self._set_status("Roboterarme werden gesucht...")
            self.root.after(0, lambda: self.progress.start(10))
            self._log("USB-Geräte werden nach Roboterarmen durchsucht...")

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
                self.running = False
                self.root.after(0, lambda: self.btn_stop.config(state=tk.DISABLED))
                self.root.after(0, lambda: self.btn_open_browser.config(state=tk.DISABLED))
                self.root.after(0, self._update_start_button)

            # Clear any repair button from a previous failed scan.
            self.root.after(0, self._clear_arm_repair)

            leader, follower = device_manager.scan_and_identify_arms(IMAGE_OPEN_MANIPULATOR)

            if leader:
                self.hardware.leader = leader
                self.root.after(0, lambda: self.leader_status_var.set(
                    f"Gefunden: {leader.description} ({leader.serial_path})"
                ))
                self._log(f"Leader gefunden: {leader.serial_path}")
            else:
                self.root.after(0, lambda: self.leader_status_var.set("Nicht gefunden"))
                self._log("Leader-Arm nicht gefunden.")

            if follower:
                self.hardware.follower = follower
                self.root.after(0, lambda: self.follower_status_var.set(
                    f"Gefunden: {follower.description} ({follower.serial_path})"
                ))
                self._log(f"Follower gefunden: {follower.serial_path}")
            else:
                self.root.after(0, lambda: self.follower_status_var.set("Nicht gefunden"))
                self._log("Follower-Arm nicht gefunden.")

            self._scanning = False
            self._update_start_button()
            self.root.after(0, lambda: self.progress.stop())
            self.root.after(0, lambda: self.btn_scan_leader.config(state=tk.NORMAL))

            if leader and follower:
                self._set_status("Beide Arme gefunden! Kamera auswählen und auf Start klicken.")
                return

            # Nothing (or only one) found — run the host→WSL→docker chain
            # diagnosis so the student sees *why*, not just "Nicht gefunden".
            self._log("Diagnose wird ausgeführt — bitte einen Moment...")
            try:
                diag = device_manager.diagnose_usb_environment(image=IMAGE_OPEN_MANIPULATOR)
            except Exception as exc:
                self._log(f"Diagnose fehlgeschlagen: {exc}")
                self._set_status("Einige Arme nicht gefunden. Verbindungen prüfen und erneut versuchen.")
                return

            # Log the German message line-by-line so the GUI log pane shows the
            # full bullet list, then a single short status-bar message.
            for line in diag.message_de.splitlines():
                self._log(line)
            if diag.details:
                self._log(f"(Technisch: {diag.details})")
            self._log(f"Vollständiges Diagnose-Log: {device_manager.get_diagnostics_log_path()}")

            short_status = diag.message_de.splitlines()[0] if diag.message_de else (
                "Einige Arme nicht gefunden. Verbindungen prüfen und erneut versuchen."
            )
            self._set_status(short_status)

            # Turn the diagnosis into a one-click guided repair button that
            # matches the actual failure, instead of leaving the student to
            # parse a wall of log text.
            self.root.after(0, lambda d=diag: self._show_arm_repair(d))

        threading.Thread(target=_do_scan, daemon=True).start()

    # ── Guided arm repair ────────────────────────────────────────────────

    def _clear_arm_repair(self):
        """Remove any repair widgets from a previous failed arm scan."""
        for w in self.arm_repair_frame.winfo_children():
            w.destroy()

    def _show_arm_repair(self, diag: "device_manager.UsbDiagnosis"):
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
                unbound = device_manager.list_unbound_robotis()
            except Exception:  # noqa: BLE001
                unbound = []
            if not unbound:
                # Fall back to every visible ROBOTIS device — binding an
                # already-bound one is a cheap no-op in bind_devices.ps1.
                try:
                    unbound = device_manager.list_robotis_devices()
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
            return
        self._scanning = True
        self.btn_scan_camera.config(state=tk.DISABLED)

        def _do_scan():
            self._set_status("Kameras werden gesucht...")
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
                        if blocked:
                            self._log(
                                "ACHTUNG: Windows blockiert den Kamera-Zugriff für "
                                "Desktop-Apps. Einstellungen → Datenschutz und "
                                "Sicherheit → Kamera → 'Desktop-Apps den Zugriff "
                                "auf Ihre Kamera erlauben' EINSCHALTEN, dann erneut scannen."
                            )
                            ttk.Button(
                                self.camera_checks_frame,
                                text="Windows-Kamera-Einstellungen öffnen",
                                command=self._open_camera_privacy_settings,
                            ).pack(anchor=tk.W, pady=(4, 0))
                        else:
                            self._log(
                                "Tipp: Kamera per USB anschließen und prüfen, ob sie im "
                                "Windows Geräte-Manager als 'Kamera' erscheint. "
                                "Bei Bedarf USB-Port wechseln, dann erneut scannen."
                            )

                # Unbound-cameras hint + repair button. Shows even when SOME
                # cameras were found, because the student may have plugged in
                # one bound + one unbound camera.
                if unbound:
                    summary = ", ".join(
                        f"{d.description or d.vid_pid}" for d in unbound
                    )
                    self._log(
                        f"Hinweis: {len(unbound)} Kamera(s) sichtbar aber nicht freigegeben: {summary}"
                    )
                    btn = ttk.Button(
                        self.camera_checks_frame,
                        text=f"Kamera(s) freigeben ({len(unbound)})",
                        command=lambda u=unbound: self._prompt_bind_cameras(u),
                    )
                    btn.pack(anchor=tk.W, pady=(5, 0))

                self.btn_scan_camera.config(state=tk.NORMAL)

            self._scanning = False
            self.root.after(0, _update_checkbuttons)
            self._set_status("Kamera-Scan abgeschlossen.")

        threading.Thread(target=_do_scan, daemon=True).start()

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
            import tempfile
            log_file = os.path.join(tempfile.gettempdir(), "edubotics_bind_devices.log")
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
            exit_code, cancelled, err = _elevate_and_wait(
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
                return
            if exit_code not in (0, None):
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
        selected = []
        for i, var in enumerate(self.camera_check_vars):
            if var.get() and i < len(self.cameras):
                selected.append(self.cameras[i])
        selected = selected[:2]  # Max 2 cameras

        # Clear role assignment UI
        for w in self.camera_role_frame.winfo_children():
            w.destroy()

        if len(selected) == 1:
            # Single camera — auto-assign as gripper
            selected[0].role = "gripper"
            self.hardware.cameras = selected
            ttk.Label(self.camera_role_frame,
                      text=f"Greifer-Kamera: {selected[0].name}",
                      foreground="green").pack(anchor=tk.W)
        elif len(selected) == 2:
            # Two cameras — let student assign roles
            cam_names = [f"{c.name} ({c.path})" for c in selected]

            ttk.Label(self.camera_role_frame,
                      text="Kamera-Rollen zuweisen:",
                      font=("", 9, "bold")).pack(anchor=tk.W, pady=(5, 2))

            gripper_row = ttk.Frame(self.camera_role_frame)
            gripper_row.pack(fill=tk.X, pady=2)
            ttk.Label(gripper_row, text="Greifer-Kamera:").pack(side=tk.LEFT)
            self.gripper_cam_var.set(cam_names[0])
            gripper_combo = ttk.Combobox(gripper_row, textvariable=self.gripper_cam_var,
                                         values=cam_names, state="readonly", width=40)
            gripper_combo.pack(side=tk.LEFT, padx=5)
            gripper_combo.bind("<<ComboboxSelected>>", lambda e: self._assign_camera_roles(selected, cam_names))

            scene_row = ttk.Frame(self.camera_role_frame)
            scene_row.pack(fill=tk.X, pady=2)
            ttk.Label(scene_row, text="Szenen-Kamera: ").pack(side=tk.LEFT)
            self.scene_cam_var.set(cam_names[1])
            scene_combo = ttk.Combobox(scene_row, textvariable=self.scene_cam_var,
                                       values=cam_names, state="readonly", width=40)
            scene_combo.pack(side=tk.LEFT, padx=5)
            scene_combo.bind("<<ComboboxSelected>>", lambda e: self._assign_camera_roles(selected, cam_names))

            self._assign_camera_roles(selected, cam_names)
        else:
            self.hardware.cameras = selected

    def _assign_camera_roles(self, cameras, cam_names):
        """Assign gripper/scene roles based on combo selection and auto-swap."""
        gripper_selection = self.gripper_cam_var.get()
        gripper_idx = cam_names.index(gripper_selection) if gripper_selection in cam_names else 0
        scene_idx = 1 - gripper_idx  # the other one

        # Auto-sync the other combo
        self.scene_cam_var.set(cam_names[scene_idx])

        cameras[gripper_idx].role = "gripper"
        cameras[scene_idx].role = "scene"

        # Order: gripper first, scene second
        self.hardware.cameras = [cameras[gripper_idx], cameras[scene_idx]]

    # ── Start Environment ────────────────────────────────────────────

    def _start_environment(self):
        """EduBotics-Umgebung starten und Web-Oberfläche öffnen."""
        is_cloud_only = self.cloud_only.get()

        if not is_cloud_only and not self.hardware.is_complete:
            messagebox.showwarning("Fehlende Hardware", "Bitte beide Arme scannen und identifizieren, bevor du startest.")
            return

        self.btn_start.config(state=tk.DISABLED)
        self.running = True

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
                            device_manager.attach_all_robotis_devices()
                            import time
                            for _ in range(5):
                                serial_paths = wsl_bridge.list_serial_devices()
                                if all(arm.serial_path in serial_paths for _, arm in missing_arms):
                                    break
                                time.sleep(1)

                            for arm_name, arm in missing_arms:
                                if arm.serial_path not in serial_paths:
                                    self._log(f"FEHLER: {arm_name}-Arm ({arm.serial_path}) nicht erreichbar!")
                                    self._log("USB-Verbindung prüfen und erneut scannen.")
                                    self._set_status(f"{arm_name}-Arm getrennt")
                                    return
                            self._log("USB-Geräte erfolgreich neu verbunden.")
                    except Exception as e:
                        self._log(f"WARNUNG: Serielle Ports konnten nicht validiert werden: {e}")
                        self._log("Fahre trotzdem fort — Container versuchen erneut auf Geräte zuzugreifen.")

                # 1. .env generieren
                self._set_status("Konfiguration wird erstellt...")
                self._log(".env-Datei wird erstellt...")
                if is_cloud_only:
                    env_content = config_generator.generate_cloud_only_env(ENV_FILE)
                else:
                    env_content = config_generator.generate_env_file(self.hardware, ENV_FILE)
                self._log(f"Konfiguration geschrieben: {ENV_FILE}")
                for line in env_content.strip().splitlines():
                    self._log(f"  {line}")

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
                    self._log("FEHLER: Container konnten nicht gestartet werden. EduBotics-Umgebung prüfen.")
                    self._set_status("Start fehlgeschlagen")
                    return

                self._log("Container gestartet. Warte auf Dienste...")

                # 3. Auf Web-Oberfläche warten
                self._set_status("Warte auf Web-Oberfläche...")
                if not health_checker.wait_for_web_ui(
                    callback=lambda e, t: self._set_status(f"Warte auf Web-Oberfläche... {e}s/{t}s")
                ):
                    self._log("WARNUNG: Web-Oberfläche antwortet noch nicht. Container starten möglicherweise noch.")
                    self._log("Du kannst die Web-Oberfläche manuell über die Schaltfläche öffnen.")
                else:
                    self._log("Web-Oberfläche ist bereit!")

                # 4. Gesundheitsprüfung
                health = health_checker.full_health_check()
                for service, ok in health.items():
                    self._log(f"  {service}: {'OK' if ok else 'NICHT BEREIT'}")

                # 4.5 Kamera-Bridge starten (native Aufnahme auf Windows →
                # Stream in den Container). Nur im native_bridge-Modus und
                # wenn Kameras zugewiesen sind.
                if not is_cloud_only:
                    self._start_camera_bridge()

                # 5. Web-Oberfläche öffnen
                self._log("Web-Oberfläche wird geöffnet...")
                self._open_webview()

                self._set_status("Aktiv — Umgebung läuft")
                self.root.after(0, lambda: self.btn_stop.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.btn_open_browser.config(state=tk.NORMAL))
                startup_ok = True

            except Exception as e:
                self._log(f"FEHLER: Unerwarteter Fehler beim Starten: {e}")
                import traceback
                self._log(traceback.format_exc())
            finally:
                self.root.after(0, lambda: self.progress.stop())
                if not startup_ok:
                    self.running = False
                    self._update_start_button()

        threading.Thread(target=_do_start, daemon=True).start()

    # ── Native camera bridge ─────────────────────────────────────────

    def _start_camera_bridge(self):
        """Start native Windows camera capture → TCP stream into the container.

        No-op when cameras run via usb_cam (Jetson) or when no camera was
        assigned a role. Maps each assigned camera's OpenCV index to its role
        (gripper -> cam_id 0, scene -> cam_id 1, matching the container's
        EDUBOTICS_CAMERA_NAMES order).
        """
        if not cameras_use_native_bridge():
            return
        # Tear down any previous bridge (e.g. a stop→start without app restart).
        self._stop_camera_bridge()
        role_to_index = {
            cam.role: cam.win_index
            for cam in self.hardware.cameras
            if cam.role in ("gripper", "scene") and cam.win_index >= 0
        }
        if not role_to_index:
            self._log("Keine Kamera zugewiesen — Kamera-Bridge wird nicht gestartet.")
            return
        try:
            self.camera_bridge = camera_bridge_mod.CameraBridge(role_to_index)
            self.camera_bridge.start()
            roles = ", ".join(sorted(role_to_index))
            self._log(f"Kamera-Bridge gestartet (nativ, {roles}) — streamt mit "
                      f"{int(camera_bridge_mod.constants.CAMERA_FRAMERATE)} fps in den Container.")
        except Exception as e:  # noqa: BLE001
            self.camera_bridge = None
            self._log(f"WARNUNG: Kamera-Bridge konnte nicht gestartet werden: {e}")

    def _stop_camera_bridge(self):
        """Stop the native camera bridge if running. Best-effort."""
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

        suffix = "/?cloud=1" if self.cloud_only.get() else "/"
        url = f"http://localhost:{PORT_WEB_UI}{suffix}"

        icon = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "assets", "icon.ico",
        )
        icon_path = icon if os.path.isfile(icon) else None

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
        self._log("WARNUNG: WebView2 nicht verfügbar – System-Browser wird geöffnet.")
        messagebox.showwarning(
            "WebView2 nicht verfügbar",
            "Das Microsoft Edge WebView2-Runtime wurde nicht gefunden.\n"
            "Die Web-Oberfläche wird stattdessen im Standard-Browser geöffnet.",
        )
        webbrowser.open(url)

    def _stop_environment(self):
        """Alle Container in der EduBotics-Umgebung stoppen."""
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_open_browser.config(state=tk.DISABLED)

        def _do_stop():
            self._set_status("Container werden gestoppt...")
            self._log("Container werden gestoppt...")
            self.root.after(0, lambda: self.progress.start(10))

            self._stop_camera_bridge()
            if self.cloud_only.get():
                docker_manager.stop_cloud_only(log=self._log)
            else:
                docker_manager.stop_containers(gpu=self.gpu_available)
            docker_manager.stop_keepalive()

            self._log("Alle Container gestoppt.")
            self._set_status("Gestoppt")
            self.running = False
            self._update_start_button()
            self.root.after(0, lambda: self.progress.stop())

        threading.Thread(target=_do_stop, daemon=True).start()


    def _on_close(self):
        """Fenster-Schließen abfangen — Container stoppen, wenn nötig."""
        if self.running:
            if messagebox.askyesno(
                "EduBotics beenden",
                "Die Umgebung läuft noch.\nContainer stoppen und beenden?",
            ):
                self._log("Beende — Container werden gestoppt...")
                self._stop_camera_bridge()
                webview_window.destroy_all()
                if self.cloud_only.get():
                    docker_manager.stop_cloud_only()
                else:
                    docker_manager.stop_containers(gpu=self.gpu_available)
                docker_manager.stop_keepalive()
                self.root.destroy()
            # else: user clicked No, don't close
        else:
            webview_window.destroy_all()
            docker_manager.stop_keepalive()
            self.root.destroy()


def run():
    """GUI-Anwendung starten."""
    root = tk.Tk()
    app = EduBoticsApp(root)
    root.mainloop()
