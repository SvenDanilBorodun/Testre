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
_OPI_NGINX = _REPO_ROOT / "physical_ai_tools/physical_ai_manager/nginx.opi.conf.template"


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


class OpiDebugPublishesAreLoopbackPinned(unittest.TestCase):
    """The opi debug ports keep existing, but ONLY on 127.0.0.1.

    This class replaces `OpiComposeKeepsItsPublishesDeliberately`, which
    asserted merely that :9090/:8080 appear in the block and therefore stayed
    green while they were bound to 0.0.0.0 — i.e. it pinned the hole open.

    Measured 2026-08-08 against rosbridge_server 2.7.0: `check_origin` is
    `return True`, so with a LAN-bound publish an `Origin: http://evil.example`
    handshake straight to :9090 returns 101 and can publish to
    /leader/joint_trajectory. The manager's `location = /rosbridge` Origin gate
    is then simply skipped rather than defeated. Loopback-pinning is what makes
    that gate the only browser-reachable route, exactly as the student compose
    records for its own removed publishes.

    The bind host is asserted LITERALLY: `${EDUBOTICS_BIND_HOST}` resolves to
    0.0.0.0 on any Pi with the shipped `DEFAULT_LAN_OPEN = "1"`, so accepting
    an interpolation here would accept the defect.
    """

    def setUp(self):
        self.assertTrue(_OPI_COMPOSE.is_file(), _OPI_COMPOSE)
        block = _service_block(_OPI_COMPOSE, "physical_ai_server")
        self.assertTrue(block, "opi physical_ai_server block not found")
        self.published = _published_ports(block)
        self.assertTrue(self.published, "opi physical_ai_server publishes nothing at all")

    def test_the_debug_ports_are_still_published_for_ssh_tunnelled_debugging(self):
        joined = " ".join(self.published)
        self.assertIn("9090", joined, "opi keeps :9090 for `ssh -L`-tunnelled debugging")
        self.assertIn("8080", joined, "opi keeps :8080 for `ssh -L`-tunnelled debugging")

    def test_every_debug_publish_is_pinned_to_loopback(self):
        for entry in self.published:
            if "9090" not in entry and "8080" not in entry:
                continue
            self.assertTrue(
                entry.startswith("127.0.0.1:"),
                "opi debug publish %r must be pinned to 127.0.0.1 — rosbridge is "
                "unauthenticated and accepts every Origin, so a LAN-bound publish "
                "lets any web page skip the nginx gate and drive the arm" % (entry,),
            )

    def test_the_bind_host_variable_never_reaches_a_debug_publish(self):
        # The whole defect was one `${EDUBOTICS_BIND_HOST}` here: the agent
        # writes 0.0.0.0 into it whenever EDUBOTICS_LAN_OPEN=1, which is the
        # shipped default. The manager's :80 publish MUST keep using it.
        for entry in self.published:
            if "9090" in entry or "8080" in entry:
                self.assertNotIn(
                    "EDUBOTICS_BIND_HOST", entry,
                    "the debug publish %r follows EDUBOTICS_BIND_HOST again; that "
                    "resolves to 0.0.0.0 on every Pi with DEFAULT_LAN_OPEN='1'" % (entry,),
                )

    def test_the_manager_port_80_still_follows_the_bind_host(self):
        # The counterpart the fix must NOT break: LAN_OPEN drives :80, and
        # pinning that to loopback would make every Pi unreachable.
        mgr = _service_block(_OPI_COMPOSE, "physical_ai_manager")
        self.assertTrue(mgr, "opi physical_ai_manager block not found")
        joined = " ".join(_published_ports(mgr))
        self.assertIn("EDUBOTICS_BIND_HOST", joined)
        self.assertIn(":80:80", joined)


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
        # EXACT match on /video/stream, not a /video/ prefix — see the next
        # test for why the prefix was a same-origin XSS surface.
        self.assertRegex(self.text, r"location\s*=\s*/video/stream\s*\{")
        self.assertIn(":8080", self.text)

    def test_only_the_stream_endpoint_is_exposed_on_our_origin(self):
        """A /video/ PREFIX put upstream's whole endpoint set same-origin.

        web_video_server's `handle_stream_viewer` reflects its `topic` query
        parameter RAW into a text/html response (it validates `type`, never the
        topic), so /video/stream_viewer?topic=<script> was a reflected XSS on
        http://localhost — exactly the Origin the /rosbridge gate trusts, i.e.
        a full rosbridge session and the ability to drive the arm.

        The app only ever requests `<base>/stream`, so an exact-match location
        keeps the cameras working with none of that surface.
        """
        self.assertNotRegex(
            self.text, r"location\s+/video/\s*\{",
            "the /video/ PREFIX location is back — that re-exposes "
            "web_video_server's HTML-reflecting stream_viewer on our own origin")

    def test_the_student_config_carries_the_same_security_headers_as_the_opi_one(self):
        """This file cites nginx.opi.conf.template as its model.

        It shipped without any of that template's four headers, i.e. a strictly
        weaker copy of the config it names. Now that both robot transports are
        same-origin, nosniff and DENY are what keep a reflected upstream
        response from being sniffed or framed into script on the trusted origin.
        """
        for header in ("X-Frame-Options", "X-Content-Type-Options",
                       "Referrer-Policy", "Permissions-Policy"):
            self.assertRegex(
                self.text, r"add_header\s+%s\s" % re.escape(header),
                f"missing an add_header for {header}")
        # Deliberately NOT HSTS: nothing terminates TLS in front of a rig, and
        # RFC 6797 makes a UA ignore it over plain HTTP. Matched as a
        # DIRECTIVE — the name legitimately appears in the comment that
        # explains the omission.
        self.assertNotRegex(self.text, r"add_header\s+Strict-Transport-Security")

    def test_locations_with_their_own_add_header_redeclare_the_security_set(self):
        """nginx add_header does NOT inherit into a location that sets one.

        So every Cache-Control location must repeat the four, or it silently
        serves them bare — the trap the opi template documents at its top.
        """
        import re as _re
        for block in _re.findall(r"location[^{]*\{(.*?)\n    \}", self.text, _re.S):
            if "Cache-Control" in block:
                self.assertIn("X-Content-Type-Options", block,
                              "a Cache-Control location lost the security headers")

    def test_video_is_deliberately_not_origin_gated(self):
        """<img> loads send no Origin; gating /video/ could blank the cameras.

        Pinned so re-adding the gate there is a conscious decision (and so the
        reasoning is discoverable from the test, not only from a comment).
        """
        video_body = self.text.split("location = /video/stream")[1]
        self.assertNotIn("$edubotics_origin_ok", video_body)

    def test_the_upstream_wildcard_cors_header_is_hidden_on_video(self):
        """web_video_server sends `Access-Control-Allow-Origin: *` itself.

        Verified in upstream `MultipartStream::send_initial_header()`
        (src/multipart_stream.cpp — the multipart/x-mixed-replace path this app
        uses) and again in the snapshot streamers. nginx FORWARDS an upstream
        header it does not explicitly hide, so left alone that wildcard lets any
        page fetch() a camera frame and READ the pixels. Since /video/ is
        deliberately not Origin-gated (an <img> carries no Origin), this
        proxy_hide_header is the ONLY thing standing between a third-party page
        and the classroom camera. It costs the app nothing — <img> needs no
        CORS header.
        """
        video_body = self.text.split("location = /video/stream")[1].split("location ")[0]
        self.assertRegex(
            video_body,
            r"proxy_hide_header\s+Access-Control-Allow-Origin\s*;",
            "the upstream wildcard CORS header is no longer stripped from "
            "/video/ — cross-origin JavaScript can now read the camera",
        )


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
        self.assertEqual(health_checker._PROXY_VIDEO_PATH, "/video/stream")
        # Default port is the manager's :80, not the retired 9090/8080.
        import inspect
        for fn in (health_checker.check_rosbridge, health_checker.check_video_server):
            default = inspect.signature(fn).parameters["port"].default
            self.assertNotIn(default, (9090, 8080), f"{fn.__name__} still defaults to a retired port")


class ProbePathsActuallyRouteToTheUpstream(unittest.TestCase):
    """A probe path that matches no proxy location is a health check that
    CANNOT FAIL — and nothing above notices, because the stub server in
    `HealthChecksStayRealAfterTheProxyMove` answers every path identically.

    That is not hypothetical. `_PROXY_VIDEO_PATH` was `/video/` while nginx
    carried a `location /video/` PREFIX. Narrowing it to
    `location = /video/stream` (so web_video_server's HTML-reflecting
    `stream_viewer` stopped being same-origin) left the probe matching only the
    SPA catch-all `location / { try_files $uri /index.html; }`, which nginx
    answers 200 from disk. Measured against a real nginx + a stopped upstream:
    `check_rosbridge()` went False while `check_video_server()` stayed True.

    So this asserts the CROSS-FILE contract the two commits broke between
    them: every probe path must be served by a location that proxies, never by
    the SPA fallback.
    """

    def setUp(self):
        self.assertTrue(_NGINX_CONF.is_file(), _NGINX_CONF)
        self.text = _NGINX_CONF.read_text(encoding="utf-8")

    def _proxying_locations(self):
        """Map of exact-match location path -> its body, for bodies that proxy.

        Only `location = <path>` is considered: a probe must match a location
        DETERMINISTICALLY. nginx picks an exact match before any prefix, so an
        exact hit cannot be stolen by the SPA catch-all.
        """
        found = {}
        for match in re.finditer(r"location\s*=\s*(\S+)\s*\{", self.text):
            body = self.text[match.end():].split("\n    }")[0]
            if "proxy_pass" in body:
                found[match.group(1)] = body
        return found

    def test_every_probe_path_is_served_by_a_proxying_location(self):
        proxied = self._proxying_locations()
        self.assertTrue(proxied, "no proxying exact-match locations found at all")
        for name, path in (
            ("_PROXY_ROSBRIDGE_PATH", health_checker._PROXY_ROSBRIDGE_PATH),
            ("_PROXY_VIDEO_PATH", health_checker._PROXY_VIDEO_PATH),
        ):
            self.assertIn(
                path, proxied,
                f"{name}={path!r} matches no proxying `location = …` in "
                f"nginx.conf, so the probe is answered by the SPA catch-all "
                f"and the health check can never report a dead upstream. "
                f"Proxying locations present: {sorted(proxied)}",
            )


class TheOriginMapDefaultIsDeny(unittest.TestCase):
    """`default 0` is the entire gate; every allowlist entry is an exception.

    Flipping it to `1` allows EVERY origin — the one-character way to turn the
    whole control-plane gate off — and it was invisible to every other test
    here, all of which inspect only the allowlist REGEXES. Measured against a
    real nginx with that single character changed: `http://evil.example`,
    `http://localhost.evil.com` and `null` all completed the WebSocket upgrade
    (101) instead of being refused.
    """

    def setUp(self):
        self.assertTrue(_NGINX_CONF.is_file(), _NGINX_CONF)
        text = _NGINX_CONF.read_text(encoding="utf-8")
        self.map_block = text.split("map $http_origin $edubotics_origin_ok")[1].split("}")[0]

    def test_the_map_default_denies(self):
        match = re.search(r"^\s*default\s+(\S+?)\s*;", self.map_block, re.MULTILINE)
        self.assertIsNotNone(
            match, "the origin map has no explicit `default` — an unmatched "
                   "Origin would fall through to an empty value")
        self.assertEqual(
            match.group(1), "0",
            "the origin map defaults to ALLOW: every Origin is now accepted "
            "and the rosbridge gate is off")


class TheOrangePiCarriesTheSameGate(unittest.TestCase):
    """Parity, on the ONLY manager published on 0.0.0.0 by default.

    The 2026-08-06 pass closed all three holes on Windows and left the Pi with
    none of them: no Origin check on /rosbridge at all, a `/video/` PREFIX (so
    upstream's HTML-reflecting `stream_viewer` stayed same-origin), and no
    `proxy_hide_header`, so the `Access-Control-Allow-Origin: *` the Windows
    side had just stripped still rode the Pi's proxy.

    THE POLICY DIFFERS AND MUST. The student config names `localhost` /
    `127.0.0.1` because gui_app opens exactly that URL; a Pi is reached at
    whatever LAN IP or hostname the school gave it, so a fixed allowlist would
    lock every Pi out. The Pi rule is HOST-RELATIVE — the Origin must name the
    same AUTHORITY the request was addressed to — which is why this is a
    separate class and not an extension of the map assertions above.

    Driven against a real nginx 1.27.5-alpine (the base Dockerfile.opi ships)
    with the template rendered by nginx:alpine's own envsubst and a fake
    upstream aliased `physical_ai_server` on a user-defined network, so
    `resolver 127.0.0.11` resolves. Measured, with `Host: pi.local`:

        no Origin                          -> 101 (reached the upstream)
        http://pi.local                    -> 101
        https://pi.local                   -> 101
        http://pi.local:8080               -> 403
        http://evil.example                -> 403
        null                               -> 403
        http://pi.local.evil.com           -> 403
        http://evil.com/#http://pi.local   -> 403
        http://pi.localX                   -> 403

    ...and with `Host: 192.168.1.50`, `http://192.168.1.50` -> 101 while
    `http://192.168.1.51` -> 403; with `Host: pi.local:8080`,
    `http://pi.local:8080` -> 101 and `http://pi.local` -> 403; `Host: [::1]`
    with `http://[::1]` -> 101. The allowed cases reached the upstream with
    `Upgrade: websocket` and `Connection: upgrade` intact, i.e. the `if` blocks
    do not break proxy_pass.

    Mutation-verified on that same rig: flipping the initial
    `set $edubotics_origin_ok 0` to 1, deleting the `return 403` block, and
    removing the gate entirely each turned `http://evil.example` from 403 into
    200. Restoring the `/video/` prefix let `/video/stream_viewer` reach the
    upstream again; removing `proxy_hide_header` put the wildcard ACAO back on
    `/video/stream`.
    """

    def setUp(self):
        self.assertTrue(_OPI_NGINX.is_file(), _OPI_NGINX)
        self.text = _OPI_NGINX.read_text(encoding="utf-8")
        self.assertIn(
            "location = /rosbridge", self.text,
            "the opi /rosbridge location is gone — nothing below is compared")
        self.gate = self.text.split("location = /rosbridge")[1].split("proxy_pass")[0]

    def _host_map_body(self):
        """Body of the $edubotics_host_is_own map.

        Terminated on a LINE-INITIAL `}`, never on the first `}` in the text:
        the map's own regexes contain `{1,3}` and `(:[0-9]+)?`, so a naive
        `split('}')` cuts inside the first entry and the assertions below go
        vacuously green on a truncated string.
        """
        match = re.search(
            r"map \$http_host \$edubotics_host_is_own \{(.*?)\n\}",
            self.text, re.S)
        self.assertIsNotNone(
            match, "the $edubotics_host_is_own map is gone — the Pi gate has "
                   "no DNS-rebinding constraint left")
        return match.group(1)

    def test_the_rosbridge_location_is_origin_gated_at_all(self):
        self.assertIn(
            "$edubotics_origin_ok", self.gate,
            "the Pi's rosbridge proxy has NO Origin gate: any page a student "
            "has open can drive the arm and read every dataset")
        self.assertRegex(self.gate, r"return\s+403\s*;")

    def test_the_host_is_constrained_against_dns_rebinding(self):
        """The host-relative rule's own blind spot.

        A host-relative gate accepts whatever authority the CLIENT chose, so an
        attacker page whose DNS is rebound to the Pi's LAN IP sends a matching
        Host and Origin and the gate opens. Measured 2026-08-08 against real
        rosbridge behind this template: 101, and the subsequent advertise of
        /leader/joint_trajectory took effect.
        """
        self.assertIn(
            "$edubotics_host_is_own", self.gate,
            "the Pi gate has no Host constraint — a DNS-rebound page satisfies "
            "the host-relative Origin rule by construction")
        self.assertRegex(self.gate, r"return\s+421\s*;")

    def test_the_host_map_starts_from_DENY(self):
        block = self._host_map_body()
        match = re.search(r"default\s+(\S+?)\s*;", block)
        self.assertIsNotNone(match, "the host map has no default")
        self.assertEqual(
            match.group(1), "0",
            "the host map defaults to ALLOW, so every rebound name passes")

    def test_the_host_map_accepts_every_shape_a_pi_is_really_reached_at(self):
        """Availability half. A fixed allowlist locks every Pi out.

        Verified against real nginx 1.27.5: each of these returned 101 with a
        matching Origin, while `attacker.example` and `evil.com` returned 421.
        """
        block = self._host_map_body()
        for shape, why in (
            ("localhost", "kiosk mode with a local monitor"),
            ("[0-9]{1,3}", "a LAN IPv4 literal — the common case"),
            (r"\[[0-9a-f:.]+\]", "a bracketed IPv6 literal"),
            (".local", "mDNS, how a Pi advertises itself"),
        ):
            self.assertIn(
                shape, block,
                f"the host map no longer accepts {why}; every Pi reached that "
                f"way answers 421 on /rosbridge")

    def test_the_agent_api_does_not_leak_the_wildcard_cors_header(self):
        """`/video/stream` strips it; `/api/system/` was one location up.

        `pi_agent/agent.py::Handler._cors` sets `Access-Control-Allow-Origin: *`
        on EVERY JSON response and GETs there are deliberately not Origin-gated,
        so without a strip any LAN page could read /status, /protokoll and the
        /cameras/* preview cross-origin. Measured 2026-08-08 against the real
        agent handler behind this template.
        """
        block = self.text.split("location /api/system/")[1].split("\n    }")[0]
        self.assertIn(
            "proxy_hide_header Access-Control-Allow-Origin", block,
            "the pi-agent's wildcard CORS header rides the same-origin proxy")

    def test_the_gate_starts_from_DENY(self):
        """The one-character way to turn the whole thing off.

        Same class of defect as the student map's `default 0`, in the shape
        this file uses instead of a map.
        """
        match = re.search(r"set\s+\$edubotics_origin_ok\s+(\S+?)\s*;", self.gate)
        self.assertIsNotNone(match, "the gate has no initial `set`")
        self.assertEqual(
            match.group(1), "0",
            "the Pi gate starts from ALLOW — every Origin is accepted and the "
            "gate is theatre")

    def test_the_refusal_is_the_LAST_decision_not_an_earlier_one(self):
        """Order matters: the `return 403` must follow every accepting `set`.

        nginx executes sibling `if` blocks in order (verified on 1.27.5), so a
        `return` placed above an accepting `set` refuses a legitimate Origin.
        """
        accepts = [m.start() for m in re.finditer(
            r"set\s+\$edubotics_origin_ok\s+1\s*;", self.gate)]
        refusal = self.gate.index("return 403")
        self.assertTrue(accepts, "nothing ever sets the gate to allow")
        self.assertLess(max(accepts), refusal)

    def test_the_rule_is_HOST_RELATIVE_not_a_fixed_allowlist(self):
        """A fixed localhost set would lock out every Pi in the field."""
        self.assertRegex(self.gate, r'"https?://\$http_host"')
        for fixed in ("localhost", "127.0.0.1"):
            self.assertNotIn(
                f"//{fixed}", self.gate,
                f"the Pi gate hardcodes {fixed} — a Pi is reached at a LAN IP "
                f"or hostname, so that refuses every real browser")

    def test_it_compares_against_http_host_and_not_host(self):
        """$host drops the port AND is taken from an absolute request line.

        Measured on nginx 1.27.5, the two differ in exactly two places:
        with `Host: pi.local:8080` the $host form ACCEPTS a page on
        `http://pi.local` (a different origin — 200) while REFUSING the
        legitimate `http://pi.local:8080` (403); and `GET
        http://evil.com/rosbridge` with `Host: pi.local` +
        `Origin: http://evil.com` is ACCEPTED under $host (200) and refused
        under $http_host. On a Pi served on :80 they otherwise agree.
        """
        self.assertNotRegex(
            self.gate, r'"https?://\$host"',
            "the gate compares against $host, which drops the port and is "
            "taken from an absolute-form request line")

    def test_both_schemes_are_accepted_for_the_same_authority(self):
        self.assertIn('"http://$http_host"', self.gate)
        self.assertIn('"https://$http_host"', self.gate)

    def test_an_ABSENT_origin_is_allowed_exactly_as_on_windows(self):
        """A browser cannot omit Origin on a WebSocket handshake.

        So a blank Origin provably did not come from the attack this gate
        exists to stop, while denying it would break roslibpy / wscat
        diagnostics and buy nothing — anything that can forge a header can also
        talk to the still-published :9090 directly.
        """
        self.assertRegex(
            self.gate,
            r'if\s*\(\s*\$http_origin\s*=\s*""\s*\)\s*\{\s*set\s+\$edubotics_origin_ok\s+1',
            "the Pi gate refuses an absent Origin, diverging from the student "
            "config for no stated reason")

    def test_only_the_stream_endpoint_is_exposed_on_the_pi_origin(self):
        """The /video/ PREFIX was still there, so the reflected XSS still was.

        `handle_stream_viewer` echoes its `topic` query parameter RAW into a
        text/html response on the manager's OWN origin — which is precisely the
        origin the host-relative gate above trusts. Nothing on a Pi requests a
        /video/ path other than `/stream`: piMode.js::videoStreamBase returns
        the bare `/video` and its only callers are ImageGridCell.js and
        CameraFeedOverlay.jsx; no compose healthcheck and no pi-agent probe
        touches it (the wizard's camera preview rides /api/system/).
        """
        self.assertRegex(self.text, r"location\s*=\s*/video/stream\s*\{")
        self.assertNotRegex(
            self.text, r"location\s+/video/\s*\{",
            "the /video/ PREFIX is back on the Pi — that re-exposes "
            "web_video_server's HTML-reflecting stream_viewer on the very "
            "origin the rosbridge gate trusts")

    def test_the_upstream_wildcard_cors_header_is_hidden_on_the_pi_too(self):
        """The parity gap the P0 pass left, on the most exposed manager.

        The opi compose binds the manager on ${EDUBOTICS_BIND_HOST} with
        constants.DEFAULT_LAN_OPEN="1", i.e. 0.0.0.0 — so this is the one build
        where any page on the school LAN can fetch() a camera frame if the
        wildcard is forwarded.
        """
        video = self.text.split("location = /video/stream")[1].split("\n    location ")[0]
        self.assertRegex(
            video, r"proxy_hide_header\s+Access-Control-Allow-Origin\s*;",
            "the Pi still forwards web_video_server's "
            "`Access-Control-Allow-Origin: *`, so cross-origin JavaScript can "
            "read the classroom camera")

    def test_video_is_deliberately_not_origin_gated_on_the_pi_either(self):
        """<img> loads send no Origin; gating /video/ could blank the cameras."""
        video = self.text.split("location = /video/stream")[1].split("\n    location ")[0]
        self.assertNotIn("$edubotics_origin_ok", video)

    def test_the_gate_is_scoped_to_rosbridge_alone(self):
        """/api/system/ and the SPA must not inherit it.

        The pi-agent has its own exact-host CSRF allowlist and the SPA is
        static; measured on the real rig, `Origin: http://evil.example` reaches
        both unchanged while /rosbridge answers 403.
        """
        for other in ("location /api/system/", "location /static/", "location / {"):
            self.assertIn(other, self.text)
        api = self.text.split("location /api/system/")[1].split("\n    location ")[0]
        self.assertNotIn("$edubotics_origin_ok", api)

    def test_the_new_variables_survive_envsubst(self):
        """Dockerfile.opi renders this with NGINX_ENVSUBST_FILTER.

        Only `EDUBOTICS_ROS_NET_GATEWAY` is substituted; every nginx runtime
        variable must therefore be single-`$`. A `${...}` spelling would be
        replaced with an empty string at container start and the gate would
        compare against nothing.
        """
        for var in ("$http_origin", "$http_host", "$edubotics_origin_ok"):
            self.assertIn(var, self.text)
            self.assertNotIn(
                "${%s}" % var[1:], self.text,
                f"{var} is written in ${{...}} form, which envsubst would "
                f"blank out at container start")


if __name__ == "__main__":
    unittest.main()
