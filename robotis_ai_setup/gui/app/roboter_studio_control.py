#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Localhost HTTP control bridge for the Roboter Studio leader toggle.

The student does Roboter Studio in the React app (served from the
``physical_ai_manager`` container on localhost), but ONLY the GUI
(``EduBotics.exe``) can drive Docker. This tiny localhost HTTP server lets the
React Roboter Studio tab signal the GUI to switch the arm stack between
**follower-only** (leader off — autonomous picking, no teleop contention) and
**both-arms** (leader on — teleop / recording).

Bound to ``127.0.0.1`` only, so no off-host process can reach it; the browser
(running on the Windows host that serves the React app from localhost) can.

Endpoints (JSON, CORS-open):

* ``GET  /roboter-studio/status``         → ``{"follower_only": bool,
  "ready": bool, "busy": bool, "has_leader": bool}``
* ``POST /roboter-studio/leader-disable`` → switch to follower-only, restart arm
* ``POST /roboter-studio/leader-enable``  → switch to both-arms, restart arm
* ``GET  /station/status`` → ``{"enabled": bool, "stream": "off"|"on",
  "scene_fps": float, "station_fps": float, "error_de": str}``
* ``POST /station/stream`` body ``{"on": true|false}`` (sent as
  ``application/json`` or ``text/plain``, so ``navigator.sendBeacon`` can
  switch it off on ``pagehide``) → the camera station's Studio stream.

The actual work (regenerate ``.env`` + recreate the ``open_manipulator``
container; switch the bridge's Studio stream) is injected as callables so this
module stays free of GUI/docker imports and is unit-testable.

**The Studio stream lease (camera station, plan §3.3).** ``on`` is
START-OR-RENEW: it requires the station to be enabled and no leader switch in
flight, starts the stream if it is off, and (re)sets a lease of
``STATION_LEASE_S`` (10 s). There is no separate keepalive endpoint. ``off``
stops it at once; lease expiry switches it off. The owner page renews every
2 s, only right after an accepted server keepalive, so a stray ``off`` is
undone within 2 s and a page that lost the server stops renewing. The bridge
never asks the server for permission, and nothing here can change, pause or
reopen the RECORDING stream: the stream switch only starts or stops the 1080p
Studio sender.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlsplit

# Hosts a browser Origin may legitimately carry when the React app (served from
# the localhost container) POSTs to this bridge. Anything else is rejected.
_ALLOWED_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1"})
# ...and the scheme + port halves of the SAME policy. These exist because the
# host check alone was NOT the policy nginx.conf's `$edubotics_origin_ok` map
# enforces, while nginx.conf:28-30 claims the two are "deliberately IDENTICAL":
# the map is `~*^https?://localhost(:80)?$`, so nginx answers 403 to
# `http://localhost:8444`, `http://localhost:3000`, `ftp://localhost` and
# `ws://localhost` — all four of which this handler used to ALLOW, because it
# read only `urlsplit(origin).hostname`. That gap mattered: the phone receiver
# binds 0.0.0.0:8444 on this very machine, so any other HTML-serving surface on
# a localhost port was a pivot into the origin trusted to restart the arm.
#
# `None` is in the port set because a browser ELIDES the default port from the
# Origin serialization: the SPA is served from http://localhost:80, so the
# header it sends is `Origin: http://localhost`. `80` is spelled out as well
# because a non-browser client may include it — exactly the `(:80)?` group.
_ALLOWED_ORIGIN_SCHEMES = frozenset({"http", "https"})
_ALLOWED_ORIGIN_PORTS = frozenset({None, 80})
# Cap the request body we drain so a malicious/oversized POST can't make us read
# an unbounded amount into memory. The legitimate body is a few bytes of JSON.
_MAX_BODY_BYTES = 64 * 1024

# Fixed loopback port the React app POSTs to. High + uncommon to avoid clashes.
DEFAULT_PORT = 8769
_HOST = "127.0.0.1"

# on_set_mode(follower_only: bool, log) -> (ok: bool, german_message: str)
SetModeFn = Callable[[bool, Callable[[str], None]], "tuple[bool, str]"]
# get_status() -> {"follower_only": bool}
StatusFn = Callable[[], dict]
# get_task_busy() -> bool (True while recording / inferencing — switch refused)
TaskBusyFn = Callable[[], bool]
# get_station_status() -> {"enabled": bool, "scene_fps": float,
#                          "station_fps": float, "error_de": str}
StationStatusFn = Callable[[], dict]
# on_station_stream(on: bool) -> (ok: bool, german_error: str). Must not block:
# it only flips the bridge's Studio sender (called under the bridge's lock).
StationStreamFn = Callable[[bool], "tuple[bool, str]"]

# The Studio stream's lease: an `on` keeps it running this long. The page
# renews every 2 s; a page that stops renewing loses the stream within this.
STATION_LEASE_S = 10.0
# How often the lease watchdog looks at the clock.
_LEASE_POLL_S = 0.25

STATION_IN_USE_DE = "Roboter Studio nutzt gerade die Stationskamera."
STATION_NOT_ENABLED_DE = "Die Kamerastation ist nicht eingeschaltet."
STATION_SWITCH_IN_FLIGHT_DE = (
    "Ein Moduswechsel läuft gerade — die Stationskamera startet danach.")
STATION_BAD_BODY_DE = 'Ungültige Anfrage — erwartet wird {"on": true} oder {"on": false}.'


class RoboterStudioControlServer:
    """Threaded localhost HTTP bridge for the Roboter Studio leader toggle."""

    def __init__(
        self,
        on_set_mode: SetModeFn,
        get_status: StatusFn,
        port: int = DEFAULT_PORT,
        log: Optional[Callable[[str], None]] = None,
        get_task_busy: Optional[TaskBusyFn] = None,
        get_station_status: Optional[StationStatusFn] = None,
        on_station_stream: Optional[StationStreamFn] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_set_mode = on_set_mode
        self._get_status = get_status
        # Camera station (plan §3.3). Absent callables = not a station rig:
        # GET /station/status answers enabled=false and `on` is refused.
        self._get_station_status = get_station_status or (
            lambda: {"enabled": False})
        self._on_station_stream = on_station_stream or (
            lambda _on: (False, STATION_NOT_ENABLED_DE))
        self._clock = clock
        # Guarded by _busy_lock, together with _busy, so a leader switch and
        # the Studio stream can never both be live.
        self._stream_on = False
        self._lease_until = 0.0
        self._lease_stop = threading.Event()
        self._lease_thread: Optional[threading.Thread] = None
        # Refuse a mode switch while a recording / inference run is active —
        # recreating the open_manipulator container mid-run blips /joint_states +
        # the camera topics and corrupts the episode. The React toggle disables
        # itself too, but that UI guard is bypassable; this is the enforced one.
        self._get_task_busy = get_task_busy or (lambda: False)
        self._port = port
        self._log = log or (lambda _m: None)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._busy_lock = threading.Lock()
        self._busy = False

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> bool:
        """Bind + serve in a daemon thread. Returns False (logged) if the port
        is taken — the toggle just won't be available, never crashes the GUI."""
        if self._httpd is not None:
            return True
        try:
            self._httpd = ThreadingHTTPServer((_HOST, self._port), self._make_handler())
        except OSError as e:
            self._log(
                f"Roboter-Studio-Steuerung: Port {self._port} nicht verfügbar ({e})."
            )
            self._httpd = None
            return False
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="rs-control", daemon=True)
        self._thread.start()
        self._lease_stop.clear()
        self._lease_thread = threading.Thread(
            target=self._lease_loop, name="rs-station-lease", daemon=True)
        self._lease_thread.start()
        return True

    def stop(self) -> None:
        # The environment is going away: the Studio stream ends with it.
        self.station_stream_off()
        self._lease_stop.set()
        self._lease_thread = None
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._httpd = None
        self._thread = None

    # ── request handling (split out for unit-testability) ────────────────────
    def handle_status(self) -> "tuple[int, dict]":
        try:
            st = self._get_status() or {}
        except Exception as e:  # noqa: BLE001
            return 500, {"error": str(e)}
        # ``ready`` reflects the TRUE arm-container state from the GUI (running
        # vs restarting / down); ``busy`` is True while a switch is in flight on
        # EITHER side, so the badge never claims the new mode is live early.
        # ``has_leader`` comes from the GUI's own robot profile (false on
        # omx_follower): on a camera-station rig the bridge exists there too,
        # and LeaderToggle.jsx hides on `bridgeHasLeader === false` before the
        # first capabilities tick. Absent from an older status fn = true.
        return 200, {
            "follower_only": bool(st.get("follower_only", False)),
            "ready": bool(st.get("ready", True)),
            "busy": self._busy or bool(st.get("busy", False)),
            "has_leader": bool(st.get("has_leader", True)),
        }

    def handle_set_mode(self, follower_only: bool) -> "tuple[int, dict]":
        # Refuse mid-record / mid-inference — restarting the arm container then
        # would corrupt the running episode. Checked BEFORE the busy lock so a
        # busy-task rejection is reported as 409 task-busy, not 409 in-flight.
        try:
            task_busy = bool(self._get_task_busy())
        except Exception:  # noqa: BLE001 — never let the probe block the bridge
            task_busy = False
        if task_busy:
            return 409, {"ok": False,
                         "message": "Während Aufnahme/Inferenz nicht verfügbar."}
        # Reject concurrent restarts — a second click while a restart is in
        # flight would race two `compose up` calls on the same container.
        with self._busy_lock:
            # Camera station (D7): the leader is switched BEFORE station mode,
            # never during it. Checked under the same lock that starts the
            # Studio stream, so the two can never both begin.
            if self._stream_on:
                return 409, {"ok": False, "message": STATION_IN_USE_DE}
            if self._busy:
                return 409, {"ok": False,
                             "message": "Ein Moduswechsel läuft bereits."}
            self._busy = True
        try:
            ok, msg = self._on_set_mode(follower_only, self._log)
            return (200 if ok else 500), {
                "ok": bool(ok), "message": msg, "follower_only": follower_only}
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "message": f"Fehler: {e}"}
        finally:
            with self._busy_lock:
                self._busy = False

    # ── camera station: the Studio stream and its lease ──────────────
    def _station_status(self) -> dict:
        try:
            st = self._get_station_status() or {}
        except Exception:  # noqa: BLE001 — a status probe never breaks the bridge
            st = {}
        return st

    def station_enabled(self) -> bool:
        return bool(self._station_status().get("enabled", False))

    def handle_station_status(self) -> "tuple[int, dict]":
        st = self._station_status()
        with self._busy_lock:
            stream = "on" if self._stream_on else "off"
        return 200, {
            "enabled": bool(st.get("enabled", False)),
            "stream": stream,
            "scene_fps": float(st.get("scene_fps", 0.0) or 0.0),
            "station_fps": (float(st.get("station_fps", 0.0) or 0.0)
                            if stream == "on" else 0.0),
            "error_de": str(st.get("error_de", "") or ""),
        }

    def handle_station_stream(self, on: bool) -> "tuple[int, dict]":
        """``on`` = start-or-renew (station enabled, no leader switch in
        flight; (re)sets the 10 s lease); ``off`` = stop at once."""
        if not on:
            self.station_stream_off()
            return 200, {"ok": True, "stream": "off"}
        if not self.station_enabled():
            return 409, {"ok": False, "message": STATION_NOT_ENABLED_DE}
        with self._busy_lock:
            if self._busy:
                return 409, {"ok": False, "message": STATION_SWITCH_IN_FLIGHT_DE}
            try:
                ok, err = self._on_station_stream(True)
            except Exception as e:  # noqa: BLE001
                ok, err = False, f"Fehler: {e}"
            if not ok:
                self._stream_on = False
                return 409, {"ok": False, "message": err or STATION_NOT_ENABLED_DE}
            self._stream_on = True
            self._lease_until = self._clock() + STATION_LEASE_S
        return 200, {"ok": True, "stream": "on", "lease_s": STATION_LEASE_S}

    def station_stream_off(self) -> None:
        """Switch the Studio stream off now. Idempotent, never raises. Called
        by `off`, the lease watchdog, and the GUI's teardown paths."""
        with self._busy_lock:
            self._stream_on = False
            self._lease_until = 0.0
            try:
                self._on_station_stream(False)
            except Exception:  # noqa: BLE001 — switching off must never fail loudly
                pass

    def expire_station_lease(self) -> bool:
        """Switch the stream off if its lease ran out. Returns True when it
        did. One tick of the lease watchdog; public for tests."""
        with self._busy_lock:
            if not self._stream_on or self._clock() < self._lease_until:
                return False
            self._stream_on = False
            self._lease_until = 0.0
            try:
                self._on_station_stream(False)
            except Exception:  # noqa: BLE001
                pass
        self._log("Stationskamera: Studio-Bild beendet (keine Verlängerung "
                  "von der Web-Oberfläche).")
        return True

    def _lease_loop(self) -> None:
        while not self._lease_stop.wait(_LEASE_POLL_S):
            try:
                self.expire_station_lease()
            except Exception:  # noqa: BLE001 — the watchdog must not die
                pass

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # silence default stderr logging
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            def _send(self, code: int, payload: dict):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:  # noqa: BLE001 — client hung up
                    pass

            def do_OPTIONS(self):  # noqa: N802 — http.server API
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):  # noqa: N802
                p = self.path.rstrip("/")
                if p == "/roboter-studio/status":
                    self._send(*server.handle_status())
                elif p == "/station/status":
                    self._send(*server.handle_station_status())
                else:
                    self._send(404, {"error": "not found"})

            def _drain_body(self) -> bytes:
                # Read any request body so the kernel doesn't reset the
                # connection (some clients send a body on POST); bounded so a
                # bogus huge Content-Length can't exhaust memory. Returned for
                # the one endpoint that reads it (/station/stream).
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except (TypeError, ValueError):
                    length = 0
                if length > 0:
                    try:
                        return self.rfile.read(min(length, _MAX_BODY_BYTES))
                    except Exception:  # noqa: BLE001 — client hung up mid-body
                        pass
                return b""

            def _origin_allowed(self):
                # The state-changing POSTs restart the arm container. CORS only
                # blocks the cross-origin RESPONSE read, not a "simple" POST's
                # SIDE EFFECT — so we reject a cross-site Origin at the handler.
                # Empty Origin = same-origin / non-browser caller (allowed).
                # Parse the URL and require the HOST to be EXACTLY localhost /
                # 127.0.0.1 — a `startswith` check is bypassable by an attacker
                # origin like http://localhost.evil.com.
                #
                # Scheme and PORT are checked too, so this is the same policy
                # nginx.conf's `~*^https?://localhost(:80)?$` map enforces for
                # /rosbridge. Checking the host alone let `http://localhost:8444`
                # (the phone receiver's own port) through here while nginx
                # returned 403 for it — one product with two different answers.
                # `.port` is inside the try: urlsplit defers the port parse, so
                # `http://localhost:notaport` raises ValueError only here.
                origin = self.headers.get("Origin", "")
                if origin == "":
                    return True
                try:
                    parts = urlsplit(origin)
                    host = parts.hostname
                    port = parts.port
                except ValueError:
                    return False
                return (
                    parts.scheme in _ALLOWED_ORIGIN_SCHEMES
                    and host in _ALLOWED_ORIGIN_HOSTS
                    and port in _ALLOWED_ORIGIN_PORTS
                )

            def do_POST(self):  # noqa: N802
                body = self._drain_body()
                if not self._origin_allowed():
                    self._send(403, {"ok": False, "message": "Origin nicht erlaubt."})
                    return
                p = self.path.rstrip("/")
                if p == "/roboter-studio/leader-disable":
                    self._send(*server.handle_set_mode(True))
                elif p == "/roboter-studio/leader-enable":
                    self._send(*server.handle_set_mode(False))
                elif p == "/station/stream":
                    on = parse_station_stream_body(body)
                    if on is None:
                        self._send(400, {"ok": False,
                                         "message": STATION_BAD_BODY_DE})
                    else:
                        self._send(*server.handle_station_stream(on))
                else:
                    self._send(404, {"error": "not found"})

        return Handler


def parse_station_stream_body(body: bytes) -> "bool | None":
    """``{"on": true|false}`` → the bool, anything else → None.

    The body is read whatever its Content-Type says: the page POSTs it as
    ``application/json`` and ``navigator.sendBeacon`` as ``text/plain`` (a
    CORS-simple request, so the pagehide beacon needs no preflight). Only a
    real JSON boolean counts — ``"on": 1`` or ``"on": "true"`` is refused."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    on = data.get("on")
    if not isinstance(on, bool):
        return None
    return on
