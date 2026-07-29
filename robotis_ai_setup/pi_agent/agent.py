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
the docker ``ros_net`` gateway IP — NEVER the LAN NIC. BOTH listeners start
BEFORE the boot-time manager ``up`` (and before the post-self-update image pull),
because nginx proxies ``/api/system`` to the GATEWAY ip — a listener started
after a multi-GB pull would 502 every browser for the whole download. The gateway
binder is a LOOP, not a one-shot: it re-reads the gateway ip every
``_GATEWAY_BIND_INTERVAL_S`` and simply no-ops while ``ros_net`` does not exist
yet, then binds the moment it appears (immediately on a post-self-update boot,
where the manager container outlived the agent restart; after ``start_manager``
on a genuinely first boot). Interface-gone is a rebind trigger, not a fatal
error. The React side still needs its backoff-retried ``/api/system/status``
probe because that first-boot window — agent up, ``ros_net`` not yet created —
is real, so a one-shot probe can land on a 502.

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
    IMAGE_TAG,
    PHONE_FRAME_STALE_MAX_AGE_S,
    PORT_AGENT,
    REGISTRY,
    ROBOT_PROFILES,
    SHIPPED_COMPOSE_RELPATH,
    SYSTEM_FILES_VERSION_FILE,
    SYSTEMD_UNIT_DIR,
    SYSTEMD_UNIT_NAMES,
    UDEV_RULE_NAMES,
    UDEV_RULES_DIR,
    UPDATE_API_URL,
    arm_family_for_robot_type,
    resolve_robot_type,
    robot_profile,
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

# Cache TTL for the read-only /update/check availability probe. A newly booted
# Pi serves MANY student browsers, and each one polls this endpoint (the forced
# PiUpdateGate), so the cloud `/version` call it makes (a 5 s-timeout network
# fetch) is memoised for this long — a poll storm reads the cache instead of
# hammering the cloud. Deliberately a plain module constant (an EDUBOTICS_* env
# NAME would trip ci.yml::env-forwarding-guard); the check is read-only and needs
# no per-rig override.
UPDATE_CHECK_TTL_S = 600

# Floor between two FORCED (cache-bypassing) /update/check refreshes. GETs are
# not Origin-gated by design, and a no-cors cross-origin GET carries no Origin
# header at all — so the allowlist cannot be the only guard here. Without a
# floor, `?force=1` skips UPDATE_CHECK_TTL_S entirely: any page a student opens
# on the school LAN can loop `fetch('http://edubotics-NN.local/api/system/
# update/check?force=1', {mode:'no-cors'})`, and each request spawns a
# ThreadingHTTPServer thread holding a 5 s outbound HTTPS call — defeating the
# poll-storm cache and turning the Pi into a small amplifier against the cloud
# API. The floor bounds forced refreshes to one per interval no matter who asks,
# while leaving a human's in-wizard manual re-check instant (nothing in the SPA
# passes `force` today; PiUpdateGate cache-busts with `?_=<ts>`). Plain constant,
# like UPDATE_CHECK_TTL_S — an EDUBOTICS_* env NAME would trip
# ci.yml::env-forwarding-guard.
_UPDATE_CHECK_FORCE_MIN_INTERVAL_S = 30

# TTL for COMPLETED/FAILED update-job records in the in-memory job map — the
# map would otherwise grow unbounded over a long agent uptime (one dict per
# /update, never removed). Swept on every new /update (mirroring the
# cleanup_stale_tarballs pattern); the most recent job is ALWAYS kept so a
# late status poller can still read the last terminal state. Plain constant
# (an EDUBOTICS_* env name would trip ci.yml::env-forwarding-guard).
_UPDATE_JOB_TTL_S = 6 * 3600

# How long a new MJPEG preview waits for the superseded preview loop to release
# its VideoCapture before giving up with a German 503. The old loop exits
# within roughly one frame read (~50 ms + camera latency), so this is generous.
_PREVIEW_HANDOFF_TIMEOUT_S = 2.0

# How long a MUTATING wizard request waits for _lifecycle_lock before answering
# 503. Bounded on purpose: „Umgebung starten" holds the lock across a resilient
# multi-GB image pull, so a blocking acquire would make „Stoppen", „Arme
# scannen", the camera roles, the HF token and Factory Reset hang for as long as
# that pull runs — with nginx's proxy_read_timeout at 3600 s, no 504 rescues
# them, so every wizard control simply stops answering. A distinguishing 503 is
# what _busy_updating already gives an urgent request during an update; the same
# reasoning applies to every other long lock holder. Short enough that the
# student sees an answer, long enough to ride out the LEGITIMATE brief holders.
#
# Derived from _PREVIEW_HANDOFF_TIMEOUT_S, not picked: stream_camera_preview
# holds _lifecycle_lock across its `_preview_cond.wait(...)` while a superseded
# preview loop releases its VideoCapture — up to that timeout. A shorter wait
# here would turn an ordinary preview handoff into a spurious 503 on „Stoppen".
# Keep this strictly GREATER than the handoff timeout (pinned by a test).
_LIFECYCLE_LOCK_WAIT_S = _PREVIEW_HANDOFF_TIMEOUT_S + 1.0

# Netzwerk-Check probe hosts. GHCR + Docker Hub break image pulls; huggingface.co
# breaks dataset upload / model download; the cloud API host breaks login/updates.
_HF_HOST = "huggingface.co"
_NETCHECK_TCP_TIMEOUT = 5
_NETCHECK_HTTP_TIMEOUT = 6
_CLOCK_SKEW_WARN_S = 120  # a skew past this breaks JWT/TLS — the „Anmeldung läuft ab" case

# Budget for the `docker compose config -q` gate that gates the compose repair
# (_validate_compose_file). Client-side YAML parse + interpolation only, no
# daemon round-trip — measured in tens of milliseconds; generous so a loaded Pi
# cannot turn a healthy release into a refused repair.
_COMPOSE_VALIDATE_TIMEOUT_S = 60


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


# ── tar-member validation for the pre-PEP-706 extractall fallback ────────────


def _validate_tar_member(member: "tarfile.TarInfo") -> None:
    """Refuse tar members that PEP 706's ``filter='data'`` would refuse:
    absolute paths, ``..`` traversal, and non-file/dir specials (symlinks,
    hardlinks, devices, FIFOs). Used ONLY on the fallback path for Pythons
    whose ``extractall`` lacks ``filter=`` — the SHA-256 gate proves the
    tarball is the released one, but defense-in-depth costs nothing.

    Raises ``ValueError`` (caught by the apply path's blanket handler, which
    logs and aborts the self-update) on the first offending member.
    """
    name = member.name
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        raise ValueError(f"absolute path in tar member: {name!r}")
    if ".." in name.replace("\\", "/").split("/"):
        raise ValueError(f"path traversal in tar member: {name!r}")
    if not (member.isfile() or member.isdir()):
        raise ValueError(f"unsupported tar member type: {name!r}")


def _fsync_path(path: str) -> None:
    """fsync a single file or directory. Best-effort: a platform that refuses to
    open a directory read-only (Windows — the tests' host, never the Pi) or a
    file that vanished mid-walk is skipped, never raised."""
    try:
        fd = os.open(path, getattr(os, "O_RDONLY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems refuse fsync on a directory fd
    finally:
        os.close(fd)


def _fsync_tree(path: str) -> None:
    """Flush every file AND directory under ``path`` (and ``path`` itself) to
    stable storage, so a later rename of ``path`` cannot be committed by the
    filesystem ahead of the data it names.

    This is what makes the self-update's rename swap crash-safe rather than
    merely non-mixing. ``rename`` is atomic with respect to concurrent readers,
    but that is an ORDERING property, not a DURABILITY one: ext4's
    ``auto_da_alloc`` heuristic force-allocates delayed blocks only when a file
    is renamed OVER an existing file — it does NOT cover renaming a DIRECTORY of
    freshly-written files. Without this, a classroom power yank on eMMC can land
    the new ``pi_agent/`` directory entry while its file contents are still
    unallocated, i.e. zero-length modules → ImportError under systemd
    ``Restart=always`` → a crash-loop with no self-heal. rsync does not fsync by
    default, so nothing else in the apply path does this.
    """
    for root, dirs, files in os.walk(path):
        for name in files:
            _fsync_path(os.path.join(root, name))
        for name in dirs:
            _fsync_path(os.path.join(root, name))
    _fsync_path(path)


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

        # System-files drift (M1), resolved ONCE in boot() and reported by
        # /status for a System-tab banner. `None` = no stamp on disk (a Pi
        # provisioned before the stamp existed — say nothing) or not yet
        # resolved; a string = the version the on-disk compose/systemd files
        # came from, which boot() only records when it DISAGREES with
        # APP_VERSION. Plain attribute: written once on the boot thread before
        # the listeners start serving, read-only thereafter.
        # PROVEN system-file drift (installed systemd units != the ones this
        # agent ships) + the version stamp naming where those files came from.
        # The stamp is context for the message, never the trigger — see
        # _check_system_files_version.
        self._system_files_stale: bool = False
        self._system_files_version: Optional[str] = None

        # Cached /update/check result (the read-only availability probe the
        # forced PiUpdateGate polls). `None` or `(monotonic_ts, result_dict)`,
        # guarded by its own lock so a poll storm can't repeat the 5 s cloud
        # /version fetch — see handle_update_check + UPDATE_CHECK_TTL_S.
        self._update_check_lock = threading.Lock()
        self._update_check_cache: Optional[tuple] = None
        # monotonic timestamp of the last honoured FORCED refresh; guarded by
        # _update_check_lock. -inf, not 0.0: time.monotonic() is time since
        # SYSTEM boot on Linux, so 0.0 would silently swallow the first forced
        # re-check whenever the agent starts within the interval of boot.
        # See _UPDATE_CHECK_FORCE_MIN_INTERVAL_S.
        self._update_check_forced_at: float = float("-inf")

        # Phone-camera receiver (preview-only backend; OPEN — no ROS republish).
        # _phone_lock guards the check-then-set on _phone_server so two enable
        # POSTs can't both bind :8444 (held alone — never nested → no cycle).
        self._phone_lock = threading.Lock()
        self._phone_server = None
        self._phone_slot = None

        # Live camera preview coordination. A GENERATION counter supersedes
        # older previews: each streaming loop polls its own generation, so a
        # bump can never be "missed" the way the old set→sleep(0.1)→clear
        # Event window could be by a loop blocked in a frame read. The
        # active-loop registry (device → generation) lets a new preview WAIT
        # until the superseded loop has provably released its VideoCapture
        # before re-opening the device (no leaked capture handle).
        self._preview_lock = threading.Lock()
        self._preview_cond = threading.Condition(self._preview_lock)
        self._preview_gen = 0
        self._preview_active: dict = {}  # device -> generation of the live loop

        # Cache for _own_ip_addresses(): the Origin allowlist consults it on
        # EVERY mutating POST with an IP-literal Origin, and the underlying
        # `ip -o addr show` shell-out costs up to 3 s. Interface addresses
        # change rarely; the TTL matches the gateway binder cadence.
        # `None` or `(monotonic_ts, frozenset_of_ips)`, guarded by its lock.
        self._own_ips_lock = threading.Lock()
        self._own_ips_cache: Optional[tuple] = None

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
        IPv6 short/long forms compare equal.

        Cached for ``_GATEWAY_BIND_INTERVAL_S``: ``list_interface_ips`` shells
        out to ``ip -o addr show`` (up to 3 s), which would otherwise run on
        every mutating POST carrying an IP-literal Origin."""
        now = time.monotonic()
        with self._own_ips_lock:
            cached = self._own_ips_cache
            if cached is not None and (now - cached[0]) < _GATEWAY_BIND_INTERVAL_S:
                return set(cached[1])

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
        with self._own_ips_lock:
            self._own_ips_cache = (time.monotonic(), frozenset(normalized))
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

    def _current_robot_type(self) -> str:
        """The CURRENT on-disk managed robot type, VALIDATED against the profile
        registry (unknown / absent / blank → ``DEFAULT_ROBOT_PROFILE``).

        Every regenerate must carry this through — EDUBOTICS_ROBOT_TYPE is a
        MANAGED key, so a caller that omits it silently rewrites it. Validating
        here rather than trusting the raw value means one bad hand-edit cannot
        propagate an unknown id into the scan family, the readiness gate or the
        next .env: it degrades to today's OMX behaviour, exactly as the server's
        ``robot_profiles.resolve`` does at boot."""
        return resolve_robot_type(config_generator.read_env_var(
            "EDUBOTICS_ROBOT_TYPE", self.env_file))

    def _hardware_ready(self, profile: "str | None" = None) -> bool:
        """Whether the scanned hardware satisfies the (given/selected) robot type.

        Twin of ``gui_app.py::_hardware_ready``: a both-arms type needs
        leader+follower, a follower-only type (Roboter Studio kit, edu6) needs
        only the follower. The Pi's ``HardwareConfig`` has no ``is_complete`` /
        ``follower_present`` properties, so the two cases are spelled out."""
        row = robot_profile(profile if profile is not None
                            else self._current_robot_type())
        hw = self._hardware
        if hw.follower is None:
            return False
        return hw.leader is not None if row["scan_requires_leader"] else True

    def _persist_env_if_ready(self, follower_only: "bool | None" = None) -> None:
        """Write the managed .env from ``self._hardware`` when it holds enough to
        generate a valid config (follower present; leader too unless
        ``follower_only``). Otherwise the in-memory state stays authoritative
        until „Umgebung starten" regenerates it — ``generate_env_file`` refuses a
        partial config, so we never write a half-formed both-arms .env.

        ``follower_only=None`` DERIVES the value from the selected profile, which
        is what every caller that is not carrying a live session mode forward
        should pass. For a follower-only PROFILE the profile wins outright: it
        has no leader at all, so a stale ``EDUBOTICS_FOLLOWER_ONLY=0`` in a
        hand-edited .env is state to correct, not a session override — and
        forwarding it would hit ``generate_env_file``'s contradiction guard and
        turn an ordinary camera-role edit into a 500. On a both-arms profile
        nothing is clamped, so an explicit True (the Roboter-Studio toggle) is
        honoured unchanged."""
        robot_type = self._current_robot_type()
        profile_follower_only = ROBOT_PROFILES[robot_type]["follower_only"]
        if follower_only is None:
            follower_only = profile_follower_only
        elif profile_follower_only:
            follower_only = True
        hw = self._hardware
        if hw.follower is None:
            return
        if not follower_only and hw.leader is None:
            return
        config_generator.generate_env_file(hw, self.env_file, follower_only=follower_only,
                                           robot_type=robot_type)

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
        robot_type = self._current_robot_type()
        return 200, {
            "lan_ip": detect_lan_ip(),
            "hostname": socket.gethostname(),
            "agent_ready": self._ready.is_set(),
            "agent_version": APP_VERSION,
            # M1 drift banner. `system_files_stale` is True ONLY on drift proven
            # by a byte-compare of the installed systemd units against the ones
            # this agent ships — False on every healthy Pi, on every pre-stamp Pi
            # and wherever the comparison is impossible. Additive keys: an old
            # System tab that ignores them is unaffected. `system_files_version`
            # is the provisioning stamp (may be None even when stale — an old Pi
            # can be drifted AND unstamped), so a banner should name it only when
            # present rather than assume the pair.
            "system_files_stale": self._system_files_stale,
            "system_files_version": self._system_files_version,
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
            # Robot profile surface (additive — an old System tab ignores these).
            # `robot_type` is the VALIDATED on-disk id; `robot_profiles` is the
            # selectable list (the wizard renders `display_de`, and hides the
            # Leader tile when `scan_requires_leader` is false);
            # `hardware_ready` is the PROFILE-AWARE gate — `arms_identified.both`
            # stays as it is, because it answers a different question (are two
            # arms present) and a follower-only rig can be fully ready with
            # `both` false.
            "robot_type": robot_type,
            "robot_profiles": [
                {"id": pid,
                 "display_de": row["display_de"],
                 "scan_requires_leader": row["scan_requires_leader"]}
                for pid, row in ROBOT_PROFILES.items()
            ],
            "hardware_ready": self._hardware_ready(robot_type),
            "cameras": cameras,
            "follower_only": str(follower_only_raw).strip() == "1",
            "hf_token_saved": bool(config_generator.read_env_var("HF_TOKEN", self.env_file)),
            "images": docker_manager.get_last_pull_status(),
        }

    # ── update-in-flight fast-fail (mutating lifecycle endpoints) ────────────

    def _update_in_flight(self) -> bool:
        with self._update_lock:
            return self._update_busy

    @staticmethod
    def _busy_updating() -> "tuple[int, dict]":
        """Fast-fail response for mutating lifecycle endpoints while an update
        job holds ``_lifecycle_lock`` (minutes of stop/pull/recreate): an
        urgent „Stoppen" must get a distinguishing 503 immediately instead of
        blocking silently behind the update. Read-only endpoints (status,
        Protokoll, update status/check) stay available throughout."""
        return 503, {"ok": False,
                     "message": "Aktualisierung läuft — bitte warten, bis das "
                                "Update abgeschlossen ist."}

    def _acquire_lifecycle(self) -> bool:
        """Bounded acquire of ``_lifecycle_lock`` for a wizard REQUEST thread.

        Never block indefinitely on this lock from an HTTP handler. The lock is
        held across „Umgebung starten"'s resilient image pull and across the
        whole update job — minutes to tens of minutes — and the manager's nginx
        gives the browser an hour before it would time out. A blocking acquire
        therefore does not "queue" the request, it kills the wizard: „Stoppen"
        (the one control a student reaches for when a start is taking too long)
        would wait behind the very operation it is meant to interrupt.

        Callers MUST pair a True return with ``try: … finally: release()``.
        Background workers (``_run_update_job``) keep the blocking ``with`` —
        nothing is waiting on them and giving up there would abort an update.
        """
        return self._lifecycle_lock.acquire(timeout=_LIFECYCLE_LOCK_WAIT_S)

    @staticmethod
    def _busy_lifecycle() -> "tuple[int, dict]":
        """503 for a mutating endpoint that could not take ``_lifecycle_lock``."""
        return 503, {"ok": False,
                     "message": "Ein anderer Vorgang läuft gerade — bitte kurz "
                                "warten und erneut versuchen."}

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

        The ARM FAMILY comes from the on-disk managed ``EDUBOTICS_ROBOT_TYPE``
        (edu6 §4.4): it scopes the /dev/serial/by-id filter AND selects the
        in-container prober's protocol, so an edu6 rig is probed with
        ``--protocol=feetech`` and its single arm is slotted as the FOLLOWER.
        A failed scan can carry a one-sentence German ``notice`` naming the most
        likely setup mistake (wrong family plugged in, partial servo chain, 12-V
        supply off). In the 404 branch it REPLACES the generic message, exactly
        as the GUI replaces its status line; the two 409 branches keep their own
        specific wording and return the notice as a SEPARATE key (which no
        client renders today — it is the Protokoll that carries it there).

        A LEADER-LESS PROFILE SUCCEEDS WITH ONE ARM. ``scan_requires_leader``
        (the profile registry, not the arm family — ``omx_follower`` shares the
        two-arm ``omx`` family) decides three things: whether the fast-rehydrate
        path may run without a saved ``LEADER_PORT``, whether a missing leader
        is a failure at all, and which German sentence a result gets. Reporting
        „Nur der Follower-Arm wurde erkannt — der Leader-Arm fehlt" to a
        Roboter-Studio kit that HAS no leader is the misleading 409 this closes.
        """
        if self._update_in_flight():
            return self._busy_updating()
        force = bool(body.get("force"))
        if not self._acquire_lifecycle():
            return self._busy_lifecycle()
        try:
            self.stop_active_previews()
            docker_manager.ensure_environment_stopped(log=self._log)

            robot_type = self._current_robot_type()
            requires_leader = ROBOT_PROFILES[robot_type]["scan_requires_leader"]
            arm_family = arm_family_for_robot_type(robot_type)
            notice = ""
            leader = follower = None
            if not force:
                saved_leader = config_generator.read_env_var("LEADER_PORT", self.env_file)
                saved_follower = config_generator.read_env_var("FOLLOWER_PORT", self.env_file)
                # A follower-only .env has no LEADER_PORT by construction, so
                # demanding one here would send every such rig down the slow
                # scanner-container path on every single revisit.
                if saved_follower and (saved_leader or not requires_leader):
                    self._log("Arme werden schnell überprüft (vorherige Zuordnung) …")
                    leader, follower = identify_arm.fast_rehydrate_arms(
                        saved_leader, saved_follower, arm_family=arm_family,
                        require_leader=requires_leader)

            if follower is None or (requires_leader and leader is None):
                self._log("Arme werden gescannt (Leader/Follower werden bestimmt) …")
                leader, follower = identify_arm.scan_and_identify_arms(
                    IMAGE_OPEN_MANIPULATOR, arm_family=arm_family)
                # Only meaningful for the run that just happened — never read it
                # off a fast-rehydrate path, where it would be a stale sentence
                # from some earlier scan.
                notice = identify_arm.LAST_SCAN_NOTICE

            # Keep whatever we found in the in-memory config for the status view.
            self._hardware.leader = leader
            self._hardware.follower = follower

            # A notice DIAGNOSES a port that did not become this profile's arm,
            # so it is neither logged nor returned once the scan produced what
            # the profile needs. The scanner clears it at the source, but only
            # against the arm FAMILY — and `omx_follower` shares the two-arm
            # `omx` family, so a successful ONE-arm scan there kept a sentence
            # ending „… und erneut scannen" attached to a success (MEASURED,
            # with a stray CH34x device plugged in). No notice is informative on
            # a success: `partial:N` and `feetech_silent` both mean the arm did
            # NOT identify, and the two cross-family ones only fire when the
            # wrong family is attached. Byte-identical on `omx_full` and
            # `edu6_studio`, where the scanner's own clear already fired.
            if follower is not None and (leader is not None or not requires_leader):
                notice = ""
            elif notice:
                self._log(notice)

            if follower is None and leader is None:
                return 404, {"ok": False, "notice": notice,
                             "message": notice or
                                        "Kein Arm gefunden — USB-Verbindung und "
                                        "Stromversorgung der Arme prüfen."}
            if follower is None:
                # `leader` is non-None here by construction — the both-None case
                # returned 404 above. So an arm WAS found and it is the wrong
                # one, on either profile shape.
                #
                # Reachable on a leader-less profile too: the omx family scan
                # can find an OMX leader while the follower is unplugged. The
                # message must not send that student hunting a cable — the scan
                # succeeded, they plugged in the wrong arm. („Leader"/„Follower"
                # are the German terms this product already uses throughout, so
                # naming the arm is consistency, not new jargon.) Genuinely
                # nothing found keeps its own „Kein Arm gefunden" 404 above.
                return 409, {"ok": False, "leader": leader.serial_path if leader else None,
                             "follower": None, "notice": notice,
                             "message": ("Nur der Leader-Arm wurde erkannt — der "
                                         "Follower-Arm fehlt. Bitte USB prüfen."
                                         if requires_leader else
                                         "Es wurde der Leader-Arm erkannt — dieser "
                                         "Robotertyp braucht den Follower-Arm. Bitte "
                                         "den Follower-Arm anschließen.")}
            if leader is None and requires_leader:
                return 409, {"ok": False, "leader": None,
                             "follower": follower.serial_path, "notice": notice,
                             "message": "Nur der Follower-Arm wurde erkannt — der "
                                        "Leader-Arm fehlt. Bitte USB prüfen."}

            # Enough hardware for this profile — persist the .env. The mode is
            # derived, not hardcoded False: on a follower-only profile (whose
            # omx-family scan can still see two arms plugged in) a both-arms
            # write would contradict the profile and refuse.
            try:
                self._persist_env_if_ready()
            except Exception as e:  # noqa: BLE001 — surfaced in German
                return 500, {"ok": False,
                             "message": f"Konfiguration konnte nicht gespeichert werden: {e}"}
            return 200, {"ok": True,
                         "leader": leader.serial_path if leader else None,
                         "follower": follower.serial_path, "notice": notice,
                         # A leader-less profile never speaks of „beide Arme" —
                         # even when a stray second arm happened to be found,
                         # because the generated .env has no LEADER_PORT.
                         "message": ("Beide Arme erkannt und gespeichert."
                                     if requires_leader else
                                     "Roboterarm erkannt und gespeichert.")}
        finally:
            self._lifecycle_lock.release()

    # ── GET: /cameras/scan ───────────────────────────────────────────────────

    def handle_cameras_scan(self) -> "tuple[int, dict]":
        """Enumerate UVC capture devices (v4l2 by-id/by-path + identical-serial
        dedup, native — no ``wsl -d`` wrapper). Roles are assigned separately.

        DOCUMENTED EXCEPTION to "GETs don't mutate": this stops any live MJPEG
        preview first — enumeration must probe the very device a preview holds.
        The React caller depends on the GET method, so the method stays; the
        state change is confined to the agent's own preview session."""
        self.stop_active_previews()  # release any device the preview holds first
        cams = camera_enum.list_video_devices()
        return 200, {"ok": True, "cameras": cams}

    # ── POST: /cameras/roles ─────────────────────────────────────────────────

    def handle_cameras_roles(self, body: dict) -> "tuple[int, dict]":
        """Assign gripper/scene roles to enumerated devices and (if arms are
        already identified) persist. Body: ``{"cameras": [{"path": …,
        "role": "gripper"|"scene"}, …]}``.

        A LONE camera may arrive with an EMPTY/absent role — the PROFILE then
        decides, taking the first entry of its ``camera_roles``: ``scene`` on
        both follower-only profiles, ``gripper`` on ``omx_full``. This is the
        twin of the Windows GUI's single-camera auto-assign
        (``gui_app._on_cameras_changed``), and the value matters: perception and
        the ``omx_f``/edu6 config topics hang off the role NAME, so defaulting a
        Roboter-Studio kit's only camera to ``gripper`` broke every such rig
        (CLAUDE.md). With TWO cameras the student must choose — the two
        Innomakers are identical-serial, so only the live preview tells them
        apart — and a blank role there stays a German 400.
        """
        if self._update_in_flight():
            return self._busy_updating()
        entries = body.get("cameras")
        if not isinstance(entries, list):
            return 400, {"ok": False, "message": "Ungültige Kamera-Zuordnung."}
        named = [e for e in entries if (e or {}).get("path")]
        # Resolved once (it reads the .env), and only where it can be used.
        default_role = (robot_profile(self._current_robot_type())["camera_roles"][0]
                        if len(named) == 1 else "")
        cameras: list = []
        for e in named:
            path = e.get("path")
            role = e.get("role") or default_role
            if role not in ("gripper", "scene"):
                return 400, {"ok": False,
                             "message": f"Ungültige Rolle für {path} (nur Greifer/Szene)."}
            cameras.append(CameraDevice(path=path, role=role, name=str(path).split("/")[-1]))
        if not self._acquire_lifecycle():
            return self._busy_lifecycle()
        try:
            self._hardware.cameras = cameras
            # Carry the CURRENT session mode forward (the set_leader_mode
            # prev_val pattern): a camera-role edit during a live follower-only
            # Roboter-Studio session must not silently rewrite
            # EDUBOTICS_FOLLOWER_ONLY=0 and re-arm the leader on the next
            # container recreate.
            prev_val = config_generator.read_env_var(
                "EDUBOTICS_FOLLOWER_ONLY", self.env_file)
            # An ABSENT key is not "both arms" — it is "no session mode
            # recorded yet" (a fresh .env, or one written before the key
            # existed). Deriving from the profile there is what stops a
            # follower-only rig writing a both-arms .env behind a camera edit.
            follower_only = (None if prev_val is None
                             else str(prev_val).strip() == "1")
            try:
                self._persist_env_if_ready(follower_only=follower_only)
            except Exception as e:  # noqa: BLE001
                # Not fatal: the roles live in-memory and are written at env
                # start. Only a genuine write failure (disk) surfaces here.
                return 500, {"ok": False,
                             "message": f"Konfiguration konnte nicht gespeichert werden: {e}"}
        finally:
            self._lifecycle_lock.release()
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
        if self._update_in_flight():
            return self._busy_updating()
        token = body.get("token")
        if token is None:
            return 400, {"ok": False, "message": "Kein Token angegeben."}
        # Serialize with every other .env writer (scan/roles/start): the atomic
        # os.replace is safe, but two concurrent writers could still race the
        # token against a regenerate that carries it forward.
        if not self._acquire_lifecycle():
            return self._busy_lifecycle()
        try:
            config_generator.upsert_env_var("HF_TOKEN", str(token), self.env_file)
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "message": f"Token konnte nicht gespeichert werden: {e}"}
        finally:
            self._lifecycle_lock.release()
        if str(token).strip():
            return 200, {"ok": True, "saved": True, "message": "Token gespeichert."}
        return 200, {"ok": True, "saved": False, "message": "Token entfernt."}

    # ── POST: /robot-type ────────────────────────────────────────────────────

    def handle_set_robot_type(self, body: dict) -> "tuple[int, dict]":
        """Select the rig's ArmProfile. Body: ``{"robot_type": "<id>"}``.

        The robot type is a HARDSET decision — the arm container branches on it
        at start (``entrypoint_omx.sh``) and the server resolves its ArmProfile
        from it once at boot — so it is REFUSED while the robot tier is running,
        mirroring the GUI graying its Robotertyp combobox whenever the
        environment runs (``gui_app.py::_update_robot_type_combo_state``). It
        must be settable BEFORE any hardware exists, because the scan reads the
        arm FAMILY back off this key: the id is therefore persisted
        unconditionally (in place, unquoted), and the full .env is regenerated on
        top only once the scanned hardware satisfies the new profile.

        An unknown id is a 400 rather than a silent fallback: the wizard offers
        only registry ids, so an unknown one is a client bug worth surfacing.
        (The read path keeps the forgiving fallback — see ``_current_robot_type``
        — which is where the documented one-variable rollback lives.)
        """
        if self._update_in_flight():
            return self._busy_updating()
        requested = body.get("robot_type")
        if not isinstance(requested, str) or not requested.strip():
            return 400, {"ok": False, "message": "Kein Robotertyp angegeben."}
        requested = requested.strip()
        if requested not in ROBOT_PROFILES:
            return 400, {"ok": False,
                         "message": f'Unbekannter Robotertyp „{requested}".'}
        if not self._acquire_lifecycle():
            return self._busy_lifecycle()
        try:
            try:
                container = docker_manager.get_container_status()
            except Exception:  # noqa: BLE001
                # Deliberately FAIL-OPEN, same posture as handle_status: an
                # unreadable docker state must not wedge the wizard's only way
                # to choose a robot. The consequence of a change slipping past
                # a live tier is bounded — the .env and the containers disagree
                # until the next „Umgebung starten", which regenerates anyway.
                container = {}
            if any(container.get(n) == "running"
                   for n in ("open_manipulator", "physical_ai_server")):
                return 409, {"ok": False,
                             "message": "Der Robotertyp kann nur geändert werden, "
                                        "wenn die Roboter-Umgebung gestoppt ist."}
            try:
                # Unquoted: entrypoint_omx.sh compares this value literally.
                config_generator.upsert_env_var(
                    "EDUBOTICS_ROBOT_TYPE", requested, self.env_file, quote=False)
            except Exception as e:  # noqa: BLE001 — surfaced in German
                return 500, {"ok": False,
                             "message": f"Robotertyp konnte nicht gespeichert werden: {e}"}
            try:
                # Convenience on top, NOT part of the contract: once the scanned
                # hardware fits the new profile, rewrite the whole managed .env
                # so EDUBOTICS_FOLLOWER_ONLY (and LEADER_PORT) follow the derive
                # immediately rather than at the next „Umgebung starten". A rig
                # that is not ready yet keeps only the id — which is all the scan
                # needs. This must NOT fail the request: the type IS saved by
                # then, so reporting „konnte nicht gespeichert werden" would be a
                # lie, and the one reachable cause is unrelated (a hand-edited
                # CAMERA_NAME_n that rehydrate carried in verbatim).
                self._persist_env_if_ready()
            except Exception as e:  # noqa: BLE001 — logged, never fatal
                self._log("[WARNUNG] Robotertyp gesetzt, aber die Konfiguration "
                          f"konnte nicht neu erzeugt werden: {e}")
        finally:
            self._lifecycle_lock.release()
        row = ROBOT_PROFILES[requested]
        self._log(f"Robotertyp gewählt: {row['display_de']}")
        return 200, {"ok": True, "robot_type": requested,
                     "display_de": row["display_de"],
                     "scan_requires_leader": row["scan_requires_leader"],
                     "hardware_ready": self._hardware_ready(requested),
                     "message": f"Robotertyp gesetzt: {row['display_de']}."}

    # ── POST: /environment/start + /environment/stop ─────────────────────────

    def handle_environment_start(self, body: dict) -> "tuple[int, dict]":
        """„Umgebung starten": bring up the student-owned robot tier. Env-start
        regenerates the .env for the SELECTED robot profile — both-arms on
        ``omx_full`` (where the Roboter-Studio leader toggle is the only path
        that flips to follower-only), follower-only on a profile that has no
        leader at all. Cloud-only start is a no-op — the manager is already up."""
        if self._update_in_flight():
            return self._busy_updating()
        if bool(body.get("cloud_only")):
            return 200, {"ok": True, "cloud_only": True,
                         "message": "Cloud-Modus aktiv — die Weboberfläche läuft bereits."}
        if not self._acquire_lifecycle():
            return self._busy_lifecycle()
        try:
            # PROFILE-AWARE readiness, not "both arms": a follower-only kit has
            # no leader to scan, so demanding one made both omx_follower and
            # edu6_studio unstartable on a Pi.
            robot_type = self._current_robot_type()
            if not self._hardware_ready(robot_type):
                return 400, {
                    "ok": False,
                    "message": (
                        "Bitte zuerst beide Arme scannen (Leader und Follower)."
                        if ROBOT_PROFILES[robot_type]["scan_requires_leader"]
                        else "Bitte zuerst den Roboterarm scannen."
                    ),
                }
            self.stop_active_previews()  # free /dev/video* before usb_cam claims it
            try:
                config_generator.generate_env_file(
                    self._hardware, self.env_file, follower_only=None,
                    robot_type=robot_type)
            except Exception as e:  # noqa: BLE001
                return 500, {"ok": False,
                             "message": f"Konfiguration konnte nicht erstellt werden: {e}"}
            self._log("Roboter-Umgebung wird gestartet …")
            ok = docker_manager.start_robot_tier(log=self._log)
        finally:
            self._lifecycle_lock.release()
        if not ok:
            return 500, {"ok": False,
                         "message": "Die Roboter-Umgebung konnte nicht gestartet werden — "
                                    "bitte das Protokoll prüfen."}
        return 200, {"ok": True, "message": "Roboter-Umgebung gestartet."}

    def handle_environment_stop(self) -> "tuple[int, dict]":
        """„Stoppen": stop the robot tier ONLY (TARGETED — never ``compose
        down``; the manager keeps serving the wizard). The graceful SIGTERM lets
        the entrypoint's torque-disable trap run (Rule §2)."""
        if self._update_in_flight():
            # The update worker stops the robot tier itself as its first step,
            # so a fast 503 here loses nothing — the tier is already going down.
            return self._busy_updating()
        # THE handler the bounded acquire exists for: „Stoppen" is what a student
        # presses when „Umgebung starten" is taking too long, and that start holds
        # the lock across its image pull. Blocking here would make the stop button
        # hang behind the operation it is meant to interrupt.
        if not self._acquire_lifecycle():
            return self._busy_lifecycle()
        try:
            self.stop_active_previews()
            self._log("Roboter-Umgebung wird gestoppt …")
            ok = docker_manager.stop_robot_tier(log=self._log)
        finally:
            self._lifecycle_lock.release()
        return (200 if ok else 500), {
            "ok": bool(ok),
            "message": ("Roboter-Umgebung gestoppt." if ok else
                        "Die Roboter-Umgebung konnte nicht sauber gestoppt werden."),
        }

    # ── POST: /update (ACK early) + GET: /update/status/{id} ─────────────────

    def _evict_stale_update_jobs_locked(self) -> None:
        """Drop terminal (succeeded/failed) job records older than
        ``_UPDATE_JOB_TTL_S`` from the in-memory map — it would otherwise grow
        unbounded over a long uptime. Caller MUST hold ``_update_lock``. The
        most recent job is ALWAYS kept (a late poller may still read its
        terminal state); running jobs are never evicted."""
        if not self._update_jobs:
            return
        newest_id = max(self._update_jobs,
                        key=lambda j: self._update_jobs[j].get("started_at", 0))
        cutoff = time.time() - _UPDATE_JOB_TTL_S
        for job_id in list(self._update_jobs):
            if job_id == newest_id:
                continue
            job = self._update_jobs[job_id]
            if job.get("status") not in ("succeeded", "failed"):
                continue
            if job.get("finished_at", job.get("started_at", 0)) < cutoff:
                del self._update_jobs[job_id]

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
            # Sweep stale terminal records first — /update is the only grower
            # of the job map, so this is the natural eviction point.
            self._evict_stale_update_jobs_locked()
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

    # ── GET: /update/check (read-only availability probe) ────────────────────

    def handle_update_check(self, force: bool = False) -> "tuple[int, dict]":
        """Report whether the cloud advertises a NEWER pi-agent release.

        This is the Pi's parity for the Windows GUI's ``_check_prerequisites``
        first step (``update_checker.check_for_update`` → the forced modal): the
        React ``PiUpdateGate`` polls this and, on ``update_available``, forces
        the update modal before the student continues.

        Read-ONLY: it NEVER triggers the update (``POST /update`` is the sole
        trigger) and NEVER raises out of the handler — any error (offline, cloud
        not advertising the Pi fields yet) reports ``update_available: false``,
        the same backward-compatible posture the GUI takes. The result is cached
        for ``UPDATE_CHECK_TTL_S`` so many student browsers polling at once don't
        each incur the 5 s cloud ``/version`` fetch; ``force`` bypasses the cache
        (a manual re-check) but no more often than
        ``_UPDATE_CHECK_FORCE_MIN_INTERVAL_S`` — an unrated ``?force=1`` is an
        open amplifier, see that constant. The cloud call runs OUTSIDE the cache
        lock so no caller ever blocks waiting on another's network fetch.
        """
        now = time.monotonic()
        with self._update_check_lock:
            if force and (now - self._update_check_forced_at) < _UPDATE_CHECK_FORCE_MIN_INTERVAL_S:
                # Too soon since the last honoured force — degrade to an ordinary
                # cached read rather than refusing, so a double-click still gets
                # a correct answer and only the network call is suppressed.
                force = False
            elif force:
                self._update_check_forced_at = now
            cached = self._update_check_cache
            if not force and cached is not None and (now - cached[0]) < UPDATE_CHECK_TTL_S:
                return 200, dict(cached[1])

        update_available = False
        latest_version = ""
        try:
            upd = update_checker.check_for_agent_update(APP_VERSION, UPDATE_API_URL)
            if upd:
                update_available = True
                latest_version = str(upd.get("version") or "")
        except Exception:  # noqa: BLE001 — the handler must never raise (fail closed)
            update_available = False
            latest_version = ""

        result = {
            "update_available": update_available,
            "current_version": APP_VERSION,
            "latest_version": latest_version,
        }
        with self._update_check_lock:
            self._update_check_cache = (time.monotonic(), result)
        return 200, dict(result)

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

        restart_scheduled = False
        try:
            with self._lifecycle_lock:
                setphase("stopping", 10, "Roboter-Umgebung wird gestoppt …")
                joblog("Roboter-Umgebung wird für die Aktualisierung gestoppt …")
                docker_manager.stop_robot_tier(log=joblog)

                setphase("pulling", 30, "Container-Images werden aktualisiert …")
                # Transient pull failures stay non-fatal (they classify
                # PULL_TRANSIENT inside check_for_updates), so a Wi-Fi blip
                # still degrades gracefully onto the images already on disk —
                # that resilience is deliberate and unchanged. A PULL_MISSING
                # is a different animal: the pinned tag exists on NEITHER
                # registry, which means this release's `-opi` images were never
                # published (the opi build leg is continue-on-error in
                # docker-publish.yml, so it can skip silently while the agent
                # tarball ships anyway). Waiting cannot fix it, and on a freshly
                # flashed Pi there are no local images to fall back to — so
                # reporting „Aktualisierung abgeschlossen" over it is exactly
                # how a rig ends up on the wrong containers with nobody the
                # wiser. Fail the job instead; the except below turns this into
                # the card's German failure message.
                missing: list = []
                docker_manager.check_for_updates(log=joblog, missing_out=missing)
                if missing:
                    # Plural matters here: the scenario this message exists for
                    # is a SKIPPED opi build leg, which leaves all three -opi
                    # images untagged — so „Images … sind" is the common case
                    # and „Image … ist" the rare one. Mirror docker_manager's
                    # wording: claim only GHCR's contents (which we established)
                    # and say of Hub only that it did not yield the image.
                    noun, verb = (("Image", "ist") if len(missing) == 1
                                  else ("Images", "sind"))
                    raise RuntimeError(
                        f"Version {IMAGE_TAG} ist unvollständig veröffentlicht — "
                        f"{noun} {', '.join(missing)} {verb} bei GHCR nicht "
                        "vorhanden und auch über Docker Hub nicht zu beziehen. "
                        "Das ist kein Netzwerkfehler. Bitte die Version dem "
                        "EduBotics-Team melden."
                    )

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

                # Apply the staged agent tarball BEFORE publishing any terminal
                # status. Writing "succeeded" first and correcting it to "failed"
                # afterwards looks harmless but is not: React's useAgentUpdate
                # polls every 2 s and STOPS the loop at the first non-"running"
                # status, so a poll landing anywhere inside the apply window
                # latches "succeeded" forever — PiUpdateGate calls setDone(),
                # dismisses the modal, and never reveals „Ohne Update
                # fortfahren". Measured against this code with a randomized poll
                # phase: 41 % of runs for a 0.5 s apply, 58 % at 1.5 s, and 100 %
                # at ≥2 s. Since APP_VERSION is unchanged (no restart happened),
                # the forced modal then re-arms on every page load — a
                # persistently-failing apply (full disk, read-only /opt) loops
                # forever with no way out. That disk-full loop is the exact
                # scenario this reporting exists to break, so the terminal status
                # must be written ONCE, after the outcome is known.
                #
                # The happy path stays intact: _apply_agent_update_and_restart
                # defers its SIGTERM by 2.0 s precisely so the poller can read the
                # terminal state, and we publish "succeeded" inside that window,
                # immediately on return.
                if staged_tarball:
                    joblog("Agent-Aktualisierung wird angewendet — der Agent startet neu.")
                    restart_scheduled = self._apply_agent_update_and_restart(staged_tarball)
                    if not restart_scheduled:
                        # The apply FAILED (rsync error — /opt read-only, or a
                        # full disk). The running agent stays alive and the
                        # images/manager DID update — only the agent self-update
                        # did not. Report the REAL outcome so useAgentUpdate
                        # reveals the skip button.
                        joblog("Agent-Aktualisierung nicht angewendet — der Agent läuft "
                               "unverändert weiter.")

                with self._update_lock:
                    j = self._update_jobs.get(job_id)
                    if j is not None:
                        j["finished_at"] = int(time.time())
                        if staged_tarball and not restart_scheduled:
                            j["status"] = "failed"
                            j["agent_restarting"] = False
                            j["message"] = (
                                "Agent-Aktualisierung konnte nicht angewendet "
                                "werden — bitte Speicherplatz und Verbindung "
                                "prüfen und erneut versuchen."
                            )
                        else:
                            j["status"] = "succeeded"
                            j["progress"] = 100
                            j["phase"] = "done"
                            j["message"] = "Aktualisierung abgeschlossen."
                            if restart_scheduled:
                                j["agent_restarting"] = True
                                j["message"] = (
                                    "Aktualisierung abgeschlossen — der Agent startet neu.")
        except Exception as e:  # noqa: BLE001 — report, never crash the thread
            with self._update_lock:
                j = self._update_jobs.get(job_id)
                if j is not None:
                    j["status"] = "failed"
                    j["finished_at"] = int(time.time())
                    j["message"] = f"Aktualisierung fehlgeschlagen: {e}"
            self._log(f"Update job {job_id} failed: {e}")
        finally:
            # Release the single-flight guard so a later /update can run —
            # EXCEPT when an agent self-restart is pending: the process SIGTERMs
            # itself in ~2 s, and releasing here would let a second /update
            # start stop/pull/recreate work inside a dying process. The flag
            # then resets naturally when systemd boots the fresh agent.
            with self._update_lock:
                if not restart_scheduled:
                    self._update_busy = False

    def _apply_agent_update_and_restart(self, tarball_path: str) -> bool:
        """Unpack the verified agent tarball over the install root and trigger a
        clean process exit so systemd (``Restart=always``) restarts the new
        agent. Best-effort: a failed apply keeps the running agent alive.
        Returns True iff the deferred restart was actually scheduled — the
        caller keeps ``_update_busy`` held through a pending restart (a second
        /update must not start inside a process about to SIGTERM itself) and
        releases it on a failed apply.

        The install root is the compose file's directory (``/opt/edubotics`` in
        the field, per setup.sh P4). The tarball's top-level dir is ``pi_agent/``
        — which is NO LONGER "agent code only". That tree now also carries the
        REFERENCE copies of the system files this agent version expects: the opi
        compose twin (``SHIPPED_COMPOSE_RELPATH``), ``systemd/*.service``,
        ``udev/*.rules``, the CI-baked image pin ``docker/versions.env``, and the
        first-boot script the firstboot unit ``ExecStart``s IN PLACE out of this
        very tree (so that one moves with the swap directly, no repair needed).
        This apply installs none of them into ``/etc`` or the install root — it
        swaps in the new reference bytes, and the NEXT boot's
        ``_check_system_files_version`` byte-compares those against what is
        installed and repairs the difference. Net effect: a release that changes
        the compose, a systemd unit or a udev rule DOES reach a fielded Pi
        through this path. It did not until 2026-07-28, and this docstring
        asserting the opposite is a large part of why six compose changes
        shipped without anyone noticing they stopped at the tarball.

        What still requires re-running ``setup.sh`` is exactly what has no
        shipped reference copy to compare against: ``physical_ai_server/.s6-keep``
        (0 bytes — nothing a byte compare could ever find), the
        ``/opt/edubotics/VERSION`` convenience copy (superseded on this path by
        the packaged ``pi_agent/VERSION``), and the ``.system-files-version``
        stamp (deliberately frozen — see ``SYSTEM_FILES_VERSION_FILE``).

        We extract to a TEMP dir (tar ``data`` filter — path-traversal / device-node
        guard; the pre-PEP-706 fallback replicates those guards manually via
        ``_validate_tar_member``), rsync it into a SIBLING ``pi_agent.new/`` (a
        complete fresh tree, so a module REMOVED in a release simply isn't
        there), ``fsync`` it, then swap it into place by rename. That is
        crash-safe where an in-place ``rsync --delete`` was not: an interrupt can
        never leave a mixed old/new tree that ImportErrors into a crash-loop, the
        fsync stops the rename from committing ahead of the staged data, and the
        refreshed ``pi_agent/VERSION`` (which the running agent reports) lands
        together with the rest. One window is accepted rather than closed: a
        crash BETWEEN the two renames leaves no ``pi_agent/`` at all, recoverable
        by hand from ``pi_agent.old`` — see the comment at the swap.
        """
        install_root = os.path.dirname(os.path.abspath(self.compose_file)) or "/opt/edubotics"
        staging = None
        try:
            staging = tempfile.mkdtemp(prefix="edubotics-agent-update-")
            with tarfile.open(tarball_path, "r:gz") as tf:
                try:
                    tf.extractall(staging, filter="data")  # py>=3.12 safe filter
                except TypeError:
                    # Older Python without `filter=`: replicate the data
                    # filter's traversal/absolute-path/special-member guards
                    # before extracting (a bad member raises → apply aborts).
                    members = tf.getmembers()
                    for m in members:
                        _validate_tar_member(m)
                    tf.extractall(staging, members=members)
            src_pkg = os.path.join(staging, "pi_agent")
            if not os.path.isdir(src_pkg):
                self._log("Agent-Aktualisierung: kein pi_agent/-Verzeichnis im Archiv — abgebrochen.")
                return False
            dst_pkg = os.path.join(install_root, "pi_agent")
            new_pkg = dst_pkg + ".new"
            old_pkg = dst_pkg + ".old"
            # ATOMIC SWAP (M2): build the COMPLETE new tree in a sibling dir and
            # swap it in by rename, instead of rsync --delete over the LIVE tree.
            # An in-place --delete is not tree-atomic — a classroom power yank on
            # eMMC mid-rsync can leave a mixed old/new pi_agent/ (a new agent.py
            # beside an old constants.py, or a half-deleted module) that
            # ImportErrors under systemd Restart=always into a crash-loop with no
            # self-heal. Copying into an EMPTY sibling also means there is nothing
            # to --delete and no reason to --checksum (that flag guarded against a
            # same-byte-length VERSION bump being skipped by size+mtime — but the
            # sibling has no prior file to skip against), removing the eMMC
            # read+hash of every file the old path did.
            shutil.rmtree(new_pkg, ignore_errors=True)  # clear a prior interrupted apply
            result = subprocess.run(
                ["rsync", "-a",
                 "--exclude=tests", "--exclude=__pycache__", "--exclude=*.pyc",
                 src_pkg + "/", new_pkg + "/"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                self._log("Agent-Aktualisierung konnte nicht angewendet werden: "
                          f"{(result.stderr or '').strip()[:200]}")
                shutil.rmtree(new_pkg, ignore_errors=True)
                return False
            # Validate the staged tree BEFORE the swap: a tarball that unpacks
            # but does not even byte-compile (truncated content the hash check
            # cannot catch — the hash covers the tarball, not the release's
            # correctness — or a release built for a newer Python) must never
            # replace a working agent. Post-swap it would crash-loop under
            # Restart=always with the agent itself being the ONLY repair
            # surface on the Pi — recovery would need SSH on every fleet rig.
            check = subprocess.run(
                [sys.executable, "-m", "compileall", "-q", new_pkg],
                capture_output=True, text=True, timeout=120,
            )
            if check.returncode != 0:
                detail = (check.stderr or check.stdout or "").strip()[:200]
                self._log("Agent-Aktualisierung verworfen: die neue Version "
                          f"besteht die Syntaxprüfung nicht: {detail}")
                shutil.rmtree(new_pkg, ignore_errors=True)
                return False
            # Force the staged bytes to stable storage BEFORE the swap. rsync
            # does not fsync, and a directory rename gets no help from ext4's
            # auto_da_alloc (see _fsync_tree) — so without this a power cut can
            # commit the rename over unallocated data and leave zero-length
            # modules behind. Costs one flush of a small pure-Python tree.
            _fsync_tree(new_pkg)
            # Swap by rename: move the live tree aside to pi_agent.old, then move
            # the staged tree into place. Each rename is atomic on the same
            # filesystem, but the PAIR is not: there is a window of two rename
            # syscalls with no I/O between them (microseconds, vs the
            # multi-second in-place rsync it replaces) in which pi_agent/ does
            # not exist at all. A crash exactly there is self-healed by the
            # unit's ExecStartPre (restores pi_agent.old when pi_agent/ is
            # missing) — keep the two in sync.
            #
            # DELIVERY, precisely: that ExecStartPre reaches a Pi through
            # setup.sh (provisioning) or through _renew_system_units, which
            # installs the shipped units on the FIRST boot after a self-update
            # that changed them. So a Pi updating INTO the release that added it
            # runs this very apply under the OLD unit — that one window is
            # genuinely unprotected (recover by hand: mv pi_agent.old pi_agent)
            # — and is protected from the next apply onward. It is NOT, as an
            # earlier version of this comment implied, available to every Pi
            # running this code the moment the code lands.
            #
            # Keep pi_agent.old for ONE cycle as a rollback (the NEXT apply's
            # rmtree above clears it).
            shutil.rmtree(old_pkg, ignore_errors=True)  # drop the previous cycle's rollback
            try:
                if os.path.isdir(dst_pkg):
                    os.replace(dst_pkg, old_pkg)
                os.replace(new_pkg, dst_pkg)
                # Persist the two directory entries themselves before we go on to
                # SIGTERM ourselves — otherwise a power cut in the ~2 s restart
                # window can roll the swap back and the agent silently restarts
                # on the OLD tree after reporting the update applied.
                _fsync_path(install_root)
            except OSError as e:
                self._log(f"Agent-Aktualisierung: Verzeichnistausch fehlgeschlagen: {e}")
                # Best-effort recovery: if the live tree was moved aside but the
                # staged tree failed to land, put the old one back so the agent
                # still has a complete package to restart into.
                if not os.path.isdir(dst_pkg) and os.path.isdir(old_pkg):
                    try:
                        os.replace(old_pkg, dst_pkg)
                    except OSError:
                        pass
                shutil.rmtree(new_pkg, ignore_errors=True)
                return False
        except Exception as e:  # noqa: BLE001
            self._log(f"Agent-Aktualisierung konnte nicht entpackt werden: {e}")
            return False
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
        return True

    # ── POST: /factory-reset (double-confirm) ────────────────────────────────

    def handle_factory_reset(self, body: dict) -> "tuple[int, dict]":
        """Delete the persistent data volumes (datasets, HF cache, Roboter-Studio
        calibration). Requires a DOUBLE confirmation and never ``compose down``
        (that would delete ros_net + drop the manager)."""
        if self._update_in_flight():
            return self._busy_updating()
        if not (bool(body.get("confirm")) and bool(body.get("confirm_again"))):
            return 400, {"ok": False,
                         "message": "Doppelte Bestätigung erforderlich (confirm + confirm_again)."}
        if not self._acquire_lifecycle():
            return self._busy_lifecycle()
        try:
            self.stop_active_previews()
            ok, msg = docker_manager.factory_reset(log=self._log)
        finally:
            self._lifecycle_lock.release()
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
        contract, plus ``ready`` (arm container running) like the GUI badge and
        ``has_leader`` (this rig's profile has a leader arm at all).

        ``has_leader`` is ADDITIVE and Pi-only: on Windows the toggle self-hides
        because the ``:8769`` bridge is NEVER CONSTRUCTED for a follower-only
        rig, but on a Pi the same contract is served by the always-on agent, so
        the bridge probe always succeeds. The React toggle therefore also hides
        on an explicit ``has_leader === false`` — closing the window between
        mount and the first ROS capability tick, where `caps` is still undefined
        and the toggle would otherwise render on a rig that has no leader. The
        Windows bridge simply omits the key (undefined ≠ false), so its
        behaviour is unchanged. ``handle_rs_set_mode`` is the belt behind it.
        """
        follower_only = str(
            config_generator.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_file)).strip() == "1"
        has_leader = not ROBOT_PROFILES[self._current_robot_type()]["follower_only"]
        with self._rs_busy_lock:
            busy = self._rs_busy or self._rs_switch_in_flight
        if busy:
            return 200, {"follower_only": follower_only, "ready": False, "busy": True,
                         "has_leader": has_leader}
        try:
            arm = docker_manager.get_container_status().get("open_manipulator", "not found")
        except Exception:  # noqa: BLE001
            arm = "error"
        return 200, {"follower_only": follower_only, "ready": arm == "running", "busy": False,
                     "has_leader": has_leader}

    def handle_rs_set_mode(self, follower_only: bool) -> "tuple[int, dict]":
        """POST /roboter-studio/leader-disable|enable → switch the arm mode and
        recreate ONLY open_manipulator. Busy-locked so a second click can't race
        two ``compose up`` calls on the same container. The .env rollback on a
        failed restart lives in ``docker_manager.set_leader_mode`` (the ported
        ``gui_app._rs_set_leader_mode`` callback).

        REFUSED OUTRIGHT ON A LEADER-LESS PROFILE, in BOTH directions. This is
        the belt behind the React hide: on Windows a follower-only rig never
        constructs the ``:8769`` bridge at all (``gui_app._start_rs_control_
        server``), but the Pi's bridge is the always-on agent, so the lockout
        has to live here. „Leader verbinden" would ask ``generate_env_file`` for
        a both-arms .env on a rig that has no leader and hit its contradiction
        guard as an opaque 500; „Leader abschalten" would recreate the arm
        container — a ~20 s outage — to reach the mode it is already in.
        """
        robot_type = self._current_robot_type()
        if ROBOT_PROFILES[robot_type]["follower_only"]:
            return 409, {"ok": False, "follower_only": True,
                         "message": "Dieser Robotertyp hat keinen Leader-Arm — "
                                    "Roboter Studio ist hier dauerhaft aktiv."}
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
        """Supersede any in-flight MJPEG preview loop so the device is released
        BEFORE the stack (usb_cam) or a scan claims it. Bumping the generation
        counter (instead of the old set→sleep→clear Event) means a loop blocked
        in a frame read can never miss the stop window — its next generation
        check fails and it exits."""
        with self._preview_cond:
            self._preview_gen += 1
            self._preview_cond.notify_all()

    def _preview_generation(self) -> int:
        with self._preview_cond:
            return self._preview_gen

    def stream_camera_preview(self, handler: BaseHTTPRequestHandler, device: str) -> None:
        """Stream a live MJPEG preview of ``device`` (multipart/x-mixed-replace).

        One preview at a time: a new preview SUPERSEDES the previous one via the
        generation counter and then waits (bounded) until the old loop has
        provably released its VideoCapture before re-opening the device — the
        old fixed ``sleep(0.1)`` stop-window could be missed by a loop blocked
        in a frame read, leaking the capture handle. If OpenCV is unavailable
        (bare host) or the device can't be opened, a German 503 is sent. The
        loop ends when the client disconnects, on shutdown, or when a lifecycle
        op / newer preview bumps the generation.
        """
        with self._preview_cond:
            # Don't re-grab /dev/video* while a lifecycle op (env-start / update /
            # scan) is claiming the cameras for usb_cam: a preview landing in that
            # window would steal the device and break the stack's camera open (the
            # M2 race — the two paths otherwise use different locks). Probe
            # _lifecycle_lock WITHOUT blocking so a preview never stalls a
            # lifecycle op, and hold it only across the handoff + device open —
            # env-start's stop_active_previews() then bumps the generation to end
            # this loop and free the device again.
            if not self._lifecycle_lock.acquire(blocking=False):
                self._send_simple(handler, 503,
                                  "Kameravorschau nicht verfügbar (die Roboter-Umgebung "
                                  "wird gerade gestartet oder aktualisiert).")
                return
            try:
                # Supersede any prior preview, then WAIT until its loop has
                # deregistered (cond.wait releases the lock; the old loop's
                # finally-block notifies). A newer preview arriving meanwhile
                # bumps the generation past ours — we yield to it.
                self._preview_gen += 1
                my_gen = self._preview_gen
                self._preview_cond.notify_all()
                deadline = time.monotonic() + _PREVIEW_HANDOFF_TIMEOUT_S
                while self._preview_active and self._preview_gen == my_gen:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._preview_cond.wait(remaining)
                if self._preview_gen != my_gen or self._preview_active:
                    # Superseded while waiting, or the old loop never released.
                    self._send_simple(handler, 503,
                                      "Kameravorschau nicht verfügbar (die vorherige "
                                      "Vorschau wird noch beendet).")
                    return
                cap = _PreviewCapture(device)
                opened = cap.open()
                if opened:
                    self._preview_active[device] = my_gen
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
            while not self._shutdown.is_set() and self._preview_generation() == my_gen:
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
            # Release the device FIRST, then deregister + notify so a waiting
            # successor provably re-opens only after the capture is closed.
            cap.release()
            with self._preview_cond:
                if self._preview_active.get(device) == my_gen:
                    del self._preview_active[device]
                self._preview_cond.notify_all()

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

            # ---- GET routing (open; not Origin-gated). Mostly read-only, with
            # two DELIBERATE documented exceptions: /cameras/scan and
            # /cameras/preview stop any live preview first (the device must be
            # released before it can be re-opened). The React callers depend on
            # the GET method for both, so the method stays — the mutation is
            # confined to the agent's own preview session, never to the .env,
            # containers, or hardware config. ----
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
                    # Documented GET exception: supersedes (stops) the previous
                    # preview loop before opening the device — see do_GET's
                    # routing comment and stream_camera_preview.
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
                elif path == "/update/check":
                    # `force` skips the poll-storm cache, so it is the one bit of
                    # this read-only endpoint worth gating. A browser omits Origin
                    # on a no-cors GET, so an ABSENT header proves nothing and
                    # stays allowed (same posture as origin_allowed's empty case,
                    # and the header-less curl path) — but a PRESENT, foreign
                    # Origin is a cross-site caller with no business bypassing the
                    # cache, and it silently gets the cached answer. The real
                    # bound on the no-cors case is the rate floor inside
                    # handle_update_check; this is the cheap layer on top.
                    force = (query.get("force") or query.get("refresh")
                             or [""])[0] in ("1", "true", "yes")
                    if force:
                        try:
                            force = app.origin_allowed(self.headers.get("Origin", ""))
                        except Exception:  # noqa: BLE001 — /update/check never 500s
                            force = False
                    self._send_json(*app.handle_update_check(force=force))
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
                elif path == "/robot-type":
                    self._send_json(*app.handle_set_robot_type(body))
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
        """Bind the always-available 127.0.0.1 listener. Never raises.

        A bind failure here used to escape ``boot()`` into ``main()``'s bare
        ``finally`` and exit the process — which under ``Restart=always`` +
        ``RestartSec=5`` is a 5 s crash-loop, and the most likely cause is
        precisely the one that makes the loop self-sustaining: :8769 still held
        by a stale agent process, or by the loopback socket of an agent systemd
        has not finished reaping. The loopback listener is also the LEAST
        load-bearing of the two — the browser reaches the wizard through the
        ros_net-gateway listener (nginx cannot proxy to the Pi's ``localhost``
        from a remote Chrome), and the gateway binder retries forever on its own.
        So log it in German and carry on booting; the manager still comes up and
        the Protokoll still explains what happened.
        """
        handler = self._make_handler()
        try:
            httpd = ThreadingHTTPServer((_LOOPBACK_HOST, PORT_AGENT), handler)
        except OSError as e:
            self._log(
                f"[WARNUNG] Management-API konnte nicht an {_LOOPBACK_HOST}:{PORT_AGENT} "
                f"gebunden werden: {e}. Der Agent läuft weiter; die Weboberfläche "
                "erreicht ihn über das ros_net-Gateway. Läuft eventuell noch ein "
                "zweiter Agent-Prozess?"
            )
            return
        self._loopback_httpd = httpd
        httpd.daemon_threads = True
        threading.Thread(
            target=self._serve, args=(httpd,),
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

    def _pull_images_after_self_update(self) -> None:
        """Resiliently pull the NEW pinned `-opi` images right after a
        self-update landed (a boot-time IMAGE_TAG mismatch is the signal).

        Reuses ``docker_manager.check_for_updates`` so the arm64 digest
        pre-check, the GHCR→Docker-Hub-twin fallback, the PULL_MISSING
        classification AND the superseded-tag prune all apply to the exact
        images an update installs — instead of leaving them to start_manager's
        GHCR-only lazy compose pull. A PULL_MISSING (the pinned tag exists on
        NEITHER registry → this release's `-opi` images were never published;
        the opi build leg is continue-on-error in docker-publish.yml, so it can
        skip silently while the agent tarball ships anyway) is reported LOUDLY in
        German naming the version, so the failure is not mistaken for a network
        fault and the manager is not recreated onto a release that does not
        exist. Never raises out of boot."""
        missing: list = []
        try:
            docker_manager.check_for_updates(log=self._log, missing_out=missing)
        except Exception as e:  # noqa: BLE001 — never block boot on the pull
            self._log(f"Images konnten nach der Aktualisierung nicht geladen werden: {e}")
            return
        if missing:
            # Mirror _run_update_job's wording (plural is the common case — a
            # skipped opi build leaves all three -opi images untagged): claim
            # only GHCR's contents and say of Hub only that it did not yield them.
            noun, verb = (("Image", "ist") if len(missing) == 1
                          else ("Images", "sind"))
            self._log(
                f"[FEHLER] Version {IMAGE_TAG} ist unvollständig veröffentlicht — "
                f"{noun} {', '.join(missing)} {verb} bei GHCR nicht vorhanden und "
                "auch über Docker Hub nicht zu beziehen. Das ist kein "
                "Netzwerkfehler. Bitte die Version dem EduBotics-Team melden."
            )

    def _read_system_files_stamp(self) -> "Optional[str]":
        """The version setup.sh recorded for the on-disk system files, or None.

        CONTEXT ONLY — never a trigger (see ``_check_system_files_version``).
        Absent / unreadable / blank all degrade to None: Pis provisioned before
        the stamp existed have no file, and a truncated write must not be read
        as the literal version ''.
        """
        try:
            with open(SYSTEM_FILES_VERSION_FILE, "r", encoding="utf-8") as f:
                stamped = f.read().strip()
        except (OSError, UnicodeDecodeError):
            return None
        return stamped or None

    def _shipped_dir(self, subdir: str) -> str:
        """Absolute path to a subdirectory of the `pi_agent` package THIS agent
        version ships from. Resolved off ``__file__`` so it follows the package
        through the self-update swap (rsync into ``pi_agent.new`` → rename over
        ``pi_agent``) with no path knowledge anywhere else."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), subdir)

    def _drifted_in(self, shipped_dir: str, installed_dir: str,
                    names: "tuple") -> "list":
        """Names whose INSTALLED copy differs byte-for-byte from the copy THIS
        agent version ships. Hard evidence, not inference.

        These files ship inside the ``pi_agent`` package (so a self-update rsync
        refreshes the agent's copy) while setup.sh installed them VERBATIM into a
        system directory self-update never touches. A byte difference therefore
        proves the running Pi is on a file older than this agent expects;
        equality proves it is not. Either side being absent or unreadable (a dev
        box, a hand-relocated install, a non-root reader) proves nothing, so it
        is skipped — this must never guess.
        """
        drifted = []
        for name in names:
            try:
                with open(os.path.join(shipped_dir, name), "rb") as f:
                    shipped = f.read()
                with open(os.path.join(installed_dir, name), "rb") as f:
                    installed = f.read()
            except OSError:
                continue  # cannot compare → cannot prove → silent
            if shipped != installed:
                drifted.append(name)
        return drifted

    def _drifted_units(self) -> "list":
        """Systemd units whose INSTALLED copy differs from the one this agent
        ships. A unit the Pi does not have at all is NOT in here (see
        ``_drifted_in``) — absent is not drift."""
        return self._drifted_in(self._shipped_dir("systemd"), SYSTEMD_UNIT_DIR,
                                SYSTEMD_UNIT_NAMES)

    def _drifted_udev_rules(self) -> "list":
        """Udev rules whose INSTALLED copy differs from the one this agent
        ships. Like the units: a rule the Pi does not have installed at all is
        skipped, never "helpfully" created.

        The same fail-open the compose had, one layer down and easier to miss
        because a udev rule looks inert: it only sets a permission floor
        (group=dialout, mode 0660) plus a stable ``TAG+="edubotics-arm"`` on the
        arm boards' tty nodes — it is explicitly NOT the leader/follower role
        source. A release that adds a VID/PID line — the planned CH343P line for
        the edu6 arm is precisely that — would otherwise ship an agent that
        knows how to scan for an arm whose ``/dev`` node its scanner container
        cannot open, and the drift would be invisible because nothing compared
        the files.

        Consequence worth naming, shared with the systemd leg: a locally
        hand-added rule LINE is drift, so it is reverted (with a German
        Protokoll line and one ``.edubotics-bak``) on the next agent boot. That
        is the intended semantics — the shipped bytes are the definition — but a
        udev rule is a far more plausible thing for an admin to hand-edit than a
        systemd unit, so the local workaround for a not-yet-shipped board is to
        add a SEPARATE rules file, not to edit this one.
        """
        return self._drifted_in(self._shipped_dir("udev"), UDEV_RULES_DIR,
                                UDEV_RULE_NAMES)

    def _install_system_file(self, src: str, dst: str, what_de: str,
                             validate=None) -> bool:
        """Overwrite ONE installed system file with the copy this agent ships.

        Atomic (temp in the same directory → fsync → chmod 0644 → ``os.replace``)
        because a torn write to ``edubotics-pi.service`` would leave the fleet
        with no way to start the agent at all — and the agent IS the only repair
        surface on a Pi. The compose file has the same property one level up: a
        truncated ``docker-compose.opi.yml`` means no manager container, i.e. no
        wizard, i.e. no way in. One rollback copy of whatever was there is kept
        beside it (``.edubotics-bak``); neither the backup nor a leftover temp
        file can ever be LOADED, and that has to hold separately for each of the
        three destinations: systemd ignores files without a unit suffix, udev
        only reads files ending in ``.rules`` (so a stale ``…rules.edubotics-bak``
        — which would otherwise sort AFTER the real rule and win — is invisible
        to it), and the agent only ever passes ``COMPOSE_FILE`` to ``-f``.
        A fourth destination must re-establish this before it is added.

        The explicit chmod matters: the unit runs with ``UMask=0077``, so a
        freshly created file would be 0600 while setup.sh installs 0644.

        ``validate`` is an optional ``(tmp_path) -> Optional[str]`` gate run on
        the STAGED bytes after the chmod and BEFORE anything mutating: return a
        German reason to refuse the swap (the installed file is then left
        untouched and this returns False), or None to accept. The systemd path
        passes none — see ``_renew_compose`` for why the compose does.
        """
        tmp = dst + ".edubotics-tmp"
        try:
            with open(src, "rb") as f:
                shipped = f.read()
            with open(tmp, "wb") as f:
                f.write(shipped)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            if validate is not None:
                refusal = validate(tmp)
                if refusal:
                    self._log(f"{what_de} wurde NICHT erneuert: {refusal} Die "
                              "bisherige, funktionierende Datei bleibt unverändert.")
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    return False
            try:
                shutil.copyfile(dst, dst + ".edubotics-bak")
            except OSError:
                pass  # a backup is a courtesy, never a precondition
            os.replace(tmp, dst)
            _fsync_path(os.path.dirname(dst))
            return True
        except OSError as e:
            self._log(f"{what_de} konnte nicht erneuert werden: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def _install_unit_file(self, unit: str) -> bool:
        """Overwrite ONE installed systemd unit with the copy this agent ships.

        Thin wrapper over ``_install_system_file`` — the atomic sequence lives
        there once, so the compose repair cannot drift away from the unit
        repair it is modelled on.
        """
        return self._install_system_file(
            os.path.join(self._shipped_dir("systemd"), unit),
            os.path.join(SYSTEMD_UNIT_DIR, unit),
            f"Systemd-Dienst {unit}",
        )

    def _install_udev_rule(self, rule: str) -> bool:
        """Overwrite ONE installed udev rule with the copy this agent ships.

        Same atomic sequence as the unit path, deliberately: a torn write here
        is a rule file udev parses PARTIALLY (it reads line by line and skips
        what it cannot parse), i.e. a silent, partial permission floor — the
        failure mode hardest to diagnose from a classroom.
        """
        return self._install_system_file(
            os.path.join(self._shipped_dir("udev"), rule),
            os.path.join(UDEV_RULES_DIR, rule),
            f"Udev-Regel {rule}",
        )

    def _install_each(self, names: "list", install) -> "tuple":
        """Attempt EVERY name and return ``(repaired, failed)``. Never
        short-circuits, and never reports a file it did not actually touch.

        This replaces ``all(install(n) for n in names)``, which was wrong twice
        over on a set with more than one entry — and both halves were MEASURED
        against the real code with the second unit's ``os.replace`` raising
        EPERM:

        * ``all()`` over a GENERATOR stops at the first False, so a failure on
          the FIRST file left every later file unattempted
          (``edubotics-pi-firstboot.service swapped=False``, no
          ``.edubotics-bak``) even where it would have succeeded. The agent
          silently repaired less than it could.
        * The single bool then made the caller name the WHOLE drifted set as
          unrepaired, so a file that HAD been swapped was still reported to the
          operator as broken and got no success line — while the System-tab
          banner's „mit der zuletzt funktionierenden Konfiguration" was false
          for exactly that file.

        Returning the two lists is what lets the caller say only what is true of
        each file. A mixed outcome stays possible — there is no transaction
        across several ``os.replace`` calls, and rolling the successes back
        would mean re-installing bytes we just proved stale — but it is now
        REPORTED as mixed instead of flattened into one verdict, and every file
        named in the banner really is byte-intact at its previous version.
        """
        repaired: list = []
        failed: list = []
        for name in names:
            (repaired if install(name) else failed).append(name)
        return repaired, failed

    def _renew_system_units(self, drifted: "list") -> "tuple":
        """Install the shipped systemd units over the drifted installed copies
        and reload systemd. Returns ``(repaired, failed)`` — see
        ``_install_each`` for why this is not one bool.

        This is what makes the drift signal actionable instead of decorative.
        The units only ever reach a Pi through ``setup.sh``, which needs a source
        checkout the classroom does not have — so a release that changes a unit
        (this one changes ``EnvironmentFile=-`` and adds ``ExecStartPre``) would
        otherwise pin a permanent banner on 100 % of the fielded fleet asking for
        a remedy nobody can perform, AND leave the ``ExecStartPre`` self-heal
        undeliverable to exactly the population running the code that relies on
        it. The agent already runs as root and already holds the correct bytes
        (SHA-256-verified and byte-compiled at apply time), so it can simply do
        the install itself.

        Deliberately NOT a restart. ``daemon-reload`` makes systemd re-read the
        files; the new ``ExecStartPre`` / ``EnvironmentFile`` take effect the next
        time the unit STARTS. Restarting ourselves from inside a boot sequence to
        pick up a cosmetic unit change would risk a loop for no gain — and by the
        next apply (the only moment the ExecStartPre matters) the renewal is long
        since in place.

        Only ever REPLACES: ``_drifted_units`` skips a unit whose installed copy
        cannot be read, so an absent unit is never "installed" here — which is
        what keeps this out of `systemctl enable` territory.
        """
        repaired, failed = self._install_each(drifted, self._install_unit_file)
        if repaired:
            # Reload iff something actually changed on disk. Identical to the
            # previous all-or-nothing behaviour on the all-succeed path; the
            # difference is only that a PARTIAL repair now also gets its reload,
            # which it must — systemd would otherwise keep serving the old
            # definition of a unit whose file we just replaced.
            try:
                subprocess.run(["systemctl", "daemon-reload"],
                               capture_output=True, text=True, timeout=30)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                # No systemd (dev box) or a reload hiccup: the FILES are
                # correct, which is the durable half — systemd re-parses every
                # unit at boot anyway. Never fail the renewal over the reload.
                pass
        return repaired, failed

    def _renew_udev_rules(self, drifted: "list") -> "tuple":
        """Install the shipped udev rules over the drifted installed copies and
        make udev re-read them. Returns ``(repaired, failed)``, exactly like the
        systemd leg — see ``_install_each``.

        ``UDEV_RULE_NAMES`` carries one entry today, so the short-circuit this
        shares the fix for is currently inert here. It is fixed anyway because
        the set is about to grow: the planned CH343P line for the edu6 arm
        arrives as a rule change, and a second rule file is the obvious next
        step. A latent bug in a repair path is not worth keeping until it has
        something to bite.

        RELOAD, NEVER TRIGGER — the one thing to get right here, and the exact
        analogue of the systemd path's "``daemon-reload``, never ``restart``".
        ``udevadm control --reload-rules`` only makes the new rule set KNOWN to
        udevd; it changes nothing about devices that are already enumerated.
        ``udevadm trigger`` would additionally REPLAY add/change events for live
        devices, which on a Pi mid-lesson means re-firing events for the very
        tty nodes an open serial connection is holding — an unannounced
        disturbance to a running session, from a repair that is supposed to be
        advisory. The new floor therefore applies from the next replug or the
        next boot, which is when it can matter (a rule that grants access to an
        arm the running session already opened has nothing to fix).

        setup.sh, which runs before anything is live, is free to trigger — and
        does. That asymmetry is intentional, not an oversight.

        A failed reload is NON-fatal, exactly like a failed ``daemon-reload``:
        the FILES are correct, which is the durable half, and udev re-reads
        ``/etc/udev/rules.d`` at boot regardless. Never fail the renewal over it.

        Only ever REPLACES: ``_drifted_udev_rules`` skips a rule whose installed
        copy cannot be read, so a rule the Pi never had is never installed here.
        """
        repaired, failed = self._install_each(drifted, self._install_udev_rule)
        if repaired:
            try:
                subprocess.run(["udevadm", "control", "--reload-rules"],
                               capture_output=True, text=True, timeout=30)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass  # no udev (dev box) or a hiccup — the files are correct
        return repaired, failed

    def _shipped_compose_path(self) -> str:
        """Absolute path to the opi compose file THIS agent version ships.

        Resolved off ``__file__`` exactly like the systemd units, so it follows
        the package through the self-update swap (rsync into ``pi_agent.new`` →
        rename over ``pi_agent``) with no path knowledge anywhere else.
        """
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            SHIPPED_COMPOSE_RELPATH)

    def _compose_drifted(self) -> bool:
        """True iff the INSTALLED opi compose differs byte-for-byte from the one
        THIS agent version ships. The compose half of ``_drifted_units``.

        Same evidence rule, deliberately: either file being absent or unreadable
        (a dev box, a hand-relocated install, a non-root reader, a Pi
        provisioned before the twin shipped) proves nothing, so it is skipped —
        never repaired, never bannered. This must never guess: the alternative
        reading, "the installed file is missing, so install ours", would turn a
        relocated ``EDUBOTICS_OPI_COMPOSE`` into a surprise write.
        """
        try:
            with open(self._shipped_compose_path(), "rb") as f:
                shipped = f.read()
            with open(COMPOSE_FILE, "rb") as f:
                installed = f.read()
        except OSError:
            return False  # cannot compare → cannot prove → silent
        return shipped != installed

    def _validate_compose_file(self, path: str) -> "Optional[str]":
        """None if ``docker compose -f <path> config -q`` accepts the staged
        file, otherwise a German reason to refuse the swap.

        The question here is whether the SHIPPED BYTES are a valid compose file,
        so the student's ``.env`` must not be able to answer it — an unrelated
        local edit vetoing a release's orchestration fix would banner the Pi
        permanently, with the wrong remedy. That takes BOTH of:

        * no ``--env-file`` — the obvious half, and
        * ``env=docker_manager._scrubbed_env()`` — the half an earlier version of
          this docstring wrongly claimed was unnecessary. Compose interpolates
          from the PROCESS environment as well as from ``--env-file``, and the
          systemd unit is ``EnvironmentFile=-/etc/edubotics/.env``, so the whole
          managed ``.env`` is already in this process's ``os.environ``.
          MEASURED: with no ``.env`` file anywhere, a bare
          ``EDUBOTICS_BIND_HOST='a b'`` in the process env alone makes
          ``docker compose config -q`` exit 1 (``invalid IP address: a b``);
          scrubbed, the same file exits 0.

        Reusing ``docker_manager``'s allowlist rather than writing a second one
        is deliberate: it is the module that owns "what a docker subprocess may
        see" (it keeps EDUBOTICS_AGENT_TOKEN and any Supabase JWT out of every
        other docker call), and a duplicate here could only drift. It gives the
        gate the same PROCESS ENVIRONMENT ``_compose_up`` runs under — and
        nothing more than that: ``_compose_up`` also passes ``--env-file``
        (``_compose`` adds it whenever the .env exists), which this deliberately
        does not. Do NOT "align" the two by adding it; that difference IS the
        fix.

        Unset ``${VAR}`` interpolation is a warning, not an error, so the file
        validates on its own (MEASURED); the opi compose uses no ``${VAR:?}``
        required-variable form, and none of its interpolated names is ``DOCKER_*``
        (the one prefix the scrub keeps), so nothing in the student's ``.env``
        can reach it.

        Any inability to run the check (no docker binary, a timeout) is ALSO a
        refusal. Refusing keeps the working compose in place, which is the safe
        direction — and on a real Pi docker is always present, while a box
        without it has no installed compose to drift from in the first place.
        """
        try:
            res = subprocess.run(
                ["docker", "compose", "-f", path, "config", "-q"],
                capture_output=True, text=True, timeout=_COMPOSE_VALIDATE_TIMEOUT_S,
                env=docker_manager._scrubbed_env(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            return f"Docker Compose konnte die Datei nicht prüfen ({e})."
        if res.returncode != 0:
            lines = [ln for ln in (res.stderr or res.stdout or "").splitlines()
                     if ln.strip()]
            detail = lines[-1].strip()[:200] if lines else f"Exit-Code {res.returncode}"
            return f"Docker Compose lehnt sie ab ({detail})."
        return None

    def _renew_compose(self) -> bool:
        """Install the shipped opi compose over the drifted installed copy.

        VALIDATE-THEN-SWAP — the one deliberate asymmetry with the systemd path.
        A systemd unit that fails to parse is inert (systemd logs and ignores
        it), but ``docker compose -f`` refuses to do ANYTHING with an unparseable
        file: the always-on manager would stop coming up, and the manager IS the
        wizard, so the Pi would have no UI left to report the problem through.
        The shipped bytes are gated in CI (``pi-compose-twin-guard`` +
        ``compose-validate`` on both copies), so this is a belt against a
        corrupt/truncated extraction rather than against a bad release — which
        is exactly the failure the banner's ``sudo ./setup.sh`` remedy fixes.

        Deliberately NOT a compose ``up``/``restart`` here. Note nonetheless
        what boot() does a few steps later: ``start_manager`` runs
        ``up -d --force-recreate --no-deps physical_ai_manager``, so the
        always-on manager tier DOES adopt the newly installed compose within the
        same boot. The robot tier is the half that waits for „Umgebung starten".
        Say exactly that in German and nothing more optimistic.
        """
        return self._install_system_file(
            self._shipped_compose_path(), COMPOSE_FILE,
            f"Compose-Datei {os.path.basename(COMPOSE_FILE)}",
            validate=self._validate_compose_file,
        )

    def _check_system_files_version(self) -> None:
        """Renew drifted system files, and report what could not be renewed (M1).

        The release tarball carries ``pi_agent/`` ONLY, so a self-update moves
        the agent + (via ``docker/versions.env``) the images while everything
        ``setup.sh`` laid down OUTSIDE that tree stays frozen. That used to
        include ``docker-compose.opi.yml``: a release that added a forwarded
        ``EDUBOTICS_*`` env, changed a healthcheck, or re-pointed an image ref
        reached the field HALF-applied, and the failure was SILENT (the
        container simply never saw the var; Rule §2's fail-open class). The
        compose now ships INSIDE the package as a byte-identical twin
        (``SHIPPED_COMPOSE_RELPATH``), so it is repairable here exactly like the
        units.

        The udev rules were the same fail-open one layer down, and easier to
        miss because a rule file looks inert: they already shipped inside
        ``pi_agent/udev/`` (so the tar and the rsync always carried them) but
        NOTHING compared them, so a release adding a VID/PID line would put an
        agent that scans for an arm on a fleet whose ``/dev`` node it may not be
        allowed to open. They are in the set now (``UDEV_RULE_NAMES``).

        ``.s6-keep`` is still not, and cannot be: it is a 0-byte marker whose
        content has never changed, so a byte compare has nothing to find.

        This keys on CONTENT, never on ``stamp != APP_VERSION``. That inequality
        looks like a drift signal and is not one: the stamp cannot advance by
        design (that is the whole point of writing it outside ``pi_agent/``) and
        every release bumps APP_VERSION, so it is true on ~100 % of healthy,
        self-updated Pis while proving nothing about what is actually in the
        files. An always-on warning is not a signal — it is a thing teachers
        learn to click past, exactly when the one real occurrence needs them.
        Comparing the installed files to the ones this agent ships is instead
        decidable: it fires only when the bytes really differ.

        Proven drift is REPAIRED first, not merely announced: ``_renew_system_units``
        installs the shipped units and reloads systemd; ``_renew_compose``
        installs the shipped compose behind a ``docker compose config`` gate;
        ``_renew_udev_rules`` installs the shipped rules and reloads udev.
        Every fielded Pi was provisioned before this release, so a release that
        touches any of them drifts 100 % of the fleet — a banner alone would
        therefore be permanent, and its own remedy (``sudo ./setup.sh`` from a
        source checkout) is one a classroom cannot perform. Only what the agent
        could NOT repair reaches the banner, and there is at most ONE banner line
        naming exactly those files.

        Each renewal makes its new definition KNOWN and nothing more —
        ``daemon-reload`` / ``--reload-rules``, never ``restart``, never
        ``trigger``, never a compose ``up``. A drift check must not be able to
        disturb a Pi that is mid-lesson.

        ADVISORY ONLY — never blocks boot or the manager. The remedy for the
        unrepairable remainder is re-running ``setup.sh`` (idempotent; it
        re-installs the units, the udev rules and the compose and leaves the
        Docker volumes — the student's datasets, HF cache and calibration —
        untouched). It must NEVER
        instruct a re-flash: the drift is usually benign, the data is
        irreplaceable, and a wipe is not the fix.
        """
        stamped = self._read_system_files_stamp()
        drifted = self._drifted_units()
        drifted_udev = self._drifted_udev_rules()
        compose_drifted = self._compose_drifted()
        if not drifted and not drifted_udev and not compose_drifted:
            return
        # The origin suffix exists to name a version DIFFERENT from this
        # agent's, so the reader can see how far the on-disk files lag. When
        # the stamp equals APP_VERSION it names the same number twice and the
        # sentence contradicts itself: „(Stand: Version 2.13.0) entsprachen
        # nicht der Agent-Version 2.13.0". Drop it there — the drift is real
        # (proven by the bytes, and a hand-edited udev rule is the likeliest
        # cause), the origin version just says nothing about it. Reachable
        # whenever setup.sh last ran at the version now running: a bench
        # re-provision, or a source-checkout install that has not self-updated
        # since.
        origin = (f" (Stand: Version {stamped})"
                  if stamped and stamped != APP_VERSION else "")
        unrepaired: list = []
        # Each leg reports per FILE, not per leg. A partial outcome therefore
        # produces BOTH lines — the success sentence naming exactly what was
        # renewed, and the banner naming exactly what was not — instead of one
        # verdict that is wrong about half the set. No extra German string was
        # needed for that: the existing sentence already reads correctly over a
        # subset, so scoping it to `repaired` is the whole change.
        if drifted:
            repaired, failed = self._renew_system_units(drifted)
            if repaired:
                # Repaired — nothing for the System tab to nag about. Still say
                # so in the Protokoll: a support case wants to see that it
                # happened (and when it takes effect).
                self._log(
                    f"Systemd-Dienste ({', '.join(repaired)}){origin} entsprachen nicht "
                    f"der Agent-Version {APP_VERSION} und wurden automatisch erneuert. "
                    "Die Änderung wird beim nächsten Start des Agenten wirksam."
                )
            unrepaired.extend(failed)
        if drifted_udev:
            repaired_udev, failed_udev = self._renew_udev_rules(drifted_udev)
            if repaired_udev:
                # Says exactly when it applies. A reload makes udev KNOW the
                # rule; it does not re-evaluate devices that are already
                # enumerated (that would be `udevadm trigger`, which this
                # deliberately does not run — see _renew_udev_rules).
                self._log(
                    f"Udev-Regeln ({', '.join(repaired_udev)}){origin} entsprachen nicht "
                    f"der Agent-Version {APP_VERSION} und wurden automatisch erneuert. "
                    "Sie gelten für Geräte, die danach neu eingesteckt werden, "
                    "spätestens ab dem nächsten Neustart."
                )
            unrepaired.extend(failed_udev)
        if compose_drifted:
            compose_name = os.path.basename(COMPOSE_FILE)
            if self._renew_compose():
                # WHEN it takes effect is split, and the honest wording says so:
                # boot() reaches ``start_manager`` a few steps below and that is
                # an ``up -d --force-recreate --no-deps physical_ai_manager``, so
                # the always-on manager adopts the new file in THIS boot. Only
                # the robot tier waits for the student's „Umgebung starten".
                self._log(
                    f"Die Compose-Datei ({compose_name}){origin} entsprach nicht der "
                    f"Agent-Version {APP_VERSION} und wurde automatisch erneuert. Die "
                    "Weboberfläche wird noch in diesem Startvorgang darauf umgestellt; "
                    "für den Roboter gilt sie ab dem nächsten „Umgebung starten\"."
                )
            else:
                unrepaired.append(compose_name)
        if not unrepaired:
            return
        self._system_files_stale = True
        self._system_files_version = stamped
        # Says the same thing the System-tab banner says, because it is the line
        # that banner points at („den Grund nennt das Protokoll"). Two claims it
        # used to make are gone: „Aktualisierungen erneuern sonst nur den
        # Agenten und die Images — nicht diese Dateien" became FALSE the moment
        # the units, the udev rules and the compose joined the self-repaired
        # set, and „sudo ./setup.sh aus dem EduBotics-Quellordner" is no longer
        # the PRIMARY remedy — it needs a source checkout no classroom has,
        # while the agent's own retry on the next boot needs nothing. It stays
        # as the fallback for the case the retry cannot fix (a genuinely
        # read-only /etc, a corrupt extraction), and it still says the students'
        # data survives, because that is what stops anyone reaching for a
        # re-flash.
        #
        # „der Grund steht in den Zeilen oberhalb" is a promise the code keeps:
        # every path that appends to `unrepaired` runs through
        # `_install_system_file`, whose only two False returns each log a German
        # reason first (the validate refusal and the OSError branch).
        self._log(
            f"[WARNUNG] Systemdateien konnten nicht automatisch erneuert werden: "
            f"{', '.join(unrepaired)}{origin}. Der Pi läuft mit den bisherigen, "
            "funktionierenden Dateien weiter — der Grund steht in den Zeilen "
            "oberhalb. Beim nächsten Neustart versucht der Pi es erneut. Bleibt "
            "die Meldung bestehen, hilft 'sudo ./setup.sh' aus dem "
            "EduBotics-Quellordner; die Datensätze der Schüler bleiben dabei "
            "erhalten."
        )

    def boot(self) -> None:
        """Boot sequence: rehydrate hardware, renew drifted system files (the
        systemd units, the udev rules and the opi compose), seed or realign the
        .env, start BOTH
        listeners (loopback + the ros_net-gateway binder loop), then — only if a
        self-update just landed — pull the newly pinned images, and finally bring
        up the ALWAYS-ON manager. The robot tier is intentionally left down.

        Note the ordering consequence of the compose repair: it runs FIRST, and
        ``start_manager`` below is an ``up -d --force-recreate --no-deps
        physical_ai_manager``, so a compose the agent just renewed is adopted by
        the always-on manager tier in THIS boot — not merely at the next
        „Umgebung starten", which is only true of the robot tier. That is
        deliberate (the manager IS the wizard, so it should be the release's
        manager) and it is what the German Protokoll line says.

        The listeners start BEFORE the manager ``up``, not after. The gateway
        interface does not exist yet on a genuinely first boot, but the binder is
        a retry LOOP that no-ops until ``ros_net`` appears — whereas starting it
        afterwards would black out ``/api/system/*`` for the entire duration of
        the post-self-update image pull (~5-6 GB), which is exactly when the
        student needs the Protokoll to read what is happening. See the long
        comment at the call site.
        """
        self.rehydrate_hardware()
        self._check_system_files_version()

        needs_image_pull = False
        if not os.path.isfile(self.env_file):
            # A freshly flashed Pi has no arms configured yet; seed a cloud-only
            # .env so compose can interpolate every ${VAR} for the manager `up`.
            self._log("Keine .env vorhanden — Cloud-Konfiguration wird angelegt.")
            try:
                config_generator.generate_cloud_only_env(self.env_file)
            except Exception as e:  # noqa: BLE001
                # LOUD, because the consequence is invisible otherwise: with no
                # .env on disk `_compose` omits --env-file entirely, so compose
                # interpolates every ${REGISTRY}/${IMAGE_TAG}/${EDUBOTICS_*} to
                # the empty string. The manager `up` then fails on an unpullable
                # ref like `/physical-ai-manager-opi:` while the only symptom
                # used to be one unprefixed line scrolled past in the Protokoll.
                # Name the file, the cause and the fix.
                self._log(
                    f"[FEHLER] Die Konfigurationsdatei {self.env_file} konnte nicht "
                    f"angelegt werden: {e}. Ohne sie kann Docker Compose keine "
                    "Image-Namen auflösen und die Weboberfläche startet nicht. "
                    "Bitte Schreibrechte und freien Speicherplatz auf /etc prüfen "
                    "und den Agenten neu starten."
                )
        else:
            # The .env EXISTS but may pin a stale IMAGE_TAG. This matters because
            # the agent self-updates in place (rsync of the new pi_agent/ tree,
            # which carries a new docker/versions.env): constants.IMAGE_TAG then
            # resolves to the NEW release while the .env — the file compose
            # actually reads via --env-file — still names the OLD one. The
            # always-on manager would keep running the previous release until
            # some unrelated regenerate (arm scan, leader toggle, factory reset)
            # happened to fix it. IMAGE_TAG is a MANAGED key precisely so "a
            # stale pinned tag is superseded" (config_generator.MANAGED_KEYS) —
            # but nothing re-asserted it after a self-update, so do it here, on
            # the one code path every boot goes through. Cheap and idempotent:
            # a match is a no-op, and upsert is atomic (temp + os.replace, 0600).
            try:
                current = config_generator.read_env_var("IMAGE_TAG", self.env_file)
                if (current or "").strip() != IMAGE_TAG:
                    self._log(
                        f"IMAGE_TAG in der .env ({current or 'nicht gesetzt'}) "
                        f"passt nicht zur Agent-Version ({IMAGE_TAG}) — wird angeglichen."
                    )
                    config_generator.upsert_env_var("IMAGE_TAG", IMAGE_TAG, self.env_file)
                    # A tag mismatch on THIS path means a self-update just landed
                    # (the rsync'd pi_agent/ carried a new docker/versions.env, so
                    # constants.IMAGE_TAG jumped to the new release). The OLD
                    # agent's update job pulled against the OLD, still-present tag,
                    # so the NEW `-opi:IMAGE_TAG` images have NOT been through the
                    # resilient acquisition — left to start_manager below they'd be
                    # fetched only by compose's GHCR-only lazy pull_policy (no Hub
                    # fallback, no retry, no prune, no loud failure for a
                    # never-published release, so a Pi that was fine on the prior
                    # tag would brick its manager on the next reboot). Do the
                    # resilient pull below, after the listener binds. Runs ONLY on
                    # a genuine tag change — a plain reboot / crash-restart has no
                    # mismatch and skips it, keeping boot cheap.
                    needs_image_pull = True
            except Exception as e:  # noqa: BLE001 — never block boot on this
                self._log(f"IMAGE_TAG konnte nicht angeglichen werden: {e}")

        # Bind the API BEFORE the pull below. The pull only has to precede
        # start_manager (so the manager's `up` finds the new images already
        # acquired resiliently) — it has no relationship to the listener, and
        # running it first blacks out /api/system/* for the whole download: on a
        # post-self-update boot every `-opi` image is absent at the new tag, so
        # that is ~5-6 GB (bounded by _PULL_ATTEMPT_TIMEOUT_S × attempts ×
        # images, i.e. tens of minutes on a school link). The manager container
        # survives the agent restart (restart: unless-stopped), so the browser
        # loads the wizard and every /api/system/* 502s: useAgentUpdate's poll
        # exhausts and reports a FALSE failure, and — worse — the SSE Protokoll
        # is unreachable, so the student cannot read the [FEHLER] the pull exists
        # to surface. That is worst precisely on the slow-link / GHCR-blocked
        # school this fix's success path targets. Binding first costs nothing:
        # the wizard reports „Agent bereit" and streams the pull's progress.
        #
        # BOTH listeners must be up here — loopback ALONE is not enough. nginx's
        # /api/system/ reverse proxy targets the ros_net GATEWAY ip
        # (nginx.opi.conf.template), because a remote Chrome's `localhost` is the
        # student's OWN laptop, not the Pi. So a loopback-only bind still 502s
        # every browser for the whole download — the exact blackout the paragraph
        # above exists to prevent. The binder loop is safe to start this early: it
        # re-reads the gateway ip every _GATEWAY_BIND_INTERVAL_S and simply
        # no-ops while the interface is absent. On a post-self-update boot ros_net
        # already exists (the manager container survives the agent restart), so it
        # binds immediately; on a first boot it binds once start_manager below
        # creates ros_net. Keep BOTH starts ahead of the pull.
        self.start_loopback()
        threading.Thread(
            target=self._gateway_binder_loop, name="agent-gateway-binder", daemon=True,
        ).start()

        if needs_image_pull:
            self._pull_images_after_self_update()

        # Bring the always-on manager up (up -d --force-recreate --no-deps
        # physical_ai_manager). No image pull here — boot stays fast on the
        # provisioned image; refresh is the /update path.
        if not os.path.isfile(self.env_file):
            # Repeat the seed failure HERE, next to the `up` it dooms. The
            # earlier [FEHLER] is 30+ lines back in the Protokoll by now, and the
            # compose failure that follows reads as an image problem
            # („manifest unknown" on an empty ref), sending the operator to the
            # registry instead of to /etc.
            self._log(
                f"[FEHLER] {self.env_file} fehlt — Docker Compose startet ohne "
                "--env-file, alle Platzhalter bleiben leer und die Weboberfläche "
                "kann nicht starten. Das ist KEIN Registry- oder Netzwerkfehler."
            )
        self._log("Weboberfläche (Manager) wird gestartet …")
        if not docker_manager.start_manager(log=self._log):
            self._log("[WARNUNG] Manager konnte nicht gestartet werden — bitte Images/Docker prüfen.")

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
