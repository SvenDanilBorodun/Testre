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

* ``GET  /roboter-studio/status``         → ``{"follower_only": bool, "busy": bool}``
* ``POST /roboter-studio/leader-disable`` → switch to follower-only, restart arm
* ``POST /roboter-studio/leader-enable``  → switch to both-arms, restart arm

The actual work (regenerate ``.env`` + recreate the ``open_manipulator``
container) is injected as ``on_set_mode`` so this module stays free of
GUI/docker imports and is unit-testable.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

# Fixed loopback port the React app POSTs to. High + uncommon to avoid clashes.
DEFAULT_PORT = 8769
_HOST = "127.0.0.1"

# on_set_mode(follower_only: bool, log) -> (ok: bool, german_message: str)
SetModeFn = Callable[[bool, Callable[[str], None]], "tuple[bool, str]"]
# get_status() -> {"follower_only": bool}
StatusFn = Callable[[], dict]


class RoboterStudioControlServer:
    """Threaded localhost HTTP bridge for the Roboter Studio leader toggle."""

    def __init__(
        self,
        on_set_mode: SetModeFn,
        get_status: StatusFn,
        port: int = DEFAULT_PORT,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_set_mode = on_set_mode
        self._get_status = get_status
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
        return True

    def stop(self) -> None:
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
        return 200, {
            "follower_only": bool(st.get("follower_only", False)),
            "busy": self._busy,
        }

    def handle_set_mode(self, follower_only: bool) -> "tuple[int, dict]":
        # Reject concurrent restarts — a second click while a restart is in
        # flight would race two `compose up` calls on the same container.
        with self._busy_lock:
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
                if self.path.rstrip("/") == "/roboter-studio/status":
                    self._send(*server.handle_status())
                else:
                    self._send(404, {"error": "not found"})

            def _origin_allowed(self):
                # The state-changing POSTs restart the arm container. CORS only
                # blocks the cross-origin RESPONSE read, not a "simple" POST's
                # SIDE EFFECT — so we reject a cross-site Origin at the handler.
                # Empty Origin = same-origin / non-browser caller (allowed).
                origin = self.headers.get("Origin", "")
                return (origin == ""
                        or origin.startswith("http://localhost")
                        or origin.startswith("http://127.0.0.1"))

            def do_POST(self):  # noqa: N802
                if not self._origin_allowed():
                    self._send(403, {"ok": False, "message": "Origin nicht erlaubt."})
                    return
                p = self.path.rstrip("/")
                if p == "/roboter-studio/leader-disable":
                    self._send(*server.handle_set_mode(True))
                elif p == "/roboter-studio/leader-enable":
                    self._send(*server.handle_set_mode(False))
                else:
                    self._send(404, {"error": "not found"})

        return Handler
