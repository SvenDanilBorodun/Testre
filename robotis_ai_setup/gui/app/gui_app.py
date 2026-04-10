"""Haupt-GUI für EduBotics Setup.

Bietet eine schrittweise Oberfläche für:
  1. Erkennen und Identifizieren der Roboterarme (Leader/Follower)
  2. Kamera auswählen
  3. Docker-Umgebung starten/stoppen
  4. Webbrowser mit der EduBotics Web-Oberfläche öffnen
"""

import os
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import webbrowser

from . import device_manager, docker_manager, health_checker, config_generator, wsl_bridge
from .constants import (
    IMAGE_OPEN_MANIPULATOR,
    PORT_WEB_UI,
    DOCKER_DIR,
    ENV_FILE,
)


class EduBoticsApp:
    """Hauptfenster der Anwendung."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EduBotics")
        self.root.geometry("700x800")
        self.root.resizable(True, True)

        # State
        self.hardware = device_manager.HardwareConfig()
        self.cameras: list[device_manager.CameraDevice] = []
        self.gpu_available = False
        self.running = False
        self._scanning = False
        self._prerequisites_done = False

        self._build_ui()
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

        # ── Schritt A: Leader-Arm ──
        leader_frame = ttk.LabelFrame(main, text="Schritt A: Leader-Arm", padding=10)
        leader_frame.pack(fill=tk.X, pady=5)

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

        ttk.Label(follower_frame, text="Follower-Arm per USB anschließen (wird zusammen mit Leader gescannt).").pack(anchor=tk.W)
        follower_row = ttk.Frame(follower_frame)
        follower_row.pack(fill=tk.X, pady=5)

        self.follower_status_var = tk.StringVar(value="Nicht gescannt")
        ttk.Label(follower_row, textvariable=self.follower_status_var, foreground="gray").pack(side=tk.LEFT, padx=10)

        # ── Schritt C: Kameras ──
        camera_frame = ttk.LabelFrame(main, text="Schritt C: Kameras (bis zu 2)", padding=10)
        camera_frame.pack(fill=tk.X, pady=5)

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
            btn_frame, text="Browser öffnen",
            command=lambda: webbrowser.open(f"http://localhost:{PORT_WEB_UI}"),
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
        """Start-Button nur aktivieren, wenn Voraussetzungen geprüft und Hardware vollständig."""
        def _update():
            if self._prerequisites_done and self.hardware.is_complete and not self.running:
                self.btn_start.config(state=tk.NORMAL)
            else:
                self.btn_start.config(state=tk.DISABLED)
        self.root.after(0, _update)

    # ── Prerequisites Check ──────────────────────────────────────────

    def _check_prerequisites(self):
        """Docker, WSL2, usbipd beim Start prüfen."""
        def _check():
            self.root.after(0, lambda: self.progress.start(10))
            self._log("Voraussetzungen werden geprüft...")

            # Check Docker — auto-start if not running
            self._set_status("Docker Desktop wird geprüft...")
            if not docker_manager.is_docker_running():
                self._log("Docker Desktop läuft nicht. Versuche automatisch zu starten...")
                if docker_manager.start_docker_desktop():
                    self._log("Docker Desktop wird gestartet...")
                else:
                    self._log("Docker Desktop konnte nicht automatisch gestartet werden.")
                if not docker_manager.wait_for_docker(
                    callback=lambda e, t: self._set_status(f"Warte auf Docker... {e}s/{t}s")
                ):
                    self._log("FEHLER: Docker Desktop läuft nicht. Bitte manuell starten und App neu starten.")
                    self._set_status("Docker Desktop nicht gefunden")
                    self.root.after(0, lambda: self.progress.stop())
                    return
            self._log("Docker Desktop: OK")

            # Check images
            self._set_status("Docker-Images werden geprüft...")
            img_status = docker_manager.images_exist()
            missing = [img for img, exists in img_status.items() if not exists]
            if missing:
                self._log(f"Fehlende Images: {', '.join(missing)}")
                self._log("Images werden heruntergeladen (kann beim ersten Mal 15-30 Min. dauern)...")
                self._set_status("Docker-Images werden heruntergeladen...")
                if not docker_manager.pull_images(
                    callback=lambda img, i, t: self._set_status(f"Lade Image {i+1}/{t}: {img.split('/')[-1]}"),
                    log=self._log,
                ):
                    self._log("FEHLER: Docker-Images konnten nicht heruntergeladen werden. Internetverbindung prüfen.")
                    self._set_status("Image-Download fehlgeschlagen")
                    self.root.after(0, lambda: self.progress.stop())
                    return
                self._log("Alle Images erfolgreich heruntergeladen.")

            # Check for image updates
            self._set_status("Auf Updates prüfen...")
            self._log("Prüfe auf Image-Updates...")
            if docker_manager.check_for_updates(log=self._log):
                self._log("Images auf neueste Version aktualisiert.")
            else:
                self._log("Images sind aktuell.")

            # Check GPU
            self.gpu_available = docker_manager.has_gpu()
            self._log(f"NVIDIA GPU: {'erkannt' if self.gpu_available else 'nicht erkannt (CPU-Modus)'}")

            self._prerequisites_done = True
            self._update_start_button()
            self._set_status("Bereit — Hardware scannen, um zu beginnen")
            self.root.after(0, lambda: self.progress.stop())
            self._log("Systemprüfung abgeschlossen. Arme und Kamera anschließen, dann auf Scannen klicken.")

        threading.Thread(target=_check, daemon=True).start()

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
            else:
                self._set_status("Einige Arme nicht gefunden. Verbindungen prüfen und erneut versuchen.")

        threading.Thread(target=_do_scan, daemon=True).start()

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
                self.btn_scan_camera.config(state=tk.NORMAL)

            self._scanning = False
            self.root.after(0, _update_checkbuttons)
            self._set_status("Kamera-Scan abgeschlossen.")

        threading.Thread(target=_do_scan, daemon=True).start()

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
        """Docker-Umgebung starten und Browser öffnen."""
        if not self.hardware.is_complete:
            messagebox.showwarning("Fehlende Hardware", "Bitte beide Arme scannen und identifizieren, bevor du startest.")
            return

        self.btn_start.config(state=tk.DISABLED)
        self.running = True

        def _do_start():
          try:
            self.root.after(0, lambda: self.progress.start(10))

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
                            self.root.after(0, lambda: self.progress.stop())
                            self.running = False
                            self._update_start_button()
                            return
                    self._log("USB-Geräte erfolgreich neu verbunden.")
            except Exception as e:
                self._log(f"WARNUNG: Serielle Ports konnten nicht validiert werden: {e}")
                self._log("Fahre trotzdem fort — Container versuchen erneut auf Geräte zuzugreifen.")

            # 1. .env generieren
            self._set_status("Konfiguration wird erstellt...")
            self._log(".env-Datei wird erstellt...")
            env_content = config_generator.generate_env_file(self.hardware, ENV_FILE)
            self._log(f"Konfiguration geschrieben: {ENV_FILE}")
            for line in env_content.strip().splitlines():
                self._log(f"  {line}")

            # 2. Container starten
            use_gpu = self.gpu_available
            self._set_status("Container werden gestartet...")
            self._log(f"Docker Compose wird gestartet ({'GPU' if use_gpu else 'CPU'}-Modus)...")

            if not docker_manager.start_containers(gpu=use_gpu, log=self._log):
                self._log("FEHLER: Container konnten nicht gestartet werden. Docker Desktop prüfen.")
                self._set_status("Start fehlgeschlagen")
                self.root.after(0, lambda: self.progress.stop())
                self.running = False
                self._update_start_button()
                return

            self._log("Container gestartet. Warte auf Dienste...")

            # 3. Auf Web-Oberfläche warten
            self._set_status("Warte auf Web-Oberfläche...")
            if not health_checker.wait_for_web_ui(
                callback=lambda e, t: self._set_status(f"Warte auf Web-Oberfläche... {e}s/{t}s")
            ):
                self._log("WARNUNG: Web-Oberfläche antwortet noch nicht. Container starten möglicherweise noch.")
                self._log("Du kannst http://localhost manuell im Browser öffnen.")
            else:
                self._log("Web-Oberfläche ist bereit!")

            # 4. Gesundheitsprüfung
            health = health_checker.full_health_check()
            for service, ok in health.items():
                self._log(f"  {service}: {'OK' if ok else 'NICHT BEREIT'}")

            # 5. Browser öffnen
            self._log("Browser wird geöffnet...")
            webbrowser.open(f"http://localhost:{PORT_WEB_UI}")

            self._set_status("Aktiv — Umgebung läuft")
            self.root.after(0, lambda: self.progress.stop())
            self.root.after(0, lambda: self.btn_stop.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.btn_open_browser.config(state=tk.NORMAL))

          except Exception as e:
            self._log(f"FEHLER: Unerwarteter Fehler beim Starten: {e}")
            import traceback
            self._log(traceback.format_exc())
            self.running = False
            self._update_start_button()
            self.root.after(0, lambda: self.progress.stop())

        threading.Thread(target=_do_start, daemon=True).start()

    # ── Stop Environment ─────────────────────────────────────────────

    def _stop_environment(self):
        """Alle Docker-Container stoppen."""
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_open_browser.config(state=tk.DISABLED)

        def _do_stop():
            self._set_status("Container werden gestoppt...")
            self._log("Docker Compose wird gestoppt...")
            self.root.after(0, lambda: self.progress.start(10))

            docker_manager.stop_containers(gpu=self.gpu_available)

            self._log("Alle Container gestoppt.")
            self._set_status("Gestoppt — Hardware scannen, um neu zu starten")
            self.running = False
            self._update_start_button()
            self.root.after(0, lambda: self.progress.stop())

        threading.Thread(target=_do_stop, daemon=True).start()


def run():
    """GUI-Anwendung starten."""
    root = tk.Tk()
    app = EduBoticsApp(root)
    root.mainloop()
