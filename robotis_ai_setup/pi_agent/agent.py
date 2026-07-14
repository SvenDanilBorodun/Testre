"""EduBotics Pi-Agent — systemd entrypoint + HTTP management API (Orange Pi 5 Pro).

The Pi's counterpart of the Windows ``EduBotics.exe`` GUI. On Windows the wizard
is a NATIVE app that exists before any container; on the Pi the wizard IS the
React SPA served by the always-on ``physical_ai_manager`` container, and this
agent is the brain the SPA drives — through the manager's same-origin
``/api/system`` reverse proxy (nginx strips ``/api/system`` so the agent sees
bare paths: ``/status``, ``/scan-arms``, ``/roboter-studio/status`` …).

It merges the Jetson agent's systemd/scrubbed-env skeleton (``jetson_agent/
agent.py``) with the platform-neutral GUI brain (the Phase-A ``pi_agent``
modules: ``config_generator`` / ``docker_manager`` / ``identify_arm`` /
``camera_enum`` / ``update_checker`` / ``phone_camera`` / ``lan_ip``). Every
Windows/WSL2 artifact is shed (no usbipd, no ``wsl -d`` tunnelling, no MSMF
capture bridge, no WebView2, no PowerShell/UAC repair flows).

Two lifecycle laws (deploy plan §5) shape everything below:

  1. **Two tiers.** ``physical_ai_manager`` is ALWAYS on (it serves the wizard +
     this proxy). Only the robot tier (``open_manipulator`` +
     ``physical_ai_server``) is student-owned and comes up on „Umgebung starten".
     The agent brings the manager up at boot and leaves the robot tier down.
  2. **NEVER ``compose down``.** ``down`` deletes the ``ros_net`` network, which
     severs this agent's gateway HTTP listener (the proxy target) and drops the
     manager. Every teardown is ``stop`` + ``rm -f`` on named services only
     (handled inside ``docker_manager``); the agent's own shutdown closes its
     HTTP sockets + phone/preview receivers and leaves the containers alone.

Binding (deploy plan §5 proxy mechanics): the API binds ``127.0.0.1:8769`` AND
the docker ``ros_net`` gateway IP — NEVER the LAN NIC. The gateway interface
only exists once compose has created ``ros_net``, so the gateway listener is
bound AFTER the boot-time manager ``up`` and rebound whenever the interface
comes back (interface-gone is a rebind trigger, not a fatal error).

Security (deploy plan §8): with open LAN binding and no auth, LAN peers can
drive any arm (the accepted risk). The one mitigation here is a Host/Origin
exact-host allowlist on every MUTATING (POST) endpoint — a drive-by cross-site
``POST /api/system/factory-reset`` from a hostile web page carries an Origin of
a DNS domain, which is rejected; a request from the Pi itself (or a header-less
``curl``) is allowed. Ported from ``roboter_studio_control.py``'s exact-host
check (never a ``startswith`` match).

Run as PID 1 under ``edubotics-pi.service`` (root — needs the docker socket +
``/dev``). Logs to journald; the Protokoll SSE stream mirrors a redacted
in-memory ring of the same lines. All student/teacher-facing strings are German
with literal umlauts (Rule §1); code/comments/log lines are English.
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from . import (
    camera_enum,
    config_generator,
    docker_manager,
    identify_arm,
    phone_camera,
    update_checker,
)
from .config_generator import ArmDevice, CameraDevice, HardwareConfig
from .constants import (
    APP_VERSION,
    COMPOSE_FILE,
    ENV_FILE,
    IMAGE_OPEN_MANIPULATOR,
    PHONE_FRAME_STALE_MAX_AGE_S,
    PORT_AGENT,
    REGISTRY,
    UPDATE_API_URL,
)
from .lan_ip import detect_lan_ip, list_interface_ips

logger = logging.getLogger("edubotics-pi-agent")

# Bind host for the management API. Loopback is always available; the gateway
# listener (the browser-facing side, via nginx /api/system) is bound after the
# manager is up. The API is NEVER bound to the LAN NIC — the browser reaches it
# only through the manager's same-origin proxy (deploy plan §5).
_LOOPBACK_HOST = "127.0.0.1"

# Re-bind cadence for the gateway listener: the ros_net gateway interface may
# not exist yet at process start (before the boot-time manager `up`) and can
# vanish if ros_net is ever recreated. The binder loop polls at this interval.
_GATEWAY_BIND_INTERVAL_S = 5.0

# Bound on the request body we read for a POST — the legitimate bodies are a few
# bytes of JSON (roles map, token, flags). Mirrors roboter_studio_control's cap.
_MAX_BODY_BYTES = 256 * 1024

# Recent-log ring depth for the Protokoll SSE panel.
_LOG_RING_MAXLEN = 800

# Netzwerk-Check probe hosts. GHCR + Docker Hub break image pulls; huggingface.co
# breaks dataset upload / model download; the cloud API host breaks login/updates.
_HF_HOST = "huggingface.co"
_NETCHECK_TCP_TIMEOUT = 5
_NETCHECK_HTTP_TIMEOUT = 6
_CLOCK_SKEW_WARN_S = 120  # a skew past this breaks JWT/TLS — the „Anmeldung läuft ab" case


# ── Secret redaction (ported from gui_app._redact_secret_env_line) ───────────

_SECRET_KEY_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "KEY")
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


def _redact_secret_line(line: str) -> str:
    """Mask secret values before a line enters the Protokoll ring, so a token
    can't leak into a screenshot / screen-share / support paste.

    Port of ``gui_app._redact_secret_env_line`` widened to free-form log lines:
    a ``KEY=VALUE`` whose KEY contains TOKEN/SECRET/PASSWORD/KEY becomes
    ``KEY=***``; a ``"key": "value"`` JSON pair is masked the same way; a
    ``Bearer <tok>`` is masked to ``Bearer ***``. Non-secret lines pass through.
    """
    text = _BEARER_RE.sub(r"\1***", line)
    # JSON-ish "key": "value" with a secret key.
    text = re.sub(
        r'("[^"]*(?:token|secret|password|key)[^"]*"\s*:\s*)"[^"]*"',
        r'\1"***"',
        text,
        flags=re.IGNORECASE,
    )
    # KEY=VALUE shell/.env style. Only the first '=' splits key from value.
    stripped = text.strip()
    if "=" in stripped and not stripped.startswith("#"):
        key = stripped.split("=", 1)[0].strip()
        # A bare identifier key (no spaces) that names a secret.
        if key and " " not in key and any(m in key.upper() for m in _SECRET_KEY_MARKERS):
            indent = text[: len(text) - len(text.lstrip())]
            return f"{indent}{key}=***"
    return text


# ── In-memory log ring feeding the Protokoll SSE stream ──────────────────────


class _LogRing:
    """Thread-safe ring of the most recent (already-redacted) log lines.

    A monotonic sequence lets an SSE reader fetch only lines it hasn't seen yet
    (``since``) so a reconnecting Protokoll panel replays the backlog once and
    then tails new lines.
    """

    def __init__(self, maxlen: int = _LOG_RING_MAXLEN) -> None:
        self._lock = threading.Lock()
        self._items: deque = deque(maxlen=maxlen)  # (seq:int, text:str)
        self._seq = 0

    def append(self, text: str) -> None:
        with self._lock:
            self._seq += 1
            self._items.append((self._seq, text))

    def since(self, after_seq: int) -> "tuple[list, int]":
        """Return ``(new_items, latest_seq)`` for items with seq > after_seq."""
        with self._lock:
            new = [(s, t) for (s, t) in self._items if s > after_seq]
            return new, self._seq


class _RingLogHandler(logging.Handler):
    """A logging handler that mirrors every record (redacted) into the ring, so
    the Protokoll panel shows the same stream journald gets."""

    def __init__(self, ring: _LogRing) -> None:
        super().__init__()
        self._ring = ring

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ring.append(_redact_secret_line(self.format(record)))
        except Exception:  # noqa: BLE001 — logging must never raise
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Camera preview (lazy OpenCV) — the MJPEG endpoint's capture backend.
# ─────────────────────────────────────────────────────────────────────────────


def _is_allowed_camera_device(device: str) -> bool:
    """True iff ``device`` is a v4l2 capture-node path the preview may open —
    a ``/dev/videoN`` node or a single-segment ``/dev/v4l/by-id`` / ``by-path``
    symlink (the shapes ``camera_enum`` emits). Everything else — an OpenCV
    URL like ``http://``/``rtsp://``, an integer index, a path traversal — is
    refused: the GET preview endpoint is NOT Origin-gated, so this allowlist is
    the only guard against turning ``cv2.VideoCapture`` into an SSRF vector."""
    if not device or ".." in device or "\x00" in device or "\n" in device:
        return False
    for prefix in ("/dev/v4l/by-id/", "/dev/v4l/by-path/"):
        if device.startswith(prefix):
            tail = device[len(prefix):]
            return bool(tail) and "/" not in tail
    if device.startswith("/dev/video"):
        return device[len("/dev/video"):].isdigit()
    return False


class _PreviewCapture:
    """A single live camera capture that yields JPEG frames for the MJPEG stream.

    OpenCV is imported LAZILY so the agent's core paths (and the deps-free tests)
    never require it: a Pi image carries ``opencv-python-headless`` (requirements
    note), but a bare CI host does not, and the preview endpoint degrades to a
    German 503 rather than crashing. This is the host-side analogue of the GUI's
    ``cv2.imencode`` preview — the Pi has no ``win_camera``/MSMF module.
    """

    def __init__(self, device: str) -> None:
        self.device = device
        self._cap = None

    def open(self) -> bool:
        try:
            import cv2  # noqa: PLC0415 — lazy, optional dependency
        except Exception:  # noqa: BLE001 — module absent on a bare host
            return False
        self._cv2 = cv2
        # v4l2 by-id / by-path or /dev/videoN — VideoCapture accepts the path.
        self._cap = cv2.VideoCapture(self.device)
        if not self._cap or not self._cap.isOpened():
            self.release()
            return False
        return True

    def read_jpeg(self) -> Optional[bytes]:
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        ok, buf = self._cv2.imencode(".jpg", frame)
        if not ok:
            return None
        return buf.tobytes()

    def release(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass


# ─────────────────────────────────────────────────────────────────────────────
# The management application: state + endpoint handlers.
# ─────────────────────────────────────────────────────────────────────────────


class AgentApp:
    """Holds agent state and implements every management endpoint.

    The HTTP layer (``_make_handler``) is a thin translator: it parses the
    request, enforces the Host/Origin allowlist on mutating verbs, and calls one
    of the ``handle_*`` methods, which return ``(status_code, payload_dict)`` —
    the same testable shape ``roboter_studio_control`` uses. SSE + MJPEG stream
    directly through the handler (``stream_*``).
    """

    def __init__(self, env_file: str = ENV_FILE, compose_file: str = COMPOSE_FILE) -> None:
        self.env_file = env_file
        self.compose_file = compose_file

        # Scanned hardware — rehydrated from the .env at start() so a reboot
        # keeps the arms/cameras identified without a rescan.
        self._hardware = HardwareConfig()
        # Guards hardware mutation + the robot-tier lifecycle ops so two POSTs
        # (scan + start) can't race the Dynamixel bus / the .env write.
        self._lifecycle_lock = threading.Lock()

        # Roboter-Studio leader-toggle busy state (mirrors the RS control server).
        self._rs_busy_lock = threading.Lock()
        self._rs_busy = False
        self._rs_switch_in_flight = False

        # Async update jobs (ACK-early). job_id -> dict.
        self._update_jobs: dict = {}
        self._update_lock = threading.Lock()
        # Single-flight guard: a 2nd /update while one runs is rejected (409).
        # Without it a double-click spawns a redundant stop/pull/recreate that
        # serializes behind the first on _lifecycle_lock (wasteful) or a 2nd
        # agent-self-update apply. Guarded by _update_lock.
        self._update_busy = False

        # Phone-camera receiver (preview-only backend; OPEN — no ROS republish).
        # _phone_lock guards the check-then-set on _phone_server so two enable
        # POSTs can't both bind :8444 (held alone — never nested → no cycle).
        self._phone_lock = threading.Lock()
        self._phone_server = None
        self._phone_slot = None

        # Live camera preview + a stop event the lifecycle ops set to release the
        # device BEFORE the stack (usb_cam) claims it.
        self._preview_lock = threading.Lock()
        self._preview_stop = threading.Event()

        # Log ring + shutdown signal.
        self._log_ring = _LogRing()
        self._shutdown = threading.Event()
        self._ready = threading.Event()  # set once boot() finishes

        # HTTP servers.
        self._loopback_httpd = None
        self._gateway_httpd = None
        self._gateway_ip: Optional[str] = None

    # ── logging helper (feeds the ring + journald) ───────────────────────────

    def _log(self, message: str) -> None:
        """Log a line to journald and the redacted Protokoll ring."""
        logger.info(message)  # the _RingLogHandler mirrors it (redacted)

    # ── Host/Origin allowlist (ported from roboter_studio_control) ───────────

    def _allowed_literal_hosts(self) -> "set[str]":
        """Host names that are unambiguously the Pi itself: loopback + the Pi's
        own mDNS hostname (``edubotics-NN`` / ``edubotics-NN.local``)."""
        names = {"localhost", "127.0.0.1", "::1"}
        try:
            hn = socket.gethostname().lower()
        except OSError:
            hn = ""
        if hn:
            short = hn.split(".")[0]
            names.add(hn)
            names.add(short)
            names.add(f"{short}.local")
            # gethostname() may already carry .local (avahi); add it verbatim.
            names.add(hn if hn.endswith(".local") else f"{hn}.local")
        return names

    def _own_ip_addresses(self) -> "set[str]":
        """Every IP literal that belongs to THIS Pi — loopback is handled
        separately by the caller; this returns the interface addresses so
        ``origin_allowed`` can accept a same-origin ``http://<pi-ip>/`` POST
        while rejecting an attacker's ``http://<their-ip>/`` drive-by. A
        best-effort union of independent stdlib sources (each guarded so a
        failure in one never blanks the set), normalised via ``ip_address`` so
        IPv6 short/long forms compare equal."""
        addrs: "set[str]" = set()
        # 1. Hostname-mapped addresses (the mDNS / /etc/hosts A record).
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                addrs.add(info[4][0])
        except OSError:  # gaierror is an OSError subclass
            pass
        # 2. The routable (or, on a route-less direct link, link-local) LAN IP
        #    the teacher actually reaches the wizard on.
        lan = detect_lan_ip()
        if lan:
            addrs.add(lan)
        # 3. Every interface address `ip` reports (a multi-homed wired+Wi-Fi
        #    rig). Absent on non-Linux/CI hosts — degrades to (1)+(2).
        addrs.update(list_interface_ips())

        normalized: "set[str]" = set()
        for a in addrs:
            a = a.split("%", 1)[0]  # strip a scope-id suffix (fe80::1%eth0)
            try:
                normalized.add(str(ip_address(a)))
            except ValueError:
                continue
        return normalized

    def origin_allowed(self, origin: str) -> bool:
        """Whether a mutating request's ``Origin`` is the Pi itself.

        Ported exact-host check from ``roboter_studio_control._origin_allowed``
        (never a ``startswith`` match — ``http://localhost.evil.com`` must fail)
        and adapted for the Pi (deploy plan §8): allow the Pi's mDNS hostname,
        localhost, or an IP literal that is one of the Pi's OWN addresses
        (loopback or an interface address). A hostile page served from
        ``http://<attacker-ip>/`` carries that attacker IP as its Origin and is
        REJECTED — an IP literal is no longer a blanket allow. An EMPTY Origin
        is allowed: same-origin browser requests omit it, and header-less
        callers (``curl`` — the P2 acceptance path) must work. CORS blocks the
        cross-origin RESPONSE read but not a no-cors POST's SIDE EFFECT, so this
        handler-level check is the real one.
        """
        if origin == "":
            return True
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            return False
        if not host:
            return False
        host = host.lower()
        if host in self._allowed_literal_hosts():
            return True
        try:
            ip = ip_address(host)
        except ValueError:
            return False
        # An IP literal is the Pi ONLY if it is loopback (unreachable off-host)
        # or one of this Pi's own interface addresses.
        if ip.is_loopback:
            return True
        return str(ip) in self._own_ip_addresses()

    # ── hardware rehydrate / persist ─────────────────────────────────────────

    def rehydrate_hardware(self) -> None:
        """Rebuild ``self._hardware`` from the persisted .env so a reboot keeps
        the arms/cameras identified without a rescan (the GUI keeps this state
        in-memory; on the Pi the .env is the durable record)."""
        follower_path = config_generator.read_env_var("FOLLOWER_PORT", self.env_file)
        leader_path = config_generator.read_env_var("LEADER_PORT", self.env_file)
        follower = ArmDevice(serial_path=follower_path, role="follower",
                             description=(follower_path or "").split("/")[-1]) if follower_path else None
        leader = ArmDevice(serial_path=leader_path, role="leader",
                           description=(leader_path or "").split("/")[-1]) if leader_path else None
        cameras: list = []
        i = 1
        while True:
            dev = config_generator.read_env_var(f"CAMERA_DEVICE_{i}", self.env_file)
            role = config_generator.read_env_var(f"CAMERA_NAME_{i}", self.env_file)
            if dev is None and role is None:
                break
            if dev:  # skip the empty cloud-only placeholders
                cameras.append(CameraDevice(path=dev, role=role or "", name=dev.split("/")[-1]))
            i += 1
        self._hardware = HardwareConfig(leader=leader, follower=follower, cameras=cameras)

    def _persist_env_if_ready(self, follower_only: bool = False) -> None:
        """Write the managed .env from ``self._hardware`` when it holds enough to
        generate a valid config (follower present; leader too unless
        ``follower_only``). Otherwise the in-memory state stays authoritative
        until „Umgebung starten" regenerates it — ``generate_env_file`` refuses a
        partial config, so we never write a half-formed both-arms .env."""
        hw = self._hardware
        if hw.follower is None:
            return
        if not follower_only and hw.leader is None:
            return
        config_generator.generate_env_file(hw, self.env_file, follower_only=follower_only)

    # ── GET: /status ─────────────────────────────────────────────────────────

    def handle_status(self) -> "tuple[int, dict]":
        """Full agent/rig snapshot for the System window (incl. the Pi-IP-Anzeige
        the teacher reads off the screen)."""
        try:
            container = docker_manager.get_container_status()
        except Exception:  # noqa: BLE001
            container = {}
        follower_only_raw = config_generator.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_file)
        hw = self._hardware
        cameras = [{"path": c.path, "role": c.role, "name": c.name} for c in hw.cameras]
        return 200, {
            "lan_ip": detect_lan_ip(),
            "hostname": socket.gethostname(),
            "agent_ready": self._ready.is_set(),
            "agent_version": APP_VERSION,
            "manager_up": container.get("physical_ai_manager") == "running",
            "robot_tier_up": all(
                container.get(n) == "running"
                for n in ("open_manipulator", "physical_ai_server")
            ),
            "container_status": container,
            "gateway_bound": self._gateway_httpd is not None,
            "arms_identified": {
                "leader": hw.leader.serial_path if hw.leader else None,
                "follower": hw.follower.serial_path if hw.follower else None,
                "both": hw.leader is not None and hw.follower is not None,
            },
            "cameras": cameras,
            "follower_only": str(follower_only_raw).strip() == "1",
            "hf_token_saved": bool(config_generator.read_env_var("HF_TOKEN", self.env_file)),
            "images": docker_manager.get_last_pull_status(),
        }

    # ── POST: /scan-arms ─────────────────────────────────────────────────────

    def handle_scan_arms(self, body: dict) -> "tuple[int, dict]":
        """Identify leader/follower via the scanner container and persist their
        stable by-id paths. A revisit fast-rehydrates the saved paths (skips the
        slow scanner container + per-device pings) unless the caller forces a
        full rescan.

        The Dynamixel bus must be free first — ``ensure_environment_stopped``
        tears down any running robot tier (TARGETED, never ``compose down``) so
        ``identify_arm.py`` can open the serial ports a live 100 Hz controller
        would otherwise hold.
        """
        force = bool(body.get("force"))
        with self._lifecycle_lock:
            self.stop_active_previews()
            docker_manager.ensure_environment_stopped(log=self._log)

            leader = follower = None
            if not force:
                saved_leader = config_generator.read_env_var("LEADER_PORT", self.env_file)
                saved_follower = config_generator.read_env_var("FOLLOWER_PORT", self.env_file)
                if saved_leader and saved_follower:
                    self._log("Arme werden schnell überprüft (vorherige Zuordnung) …")
                    leader, follower = identify_arm.fast_rehydrate_arms(saved_leader, saved_follower)

            if leader is None or follower is None:
                self._log("Arme werden gescannt (Leader/Follower werden bestimmt) …")
                leader, follower = identify_arm.scan_and_identify_arms(IMAGE_OPEN_MANIPULATOR)

            # Keep whatever we found in the in-memory config for the status view.
            self._hardware.leader = leader
            self._hardware.follower = follower

            if follower is None and leader is None:
                return 404, {"ok": False,
                             "message": "Kein Arm gefunden — USB-Verbindung und "
                                        "Stromversorgung der Arme prüfen."}
            if follower is None:
                return 409, {"ok": False, "leader": leader.serial_path if leader else None,
                             "follower": None,
                             "message": "Nur der Leader-Arm wurde erkannt — der "
                                        "Follower-Arm fehlt. Bitte USB prüfen."}
            if leader is None:
                return 409, {"ok": False, "leader": None,
                             "follower": follower.serial_path,
                             "message": "Nur der Follower-Arm wurde erkannt — der "
                                        "Leader-Arm fehlt. Bitte USB prüfen."}

            # Both arms found — persist the canonical both-arms .env.
            try:
                self._persist_env_if_ready(follower_only=False)
            except Exception as e:  # noqa: BLE001 — surfaced in German
                return 500, {"ok": False,
                             "message": f"Konfiguration konnte nicht gespeichert werden: {e}"}
            return 200, {"ok": True, "leader": leader.serial_path,
                         "follower": follower.serial_path,
                         "message": "Beide Arme erkannt und gespeichert."}

    # ── GET: /cameras/scan ───────────────────────────────────────────────────

    def handle_cameras_scan(self) -> "tuple[int, dict]":
        """Enumerate UVC capture devices (v4l2 by-id/by-path + identical-serial
        dedup, native — no ``wsl -d`` wrapper). Roles are assigned separately."""
        self.stop_active_previews()  # release any device the preview holds first
        cams = camera_enum.list_video_devices()
        return 200, {"ok": True, "cameras": cams}

    # ── POST: /cameras/roles ─────────────────────────────────────────────────

    def handle_cameras_roles(self, body: dict) -> "tuple[int, dict]":
        """Assign gripper/scene roles to enumerated devices and (if arms are
        already identified) persist. Body: ``{"cameras": [{"path": …,
        "role": "gripper"|"scene"}, …]}``."""
        entries = body.get("cameras")
        if not isinstance(entries, list):
            return 400, {"ok": False, "message": "Ungültige Kamera-Zuordnung."}
        cameras: list = []
        for e in entries:
            path = (e or {}).get("path")
            role = (e or {}).get("role")
            if not path:
                continue
            if role not in ("gripper", "scene"):
                return 400, {"ok": False,
                             "message": f"Ungültige Rolle für {path} (nur Greifer/Szene)."}
            cameras.append(CameraDevice(path=path, role=role, name=str(path).split("/")[-1]))
        with self._lifecycle_lock:
            self._hardware.cameras = cameras
            try:
                self._persist_env_if_ready(follower_only=False)
            except Exception as e:  # noqa: BLE001
                # Not fatal: the roles live in-memory and are written at env
                # start. Only a genuine write failure (disk) surfaces here.
                return 500, {"ok": False,
                             "message": f"Konfiguration konnte nicht gespeichert werden: {e}"}
        return 200, {"ok": True, "cameras": [{"path": c.path, "role": c.role} for c in cameras],
                     "message": f"{len(cameras)} Kamera(s) zugeordnet."}

    # ── POST: /phone/toggle ──────────────────────────────────────────────────

    def handle_phone_toggle(self, body: dict) -> "tuple[int, dict]":
        """Start/stop the phone-camera HTTPS receiver.

        ⚠ PREVIEW-ONLY BACKEND — the phone is NOT wired into ROS on the Pi
        (OPEN, owner decision pending; deploy plan §6/§11). The receiver + frame
        slot exist and can receive frames, but there is no ``camera_ingest_node``
        consumer on the ``usb_cam`` path, so frames are never republished on a
        ROS topic. P3 will NOT surface phone UI until the owner decides.
        """
        enable = bool(body.get("enable"))
        with self._phone_lock:
            if enable:
                if self._phone_server is not None:
                    return 200, {"ok": True, "running": True,
                                 "message": "Handy-Kamera-Empfang läuft bereits."}
                try:
                    cert_path, key_path = phone_camera.ensure_cert()
                except phone_camera.PhoneCertError as e:
                    return 500, {"ok": False, "message": str(e)}
                slot = phone_camera.LatestFrameSlot()
                server = phone_camera.PhoneCameraServer(
                    cert_path, key_path, slot, log=self._log)
                try:
                    server.start()
                except Exception as e:  # noqa: BLE001
                    # Do NOT null self._phone_server — it was never assigned this
                    # failed server, and nulling would drop a live one.
                    return 500, {"ok": False,
                                 "message": f"Handy-Kamera-Empfang konnte nicht starten: {e}"}
                self._phone_server = server
                self._phone_slot = slot
                return 200, {"ok": True, "running": True, "preview_only": True,
                             "message": "Handy-Kamera-Empfang gestartet (nur Vorschau)."}
            # disable — shut down UNDER the lock so a racing enable can't rebind
            # :8444 before the old listener has released it.
            srv, self._phone_server = self._phone_server, None
            self._phone_slot = None
            if srv is not None:
                srv.shutdown()
        return 200, {"ok": True, "running": False, "message": "Handy-Kamera-Empfang gestoppt."}

    # ── POST: /hf-token ──────────────────────────────────────────────────────

    def handle_hf_token(self, body: dict) -> "tuple[int, dict]":
        """Store (or clear) the Hugging Face token. ``HF_TOKEN`` is deliberately
        UNMANAGED — ``upsert_env_var`` is its sole writer, so it survives every
        .env regenerate and Factory Reset. The token is never echoed back."""
        token = body.get("token")
        if token is None:
            return 400, {"ok": False, "message": "Kein Token angegeben."}
        try:
            # Serialize with every other .env writer (scan/roles/start): the
            # atomic os.replace is safe, but two concurrent writers could still
            # race the token against a regenerate that carries it forward.
            with self._lifecycle_lock:
                config_generator.upsert_env_var("HF_TOKEN", str(token), self.env_file)
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "message": f"Token konnte nicht gespeichert werden: {e}"}
        if str(token).strip():
            return 200, {"ok": True, "saved": True, "message": "Token gespeichert."}
        return 200, {"ok": True, "saved": False, "message": "Token entfernt."}

    # ── POST: /environment/start + /environment/stop ─────────────────────────

    def handle_environment_start(self, body: dict) -> "tuple[int, dict]":
        """„Umgebung starten": bring up the student-owned robot tier. Env-start
        always regenerates a BOTH-arms .env (the Roboter-Studio leader toggle is
        the only path that flips to follower-only). Cloud-only start is a no-op —
        the manager is already up."""
        if bool(body.get("cloud_only")):
            return 200, {"ok": True, "cloud_only": True,
                         "message": "Cloud-Modus aktiv — die Weboberfläche läuft bereits."}
        with self._lifecycle_lock:
            if self._hardware.follower is None or self._hardware.leader is None:
                return 400, {"ok": False,
                             "message": "Bitte zuerst beide Arme scannen (Leader und Follower)."}
            self.stop_active_previews()  # free /dev/video* before usb_cam claims it
            try:
                config_generator.generate_env_file(
                    self._hardware, self.env_file, follower_only=False)
            except Exception as e:  # noqa: BLE001
                return 500, {"ok": False,
                             "message": f"Konfiguration konnte nicht erstellt werden: {e}"}
            self._log("Roboter-Umgebung wird gestartet …")
            ok = docker_manager.start_robot_tier(log=self._log)
        if not ok:
            return 500, {"ok": False,
                         "message": "Die Roboter-Umgebung konnte nicht gestartet werden — "
                                    "bitte das Protokoll prüfen."}
        return 200, {"ok": True, "message": "Roboter-Umgebung gestartet."}

    def handle_environment_stop(self) -> "tuple[int, dict]":
        """„Stoppen": stop the robot tier ONLY (TARGETED — never ``compose
        down``; the manager keeps serving the wizard). The graceful SIGTERM lets
        the entrypoint's torque-disable trap run (Rule §2)."""
        with self._lifecycle_lock:
            self.stop_active_previews()
            self._log("Roboter-Umgebung wird gestoppt …")
            ok = docker_manager.stop_robot_tier(log=self._log)
        return (200 if ok else 500), {
            "ok": bool(ok),
            "message": ("Roboter-Umgebung gestoppt." if ok else
                        "Die Roboter-Umgebung konnte nicht sauber gestoppt werden."),
        }

    # ── POST: /update (ACK early) + GET: /update/status/{id} ─────────────────

    def handle_update_start(self) -> "tuple[int, dict]":
        """Kick off an async update job and ACK immediately with a job id.

        The update recreates the manager LAST and the agent may self-update-
        restart — both 502 THIS very proxy — so a long-lived in-flight response
        would be severed mid-update and read as a failure. The System window
        re-attaches by polling ``/update/status/{id}``.
        """
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "running",
            "phase": "queued",
            "progress": 0,
            "log": [],
            "message": "Aktualisierung wird vorbereitet …",
            "agent_restarting": False,
            "started_at": int(time.time()),
        }
        with self._update_lock:
            if self._update_busy:
                return 409, {"ok": False,
                             "message": "Eine Aktualisierung läuft bereits — bitte warten."}
            self._update_busy = True
            self._update_jobs[job_id] = job
        threading.Thread(
            target=self._run_update_job, args=(job_id,),
            name=f"update-{job_id[:8]}", daemon=True,
        ).start()
        return 202, {"ok": True, "job_id": job_id, "status": "running",
                     "message": "Aktualisierung gestartet."}

    def handle_update_status(self, job_id: str) -> "tuple[int, dict]":
        with self._update_lock:
            job = self._update_jobs.get(job_id)
            if job is None:
                # After an agent self-update restart the in-memory job map is
                # gone; the UI treats 404 as "reconnecting after update".
                return 404, {"ok": False, "message": "Auftrag nicht gefunden."}
            return 200, dict(job)

    def _run_update_job(self, job_id: str) -> None:
        """Worker for the async update. Order (deploy plan §5): stop robot tier →
        pull images (arm64 digest pre-check, GHCR→Hub) → stage the agent tarball
        (if advertised) → recreate the manager LAST → (if staged) apply + restart.

        The whole stop/pull/recreate sequence is held under ``_lifecycle_lock``
        so a concurrent „Umgebung starten" / „Arme scannen" can't race the
        container recreate on the same .env / Dynamixel bus. This never
        deadlocks: the update REQUEST thread already ACK'd early (before this
        background worker takes the lock), so nothing waits on this thread.
        """
        def joblog(msg: str) -> None:
            with self._update_lock:
                j = self._update_jobs.get(job_id)
                if j is not None:
                    j["log"].append(_redact_secret_line(msg))
            self._log(msg)

        def setphase(phase: str, progress: int, message: str) -> None:
            with self._update_lock:
                j = self._update_jobs.get(job_id)
                if j is not None:
                    j["phase"] = phase
                    j["progress"] = progress
                    j["message"] = message

        try:
            with self._lifecycle_lock:
                setphase("stopping", 10, "Roboter-Umgebung wird gestoppt …")
                joblog("Roboter-Umgebung wird für die Aktualisierung gestoppt …")
                docker_manager.stop_robot_tier(log=joblog)

                setphase("pulling", 30, "Container-Images werden aktualisiert …")
                docker_manager.check_for_updates(log=joblog)

                setphase("agent", 70, "Agent-Version wird geprüft …")
                staged_tarball = None
                try:
                    upd = update_checker.check_for_agent_update(APP_VERSION, UPDATE_API_URL)
                except Exception as e:  # noqa: BLE001 — never fail the whole update on this
                    upd = None
                    joblog(f"Agent-Update-Prüfung fehlgeschlagen: {e}")
                if upd:
                    if not (upd.get("sha256") or "").strip():
                        # Cloud advertised a tarball WITHOUT a SHA-256 — refuse:
                        # a root process must never extractall unverified bytes.
                        joblog("[WARNUNG] Agent-Update ohne Prüfsumme angeboten — wird "
                               "aus Sicherheitsgründen übersprungen (keine SHA-256).")
                    else:
                        joblog(f"Neue Agent-Version {upd['version']} verfügbar — wird geladen …")
                        staged_tarball = update_checker.download_agent_tarball(
                            upd["download_url"], expected_sha256=upd["sha256"])
                        if staged_tarball is None:
                            joblog("Agent-Download fehlgeschlagen — Agent-Update wird übersprungen.")
                else:
                    joblog("Agent ist aktuell.")

                setphase("manager", 90, "Weboberfläche wird neu gestartet …")
                joblog("Weboberfläche (Manager) wird neu erstellt …")
                docker_manager.start_manager(log=joblog)

                with self._update_lock:
                    j = self._update_jobs.get(job_id)
                    if j is not None:
                        j["status"] = "succeeded"
                        j["progress"] = 100
                        j["phase"] = "done"
                        j["message"] = "Aktualisierung abgeschlossen."
                        if staged_tarball:
                            j["agent_restarting"] = True
                            j["message"] = "Aktualisierung abgeschlossen — der Agent startet neu."

                if staged_tarball:
                    # Apply + restart AFTER the manager is recreated. The restart
                    # 502s this proxy briefly; the UI already read "succeeded".
                    joblog("Agent-Aktualisierung wird angewendet — der Agent startet neu.")
                    self._apply_agent_update_and_restart(staged_tarball)
        except Exception as e:  # noqa: BLE001 — report, never crash the thread
            with self._update_lock:
                j = self._update_jobs.get(job_id)
                if j is not None:
                    j["status"] = "failed"
                    j["message"] = f"Aktualisierung fehlgeschlagen: {e}"
            self._log(f"Update job {job_id} failed: {e}")
        finally:
            # Release the single-flight guard so a later /update can run. On a
            # successful agent self-update the process restarts before this runs
            # — the flag resets naturally on the fresh boot either way.
            with self._update_lock:
                self._update_busy = False

    def _apply_agent_update_and_restart(self, tarball_path: str) -> None:
        """Unpack the verified agent tarball over the install root and trigger a
        clean process exit so systemd (``Restart=always``) restarts the new
        agent. Best-effort: a failed apply keeps the running agent alive.

        The install root is the compose file's directory (``/opt/edubotics`` in
        the field, per setup.sh P4). The tarball's top-level dir is ``pi_agent/``
        (agent code only — compose / unit changes need re-provisioning). We
        extract to a TEMP dir (tar ``data`` filter — path-traversal / device-node
        guard) then ``rsync -a --delete`` it over ``pi_agent/`` so a module
        REMOVED in a release doesn't linger (``extractall`` alone never deletes;
        setup.sh installs with the same ``rsync --delete`` semantics). Crash-safe:
        rsync never empties the tree, and the refreshed ``pi_agent/VERSION``
        (which the running agent reports) lands with it.
        """
        install_root = os.path.dirname(os.path.abspath(self.compose_file)) or "/opt/edubotics"
        staging = None
        try:
            staging = tempfile.mkdtemp(prefix="edubotics-agent-update-")
            with tarfile.open(tarball_path, "r:gz") as tf:
                try:
                    tf.extractall(staging, filter="data")  # py>=3.12 safe filter
                except TypeError:
                    tf.extractall(staging)  # older py — tarball is SHA-256-verified
            src_pkg = os.path.join(staging, "pi_agent")
            if not os.path.isdir(src_pkg):
                self._log("Agent-Aktualisierung: kein pi_agent/-Verzeichnis im Archiv — abgebrochen.")
                return
            dst_pkg = os.path.join(install_root, "pi_agent")
            # rsync --delete matches setup.sh's install semantics (excludes match
            # too), so orphaned modules go and pi_agent/VERSION is refreshed.
            # --checksum: a version bump of the SAME byte length (e.g. VERSION
            # 2.12.2 → 2.12.3) can share size+mtime, which rsync's default
            # quick-check would skip — checksum comparison forces the update.
            result = subprocess.run(
                ["rsync", "-a", "--checksum", "--delete",
                 "--exclude=tests", "--exclude=__pycache__", "--exclude=*.pyc",
                 src_pkg + "/", dst_pkg + "/"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                self._log("Agent-Aktualisierung konnte nicht angewendet werden: "
                          f"{(result.stderr or '').strip()[:200]}")
                return
        except Exception as e:  # noqa: BLE001
            self._log(f"Agent-Aktualisierung konnte nicht entpackt werden: {e}")
            return
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
        # Defer the restart a beat so the update-status poller can read the
        # terminal "succeeded" state, then SIGTERM ourselves → main()'s teardown
        # runs (never `compose down`) and systemd restarts us.
        def _restart() -> None:
            time.sleep(2.0)
            self._log("Agent wird neu gestartet, um die Aktualisierung zu übernehmen …")
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=_restart, name="agent-restart", daemon=True).start()

    # ── POST: /factory-reset (double-confirm) ────────────────────────────────

    def handle_factory_reset(self, body: dict) -> "tuple[int, dict]":
        """Delete the persistent data volumes (datasets, HF cache, Roboter-Studio
        calibration). Requires a DOUBLE confirmation and never ``compose down``
        (that would delete ros_net + drop the manager)."""
        if not (bool(body.get("confirm")) and bool(body.get("confirm_again"))):
            return 400, {"ok": False,
                         "message": "Doppelte Bestätigung erforderlich (confirm + confirm_again)."}
        with self._lifecycle_lock:
            self.stop_active_previews()
            ok, msg = docker_manager.factory_reset(log=self._log)
        return (200 if ok else 500), {"ok": bool(ok), "message": msg}

    # ── GET: /netzwerk-check ─────────────────────────────────────────────────

    def handle_netzwerk_check(self) -> "tuple[int, dict]":
        """Run the school-network diagnostics FROM the Pi: cloud/registry/HF
        reachability, TLS-inspection detection, and clock sync — each a
        green/red German line with a one-line hint (deploy plan §5/§6)."""
        checks = []

        # 1. Cloud API reachable — also reused for the clock-skew check.
        cloud_ok, cloud_date = self._probe_cloud()
        checks.append({
            "key": "cloud", "label": "Cloud-Dienst erreichbar", "ok": cloud_ok,
            "hint": "" if cloud_ok else
            "Cloud-Dienst nicht erreichbar — Internet/Firewall prüfen (ausgehend TCP 443).",
        })

        # 2. Container registry (GHCR) reachable.
        ghcr_host = self._registry_host(REGISTRY)
        ghcr_ok = self._tcp_reachable(ghcr_host, 443)
        checks.append({
            "key": "registry", "label": "Container-Registry erreichbar", "ok": ghcr_ok,
            "hint": "" if ghcr_ok else
            f"Registry ({ghcr_host}) nicht erreichbar — Updates schlagen fehl. "
            "IT: ausgehend TCP 443 zu ghcr.io freigeben.",
        })

        # 3. Hugging Face reachable.
        hf_ok = self._tcp_reachable(_HF_HOST, 443)
        checks.append({
            "key": "huggingface", "label": "Hugging Face erreichbar", "ok": hf_ok,
            "hint": "" if hf_ok else
            "Hugging Face nicht erreichbar — Datensatz-Upload/Modell-Download blockiert. "
            "IT: huggingface.co (TCP 443) freigeben.",
        })

        # 4. TLS certificates genuine (TLS-inspection middlebox detection).
        tls_ok, tls_detail = self._tls_genuine(ghcr_host)
        checks.append({
            "key": "tls", "label": "Zertifikate echt (keine TLS-Inspektion)", "ok": tls_ok,
            "hint": "" if tls_ok else
            "TLS-Inspektion erkannt (Zertifikat neu signiert) — bricht Pulls/Uploads/Updater. "
            "IT: Ausnahme für das Robotik-VLAN nötig, siehe Netzwerk-Anleitung."
            + (f" [{tls_detail}]" if tls_detail else ""),
        })

        # 5. Clock sane / NTP synced.
        clock_ok, clock_detail = self._clock_sane(cloud_date)
        checks.append({
            "key": "clock", "label": "Systemuhr synchron (NTP)", "ok": clock_ok,
            "hint": "" if clock_ok else
            "Systemuhr weicht ab (NTP nicht synchron) — Anmeldung/Zertifikate schlagen fehl. "
            "IT: NTP (UDP 123) freigeben." + (f" [{clock_detail}]" if clock_detail else ""),
        })

        all_ok = all(c["ok"] for c in checks)
        return 200, {"ok": all_ok, "checks": checks}

    @staticmethod
    def _registry_host(registry: str) -> str:
        """DNS host for a registry value (``ghcr.io/<owner>`` → ``ghcr.io``; a
        bare owner → Docker Hub). Mirrors ``docker_manager._registry_host``."""
        first = registry.split("/", 1)[0]
        if "." in first or ":" in first or first == "localhost":
            return first.split(":", 1)[0]
        return "registry-1.docker.io"

    @staticmethod
    def _tcp_reachable(host: str, port: int, timeout: int = _NETCHECK_TCP_TIMEOUT) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (OSError, socket.timeout):
            return False

    def _probe_cloud(self) -> "tuple[bool, Optional[str]]":
        """GET the cloud ``/version`` (HTTPS). Returns ``(reachable, date_header)``
        — the Date header feeds the clock-skew check."""
        url = UPDATE_API_URL.rstrip("/") + "/version"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=_NETCHECK_HTTP_TIMEOUT) as resp:
                return resp.status < 500, resp.headers.get("Date")
        except urllib.error.HTTPError as e:
            # An HTTP error still proves reachability; carry its Date header.
            return e.code < 500, e.headers.get("Date") if e.headers else None
        except Exception:  # noqa: BLE001
            return False, None

    @staticmethod
    def _tls_genuine(host: str, port: int = 443) -> "tuple[bool, str]":
        """Verify ``host:port`` presents a chain that validates against the system
        CA store. A verification failure with ``CERTIFICATE_VERIFY_FAILED`` is
        the TLS-inspection signal: a middlebox re-signs with the school's private
        root, which the Pi (no such root installed) rejects. A clean connect
        returns the issuer org for the green line; a transient network error is
        reported as ``ok`` (unknown, not a re-sign) so a blip doesn't cry wolf.

        ``port`` defaults to 443 (the production call); it is parameterised only
        so the detection can be exercised against a local self-signed TLS server.
        """
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=_NETCHECK_TCP_TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert() or {}
            issuer = dict(x[0] for x in cert.get("issuer", []))
            return True, issuer.get("organizationName", "")
        except ssl.SSLCertVerificationError as e:
            return False, str(getattr(e, "verify_message", "") or e)
        except (OSError, socket.timeout):
            # Reachability, not TLS, is the failing dimension — the registry
            # check above already reports that; don't double-flag as a re-sign.
            return True, "Netzwerkfehler (nicht als Inspektion gewertet)"

    @staticmethod
    def _clock_sane(cloud_date: Optional[str]) -> "tuple[bool, str]":
        """Compare the local clock against the cloud's HTTP ``Date`` header (the
        authoritative wall clock a JWT/TLS check would use). Falls back to
        ``timedatectl`` NTP-sync state when the Date header is absent."""
        if cloud_date:
            try:
                server_dt = email.utils.parsedate_to_datetime(cloud_date)
                skew = abs(time.time() - server_dt.timestamp())
                if skew <= _CLOCK_SKEW_WARN_S:
                    return True, f"Abweichung {int(skew)} s"
                return False, f"Abweichung {int(skew)} s"
            except (TypeError, ValueError, OverflowError):
                pass
        # No usable Date header — fall back to the local NTP-sync flag.
        try:
            out = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            synced = out.stdout.strip().lower() == "yes"
            return synced, "NTP synchronisiert" if synced else "NTP nicht synchronisiert"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Can't determine — don't fail the whole check on an unknowable.
            return True, "NTP-Status unbekannt"

    # ── Roboter Studio leader toggle (ported RS contract + busy lock) ────────

    def handle_rs_status(self) -> "tuple[int, dict]":
        """GET /roboter-studio/status → the exact ``roboter_studio_control`` JSON
        contract, plus ``ready`` (arm container running) like the GUI badge."""
        follower_only = str(
            config_generator.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_file)).strip() == "1"
        with self._rs_busy_lock:
            busy = self._rs_busy or self._rs_switch_in_flight
        if busy:
            return 200, {"follower_only": follower_only, "ready": False, "busy": True}
        try:
            arm = docker_manager.get_container_status().get("open_manipulator", "not found")
        except Exception:  # noqa: BLE001
            arm = "error"
        return 200, {"follower_only": follower_only, "ready": arm == "running", "busy": False}

    def handle_rs_set_mode(self, follower_only: bool) -> "tuple[int, dict]":
        """POST /roboter-studio/leader-disable|enable → switch the arm mode and
        recreate ONLY open_manipulator. Busy-locked so a second click can't race
        two ``compose up`` calls on the same container. The .env rollback on a
        failed restart lives in ``docker_manager.set_leader_mode`` (the ported
        ``gui_app._rs_set_leader_mode`` callback)."""
        with self._rs_busy_lock:
            if self._rs_busy:
                return 409, {"ok": False, "message": "Ein Moduswechsel läuft bereits."}
            self._rs_busy = True
            self._rs_switch_in_flight = True
        try:
            ok, msg = docker_manager.set_leader_mode(self._hardware, follower_only, log=self._log)
            return (200 if ok else 500), {
                "ok": bool(ok), "message": msg, "follower_only": follower_only}
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "message": f"Fehler: {e}"}
        finally:
            with self._rs_busy_lock:
                self._rs_busy = False
                self._rs_switch_in_flight = False

    # ── Live camera preview (MJPEG) ──────────────────────────────────────────

    def stop_active_previews(self) -> None:
        """Signal any in-flight MJPEG preview loop to stop so the device is
        released BEFORE the stack (usb_cam) or a scan claims it."""
        self._preview_stop.set()

    def stream_camera_preview(self, handler: BaseHTTPRequestHandler, device: str) -> None:
        """Stream a live MJPEG preview of ``device`` (multipart/x-mixed-replace).

        One preview at a time: a new preview stops the previous one. If OpenCV is
        unavailable (bare host) or the device can't be opened, a German 503 is
        sent. The loop ends when the client disconnects, on shutdown, or when a
        lifecycle op calls ``stop_active_previews``.
        """
        with self._preview_lock:
            # Don't re-grab /dev/video* while a lifecycle op (env-start / update /
            # scan) is claiming the cameras for usb_cam: a preview landing in that
            # window would steal the device and break the stack's camera open (the
            # M2 race — the two paths otherwise use different locks). Probe
            # _lifecycle_lock WITHOUT blocking so a preview never stalls a
            # lifecycle op, and hold it only across the device open — env-start's
            # stop_active_previews() then sets _preview_stop to end this loop and
            # free the device again.
            if not self._lifecycle_lock.acquire(blocking=False):
                self._send_simple(handler, 503,
                                  "Kameravorschau nicht verfügbar (die Roboter-Umgebung "
                                  "wird gerade gestartet oder aktualisiert).")
                return
            try:
                # Stop a prior preview and re-arm the stop event for this one.
                self._preview_stop.set()
                time.sleep(0.1)
                self._preview_stop.clear()
                cap = _PreviewCapture(device)
                opened = cap.open()
            finally:
                self._lifecycle_lock.release()
            if not opened:
                self._send_simple(handler, 503,
                                  "Kameravorschau nicht verfügbar (Gerät belegt oder "
                                  "Vorschau-Backend fehlt).")
                return
        handler.send_response(200)
        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        handler.send_header("Cache-Control", "no-cache")
        # Belt-and-suspenders for a proxy that ignores our config: tell nginx not
        # to buffer (the /api/system location already sets proxy_buffering off).
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        try:
            while not self._shutdown.is_set() and not self._preview_stop.is_set():
                jpeg = cap.read_jpeg()
                if jpeg is None:
                    break
                handler.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                handler.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii"))
                handler.wfile.write(jpeg)
                handler.wfile.write(b"\r\n")
                handler.wfile.flush()
                time.sleep(0.05)  # ~20 fps ceiling
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client hung up
        finally:
            cap.release()

    # ── Protokoll SSE ────────────────────────────────────────────────────────

    def stream_protokoll(self, handler: BaseHTTPRequestHandler) -> None:
        """Stream the redacted log ring as Server-Sent Events (backlog first,
        then a live tail). Flushes per event; a periodic comment keeps the
        connection alive through the /api/system proxy."""
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        last = 0
        try:
            while not self._shutdown.is_set():
                items, latest = self._log_ring.since(last)
                for _seq, text in items:
                    # An SSE event ends at a blank line; a multi-line record maps
                    # to consecutive `data:` lines (already redacted in the ring).
                    for chunk in text.splitlines() or [""]:
                        handler.wfile.write(f"data: {chunk}\n".encode("utf-8"))
                    handler.wfile.write(b"\n")
                last = latest
                handler.wfile.write(b": ping\n\n")  # keep-alive comment
                handler.wfile.flush()
                if self._shutdown.wait(0.7):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client closed the Protokoll panel

    # ── small HTTP write helpers ─────────────────────────────────────────────

    @staticmethod
    def _send_simple(handler: BaseHTTPRequestHandler, code: int, text: str) -> None:
        body = text.encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except OSError:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # HTTP server plumbing.
    # ─────────────────────────────────────────────────────────────────────────

    def _make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "EduBoticsPiAgent/1.0"

            def log_message(self, *_a):  # silence the default stderr access log
                pass

            # ---- response helpers ----
            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            def _send_json(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def _read_json_body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except (TypeError, ValueError):
                    length = 0
                if length <= 0:
                    return {}
                try:
                    raw = self.rfile.read(min(length, _MAX_BODY_BYTES))
                except OSError:
                    return {}
                if not raw:
                    return {}
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return {}
                return data if isinstance(data, dict) else {}

            def do_OPTIONS(self):  # noqa: N802 — http.server API
                self.send_response(204)
                self._cors()
                self.end_headers()

            # ---- GET routing (open; no state change) ----
            def do_GET(self):  # noqa: N802
                path = urlsplit(self.path).path.rstrip("/") or "/"
                query = parse_qs(urlsplit(self.path).query)
                if path in ("/", "/health"):
                    self._send_json(200, {"ok": True, "agent_ready": app._ready.is_set()})
                elif path == "/status":
                    self._send_json(*app.handle_status())
                elif path == "/cameras/scan":
                    self._send_json(*app.handle_cameras_scan())
                elif path == "/cameras/preview":
                    device = (query.get("device") or query.get("path") or [""])[0]
                    if not device:
                        self._send_json(400, {"ok": False, "message": "Kein Kameragerät angegeben."})
                        return
                    if not _is_allowed_camera_device(device):
                        # GETs are not Origin-gated — validate the device path so
                        # a URL/index can't drive cv2.VideoCapture (SSRF).
                        self._send_json(400, {"ok": False, "message": "Ungültiges Kameragerät."})
                        return
                    app.stream_camera_preview(self, device)
                elif path == "/protokoll":
                    app.stream_protokoll(self)
                elif path == "/netzwerk-check":
                    self._send_json(*app.handle_netzwerk_check())
                elif path == "/roboter-studio/status":
                    self._send_json(*app.handle_rs_status())
                elif path.startswith("/update/status/"):
                    job_id = path[len("/update/status/"):]
                    self._send_json(*app.handle_update_status(job_id))
                else:
                    self._send_json(404, {"ok": False, "message": "Nicht gefunden."})

            # ---- POST routing (mutating; Host/Origin allowlist) ----
            def do_POST(self):  # noqa: N802
                path = urlsplit(self.path).path.rstrip("/") or "/"
                origin = self.headers.get("Origin", "")
                if not app.origin_allowed(origin):
                    # Drain the body so the kernel doesn't reset the connection.
                    self._read_json_body()
                    self._send_json(403, {"ok": False, "message": "Origin nicht erlaubt."})
                    return
                body = self._read_json_body()
                if path == "/scan-arms":
                    self._send_json(*app.handle_scan_arms(body))
                elif path == "/cameras/roles":
                    self._send_json(*app.handle_cameras_roles(body))
                elif path == "/cameras/preview/stop":
                    app.stop_active_previews()
                    self._send_json(200, {"ok": True})
                elif path == "/phone/toggle":
                    self._send_json(*app.handle_phone_toggle(body))
                elif path == "/hf-token":
                    self._send_json(*app.handle_hf_token(body))
                elif path == "/environment/start":
                    self._send_json(*app.handle_environment_start(body))
                elif path == "/environment/stop":
                    self._send_json(*app.handle_environment_stop())
                elif path == "/update":
                    self._send_json(*app.handle_update_start())
                elif path == "/factory-reset":
                    self._send_json(*app.handle_factory_reset(body))
                elif path == "/roboter-studio/leader-disable":
                    self._send_json(*app.handle_rs_set_mode(True))
                elif path == "/roboter-studio/leader-enable":
                    self._send_json(*app.handle_rs_set_mode(False))
                else:
                    self._send_json(404, {"ok": False, "message": "Nicht gefunden."})

        return Handler

    def _serve(self, httpd) -> None:
        try:
            httpd.serve_forever(poll_interval=0.5)
        except Exception as exc:  # noqa: BLE001 — a dead listener triggers a rebind
            self._log(f"HTTP listener stopped: {exc}")

    def start_loopback(self) -> None:
        """Bind the always-available 127.0.0.1 listener."""
        handler = self._make_handler()
        self._loopback_httpd = ThreadingHTTPServer((_LOOPBACK_HOST, PORT_AGENT), handler)
        self._loopback_httpd.daemon_threads = True
        threading.Thread(
            target=self._serve, args=(self._loopback_httpd,),
            name="agent-loopback", daemon=True,
        ).start()
        self._log(f"Management-API gebunden an {_LOOPBACK_HOST}:{PORT_AGENT}.")

    @staticmethod
    def _ip_is_local(ip: str) -> bool:
        """True iff ``ip`` is currently assigned to a local interface (so we can
        bind to it). Probes with a throwaway datagram socket bind — EADDRNOTAVAIL
        means the interface (e.g. the ros_net gateway before compose creates it,
        or after it's deleted) isn't up yet."""
        if not ip:
            return False
        try:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            s = socket.socket(fam, socket.SOCK_DGRAM)
            try:
                s.bind((ip, 0))
                return True
            finally:
                s.close()
        except OSError:
            return False

    def _gateway_binder_loop(self) -> None:
        """Keep the ros_net-gateway listener bound whenever the gateway IP is a
        local interface. The gateway only exists after the boot-time manager
        ``up`` created ros_net, and it vanishes if ros_net is ever recreated —
        so binding is retried, and interface-gone tears the listener down to be
        rebound (never fatal). The gateway IP is read from the managed .env
        (``EDUBOTICS_ROS_NET_GATEWAY``) each tick so a subnet change is honoured.
        """
        handler = self._make_handler()
        while not self._shutdown.is_set():
            gateway_ip = config_generator.read_env_var(
                "EDUBOTICS_ROS_NET_GATEWAY", self.env_file) or ""
            local = self._ip_is_local(gateway_ip)
            if self._gateway_httpd is not None and (not local or gateway_ip != self._gateway_ip):
                # Interface gone (or the gateway IP changed) — drop the listener
                # so the next tick rebinds against the current interface.
                self._log("ros_net-Gateway-Interface verschwunden — Listener wird zurückgesetzt.")
                try:
                    self._gateway_httpd.shutdown()
                    self._gateway_httpd.server_close()
                except Exception:  # noqa: BLE001
                    pass
                self._gateway_httpd = None
                self._gateway_ip = None
            if self._gateway_httpd is None and local:
                try:
                    httpd = ThreadingHTTPServer((gateway_ip, PORT_AGENT), handler)
                    httpd.daemon_threads = True
                    self._gateway_httpd = httpd
                    self._gateway_ip = gateway_ip
                    threading.Thread(
                        target=self._serve, args=(httpd,),
                        name="agent-gateway", daemon=True,
                    ).start()
                    self._log(f"Management-API zusätzlich gebunden an {gateway_ip}:{PORT_AGENT} "
                              "(ros_net-Gateway, /api/system-Proxy).")
                except OSError as e:
                    # Port taken or interface flapped — try again next tick.
                    self._log(f"Gateway-Bindung {gateway_ip}:{PORT_AGENT} fehlgeschlagen: {e}")
            if self._shutdown.wait(_GATEWAY_BIND_INTERVAL_S):
                break

    # ─────────────────────────────────────────────────────────────────────────
    # Boot + shutdown.
    # ─────────────────────────────────────────────────────────────────────────

    def boot(self) -> None:
        """Boot sequence: seed the .env if missing, rehydrate hardware, start the
        loopback API, bring up the ALWAYS-ON manager (creating ros_net), then
        start the gateway-binder loop (the gateway interface exists only after
        the manager ``up``). The robot tier is intentionally left down."""
        self.rehydrate_hardware()

        if not os.path.isfile(self.env_file):
            # A freshly flashed Pi has no arms configured yet; seed a cloud-only
            # .env so compose can interpolate every ${VAR} for the manager `up`.
            self._log("Keine .env vorhanden — Cloud-Konfiguration wird angelegt.")
            try:
                config_generator.generate_cloud_only_env(self.env_file)
            except Exception as e:  # noqa: BLE001
                self._log(f"Cloud-.env konnte nicht erstellt werden: {e}")

        self.start_loopback()

        # Bring the always-on manager up (up -d --force-recreate --no-deps
        # physical_ai_manager). No image pull here — boot stays fast on the
        # provisioned image; refresh is the /update path.
        self._log("Weboberfläche (Manager) wird gestartet …")
        if not docker_manager.start_manager(log=self._log):
            self._log("[WARNUNG] Manager konnte nicht gestartet werden — bitte Images/Docker prüfen.")

        threading.Thread(
            target=self._gateway_binder_loop, name="agent-gateway-binder", daemon=True,
        ).start()

        self._ready.set()
        self._log("Pi-Agent bereit.")

    def request_shutdown(self) -> None:
        self._shutdown.set()

    def shutdown(self) -> None:
        """Graceful teardown: close the HTTP listeners + phone receiver + preview.
        NEVER ``compose down`` — the ros_net network + the always-on manager +
        any running robot tier survive an agent restart (systemd brings the agent
        back and it re-attaches)."""
        self._shutdown.set()
        self.stop_active_previews()
        for httpd in (self._loopback_httpd, self._gateway_httpd):
            if httpd is not None:
                try:
                    httpd.shutdown()
                    httpd.server_close()
                except Exception:  # noqa: BLE001
                    pass
        self._loopback_httpd = None
        self._gateway_httpd = None
        srv, self._phone_server = self._phone_server, None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def run_forever(self) -> None:
        """Block the main thread until a shutdown signal arrives."""
        while not self._shutdown.wait(1.0):
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry point.
# ─────────────────────────────────────────────────────────────────────────────


def _configure_logging(app: AgentApp) -> None:
    """journald (stdout) + the redacted Protokoll ring."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    ring = _RingLogHandler(app._log_ring)
    ring.setFormatter(logging.Formatter("%(message)s"))  # the panel wants bare text
    root.addHandler(ring)


def main() -> None:
    app = AgentApp()
    _configure_logging(app)
    logger.info("EduBotics Pi-Agent starting (version=%s, cloud=%s)", APP_VERSION, UPDATE_API_URL)

    def _sigterm(_signum, _frame):
        logger.info("Signal received — shutting down")
        app.request_shutdown()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    try:
        app.boot()
        app.run_forever()
    finally:
        # NEVER `compose down` here (Rule: the network + always-on manager must
        # survive); only close our own listeners/receivers.
        app.shutdown()


if __name__ == "__main__":
    main()
