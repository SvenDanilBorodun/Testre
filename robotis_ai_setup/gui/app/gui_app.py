"""Main tkinter GUI for ROBOTIS AI Setup.

Provides a step-by-step interface for:
  1. Scanning and identifying robot arms (leader/follower)
  2. Selecting a camera
  3. Starting/stopping the Docker environment
  4. Opening the web browser to the Physical AI Web UI
"""

import os
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import webbrowser

from . import device_manager, docker_manager, health_checker, config_generator
from .constants import (
    IMAGE_OPEN_MANIPULATOR,
    PORT_WEB_UI,
    DOCKER_DIR,
    ENV_FILE,
)


class RobotisAIApp:
    """Main application window."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ROBOTIS AI Setup")
        self.root.geometry("700x800")
        self.root.resizable(True, True)

        # State
        self.hardware = device_manager.HardwareConfig()
        self.cameras: list[device_manager.CameraDevice] = []
        self.gpu_available = False
        self.running = False
        self._scanning = False  # guard against concurrent scans

        self._build_ui()
        self._check_prerequisites()

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self):
        # Main container with padding
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Title
        title = ttk.Label(main, text="ROBOTIS AI Setup", font=("Segoe UI", 18, "bold"))
        title.pack(pady=(0, 5))

        # Status bar
        self.status_var = tk.StringVar(value="Checking system...")
        status_label = ttk.Label(main, textvariable=self.status_var, font=("Segoe UI", 10))
        status_label.pack(pady=(0, 10))

        # Progress bar
        self.progress = ttk.Progressbar(main, mode="indeterminate", length=400)
        self.progress.pack(pady=(0, 10))

        # ── Step A: Leader Arm ──
        leader_frame = ttk.LabelFrame(main, text="Step A: Leader Arm", padding=10)
        leader_frame.pack(fill=tk.X, pady=5)

        ttk.Label(leader_frame, text="Plug in the LEADER arm via USB, then click Scan.").pack(anchor=tk.W)
        leader_row = ttk.Frame(leader_frame)
        leader_row.pack(fill=tk.X, pady=5)

        self.btn_scan_leader = ttk.Button(leader_row, text="Scan Arms", command=self._scan_arms)
        self.btn_scan_leader.pack(side=tk.LEFT)

        self.leader_status_var = tk.StringVar(value="Not scanned")
        ttk.Label(leader_row, textvariable=self.leader_status_var, foreground="gray").pack(side=tk.LEFT, padx=10)

        # ── Step B: Follower Arm (same scan) ──
        follower_frame = ttk.LabelFrame(main, text="Step B: Follower Arm", padding=10)
        follower_frame.pack(fill=tk.X, pady=5)

        ttk.Label(follower_frame, text="Plug in the FOLLOWER arm via USB (scanned together with leader).").pack(anchor=tk.W)
        follower_row = ttk.Frame(follower_frame)
        follower_row.pack(fill=tk.X, pady=5)

        self.follower_status_var = tk.StringVar(value="Not scanned")
        ttk.Label(follower_row, textvariable=self.follower_status_var, foreground="gray").pack(side=tk.LEFT, padx=10)

        # ── Step C: Camera ──
        camera_frame = ttk.LabelFrame(main, text="Step C: Camera", padding=10)
        camera_frame.pack(fill=tk.X, pady=5)

        ttk.Label(camera_frame, text="Select your camera from the dropdown.").pack(anchor=tk.W)
        camera_row = ttk.Frame(camera_frame)
        camera_row.pack(fill=tk.X, pady=5)

        self.btn_scan_camera = ttk.Button(camera_row, text="Scan Cameras", command=self._scan_cameras)
        self.btn_scan_camera.pack(side=tk.LEFT)

        self.camera_combo = ttk.Combobox(camera_row, state="readonly", width=40)
        self.camera_combo.pack(side=tk.LEFT, padx=10)
        self.camera_combo.bind("<<ComboboxSelected>>", self._on_camera_selected)

        # ── Launch Button ──
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=15)

        self.btn_start = ttk.Button(
            btn_frame, text="Start AI Environment",
            command=self._start_environment,
            state=tk.DISABLED,
        )
        self.btn_start.pack(side=tk.LEFT, padx=5)

        self.btn_stop = ttk.Button(
            btn_frame, text="Stop",
            command=self._stop_environment,
            state=tk.DISABLED,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        self.btn_open_browser = ttk.Button(
            btn_frame, text="Open Browser",
            command=lambda: webbrowser.open(f"http://localhost:{PORT_WEB_UI}"),
            state=tk.DISABLED,
        )
        self.btn_open_browser.pack(side=tk.LEFT, padx=5)

        # ── Log Output ──
        log_frame = ttk.LabelFrame(main, text="Log Output", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=12, state=tk.DISABLED,
            font=("Consolas", 9), wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ── Logging ──────────────────────────────────────────────────────

    def _log(self, msg: str):
        """Append message to the log output (thread-safe)."""
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _append)

    def _set_status(self, msg: str):
        """Update status bar (thread-safe)."""
        self.root.after(0, lambda: self.status_var.set(msg))

    def _update_start_button(self):
        """Enable Start button only when hardware config is complete."""
        def _update():
            if self.hardware.is_complete and not self.running:
                self.btn_start.config(state=tk.NORMAL)
            else:
                self.btn_start.config(state=tk.DISABLED)
        self.root.after(0, _update)

    # ── Prerequisites Check ──────────────────────────────────────────

    def _check_prerequisites(self):
        """Check Docker, WSL2, usbipd on startup."""
        def _check():
            self.root.after(0, lambda: self.progress.start(10))
            self._log("Checking prerequisites...")

            # Check Docker
            self._set_status("Checking Docker Desktop...")
            if not docker_manager.is_docker_running():
                self._log("Docker Desktop not running. Waiting up to 120s...")
                if not docker_manager.wait_for_docker(
                    callback=lambda e, t: self._set_status(f"Waiting for Docker... {e}s/{t}s")
                ):
                    self._log("ERROR: Docker Desktop is not running. Please start it and restart this app.")
                    self._set_status("Docker Desktop not found")
                    self.root.after(0, lambda: self.progress.stop())
                    return
            self._log("Docker Desktop: OK")

            # Check images
            self._set_status("Checking Docker images...")
            img_status = docker_manager.images_exist()
            missing = [img for img, exists in img_status.items() if not exists]
            if missing:
                self._log(f"Missing images: {', '.join(missing)}")
                self._log("Pulling images (this may take a while on first run)...")
                self._set_status("Pulling Docker images...")
                if not docker_manager.pull_images(
                    callback=lambda img, i, t: self._log(f"  Pulling {img} ({i+1}/{t})...")
                ):
                    self._log("ERROR: Failed to pull Docker images. Check your internet connection.")
                    self._set_status("Image pull failed")
                    self.root.after(0, lambda: self.progress.stop())
                    return
                self._log("All images pulled successfully.")

            # Check for image updates
            self._set_status("Checking for updates...")
            self._log("Checking for image updates...")
            if docker_manager.check_for_updates():
                self._log("Images updated to latest version.")
            else:
                self._log("Images are up to date.")

            # Check GPU
            self.gpu_available = docker_manager.has_gpu()
            self._log(f"NVIDIA GPU: {'detected' if self.gpu_available else 'not detected (CPU mode)'}")

            self._set_status("Ready — scan your hardware to begin")
            self.root.after(0, lambda: self.progress.stop())
            self._log("System check complete. Plug in your arms and camera, then click Scan.")

        threading.Thread(target=_check, daemon=True).start()

    # ── Arm Scanning ─────────────────────────────────────────────────

    def _scan_arms(self):
        """Scan for ROBOTIS arms — runs in background thread."""
        if self._scanning:
            return
        self._scanning = True
        self.btn_scan_leader.config(state=tk.DISABLED)

        def _do_scan():
            self._set_status("Scanning for robot arms...")
            self.root.after(0, lambda: self.progress.start(10))
            self._log("Scanning USB devices for ROBOTIS arms...")

            leader, follower = device_manager.scan_and_identify_arms(IMAGE_OPEN_MANIPULATOR)

            if leader:
                self.hardware.leader = leader
                self.root.after(0, lambda: self.leader_status_var.set(
                    f"Found: {leader.description} ({leader.serial_path})"
                ))
                self._log(f"Leader found: {leader.serial_path}")
            else:
                self.root.after(0, lambda: self.leader_status_var.set("Not found"))
                self._log("Leader arm not found.")

            if follower:
                self.hardware.follower = follower
                self.root.after(0, lambda: self.follower_status_var.set(
                    f"Found: {follower.description} ({follower.serial_path})"
                ))
                self._log(f"Follower found: {follower.serial_path}")
            else:
                self.root.after(0, lambda: self.follower_status_var.set("Not found"))
                self._log("Follower arm not found.")

            self._scanning = False
            self._update_start_button()
            self.root.after(0, lambda: self.progress.stop())
            self.root.after(0, lambda: self.btn_scan_leader.config(state=tk.NORMAL))

            if leader and follower:
                self._set_status("Both arms found! Select camera and click Start.")
            else:
                self._set_status("Some arms not found. Check connections and try again.")

        threading.Thread(target=_do_scan, daemon=True).start()

    # ── Camera Scanning ──────────────────────────────────────────────

    def _scan_cameras(self):
        """Scan for webcams — runs in background thread."""
        if self._scanning:
            return
        self._scanning = True
        self.btn_scan_camera.config(state=tk.DISABLED)

        def _do_scan():
            self._set_status("Scanning for cameras...")
            self._log("Scanning video devices...")

            self.cameras = device_manager.scan_cameras()

            def _update_combo():
                if self.cameras:
                    values = [f"{c.name} ({c.path})" for c in self.cameras]
                    self.camera_combo["values"] = values
                    self.camera_combo.current(0)
                    self._on_camera_selected(None)
                    self._log(f"Found {len(self.cameras)} camera(s).")
                else:
                    self.camera_combo["values"] = ["No cameras found"]
                    self._log("No cameras found. Camera is optional — you can still start without one.")
                self.btn_scan_camera.config(state=tk.NORMAL)

            self._scanning = False
            self.root.after(0, _update_combo)
            self._set_status("Camera scan complete.")

        threading.Thread(target=_do_scan, daemon=True).start()

    def _on_camera_selected(self, event):
        """Handle camera selection from dropdown."""
        idx = self.camera_combo.current()
        if 0 <= idx < len(self.cameras):
            self.hardware.camera = self.cameras[idx]

    # ── Start Environment ────────────────────────────────────────────

    def _start_environment(self):
        """Start the Docker environment and open browser."""
        if not self.hardware.is_complete:
            messagebox.showwarning("Missing Hardware", "Please scan and identify both arms before starting.")
            return

        self.btn_start.config(state=tk.DISABLED)
        self.running = True

        def _do_start():
            self.root.after(0, lambda: self.progress.start(10))

            # 1. Generate .env
            self._set_status("Generating configuration...")
            self._log("Generating .env file...")
            env_content = config_generator.generate_env_file(self.hardware, ENV_FILE)
            self._log(f"Config written to {ENV_FILE}")
            for line in env_content.strip().splitlines():
                self._log(f"  {line}")

            # 2. Start containers
            use_gpu = self.gpu_available
            self._set_status("Starting containers...")
            self._log(f"Starting Docker Compose ({'GPU' if use_gpu else 'CPU'} mode)...")

            if not docker_manager.start_containers(gpu=use_gpu):
                self._log("ERROR: Failed to start containers. Check Docker Desktop.")
                self._set_status("Start failed")
                self.root.after(0, lambda: self.progress.stop())
                self.running = False
                self._update_start_button()
                return

            self._log("Containers started. Waiting for services...")

            # 3. Wait for web UI
            self._set_status("Waiting for web UI...")
            if not health_checker.wait_for_web_ui(
                callback=lambda e, t: self._set_status(f"Waiting for web UI... {e}s/{t}s")
            ):
                self._log("WARNING: Web UI not responding yet. Containers may still be starting.")
                self._log("You can try opening http://localhost manually in a few seconds.")
            else:
                self._log("Web UI is ready!")

            # 4. Health check
            health = health_checker.full_health_check()
            for service, ok in health.items():
                self._log(f"  {service}: {'OK' if ok else 'NOT READY'}")

            # 5. Open browser
            self._log("Opening browser...")
            webbrowser.open(f"http://localhost:{PORT_WEB_UI}")

            self._set_status("Running — environment is active")
            self.root.after(0, lambda: self.progress.stop())
            self.root.after(0, lambda: self.btn_stop.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.btn_open_browser.config(state=tk.NORMAL))

        threading.Thread(target=_do_start, daemon=True).start()

    # ── Stop Environment ─────────────────────────────────────────────

    def _stop_environment(self):
        """Stop all Docker containers."""
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_open_browser.config(state=tk.DISABLED)

        def _do_stop():
            self._set_status("Stopping containers...")
            self._log("Stopping Docker Compose...")
            self.root.after(0, lambda: self.progress.start(10))

            docker_manager.stop_containers(gpu=self.gpu_available)

            self._log("All containers stopped.")
            self._set_status("Stopped — scan hardware to start again")
            self.running = False
            self._update_start_button()
            self.root.after(0, lambda: self.progress.stop())

        threading.Thread(target=_do_stop, daemon=True).start()


def run():
    """Launch the GUI application."""
    root = tk.Tk()
    app = RobotisAIApp(root)
    root.mainloop()
