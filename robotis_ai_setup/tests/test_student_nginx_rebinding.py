"""DNS rebinding against the manager's nginx, measured with a REAL nginx (SA-11, 2026-11-13).

A page whose host name re-resolves to the robot's address after it loaded is
same-origin with ``http://<its name>:80/`` and can read whatever that server
answers. ``/video/stream`` is deliberately not Origin-gated (an ``<img>`` sends
no Origin), so the security audit read the live classroom camera through the
student config with a foreign ``Host``. The fix keys on ``Host``, the one thing
such a page cannot choose:

* the Windows student config (``nginx.conf``): a ``default_server`` that closes
  the connection (444) for every name that is not ours, plus a server-level
  check of the exact form (localhost, 127.0.0.1 or [::1], with or without :80);
* the Orange Pi template (``nginx.opi.conf.template``): its existing
  ``$edubotics_host_is_own`` map (an IP literal, a single-label name or
  ``.local``), now applied to EVERY location with a 421, no longer to
  ``/rosbridge`` alone.

What is substituted, and only that (the ``test_pi_login_nginx.py`` pattern):
the listen address, the upstream host:port pairs (stub servers), the resolver,
the root, the log and pid paths. Every location body and the guard under test
are the files' own. Skipped where no ``nginx`` binary is installed.
"""

from __future__ import annotations

import http.server
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STUDENT = _REPO_ROOT / "physical_ai_tools/physical_ai_manager/nginx.conf"
_OPI = _REPO_ROOT / "physical_ai_tools/physical_ai_manager/nginx.opi.conf.template"
_NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if os.path.exists("/usr/sbin/nginx") else None)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Stub(http.server.BaseHTTPRequestHandler):
    """Stands in for web_video_server / rosbridge / the sidecar / the agent."""

    def do_GET(self):  # noqa: N802
        body = b"FRAME" if "stream" in self.path else b"upstream"
        self.send_response(200)
        if self.path.startswith("/auth/check"):
            self.send_response(204)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _raw_get(port: int, path: str, host) -> str:
    """The status line nginx answers, or '<closed>' when it closes (444)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    head = f"GET {path} HTTP/1.1\r\n" + (f"Host: {host}\r\n" if host is not None else "")
    s.sendall((head + "Connection: close\r\n\r\n").encode())
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    s.close()
    return data.split(b"\r\n", 1)[0].decode("latin-1") or "<closed>"


@unittest.skipUnless(_NGINX, "no nginx binary on this host")
class _Harness(unittest.TestCase):
    source = None  # set by subclasses

    def render(self, text: str) -> str:
        raise NotImplementedError

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nginx-rebind-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "html"))
        pathlib.Path(self.tmp, "html", "index.html").write_text("spa", encoding="utf-8")
        self.port = _free_port()
        self.stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        self.stub.daemon_threads = True
        threading.Thread(target=self.stub.serve_forever, daemon=True).start()
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.stub.shutdown)
        self.up = self.stub.server_address[1]
        text = "\n".join(line for line in self.source.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith("#"))
        text = self.render(text)
        text = text.replace("resolver 127.0.0.11 valid=10s ipv6=off;", "resolver 127.0.0.1 valid=10s ipv6=off;")
        text = text.replace("root /usr/share/nginx/html;", f"root {self.tmp}/html;")
        text = text.replace("access_log /var/log/nginx/access.log edubotics_tokenless;",
                            f"access_log {self.tmp}/access.log edubotics_tokenless;")
        for var in ("$edubotics_rosbridge", "$edubotics_video", "$edubotics_daten"):
            text = text.replace(f"set {var} physical_ai_server;", f"set {var} 127.0.0.1;")
        for port in (":9090/;", ":8080;", ":8095;"):
            text = text.replace(port, f":{self.up}{port[-2:] if port.endswith('/;') else ';'}")
        self.assertNotIn("${", text)
        user = "user root;\n" if hasattr(os, "geteuid") and os.geteuid() == 0 else ""
        conf = (f"{user}pid {self.tmp}/nginx.pid;\nerror_log {self.tmp}/error.log info;\n"
                "events { worker_connections 64; }\nhttp {\n"
                f"  client_body_temp_path {self.tmp}/cb; proxy_temp_path {self.tmp}/px;\n"
                f"  fastcgi_temp_path {self.tmp}/fc; uwsgi_temp_path {self.tmp}/uw; scgi_temp_path {self.tmp}/sc;\n"
                + text + "\n}\n")
        path = os.path.join(self.tmp, "nginx.conf")
        pathlib.Path(path).write_text(conf, encoding="utf-8")
        test = subprocess.run([_NGINX, "-t", "-c", path], capture_output=True, text=True)
        self.assertEqual(test.returncode, 0, test.stderr)
        subprocess.run([_NGINX, "-c", path], check=True, capture_output=True)
        self.addCleanup(subprocess.run, [_NGINX, "-c", path, "-s", "stop"], capture_output=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)

    def get(self, path, host):
        return _raw_get(self.port, path, host)


class StudentConfig(_Harness):
    source = _STUDENT

    def render(self, text):
        self.assertIn("listen 80 default_server;", text)
        text = text.replace("listen 80 default_server;", f"listen 127.0.0.1:{self.port} default_server;")
        return text.replace("listen 80;", f"listen 127.0.0.1:{self.port};")

    PATHS = ("/", "/index.html", "/video/stream?topic=/scene/image_raw&type=mjpeg",
             "/daten-api/v1/health", "/version.json", "/static/x.js")

    def test_a_foreign_host_gets_nothing_on_any_location(self):
        for host in ("rebind.attacker.example", "rebind.attacker.example:80", "localhost.evil.example",
                     "127.0.0.1.nip.io", "evil", "localhost:8080", "LOCALHOST.example"):
            for path in self.PATHS + ("/rosbridge",):
                with self.subTest(host=host, path=path):
                    self.assertEqual(self.get(path, host), "<closed>")

    def test_no_host_at_all_gets_nothing(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"GET /video/stream?topic=/scene/image_raw HTTP/1.0\r\n\r\n")
        self.assertEqual(s.recv(100), b"")
        s.close()

    def test_our_own_names_still_work(self):
        for host in ("localhost", "localhost:80", "127.0.0.1", "127.0.0.1:80", "[::1]", "[::1]:80",
                     "LOCALHOST"):
            with self.subTest(host=host):
                self.assertEqual(self.get("/video/stream?topic=/scene/image_raw&type=mjpeg", host),
                                 "HTTP/1.1 200 OK")
                self.assertEqual(self.get("/", host), "HTTP/1.1 200 OK")

    def test_the_rosbridge_origin_gate_still_answers_first_for_our_name(self):
        status = _raw_origin(self.port, "/rosbridge", "localhost", "http://evil.example")
        self.assertEqual(status, "HTTP/1.1 403 Forbidden")


class OpiTemplate(_Harness):
    """The Pi: an IP literal, a single-label name or .local are ours; a rebound
    multi-label name gets 421 on EVERY location now, before the robot login."""

    source = _OPI

    def render(self, text):
        text = text.replace("${EDUBOTICS_ROS_NET_GATEWAY}:8769", f"127.0.0.1:{self.up}")
        return text.replace("listen 80;", f"listen 127.0.0.1:{self.port};")

    def test_a_rebound_name_is_misdirected_everywhere(self):
        for path in ("/", "/index.html", "/video/stream?topic=/scene/image_raw&type=mjpeg",
                     "/daten-api/v1/health", "/api/system/auth/state", "/api/system/netzwerk-check",
                     "/pi-mode.json", "/rosbridge"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, "rebind.attacker.example"),
                                 "HTTP/1.1 421 Misdirected Request")

    def test_the_names_a_pi_is_reached_at_still_work(self):
        for host in ("192.168.1.23", "192.168.1.23:80", "edubotics-07", "edubotics-07.local",
                     "localhost", "127.0.0.1", "[fe80::1]"):
            with self.subTest(host=host):
                self.assertEqual(self.get("/", host), "HTTP/1.1 200 OK")
                self.assertEqual(self.get("/api/system/auth/state", host), "HTTP/1.1 200 OK")


def _raw_origin(port, path, host, origin):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nOrigin: {origin}\r\nConnection: close\r\n\r\n".encode())
    data = s.recv(200)
    s.close()
    return data.split(b"\r\n", 1)[0].decode("latin-1")


if __name__ == "__main__":
    unittest.main()
