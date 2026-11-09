"""The Orange Pi's robot login, end to end through a REAL nginx (2026-11-10).

The opi manager's nginx gates /rosbridge, /video/stream and /daten-api/ with
``auth_request`` to the pi-agent's ``/auth/check``; the agent gates
/api/system/* itself. A structural test cannot prove that the pieces fit (an
``auth_request`` the image's nginx does not run, a location that never reaches
the subrequest, a cookie forwarded to the robot), so this runs the shipped
template in a real nginx, the REAL agent handler as its upstream, and stub
servers in place of rosbridge, web_video_server and the dataset sidecar.

What is substituted, and only that: the listen port, the agent's address (the
ros_net gateway → 127.0.0.1 and a free port), the three upstream host:port
pairs (the container name → 127.0.0.1 and free ports), the log and pid paths.
Every location body and every directive under test is the template's own.

Skipped where no `nginx` binary is installed (GitHub's Ubuntu runners ship
one). Stdlib only.
"""

from __future__ import annotations

import http.server
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_OPI_NGINX = _REPO_ROOT / "physical_ai_tools/physical_ai_manager/nginx.opi.conf.template"

sys.path.insert(0, str(_REPO_ROOT / "robotis_ai_setup"))
os.environ.setdefault("EDUBOTICS_PI_AUTH_FILE", os.path.join(
    tempfile.mkdtemp(prefix="pi-auth-nginx-test-"), "pi_auth.json"))
os.environ.setdefault("EDUBOTICS_PI_CONSOLE", "")

from pi_agent import agent  # noqa: E402
from pi_agent import pi_auth  # noqa: E402

_NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if os.path.exists("/usr/sbin/nginx") else None)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Upstream(http.server.BaseHTTPRequestHandler):
    """Stands in for rosbridge / web_video_server / the dataset sidecar and
    records what reached it."""

    name = "upstream"
    seen: list = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((type(self).name, self.path, self.headers.get("Cookie")))
        body = type(self).name.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _OldAgent(http.server.BaseHTTPRequestHandler):
    """An agent from before the robot login: /auth/check does not exist."""

    def do_GET(self):  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def render(template: str, ports: dict, tmp: str, strip_auth: bool = False) -> str:
    # Comments mention ${…} variables by name; nginx never reads them.
    text = "\n".join(line for line in template.splitlines()
                     if not line.lstrip().startswith("#"))
    if strip_auth:
        # The pre-login template, as an OLD manager image carries it: the same
        # file without the auth_request lines (the skew cell new agent + old
        # nginx).
        text = "\n".join(line for line in text.splitlines()
                         if "auth_request /_edubotics_auth;" not in line)
    text = text.replace("${EDUBOTICS_ROS_NET_GATEWAY}:8769", f"127.0.0.1:{ports['agent']}")
    text = text.replace("listen 80;", f"listen 127.0.0.1:{ports['nginx']};")
    text = text.replace("access_log /var/log/nginx/access.log edubotics_tokenless;",
                        f"access_log {tmp}/access.log edubotics_tokenless;")
    text = text.replace("root /usr/share/nginx/html;", f"root {tmp}/html;")
    for var in ("$edubotics_rosbridge", "$edubotics_video", "$edubotics_daten"):
        text = text.replace(f"set {var} physical_ai_server;", f"set {var} 127.0.0.1;")
    text = text.replace(":9090/;", f":{ports['rosbridge']}/;")
    text = text.replace("$edubotics_video:8080;", f"$edubotics_video:{ports['video']};")
    text = text.replace("$edubotics_daten:8095;", f"$edubotics_daten:{ports['daten']};")
    assert "${" not in text, "an unrendered envsubst variable is left"
    user = "user root;\n" if hasattr(os, "geteuid") and os.geteuid() == 0 else ""
    return (
        f"{user}pid {tmp}/nginx.pid;\n"
        f"error_log {tmp}/error.log info;\n"
        "events { worker_connections 256; }\n"
        "http {\n"
        f"  client_body_temp_path {tmp}/cb; proxy_temp_path {tmp}/px;\n"
        f"  fastcgi_temp_path {tmp}/fc; uwsgi_temp_path {tmp}/uw; scgi_temp_path {tmp}/sc;\n"
        + text + "\n}\n"
    )


@unittest.skipUnless(_NGINX, "no nginx binary on this host")
class _NginxHarness(unittest.TestCase):
    strip_auth = False
    old_agent = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pi-login-nginx-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "html"))
        pathlib.Path(self.tmp, "html", "index.html").write_text("spa", encoding="utf-8")
        _Upstream.seen = []
        self.ports = {k: _free_port() for k in ("nginx", "agent", "rosbridge", "video", "daten")}
        for name in ("rosbridge", "video", "daten"):
            handler = type(f"_{name}", (_Upstream,), {"name": name})
            self._serve(http.server.ThreadingHTTPServer(("127.0.0.1", self.ports[name]), handler))
        if self.old_agent:
            self._serve(http.server.ThreadingHTTPServer(("127.0.0.1", self.ports["agent"]), _OldAgent))
        else:
            self.app = agent.AgentApp(env_file=os.path.join(self.tmp, ".env"),
                                      auth_file=os.path.join(self.tmp, "pi_auth.json"))
            self.app._auth = pi_auth.AuthStore(os.path.join(self.tmp, "pi_auth.json"),
                                               iterations=1000)
            # nginx reaches the agent from 127.0.0.1 here; on the Pi it comes
            # from the manager container on ros_net, which is NOT trusted.
            p = patch.object(agent.AgentApp, "peer_is_trusted", staticmethod(lambda _ip: False))
            p.start()
            self.addCleanup(p.stop)
            self.app._auth.try_claim(self.app._auth.mint_code(), "teacher")
            sid = self.app._auth.create_session("student", 600)
            self.cookie = f"{pi_auth.COOKIE_NAME}={sid}"
            self.agent_srv = agent.ThreadingHTTPServer(("127.0.0.1", self.ports["agent"]),
                                                       self.app._make_handler())
            self._serve(self.agent_srv)
        conf = os.path.join(self.tmp, "nginx.conf")
        pathlib.Path(conf).write_text(
            render(_OPI_NGINX.read_text(encoding="utf-8"), self.ports, self.tmp,
                   strip_auth=self.strip_auth), encoding="utf-8")
        test = subprocess.run([_NGINX, "-t", "-p", self.tmp, "-c", conf],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(test.returncode, 0, test.stderr)
        self.nginx = subprocess.Popen([_NGINX, "-p", self.tmp, "-c", conf,
                                       "-g", "daemon off;"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self._stop_nginx)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.ports["nginx"]), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        self.base = f"http://127.0.0.1:{self.ports['nginx']}"

    def _serve(self, srv):
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

    def _stop_nginx(self):
        self.nginx.terminate()
        try:
            self.nginx.wait(10)
        except subprocess.TimeoutExpired:
            self.nginx.kill()
        if self.nginx.stderr:
            self.nginx.stderr.close()

    def get(self, path, cookie=None, method="GET", origin=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=b"{}" if method == "POST" else None)
        if cookie:
            req.add_header("Cookie", cookie)
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


class TheCurrentConfigGatesEveryTransport(_NginxHarness):
    TRANSPORTS = (("/rosbridge", "rosbridge"),
                  ("/video/stream?topic=/scene/image_raw/compressed", "video"),
                  ("/daten-api/v1/health", "daten"))

    def test_no_cookie_no_transport(self):
        for path, _ in self.TRANSPORTS:
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 401)
        self.assertEqual(_Upstream.seen, [], "a refused request reached the robot")

    def test_a_forged_or_expired_cookie_is_refused(self):
        for cookie in (f"{pi_auth.COOKIE_NAME}={'x' * 43}", f"{pi_auth.COOKIE_NAME}=", "other=1"):
            with self.subTest(cookie=cookie):
                self.assertEqual(self.get("/rosbridge", cookie=cookie)[0], 401)
        self.assertEqual(_Upstream.seen, [])

    def test_a_session_passes_and_the_cookie_never_reaches_the_robot(self):
        for path, name in self.TRANSPORTS:
            with self.subTest(path=path):
                status, body = self.get(path, cookie=self.cookie)
                self.assertEqual(status, 200)
                self.assertEqual(body, name)
        self.assertEqual(len(_Upstream.seen), 3)
        for name, _path, cookie in _Upstream.seen:
            self.assertIsNone(cookie, f"{name} received the session cookie")

    def test_a_websocket_handshake_is_gated_the_same_way(self):
        """The browser's rosbridge connection is an Upgrade request; the auth
        subrequest runs in the access phase whatever the method or headers."""
        def handshake(cookie):
            req = urllib.request.Request(self.base + "/rosbridge")
            req.add_header("Upgrade", "websocket")
            req.add_header("Connection", "Upgrade")
            req.add_header("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
            req.add_header("Sec-WebSocket-Version", "13")
            req.add_header("Origin", self.base)
            if cookie:
                req.add_header("Cookie", cookie)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(handshake(None), 401)
        self.assertEqual(_Upstream.seen, [])
        # With a session the handshake reaches rosbridge (the stub answers 200
        # instead of 101; what matters is that it got there, without the cookie).
        self.assertEqual(handshake(self.cookie), 200)
        self.assertEqual([(n, c) for n, _p, c in _Upstream.seen], [("rosbridge", None)])

    def test_the_rosbridge_host_and_origin_refusals_still_come_first(self):
        req = urllib.request.Request(self.base + "/rosbridge")
        req.add_header("Origin", "http://evil.example")
        req.add_header("Cookie", self.cookie)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 403)

    def test_the_dataset_api_keeps_its_own_method_rule(self):
        status, _ = self.get("/daten-api/v1/health", cookie=self.cookie, method="POST")
        self.assertEqual(status, 403)
        self.assertEqual(_Upstream.seen, [])

    def test_the_agent_gates_api_system_itself(self):
        """The written acceptance: `curl -X POST …/api/system/environment/stop`
        from another LAN host answers 403 (no Origin) or 401 (a forged Origin,
        no session) and never stops anything."""
        with patch.object(agent.docker_manager, "stop_robot_tier") as stop:
            self.assertEqual(self.get("/api/system/environment/stop", method="POST")[0], 403)
            self.assertEqual(self.get("/api/system/environment/stop", method="POST",
                                      origin="http://127.0.0.1")[0], 401)
        stop.assert_not_called()
        self.assertEqual(self.get("/api/system/status")[0], 401)
        with patch.object(agent.docker_manager, "get_container_status", return_value={}):
            self.assertEqual(self.get("/api/system/status", cookie=self.cookie)[0], 200)
        # The pre-login routes stay open.
        self.assertEqual(self.get("/api/system/auth/state")[0], 200)
        # The static shell and the Pi marker are never gated.
        self.assertEqual(self.get("/pi-mode.json")[0], 200)
        self.assertEqual(self.get("/")[0], 200)

    def test_no_cookie_value_reaches_the_access_log(self):
        self.get("/rosbridge", cookie=self.cookie)
        self.get("/api/system/status", cookie=self.cookie)
        self._stop_nginx()
        log = pathlib.Path(self.tmp, "access.log").read_text(encoding="utf-8")
        self.assertTrue(log.strip())
        self.assertNotIn(self.cookie.split("=", 1)[1], log)

    def test_the_agents_probe_reads_this_config_as_enforced(self):
        with patch.object(agent, "_MANAGER_PROBE_BASE", self.base):
            self.assertEqual(self.app._manager_login_state(), "enforced")

    def test_an_agent_that_does_not_answer_fails_closed(self):
        self.assertEqual(self.get("/rosbridge", cookie=self.cookie)[0], 200)
        _Upstream.seen = []
        self.agent_srv.shutdown()
        self.agent_srv.server_close()
        # The auth subrequest cannot connect: nginx answers 500, never the robot.
        for path in ("/rosbridge", "/video/stream", "/daten-api/v1/health"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, cookie=self.cookie)[0], 500)
        self.assertEqual(_Upstream.seen, [])


class AnAgentFromBeforeTheLoginFailsClosed(_NginxHarness):
    """Skew cell OLD agent + NEW manager (a manual image pin): /auth/check
    answers 404, nginx maps it to 500 — the transports stay shut."""

    old_agent = True

    def test_every_transport_is_refused(self):
        for path in ("/rosbridge", "/video/stream", "/daten-api/v1/health"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, cookie="anything=1")[0], 500)
        self.assertEqual(_Upstream.seen, [])


class AnOldConfigIsMeasuredAsOpen(_NginxHarness):
    """Skew cell NEW agent + OLD manager: the agent's probe sees the old config
    proxy straight to the robot and refuses to start the robot tier."""

    strip_auth = True

    def test_the_probe_reads_open_and_the_start_is_refused(self):
        with patch.object(agent, "_MANAGER_PROBE_BASE", self.base):
            self.assertEqual(self.app._manager_login_state(), "open")
            cg = agent.config_generator
            self.app._hardware = cg.HardwareConfig(
                leader=cg.ArmDevice(serial_path="/dev/serial/by-id/L", role="leader"),
                follower=cg.ArmDevice(serial_path="/dev/serial/by-id/F", role="follower"),
                cameras=[cg.CameraDevice(path="/dev/video0", role="scene")])
            with patch.object(agent.AgentApp, "_hardware_ready", return_value=True), \
                    patch.object(agent.docker_manager, "start_robot_tier") as start:
                code, payload = self.app.handle_environment_start({})
        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "manager_outdated")
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
