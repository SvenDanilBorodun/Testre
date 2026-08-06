"""The rosbridge control plane must not be reachable cross-origin (2026-08-06).

rosbridge is UNAUTHENTICATED: anything that completes a WebSocket handshake to
it can publish to /leader/joint_trajectory — driving a physical arm standing
beside children — and can read every recorded dataset. It used to be published
on the host at 127.0.0.1:9090 while the SPA was served from :80, i.e. a
CROSS-ORIGIN connection. A WebSocket handshake gets no CORS preflight, so any
page open in the student's browser could complete it, and a loopback bind is
not a boundary against a page running ON that PC.

The fix has THREE parts that are only correct together, which is exactly why
they are fenced in one file:

  1. docker-compose.yml publishes NEITHER :9090 nor :8080 (else the nginx gate
     is bypassable and the whole thing is theatre),
  2. nginx.conf proxies /rosbridge same-origin behind an Origin allowlist,
  3. gui/app/health_checker.py still produces a REAL liveness signal despite
     (1) — never a stub that always returns True.

Deliberately stdlib-only (no PyYAML) so this keeps riding the deps-free
`robotis_ai_setup` suite; CI installs pyyaml but a bare developer run must not
need it.
"""

import http.server
import pathlib
import re
import threading
import unittest

from gui.app import health_checker
from gui.app.roboter_studio_control import _ALLOWED_ORIGIN_HOSTS

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STUDENT_COMPOSE = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.yml"
_OPI_COMPOSE = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.opi.yml"
_NGINX_CONF = _REPO_ROOT / "physical_ai_tools/physical_ai_manager/nginx.conf"


def _service_block(compose_path, service):
    """Return the lines of one compose service block.

    A service key sits at 2-space indent; the block runs until the next line
    at that same indent (or shallower) that is not blank/comment.
    """
    lines = compose_path.read_text(encoding="utf-8").splitlines()
    out, inside = [], False
    for line in lines:
        if re.match(r"^  %s:\s*$" % re.escape(service), line):
            inside = True
            continue
        if inside:
            if line.strip() and not re.match(r"^\s{3,}", line) and not line.startswith("  #"):
                # A line at <=2-space indent that has content ends the block.
                if not line.startswith("   "):
                    break
            out.append(line)
    return out


def _published_ports(block_lines):
    """Host port-publish entries of a compose service block.

    Only counts entries under this service's own `ports:` key, and only
    non-comment list items.
    """
    ports, in_ports = [], False
    for line in block_lines:
        stripped = line.strip()
        if re.match(r"^ports:\s*$", stripped):
            in_ports = True
            continue
        if in_ports:
            if stripped.startswith("#") or not stripped:
                continue
            if stripped.startswith("- "):
                ports.append(stripped[2:].strip().strip('"').strip("'"))
                continue
            in_ports = False
    return ports


class StudentComposeMustNotPublishTheRobotTransports(unittest.TestCase):
    """Part 1: no host publish, or the Origin gate is bypassable."""

    def setUp(self):
        self.assertTrue(_STUDENT_COMPOSE.is_file(), _STUDENT_COMPOSE)
        self.block = _service_block(_STUDENT_COMPOSE, "physical_ai_server")
        # Zero-file/zero-block floor: an empty block would make every
        # assertion below vacuously true after a rename.
        self.assertTrue(self.block, "physical_ai_server block not found")

    def test_rosbridge_9090_is_not_published_to_the_host(self):
        for entry in _published_ports(self.block):
            self.assertNotIn(
                "9090", entry,
                "docker-compose.yml re-published rosbridge on the host. That "
                "makes the nginx Origin allowlist bypassable: any page in the "
                "student's browser can then open ws://localhost:9090 and drive "
                "the arm. Route it through the manager proxy instead.",
            )

    def test_web_video_server_8080_is_not_published_to_the_host(self):
        for entry in _published_ports(self.block):
            self.assertNotIn("8080", entry, "web_video_server re-published on the host")

    def test_the_manager_still_publishes_port_80(self):
        # The proxy is worthless if the page itself is unreachable.
        mgr = _service_block(_STUDENT_COMPOSE, "physical_ai_manager")
        self.assertTrue(mgr, "physical_ai_manager block not found")
        self.assertTrue(
            any("80:80" in entry for entry in _published_ports(mgr)),
            "the manager must still publish :80 — it now carries both transports",
        )


class OpiComposeKeepsItsPublishesDeliberately(unittest.TestCase):
    """The opi divergence is intentional (CLAUDE.md: debug/rollback path).

    Asserted POSITIVELY so a future 'harmonisation' of the two composes is a
    conscious act rather than a silent one.
    """

    def test_opi_still_publishes_the_direct_ports(self):
        self.assertTrue(_OPI_COMPOSE.is_file(), _OPI_COMPOSE)
        block = _service_block(_OPI_COMPOSE, "physical_ai_server")
        self.assertTrue(block, "opi physical_ai_server block not found")
        joined = " ".join(_published_ports(block))
        self.assertIn("9090", joined, "opi keeps :9090 as a debug/rollback path")
        self.assertIn("8080", joined, "opi keeps :8080 as a debug/rollback path")


class NginxCarriesTheOriginGate(unittest.TestCase):
    """Part 2: the same-origin proxy plus an anchored Origin allowlist.

    `ci.yml::nginx-validate` only proves the file PARSES. Nothing else checks
    that the security properties are still in it.
    """

    def setUp(self):
        self.assertTrue(_NGINX_CONF.is_file(), _NGINX_CONF)
        self.text = _NGINX_CONF.read_text(encoding="utf-8")
        self.assertTrue(self.text.strip(), "nginx.conf is empty")

    def test_rosbridge_location_exists_and_proxies_to_the_server(self):
        self.assertRegex(self.text, r"location\s*=\s*/rosbridge\s*\{")
        self.assertIn("physical_ai_server", self.text)
        self.assertIn(":9090/", self.text)

    def test_rosbridge_location_refuses_a_disallowed_origin(self):
        # The gate itself. Without it the same-origin proxy is just a
        # convenience and any page could still reach rosbridge through it.
        body = self.text.split("location = /rosbridge")[1]
        self.assertRegex(body, r"if\s*\(\s*\$edubotics_origin_ok\s*=\s*0\s*\)")
        self.assertRegex(body, r"return\s+403\s*;")

    def test_websocket_upgrade_headers_are_forwarded(self):
        body = self.text.split("location = /rosbridge")[1]
        self.assertRegex(body, r"proxy_set_header\s+Upgrade\s+\$http_upgrade\s*;")
        self.assertRegex(body, r"proxy_set_header\s+Connection\s+\$connection_upgrade\s*;")

    def test_every_origin_allowlist_regex_is_anchored_at_both_ends(self):
        """`^…$` is what rejects http://localhost.evil.com and prefix tricks.

        An unanchored entry is the classic way this check is silently defeated,
        and it would still pass `nginx -t`.
        """
        map_block = self.text.split("map $http_origin $edubotics_origin_ok")[1]
        map_block = map_block.split("}")[0]
        regexes = re.findall(r"'~\*?([^']+)'", map_block)
        self.assertTrue(regexes, "no allowlist regexes found in the origin map")
        for rx in regexes:
            self.assertTrue(rx.startswith("^"), f"origin regex not ^-anchored: {rx}")
            self.assertTrue(rx.endswith("$"), f"origin regex not $-anchored: {rx}")

    def test_the_dot_in_the_loopback_literal_is_escaped(self):
        """An unescaped `.` makes 127a0b0c1 a valid origin."""
        map_block = self.text.split("map $http_origin $edubotics_origin_ok")[1].split("}")[0]
        self.assertIn(r"127\.0\.0\.1", map_block)

    def test_the_allowlist_matches_the_gui_bridges_reviewed_policy(self):
        """One product, one origin policy — a twin lockstep.

        gui/app/roboter_studio_control.py::_ALLOWED_ORIGIN_HOSTS is the
        already-reviewed set. If someone widens one side, this fails.
        """
        map_block = self.text.split("map $http_origin $edubotics_origin_ok")[1].split("}")[0]
        self.assertTrue(_ALLOWED_ORIGIN_HOSTS, "GUI allowlist is empty")
        for host in _ALLOWED_ORIGIN_HOSTS:
            self.assertIn(
                host.replace(".", r"\."), map_block,
                f"nginx origin map is missing the GUI-allowed host {host!r}",
            )
        # And nothing beyond that set: count the alternatives.
        hosts_in_map = re.findall(r"\^https\?://([A-Za-z0-9\\.]+)\(", map_block)
        unescaped = {h.replace("\\", "") for h in hosts_in_map}
        self.assertEqual(
            unescaped, set(_ALLOWED_ORIGIN_HOSTS),
            "nginx origin allowlist drifted from the GUI bridge's policy",
        )

    def test_video_is_proxied_so_img_streams_still_work(self):
        self.assertRegex(self.text, r"location\s+/video/\s*\{")
        self.assertIn(":8080", self.text)

    def test_video_is_deliberately_not_origin_gated(self):
        """<img> loads send no Origin; gating /video/ could blank the cameras.

        Pinned so re-adding the gate there is a conscious decision (and so the
        reasoning is discoverable from the test, not only from a comment).
        """
        video_body = self.text.split("location /video/")[1]
        self.assertNotIn("$edubotics_origin_ok", video_body)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves whatever status the test class asks for."""

    status = 400

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


class HealthChecksStayRealAfterTheProxyMove(unittest.TestCase):
    """Part 3: `check_rosbridge`/`check_video_server` must not become stubs.

    The old raw TCP connect to the published :9090 cannot work any more. These
    run a REAL local HTTP server and assert the verdict flips on the status —
    which is what proves the functions are not hardcoded True.
    """

    def _serve(self, status):
        _Handler.status = status
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_a_rosbridge_400_counts_as_ALIVE(self):
        # A plain GET to rosbridge's WebSocket endpoint answers 400
        # ('Can "Upgrade" only to "WebSocket".'). That is liveness, not failure
        # — treating it as failure would report every healthy rig as down.
        port = self._serve(400)
        self.assertTrue(health_checker.check_rosbridge(port=port))

    def test_an_nginx_502_counts_as_DOWN(self):
        # The robot container is not up: nginx cannot reach the upstream.
        port = self._serve(502)
        self.assertFalse(health_checker.check_rosbridge(port=port))
        self.assertFalse(health_checker.check_video_server(port=port))

    def test_503_and_504_also_count_as_DOWN(self):
        for status in (503, 504):
            port = self._serve(status)
            self.assertFalse(
                health_checker.check_rosbridge(port=port), f"status {status}"
            )

    def test_a_200_counts_as_alive(self):
        port = self._serve(200)
        self.assertTrue(health_checker.check_rosbridge(port=port))
        self.assertTrue(health_checker.check_video_server(port=port))

    def test_nothing_listening_counts_as_DOWN(self):
        # Manager container down / port closed. Uses a port we never bound.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()
        self.assertFalse(health_checker.check_rosbridge(port=closed_port))

    def test_the_probes_target_the_proxy_paths_not_the_raw_ports(self):
        self.assertEqual(health_checker._PROXY_ROSBRIDGE_PATH, "/rosbridge")
        self.assertEqual(health_checker._PROXY_VIDEO_PATH, "/video/")
        # Default port is the manager's :80, not the retired 9090/8080.
        import inspect
        for fn in (health_checker.check_rosbridge, health_checker.check_video_server):
            default = inspect.signature(fn).parameters["port"].default
            self.assertNotIn(default, (9090, 8080), f"{fn.__name__} still defaults to a retired port")


if __name__ == "__main__":
    unittest.main()
