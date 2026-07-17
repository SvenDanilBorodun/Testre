"""Deps-free unit tests for pi_agent.agent — the HTTP management API.

Covers:
  - the Host/Origin exact-host allowlist on mutating endpoints (empty allowed,
    localhost/IP/mDNS-hostname allowed, foreign + `localhost.evil.com` rejected)
  - GET/POST routing through the real handler (ephemeral loopback server), incl.
    the 403 Origin gate blocking a cross-site POST's SIDE EFFECT
  - the ACK-early async update job shape (202 + job_id, polled to terminal),
    incl. the agent-tarball staging path setting agent_restarting without a
    real restart
  - the Netzwerk-Check line shape

Every docker / network call is mocked; no docker daemon or Cloud API needed.
Mirrors the sibling pi_agent tests' import convention (no tests/__init__.py;
robotis_ai_setup on sys.path, `from pi_agent import ...`).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

from pi_agent import agent  # noqa: E402
from pi_agent import config_generator  # noqa: E402
from pi_agent import phone_camera  # noqa: E402


def _wait_for(predicate, timeout=3.0, interval=0.02):
    """Poll ``predicate`` until true or timeout; return its final value."""
    deadline = time.monotonic() + timeout
    val = predicate()
    while not val and time.monotonic() < deadline:
        time.sleep(interval)
        val = predicate()
    return val


# ── Host/Origin allowlist ────────────────────────────────────────────────────


class TestOriginAllowlist(unittest.TestCase):
    def setUp(self):
        self.app = agent.AgentApp()

    def test_empty_origin_allowed(self):
        # curl / same-origin browser requests omit Origin — must be allowed
        # (the P2 acceptance path drives everything via curl).
        self.assertTrue(self.app.origin_allowed(""))

    def test_localhost_and_loopback_allowed(self):
        self.assertTrue(self.app.origin_allowed("http://localhost"))
        self.assertTrue(self.app.origin_allowed("http://localhost:8080"))
        self.assertTrue(self.app.origin_allowed("http://127.0.0.1"))
        self.assertTrue(self.app.origin_allowed("http://[::1]:80"))

    def test_own_ip_literal_allowed(self):
        # Only the Pi's OWN interface addresses are allowed as IP literals — a
        # same-origin http://<pi-ip>/ POST must still work.
        with patch.object(self.app, "_own_ip_addresses",
                          return_value={"192.168.1.42", "10.0.0.5"}):
            self.assertTrue(self.app.origin_allowed("http://192.168.1.42"))
            self.assertTrue(self.app.origin_allowed("http://10.0.0.5:80"))

    def test_foreign_ip_literal_rejected(self):
        # A drive-by page served from http://<attacker-ip>/ presents that IP as
        # its Origin — it is NOT one of the Pi's addresses, so it is rejected.
        with patch.object(self.app, "_own_ip_addresses",
                          return_value={"192.168.1.42"}):
            self.assertFalse(self.app.origin_allowed("http://203.0.113.9"))
            self.assertFalse(self.app.origin_allowed("http://10.0.0.5:80"))

    def test_loopback_ip_always_allowed(self):
        # Loopback is unreachable off-host, so it is allowed without consulting
        # the interface set (127.0.0.0/8 + ::1).
        with patch.object(self.app, "_own_ip_addresses", return_value=set()):
            self.assertTrue(self.app.origin_allowed("http://127.0.0.1"))
            self.assertTrue(self.app.origin_allowed("http://127.0.0.5:8080"))
            self.assertTrue(self.app.origin_allowed("http://[::1]"))

    def test_own_ip_addresses_includes_loopback_free_union(self):
        # The union pulls from getaddrinfo + detect_lan_ip + interface enum;
        # normalized IPv6 forms compare equal.
        with patch("pi_agent.agent.socket.getaddrinfo",
                   return_value=[(0, 0, 0, "", ("192.168.5.9", 0))]), \
             patch("pi_agent.agent.detect_lan_ip", return_value="192.168.5.9"), \
             patch("pi_agent.agent.list_interface_ips",
                   return_value=["10.1.2.3", "fe80::1%eth0", "not-an-ip"]):
            own = self.app._own_ip_addresses()
        self.assertIn("192.168.5.9", own)
        self.assertIn("10.1.2.3", own)
        self.assertIn("fe80::1", own)  # scope-id stripped, normalized
        self.assertNotIn("not-an-ip", own)

    def test_own_ip_addresses_cached_within_bind_interval(self):
        # The Origin allowlist consults _own_ip_addresses on every mutating
        # POST with an IP-literal Origin, and list_interface_ips shells out to
        # `ip -o addr show` (up to 3 s) — the second call within the TTL must
        # be served from the cache without re-probing any source.
        with patch("pi_agent.agent.socket.getaddrinfo",
                   return_value=[(0, 0, 0, "", ("192.168.5.9", 0))]) as gai, \
             patch("pi_agent.agent.detect_lan_ip", return_value="192.168.5.9") as lan, \
             patch("pi_agent.agent.list_interface_ips", return_value=[]) as lii:
            first = self.app._own_ip_addresses()
            second = self.app._own_ip_addresses()
        self.assertEqual(first, second)
        self.assertIn("192.168.5.9", second)
        self.assertEqual(gai.call_count, 1)
        self.assertEqual(lan.call_count, 1)
        self.assertEqual(lii.call_count, 1)

    def test_mdns_hostname_allowed(self):
        with patch("pi_agent.agent.socket.gethostname", return_value="edubotics-05"):
            self.assertTrue(self.app.origin_allowed("http://edubotics-05.local"))
            self.assertTrue(self.app.origin_allowed("http://edubotics-05"))
            self.assertTrue(self.app.origin_allowed("http://edubotics-05.local:80"))

    def test_foreign_origin_rejected(self):
        self.assertFalse(self.app.origin_allowed("http://evil.com"))
        self.assertFalse(self.app.origin_allowed("https://attacker.example"))

    def test_startswith_bypass_rejected(self):
        # The exact-host check must NOT be fooled by a prefix like this — the
        # roboter_studio_control comment's precise warning.
        self.assertFalse(self.app.origin_allowed("http://localhost.evil.com"))
        self.assertFalse(self.app.origin_allowed("http://edubotics-05.local.evil.com"))

    def test_malformed_origin_rejected(self):
        self.assertFalse(self.app.origin_allowed("http://["))  # urlsplit ValueError
        self.assertFalse(self.app.origin_allowed("::::"))


# ── Routing through the real handler (ephemeral loopback server) ─────────────


class _ServerBase(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading

        self.app = agent.AgentApp()
        handler = self.app._make_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path), timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _post(self, path, body=None, origin=None):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(self._url(path), data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        if origin is not None:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())


class TestRouting(_ServerBase):
    def test_health(self):
        code, payload = self._get("/health")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

    def test_status_routes(self):
        with patch.object(agent.docker_manager, "get_container_status", return_value={}):
            code, payload = self._get("/status")
        self.assertEqual(code, 200)
        self.assertIn("lan_ip", payload)
        self.assertIn("arms_identified", payload)

    def test_unknown_get_404(self):
        code, payload = self._get("/does-not-exist")
        self.assertEqual(code, 404)
        self.assertFalse(payload["ok"])

    def test_update_check_routes(self):
        # GET /update/check reaches handle_update_check (read-only; open GET).
        with patch.object(agent.update_checker, "check_for_agent_update",
                          return_value={"version": "9.9.9", "download_url": "u", "sha256": ""}):
            code, payload = self._get("/update/check")
        self.assertEqual(code, 200)
        self.assertTrue(payload["update_available"])
        self.assertEqual(payload["latest_version"], "9.9.9")

    def test_post_cross_site_origin_rejected_before_side_effect(self):
        # A drive-by POST from a hostile page must be refused 403 WITHOUT the
        # lifecycle side effect firing.
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True) as stop:
            code, payload = self._post("/environment/stop", origin="http://evil.com")
        self.assertEqual(code, 403)
        self.assertIn("Origin", payload["message"])
        stop.assert_not_called()

    def test_post_same_origin_routes(self):
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True) as stop:
            code, payload = self._post("/environment/stop", origin=None)  # no Origin = curl
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        stop.assert_called_once()

    def test_post_pi_origin_routes(self):
        with patch("pi_agent.agent.socket.gethostname", return_value="edubotics-07"), \
             patch.object(agent.docker_manager, "stop_robot_tier", return_value=True) as stop:
            code, payload = self._post("/environment/stop", origin="http://edubotics-07.local")
        self.assertEqual(code, 200)
        stop.assert_called_once()

    def test_options_preflight(self):
        req = urllib.request.Request(self._url("/environment/stop"), method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")

    def test_unknown_post_404(self):
        code, payload = self._post("/nope", origin=None)
        self.assertEqual(code, 404)

    def test_camera_preview_rejects_ssrf_device(self):
        # A URL device would turn cv2.VideoCapture into an SSRF vector; the GET
        # side is not Origin-gated, so the device path is validated → 400.
        code, payload = self._get("/cameras/preview?device=http://169.254.169.254/latest/meta-data")
        self.assertEqual(code, 400)
        self.assertIn("Ungültiges", payload["message"])

    def test_camera_preview_rejects_traversal_device(self):
        code, payload = self._get("/cameras/preview?device=/dev/v4l/by-id/../../etc/shadow")
        self.assertEqual(code, 400)

    def test_camera_preview_missing_device_400(self):
        code, payload = self._get("/cameras/preview")
        self.assertEqual(code, 400)
        self.assertIn("Kein Kameragerät", payload["message"])


# ── camera device allowlist (SSRF guard, pure) ───────────────────────────────


class TestCameraDeviceAllowlist(unittest.TestCase):
    def test_valid_device_paths(self):
        for dev in ("/dev/video0", "/dev/video11",
                    "/dev/v4l/by-id/usb-Logitech_C920_ABCD-video-index0",
                    "/dev/v4l/by-path/platform-usb-0:1:1.0-video-index0"):
            self.assertTrue(agent._is_allowed_camera_device(dev), dev)

    def test_rejected_devices(self):
        for dev in ("", "0", "1", "http://evil/x", "rtsp://cam/stream",
                    "/dev/v4l/by-id/../../etc/shadow", "/dev/video0\n/dev/video1",
                    "/dev/sda", "/etc/passwd", "/dev/video", "/dev/videoX",
                    "/dev/v4l/by-id/", "/dev/v4l/by-id/a/b"):
            self.assertFalse(agent._is_allowed_camera_device(dev), dev)


# ── boot(): IMAGE_TAG re-alignment ───────────────────────────────────────────


class TestBootRealignsImageTag(unittest.TestCase):
    """boot() must re-assert the pinned IMAGE_TAG into the existing .env.

    The agent self-updates in place (rsync of a new pi_agent/ tree carrying a
    new docker/versions.env), so constants.IMAGE_TAG jumps to the new release
    while the .env — the file compose actually reads via --env-file — still
    names the old one. Nothing else re-asserts it on the boot path, so without
    this the always-on manager keeps running the PREVIOUS release until some
    unrelated regenerate happens to fix it.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = os.path.join(self.dir, ".env")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp()
        self.app.env_file = self.env
        self.app._log = lambda _m: None

    def _boot(self):
        # _pull_images_after_self_update is mocked here and exercised on its own
        # in TestPostSelfUpdatePull. Leaving it live would let boot() reach the
        # REAL docker_manager.check_for_updates — registry TCP probes, manifest
        # inspects and pulls against ghcr.io — from a module that promises "every
        # docker / network call is mocked". Measured: 75 s for this one test, and
        # its verdict would depend on the CI runner's network.
        with patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(self.app, "_pull_images_after_self_update"), \
             patch.object(self.app, "start_loopback"), \
             patch.object(agent.threading, "Thread"):
            self.app.boot()

    def _write(self, body):
        with open(self.env, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(self.env, 0o600)

    def _read(self):
        with open(self.env, encoding="utf-8") as f:
            return f.read()

    def test_stale_tag_is_realigned_and_everything_else_survives(self):
        self._write(
            "# EduBotics Orange Pi\n"
            'REGISTRY="ghcr.io/svendanilborodun"\n'
            'IMAGE_TAG="0.0.0-stale"\n'
            'FOLLOWER_PORT="/dev/serial/by-id/xyz"\n'
            'HF_TOKEN="hf_secret"\n'
            'EDUBOTICS_VCODEC="h264"\n'
        )
        with patch.object(agent, "IMAGE_TAG", "9.9.9"):
            self._boot()
        self.assertEqual(
            config_generator.read_env_var("IMAGE_TAG", self.env), "9.9.9")
        # The .env is the compose interface AND holds the student's HF token:
        # an upsert that dropped unmanaged lines would be a silent data loss.
        self.assertEqual(
            config_generator.read_env_var("HF_TOKEN", self.env), "hf_secret")
        self.assertEqual(
            config_generator.read_env_var("FOLLOWER_PORT", self.env),
            "/dev/serial/by-id/xyz")
        self.assertEqual(
            config_generator.read_env_var("EDUBOTICS_VCODEC", self.env), "h264")
        self.assertIn("# EduBotics Orange Pi", self._read())
        # setup.sh chmods the .env 600; a rewrite must not re-widen it.
        self.assertEqual(os.stat(self.env).st_mode & 0o777, 0o600)

    def test_matching_tag_is_a_byte_identical_no_op(self):
        self._write('IMAGE_TAG="9.9.9"\nHF_TOKEN="hf_secret"\n')
        snapshot = self._read()
        with patch.object(agent, "IMAGE_TAG", "9.9.9"):
            self._boot()
        self.assertEqual(self._read(), snapshot)

    def test_env_without_image_tag_gets_one(self):
        # A hand-seeded .env has no IMAGE_TAG at all; compose would then apply
        # its own ${IMAGE_TAG:-latest} default and drift to main HEAD.
        self._write('REGISTRY="ghcr.io/svendanilborodun"\n')
        with patch.object(agent, "IMAGE_TAG", "9.9.9"):
            self._boot()
        self.assertEqual(
            config_generator.read_env_var("IMAGE_TAG", self.env), "9.9.9")

    def test_unwritable_env_never_blocks_boot(self):
        # The realign is a convenience, not a precondition: the manager must
        # still come up (degraded) if the .env cannot be rewritten.
        self._write('IMAGE_TAG="0.0.0-stale"\n')
        with patch.object(agent, "IMAGE_TAG", "9.9.9"), \
             patch.object(agent.config_generator, "upsert_env_var",
                          side_effect=OSError("read-only fs")), \
             patch.object(agent.docker_manager, "start_manager",
                          return_value=True) as mgr, \
             patch.object(self.app, "_pull_images_after_self_update"), \
             patch.object(self.app, "start_loopback"), \
             patch.object(agent.threading, "Thread"):
            self.app.boot()  # must not raise
        mgr.assert_called_once()

    # ── C1: the tag mismatch must also PULL the new images ────────────────────

    def _boot_capturing_pull(self, tag="9.9.9"):
        """Boot with the pull mocked, returning the mock so callers can assert
        whether the resilient acquisition ran."""
        with patch.object(agent, "IMAGE_TAG", tag), \
             patch.object(agent.docker_manager, "start_manager", return_value=True) as mgr, \
             patch.object(self.app, "_pull_images_after_self_update") as pull, \
             patch.object(self.app, "start_loopback"), \
             patch.object(agent.threading, "Thread"):
            self.app.boot()
        return pull, mgr

    def test_tag_mismatch_pulls_the_new_images_before_the_manager_starts(self):
        """A tag mismatch is the ONLY signal that a self-update landed, so it is
        where the NEW images must be acquired resiliently.

        Realigning the .env alone was the bug: the OLD agent's update job pulled
        against the OLD tag, so after the swap nothing had ever fetched
        `-opi:<new>`. start_manager's `compose up` would then fetch it via
        compose's lazy pull_policy — GHCR-only, no Hub twin, no retry, and no
        loud failure when the opi build leg skipped and the tag does not exist.
        A Pi that was fine on the prior tag bricks its manager on the next boot.
        """
        self._write('IMAGE_TAG="0.0.0-stale"\n')
        order = []
        with patch.object(agent, "IMAGE_TAG", "9.9.9"), \
             patch.object(agent.docker_manager, "start_manager",
                          side_effect=lambda **kw: order.append("manager") or True), \
             patch.object(self.app, "_pull_images_after_self_update",
                          side_effect=lambda: order.append("pull")), \
             patch.object(self.app, "start_loopback"), \
             patch.object(agent.threading, "Thread"):
            self.app.boot()
        # Ordering is the whole point: the pull exists so the manager's `up`
        # lands on an image already fetched through the resilient path. Pulling
        # AFTER would leave the GHCR-only lazy pull as the real acquirer.
        self.assertEqual(order, ["pull", "manager"])
        # And the realign still happened.
        self.assertEqual(
            config_generator.read_env_var("IMAGE_TAG", self.env), "9.9.9")

    def test_the_api_binds_before_the_multi_gb_pull(self):
        """BOTH listeners must bind before the pull — loopback AND the gateway.

        On a post-self-update boot every `-opi` image is absent at the new tag,
        so this pull is ~5-6 GB and can run for tens of minutes on a school link.
        boot() is synchronous, so running it before the listeners bind leaves
        /api/system/* dead for the whole download. The manager container survives
        the agent restart (restart: unless-stopped), so the browser DOES load the
        wizard — and then every call 502s: useAgentUpdate's poll exhausts and
        reports a FALSE failure, and the SSE Protokoll is unreachable, so the
        student cannot read the [FEHLER] this pull exists to surface. That is
        worst exactly on the slow-link/Hub-fallback school this path targets.

        The GATEWAY binder is the load-bearing half and MUST be pinned here:
        nginx's /api/system/ reverse proxy targets the ros_net gateway ip
        (nginx.opi.conf.template), NOT loopback — a remote Chrome's `localhost`
        is the student's own laptop. An earlier version of this test blanket-
        patched agent.threading.Thread, which mocked the gateway binder out
        entirely: it pinned only the loopback listener, so the real blackout
        survived a green suite. Record thread starts BY NAME instead.
        """
        self._write('IMAGE_TAG="0.0.0-stale"\n')
        order = []

        def _record_thread(*_a, **kw):
            # Only the gateway binder is load-bearing for the proxy path.
            if kw.get("name") == "agent-gateway-binder":
                order.append("gateway")
            return MagicMock()

        with patch.object(agent, "IMAGE_TAG", "9.9.9"), \
             patch.object(agent.docker_manager, "start_manager",
                          side_effect=lambda **kw: order.append("manager") or True), \
             patch.object(self.app, "_pull_images_after_self_update",
                          side_effect=lambda: order.append("pull")), \
             patch.object(self.app, "start_loopback",
                          side_effect=lambda: order.append("listener")), \
             patch.object(agent.threading, "Thread", side_effect=_record_thread):
            self.app.boot()
        # Both listeners up, THEN the multi-GB pull, THEN the manager.
        self.assertEqual(order, ["listener", "gateway", "pull", "manager"])

    def test_plain_reboot_does_not_pull(self):
        # No mismatch = no self-update = nothing new to fetch. Boot must stay
        # cheap (and offline-safe): a registry round-trip on every reboot would
        # make the wizard's arrival hostage to the school network.
        self._write('IMAGE_TAG="9.9.9"\n')
        pull, _ = self._boot_capturing_pull()
        pull.assert_not_called()

    def test_pull_failure_never_blocks_boot(self):
        # Advisory, like the realign itself: a Pi whose new images can't be
        # fetched must still boot and serve the wizard (that is where the
        # student's only repair trigger lives).
        self._write('IMAGE_TAG="0.0.0-stale"\n')
        with patch.object(agent, "IMAGE_TAG", "9.9.9"), \
             patch.object(agent.docker_manager, "check_for_updates",
                          side_effect=RuntimeError("registry exploded")), \
             patch.object(agent.docker_manager, "start_manager",
                          return_value=True) as mgr, \
             patch.object(self.app, "start_loopback"), \
             patch.object(agent.threading, "Thread"):
            self.app.boot()  # must not raise
        mgr.assert_called_once()


# ── C1: the post-self-update resilient pull itself ───────────────────────────


class TestPostSelfUpdatePull(unittest.TestCase):
    """`_pull_images_after_self_update` is the boot-side half of C1.

    The pre-existing `test_update_fails_when_the_release_images_were_never_
    published` mocks `check_for_updates` wholesale, so it proves the JOB reacts
    to a `missing_out` — not that anything ever calls the resilient path for the
    images an update installs. Nothing did: that was the bug. These tests pin the
    contract at the seam that actually matters.
    """

    def setUp(self):
        self.app = agent.AgentApp()
        self.logs = []
        self.app._log = self.logs.append

    def _log_text(self):
        return "\n".join(self.logs)

    def test_it_routes_through_the_resilient_path_with_a_missing_out(self):
        # The whole point of reusing check_for_updates is inheriting the
        # digest pre-check + GHCR→Hub fallback + prune. Passing a missing_out
        # list is what makes the MISSING verdict observable rather than a log
        # line nobody reads.
        with patch.object(agent.docker_manager, "check_for_updates",
                          return_value=True) as pull:
            self.app._pull_images_after_self_update()
        pull.assert_called_once()
        self.assertIsInstance(pull.call_args.kwargs.get("missing_out"), list)

    def test_unpublished_opi_tag_fails_loudly_naming_the_version(self):
        """The failure this whole fix exists for.

        `-opi` is DECOUPLED from the core release (continue-on-error legs +
        marker-gated retag), so W4 can go green having published NO
        `physical-ai-server-opi:X.Y.Z` — while the pi-agent tarball ships anyway
        and every Pi self-updates onto that version. Before C1 the new tag was
        never pulled here at all, so this state surfaced only as compose's bare
        `manifest unknown` — which reads like a network fault and sends the
        teacher to the wrong problem entirely.
        """
        def fake_pull(log=None, missing_out=None):
            if missing_out is not None:
                missing_out.append("physical-ai-server-opi:9.9.9")
            return False

        with patch.object(agent, "IMAGE_TAG", "9.9.9"), \
             patch.object(agent.docker_manager, "check_for_updates",
                          side_effect=fake_pull):
            self.app._pull_images_after_self_update()
        text = self._log_text()
        self.assertIn("[FEHLER]", text)
        self.assertIn("9.9.9", text)                        # names the VERSION
        self.assertIn("physical-ai-server-opi:9.9.9", text)  # ...and the image
        self.assertIn("kein Netzwerkfehler", text)          # not a Wi-Fi blip
        self.assertIn("unvollständig veröffentlicht", text)  # literal umlauts (Rule §1)

    def test_a_clean_pull_says_nothing_alarming(self):
        with patch.object(agent.docker_manager, "check_for_updates",
                          return_value=True):
            self.app._pull_images_after_self_update()
        self.assertNotIn("[FEHLER]", self._log_text())

    def test_a_transient_failure_is_not_reported_as_unpublished(self):
        # check_for_updates returns False with an EMPTY missing_out when the
        # registry was merely unreachable. Accusing the release of never having
        # published would be a false alarm on exactly the flaky school network
        # this product targets.
        with patch.object(agent.docker_manager, "check_for_updates",
                          return_value=False):
            self.app._pull_images_after_self_update()
        self.assertNotIn("[FEHLER]", self._log_text())

    def test_it_never_raises_out_of_boot(self):
        with patch.object(agent.docker_manager, "check_for_updates",
                          side_effect=RuntimeError("docker is gone")):
            self.app._pull_images_after_self_update()  # must not raise
        # ...and it still says something, so the Protokoll isn't silent.
        self.assertTrue(self.logs)


# ── M1: provisioned system-files drift (compose/systemd vs agent) ────────────


class TestSystemFilesVersionCheck(unittest.TestCase):
    """The agent self-updates `pi_agent/` only; the compose + systemd units stay
    frozen at whatever setup.sh provisioned.

    The signal keys on CONTENT — a byte-compare of the INSTALLED units against
    the ones this agent ships — never on `stamp != APP_VERSION`. That inequality
    is the normal state of every self-updated Pi (the stamp cannot advance by
    design), so keying on it warned ~100 % of healthy Pis, and the text told the
    teacher to re-flash the eMMC: routine advice to destroy the students'
    datasets. Advisory only — never a boot blocker, never a wipe instruction.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.stamp = os.path.join(self.dir, ".system-files-version")
        self.installed = os.path.join(self.dir, "systemd-system")
        os.makedirs(self.installed)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp()
        self.logs = []
        self.app._log = self.logs.append
        # The units this agent version ships (the real files in pi_agent/systemd/).
        self.shipped_dir = os.path.join(os.path.dirname(os.path.abspath(agent.__file__)),
                                        "systemd")

    def _install_units(self, drift=False):
        """Lay down /etc/systemd/system copies of the shipped units. `drift=True`
        models the M1 failure: setup.sh installed an OLDER unit and the agent has
        since self-updated past it."""
        for unit in agent.SYSTEMD_UNIT_NAMES:
            with open(os.path.join(self.shipped_dir, unit), "rb") as f:
                data = f.read()
            if drift:
                data += b"\n# provisioned by an older release\n"
            with open(os.path.join(self.installed, unit), "wb") as f:
                f.write(data)

    def _check(self, stamp_contents=None, app_version="2.14.0"):
        if stamp_contents is not None:
            with open(self.stamp, "w", encoding="utf-8") as f:
                f.write(stamp_contents)
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent, "APP_VERSION", app_version):
            self.app._check_system_files_version()
        return "\n".join(self.logs)

    def test_a_self_updated_pi_whose_units_match_is_silent(self):
        """THE regression guard. Stamp 2.13.0 vs agent 2.14.0 is the ordinary
        state of every self-updated Pi — the stamp CANNOT advance. If the units
        on disk are byte-identical to the ones we ship, nothing drifted and there
        is nothing to say. Keying on the version pair here is what produced a
        ~100 % false-positive banner telling teachers to re-flash the eMMC."""
        self._install_units(drift=False)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
        self.assertEqual(text, "")
        self.assertFalse(self.app._system_files_stale)
        self.assertIsNone(self.app._system_files_version)

    def test_drifted_units_warn_in_german_and_name_the_unit(self):
        self._install_units(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
        self.assertIn("[WARNUNG]", text)
        self.assertIn("edubotics-pi.service", text)  # WHICH file drifted
        self.assertIn("2.13.0", text)  # where the on-disk files came from
        self.assertIn("2.14.0", text)  # what the agent is
        self.assertTrue(self.app._system_files_stale)
        self.assertEqual(self.app._system_files_version, "2.13.0")

    def test_the_remedy_is_never_destructive(self):
        """A drift warning must NEVER tell anyone to re-flash. The drift is
        usually benign, the students' datasets are irreplaceable and live on the
        same eMMC, and re-running setup.sh fixes the units while leaving the
        Docker volumes alone. This is the whole reason the banner was reworked."""
        self._install_units(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
        for destructive in ("neu aufsetzen", "neu bespielen", "SD-Karte", "eMMC",
                            "formatieren", "löschen"):
            self.assertNotIn(destructive, text, f"destructive remedy in banner: {destructive}")
        self.assertIn("setup.sh", text)  # the non-destructive remedy
        self.assertIn("bleiben dabei erhalten", text)  # says the data survives

    def test_german_umlauts_are_literal(self):
        # german-strings-lint scans [WARNUNG] lines for ae/oe/ue transliteration.
        self._install_units(drift=True)
        text = self._check(stamp_contents="2.13.0\n")
        for bad in ("Aenderung", "ueber", "koennen", "ausfuehren", "Datensaetze"):
            self.assertNotIn(bad, text)
        self.assertIn("ausführen", text)

    def test_drift_without_a_stamp_still_warns_but_names_no_version(self):
        # A pre-stamp Pi can be genuinely drifted. The evidence is the unit
        # bytes, so the warning stands; it just cannot name an origin version.
        self._install_units(drift=True)
        text = self._check(stamp_contents=None)
        self.assertIn("[WARNUNG]", text)
        self.assertTrue(self.app._system_files_stale)
        self.assertIsNone(self.app._system_files_version)
        self.assertNotIn("Stand: Version", text)

    def test_blank_stamp_is_treated_as_absent(self):
        # A truncated write must not be read as the literal version "".
        self._install_units(drift=True)
        self._check(stamp_contents="   \n")
        self.assertIsNone(self.app._system_files_version)

    def test_uninstalled_units_prove_nothing_and_stay_silent(self):
        # A dev box / relocated install / non-root reader cannot compare. Absent
        # evidence is not evidence of drift — never guess.
        text = self._check(stamp_contents="2.13.0\n")  # no units installed
        self.assertEqual(text, "")
        self.assertFalse(self.app._system_files_stale)

    def test_unreadable_stamp_never_raises(self):
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE",
                          os.path.join(self.dir, "nope", "deeper", "x")), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed):
            self.app._check_system_files_version()  # must not raise
        self.assertIsNone(self.app._system_files_version)

    def test_status_exposes_the_drift_for_the_system_tab(self):
        self._install_units(drift=True)
        self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
        with patch.object(agent.docker_manager, "get_container_status",
                          return_value={}), \
             patch.object(agent.config_generator, "read_env_var", return_value=""), \
             patch.object(agent.docker_manager, "get_last_pull_status",
                          return_value={}):
            _, payload = self.app.handle_status()
        self.assertTrue(payload["system_files_stale"])
        self.assertEqual(payload["system_files_version"], "2.13.0")

    def test_status_reports_not_stale_on_a_healthy_pi(self):
        # False (not absent) on every healthy AND every pre-stamp Pi, so the
        # banner renders only on PROVEN drift.
        with patch.object(agent.docker_manager, "get_container_status",
                          return_value={}), \
             patch.object(agent.config_generator, "read_env_var", return_value=""), \
             patch.object(agent.docker_manager, "get_last_pull_status",
                          return_value={}):
            _, payload = self.app.handle_status()
        self.assertFalse(payload["system_files_stale"])
        self.assertIsNone(payload["system_files_version"])


# ── ACK-early async update job ───────────────────────────────────────────────


class TestUpdateJob(unittest.TestCase):
    def setUp(self):
        self.app = agent.AgentApp()

    def test_update_fails_when_the_release_images_were_never_published(self):
        """A never-published release must FAIL the job, not report success.

        Before the IMAGE_TAG pin this could not happen — `:latest` always
        existed — so check_for_updates' per-image failures were non-fatal and
        its return meant only "did bytes change". With the pin, a skipped opi
        build (continue-on-error in docker-publish.yml) leaves `:X.Y.Z`
        non-existent, and the job used to log [FEHLER] and then report
        „Aktualisierung abgeschlossen" at 100 %. On a freshly flashed Pi there
        are no local images at all, so that success is a flat lie.
        """
        def fake_pull(log=None, missing_out=None):
            if missing_out is not None:
                missing_out.append("physical-ai-server-opi:9.9.9")
            return False

        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", side_effect=fake_pull), \
             patch.object(agent.docker_manager, "start_manager", return_value=True) as start_mgr, \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=None):
            code, payload = self.app.handle_update_start()
            self.assertEqual(code, 202)
            self._join_update(payload["job_id"])
            job = self.app._update_jobs[payload["job_id"]]

        self.assertEqual(job["status"], "failed")
        msg = job["message"]
        self.assertIn("physical-ai-server-opi:9.9.9", msg)
        self.assertIn("kein Netzwerkfehler", msg)          # German, names the cause
        self.assertIn("unvollständig veröffentlicht", msg)  # literal umlauts (Rule §1)
        self.assertNotIn("abgeschlossen", msg)
        # The manager must NOT be recreated onto a release that does not exist.
        start_mgr.assert_not_called()

    def test_update_still_succeeds_when_the_pull_is_only_transient(self):
        """A Wi-Fi blip must keep degrading gracefully — the resilience is why
        per-image pull failures are non-fatal in the first place. Only
        PULL_MISSING populates missing_out, so a transient leaves it empty and
        the job completes exactly as before this classification existed."""
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=None):
            code, payload = self.app.handle_update_start()
            self.assertEqual(code, 202)
            self._join_update(payload["job_id"])
            job = self.app._update_jobs[payload["job_id"]]
        self.assertEqual(job["status"], "succeeded")

    def _join_update(self, job_id, timeout=10.0):
        """Wait for the ACK-early background worker to reach a terminal state."""
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            j = self.app._update_jobs.get(job_id) or {}
            if j.get("status") in ("succeeded", "failed"):
                return
            _t.sleep(0.02)
        self.fail(f"update job {job_id} did not finish within {timeout}s")

    def test_update_acks_early_and_completes(self):
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=True), \
             patch.object(agent.docker_manager, "start_manager", return_value=True) as start_mgr, \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=None):
            code, payload = self.app.handle_update_start()
            # ACK-early: 202 + a job id, BEFORE the work finishes.
            self.assertEqual(code, 202)
            self.assertTrue(payload["ok"])
            self.assertIn("job_id", payload)
            self.assertEqual(payload["status"], "running")
            job_id = payload["job_id"]

            def _done():
                _, j = self.app.handle_update_status(job_id)
                return j.get("status") in ("succeeded", "failed")

            self.assertTrue(_wait_for(_done), "update job never reached a terminal state")
            _, job = self.app.handle_update_status(job_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["progress"], 100)
        self.assertFalse(job["agent_restarting"])  # no agent tarball advertised
        # The manager is recreated LAST (after the robot tier is stopped + pulled).
        start_mgr.assert_called_once()

    def test_update_stages_agent_tarball_and_flags_restart(self):
        # A tarball WITH a valid SHA-256 stages + flags a restart.
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": "ab" * 32}
        applied = {}
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=upd), \
             patch.object(agent.update_checker, "download_agent_tarball",
                          return_value="/tmp/agent.tgz") as dl, \
             patch.object(self.app, "_apply_agent_update_and_restart",
                          side_effect=lambda p: applied.setdefault("path", p)):
            code, payload = self.app.handle_update_start()
            job_id = payload["job_id"]

            def _done():
                _, j = self.app.handle_update_status(job_id)
                return j.get("status") in ("succeeded", "failed")

            self.assertTrue(_wait_for(_done))
            _, job = self.app.handle_update_status(job_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(job["agent_restarting"])
        # The verified SHA is passed through to the download gate.
        self.assertEqual(dl.call_args.kwargs.get("expected_sha256"), "ab" * 32)
        # The staged tarball is applied (the real restart is patched out).
        self.assertEqual(applied.get("path"), "/tmp/agent.tgz")

    def test_update_refuses_tarball_without_sha256(self):
        # A tarball advertised WITHOUT a SHA-256 must be refused — a root process
        # must never extractall unverified bytes. The update still succeeds
        # (images/manager updated); only the AGENT self-update is skipped.
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": ""}
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=upd), \
             patch.object(agent.update_checker, "download_agent_tarball") as dl, \
             patch.object(self.app, "_apply_agent_update_and_restart") as apply_upd:
            code, payload = self.app.handle_update_start()
            job_id = payload["job_id"]

            def _done():
                _, j = self.app.handle_update_status(job_id)
                return j.get("status") in ("succeeded", "failed")

            self.assertTrue(_wait_for(_done))
            _, job = self.app.handle_update_status(job_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertFalse(job["agent_restarting"])   # agent NOT updated
        dl.assert_not_called()                       # never downloaded
        apply_upd.assert_not_called()                # never applied
        # A German [WARNUNG] names the refusal reason.
        self.assertTrue(any("Prüfsumme" in line for line in job["log"]))

    def test_update_status_unknown_job_404(self):
        code, payload = self.app.handle_update_status("nope")
        self.assertEqual(code, 404)
        self.assertFalse(payload["ok"])

    def _join_update_threads(self):
        """Join the background update worker so its finally-block (the
        single-flight release/hold decision) has provably run."""
        for t in threading.enumerate():
            if t.name.startswith("update-"):
                t.join(timeout=5)

    def test_single_flight_held_through_pending_restart(self):
        # A successful agent-tarball apply schedules a deferred SIGTERM ~2 s
        # out. The single-flight guard must stay HELD through that window — a
        # second /update must not start stop/pull/recreate work inside a
        # process about to kill itself. (The flag resets on the fresh boot.)
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": "ab" * 32}
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=upd), \
             patch.object(agent.update_checker, "download_agent_tarball",
                          return_value="/tmp/agent.tgz"), \
             patch.object(self.app, "_apply_agent_update_and_restart",
                          return_value=True):
            code, payload = self.app.handle_update_start()
            job_id = payload["job_id"]
            self._join_update_threads()
            _, job = self.app.handle_update_status(job_id)
            self.assertEqual(job["status"], "succeeded")
            self.assertTrue(job["agent_restarting"])
            # Still busy: a second /update is refused while the restart pends.
            self.assertTrue(self.app._update_in_flight())
            code2, payload2 = self.app.handle_update_start()
        self.assertEqual(code2, 409)

    def test_failed_apply_reports_failed_and_releases_single_flight(self):
        """A FAILED apply must be reported as FAILED (H2).

        This test previously asserted `status == "succeeded"` — it pinned the
        bug. The job is marked succeeded optimistically before the apply (for
        the restart-pending happy path, where the process SIGTERMs itself before
        it could report anything later), and a failed apply used to keep that AND
        overwrite the message to „Aktualisierung abgeschlossen."

        Why that is worse than cosmetic: `useAgentUpdate` / `PiUpdateGate` reveal
        „Ohne Update fortfahren" only after a FAILED attempt. Reporting success
        means the skip button never appears — and because no restart happened,
        APP_VERSION is unchanged, so the non-closable forced modal re-arms on
        every page load. A persistently failing apply (a full eMMC — i.e. exactly
        what H1's missing prune causes) then locks the student out of their own
        Pi forever, with the UI insisting the update worked.

        The running agent stays alive and the images/manager DID update; only the
        agent self-update did not. So: failed, no restart pending, guard released
        so a retry is possible.
        """
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": "ab" * 32}
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=upd), \
             patch.object(agent.update_checker, "download_agent_tarball",
                          return_value="/tmp/agent.tgz"), \
             patch.object(self.app, "_apply_agent_update_and_restart",
                          return_value=False):
            code, payload = self.app.handle_update_start()
            job_id = payload["job_id"]
            self._join_update_threads()
            _, job = self.app.handle_update_status(job_id)
        self.assertEqual(job["status"], "failed")
        # The UI must not be told to wait for a restart that is never coming...
        self.assertFalse(job["agent_restarting"])
        # ...the guard must release so „Erneut versuchen" can actually retry...
        self.assertFalse(self.app._update_in_flight())
        # ...the job must be evictable (the TTL anchor)...
        self.assertIn("finished_at", job)
        # ...and the message must not claim success.
        self.assertNotIn("abgeschlossen", job["message"])
        self.assertIn("prüfen", job["message"])  # actionable + literal umlaut

    def test_no_terminal_status_is_published_before_the_apply_resolves(self):
        """The status a poll can observe DURING the apply must still be running.

        Reporting "failed" after the apply is not enough on its own — what the
        browser actually does decides whether the fix works. React's
        useAgentUpdate polls /update/status every 2 s and STOPS the loop at the
        first non-"running" status, so publishing "succeeded" before the apply
        and correcting it afterwards means a poll landing in that window latches
        success and never sees the correction: PiUpdateGate calls setDone(),
        dismisses the modal, and „Ohne Update fortfahren" never appears. Measured
        against the pre-fix code with a randomized poll phase, that window swallowed
        41 % of failures at a 0.5 s apply, 58 % at 1.5 s and 100 % at ≥2 s — an
        rsync onto a full eMMC sits at the deterministic end.

        So the property is not "the final status is right", it is "no terminal
        status exists until the outcome is known". Poll from INSIDE the apply,
        through the real endpoint React calls, for both outcomes.
        """
        for apply_ok in (True, False):
            with self.subTest(apply_ok=apply_ok):
                app = agent.AgentApp()
                seen = []

                def fake_apply(_path, _app=app, _seen=seen, _ok=apply_ok):
                    with _app._update_lock:
                        ids = list(_app._update_jobs)
                    for jid in ids:
                        _, job = _app.handle_update_status(jid)
                        _seen.append(job["status"])
                    return _ok

                upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz",
                       "sha256": "ab" * 32}
                with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
                     patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
                     patch.object(agent.docker_manager, "start_manager", return_value=True), \
                     patch.object(agent.update_checker, "check_for_agent_update",
                                  return_value=upd), \
                     patch.object(agent.update_checker, "download_agent_tarball",
                                  return_value="/tmp/agent.tgz"), \
                     patch.object(app, "_apply_agent_update_and_restart", fake_apply):
                    _, payload = app.handle_update_start()
                    job_id = payload["job_id"]
                    self._join_update_threads()
                    _, job = app.handle_update_status(job_id)

                self.assertTrue(seen, "the apply never ran — assertion would be vacuous")
                for status in seen:
                    self.assertEqual(
                        status, "running",
                        "a terminal status was published while the apply was still "
                        f"in flight — a 2 s poll would latch {status!r}")
                # And the real outcome still lands once it is known.
                self.assertEqual(job["status"], "succeeded" if apply_ok else "failed")

    def test_successful_apply_still_reports_succeeded_with_restart_pending(self):
        # The H2 fix must not regress the happy path: a scheduled restart keeps
        # "succeeded" + agent_restarting, and HOLDS the single-flight guard (a
        # 2nd /update must not start inside a process about to SIGTERM itself).
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": "ab" * 32}
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=upd), \
             patch.object(agent.update_checker, "download_agent_tarball",
                          return_value="/tmp/agent.tgz"), \
             patch.object(self.app, "_apply_agent_update_and_restart",
                          return_value=True):
            _, payload = self.app.handle_update_start()
            self._join_update_threads()
            _, job = self.app.handle_update_status(payload["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(job["agent_restarting"])
        self.assertTrue(self.app._update_in_flight())

    def test_terminal_jobs_carry_finished_at(self):
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=None):
            _, payload = self.app.handle_update_start()
            self._join_update_threads()
            _, job = self.app.handle_update_status(payload["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertIn("finished_at", job)  # the TTL eviction anchor


# ── update-job map TTL eviction (unbounded-growth guard) ─────────────────────


class TestUpdateJobEviction(unittest.TestCase):
    def setUp(self):
        self.app = agent.AgentApp()

    def test_stale_terminal_jobs_evicted_newest_always_kept(self):
        old = time.time() - agent._UPDATE_JOB_TTL_S - 60
        self.app._update_jobs = {
            "old-ok": {"id": "old-ok", "status": "succeeded",
                       "started_at": old, "finished_at": old},
            "old-bad": {"id": "old-bad", "status": "failed",
                        "started_at": old + 1, "finished_at": old + 1},
            "newest": {"id": "newest", "status": "succeeded",
                       "started_at": old + 2, "finished_at": old + 2},
        }
        with self.app._update_lock:
            self.app._evict_stale_update_jobs_locked()
        # Stale terminal records go; the most recent job ALWAYS survives so a
        # late poller can still read the last terminal state.
        self.assertEqual(set(self.app._update_jobs), {"newest"})

    def test_fresh_and_running_jobs_never_evicted(self):
        old = time.time() - agent._UPDATE_JOB_TTL_S - 60
        now = time.time()
        self.app._update_jobs = {
            "running-old": {"id": "running-old", "status": "running",
                            "started_at": old},
            "fresh": {"id": "fresh", "status": "failed",
                      "started_at": now, "finished_at": now},
        }
        with self.app._update_lock:
            self.app._evict_stale_update_jobs_locked()
        self.assertEqual(set(self.app._update_jobs), {"running-old", "fresh"})

    def test_update_start_sweeps_stale_records(self):
        old = time.time() - agent._UPDATE_JOB_TTL_S - 60
        self.app._update_jobs = {
            "stale-1": {"id": "stale-1", "status": "succeeded",
                        "started_at": old, "finished_at": old},
            "stale-2": {"id": "stale-2", "status": "failed",
                        "started_at": old + 1, "finished_at": old + 1},
        }
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True), \
             patch.object(agent.docker_manager, "check_for_updates", return_value=False), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(agent.update_checker, "check_for_agent_update", return_value=None):
            code, payload = self.app.handle_update_start()
            job_id = payload["job_id"]
            for t in threading.enumerate():
                if t.name.startswith("update-"):
                    t.join(timeout=5)
        self.assertEqual(code, 202)
        # /update is the sole grower of the map — it sweeps first: the older
        # stale record went, the newest pre-existing record survived.
        self.assertNotIn("stale-1", self.app._update_jobs)
        self.assertIn("stale-2", self.app._update_jobs)
        self.assertIn(job_id, self.app._update_jobs)


# ── Read-only update-availability probe (/update/check) ──────────────────────


class TestUpdateCheck(unittest.TestCase):
    def setUp(self):
        self.app = agent.AgentApp()

    def test_available_reports_latest_version(self):
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": "ab" * 32}
        with patch.object(agent.update_checker, "check_for_agent_update",
                          return_value=upd):
            code, payload = self.app.handle_update_check()
        self.assertEqual(code, 200)
        self.assertTrue(payload["update_available"])
        self.assertEqual(payload["latest_version"], "9.9.9")
        self.assertEqual(payload["current_version"], agent.APP_VERSION)

    def test_not_available_reports_false(self):
        with patch.object(agent.update_checker, "check_for_agent_update",
                          return_value=None):
            code, payload = self.app.handle_update_check()
        self.assertEqual(code, 200)
        self.assertFalse(payload["update_available"])
        self.assertEqual(payload["latest_version"], "")

    def test_cloud_error_reports_false_never_raises(self):
        # The handler must fail closed (no raise) if the cloud probe errors.
        with patch.object(agent.update_checker, "check_for_agent_update",
                          side_effect=RuntimeError("cloud down")):
            code, payload = self.app.handle_update_check()
        self.assertEqual(code, 200)
        self.assertFalse(payload["update_available"])

    def test_result_is_cached_within_ttl(self):
        # A poll storm must NOT re-hit the 5 s cloud /version fetch: the second
        # call within the TTL is served from cache (checker called ONCE).
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": ""}
        with patch.object(agent.update_checker, "check_for_agent_update",
                          return_value=upd) as chk:
            first = self.app.handle_update_check()
            second = self.app.handle_update_check()
        self.assertEqual(chk.call_count, 1)
        self.assertEqual(first, second)
        self.assertTrue(second[1]["update_available"])

    def test_force_bypasses_cache(self):
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": ""}
        with patch.object(agent.update_checker, "check_for_agent_update",
                          return_value=upd) as chk:
            self.app.handle_update_check()          # populates cache
            self.app.handle_update_check(force=True)  # bypasses it
        self.assertEqual(chk.call_count, 2)


# ── Netzwerk-Check ───────────────────────────────────────────────────────────


class TestNetzwerkCheck(unittest.TestCase):
    def setUp(self):
        self.app = agent.AgentApp()

    def test_all_green(self):
        with patch.object(self.app, "_probe_cloud", return_value=(True, None)), \
             patch.object(agent.AgentApp, "_tcp_reachable", staticmethod(lambda *a, **k: True)), \
             patch.object(agent.AgentApp, "_tls_genuine",
                          staticmethod(lambda *a, **k: (True, "DigiCert Inc"))), \
             patch.object(agent.AgentApp, "_clock_sane",
                          staticmethod(lambda *a, **k: (True, "Abweichung 1 s"))):
            code, payload = self.app.handle_netzwerk_check()
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        keys = {c["key"] for c in payload["checks"]}
        self.assertEqual(keys, {"cloud", "registry", "huggingface", "tls", "clock"})
        # Every check carries a German label.
        for c in payload["checks"]:
            self.assertTrue(c["label"])

    def test_tls_inspection_flags_red_with_hint(self):
        with patch.object(self.app, "_probe_cloud", return_value=(True, None)), \
             patch.object(agent.AgentApp, "_tcp_reachable", staticmethod(lambda *a, **k: True)), \
             patch.object(agent.AgentApp, "_tls_genuine",
                          staticmethod(lambda *a, **k: (False, "self signed cert in chain"))), \
             patch.object(agent.AgentApp, "_clock_sane",
                          staticmethod(lambda *a, **k: (True, ""))):
            _, payload = self.app.handle_netzwerk_check()
        self.assertFalse(payload["ok"])
        tls = next(c for c in payload["checks"] if c["key"] == "tls")
        self.assertFalse(tls["ok"])
        self.assertIn("TLS-Inspektion", tls["hint"])


# ── Secret redaction (Protokoll ring) ────────────────────────────────────────


class TestRedaction(unittest.TestCase):
    def test_env_secret_key_masked(self):
        self.assertEqual(agent._redact_secret_line("HF_TOKEN=hf_supersecret"), "HF_TOKEN=***")
        self.assertEqual(agent._redact_secret_line("SUPABASE_KEY=abc"), "SUPABASE_KEY=***")
        self.assertEqual(agent._redact_secret_line("  MY_PASSWORD=x"), "  MY_PASSWORD=***")

    def test_non_secret_passes_through(self):
        self.assertEqual(agent._redact_secret_line("FOLLOWER_PORT=/dev/x"), "FOLLOWER_PORT=/dev/x")
        self.assertEqual(agent._redact_secret_line("Arme werden gescannt …"),
                         "Arme werden gescannt …")

    def test_bearer_and_json_masked(self):
        self.assertEqual(agent._redact_secret_line("Authorization: Bearer abc.def"),
                         "Authorization: Bearer ***")
        self.assertEqual(agent._redact_secret_line('{"token": "abc"}'), '{"token": "***"}')


# ── Agent self-update apply (stage to pi_agent.new/ + atomic rename swap) ─────


class TestApplyAgentUpdate(unittest.TestCase):
    def test_apply_removes_orphan_and_refreshes_version(self):
        # A module REMOVED in a release must not linger after a self-update, and
        # the refreshed pi_agent/VERSION (which the agent reports) must land.
        # Under the atomic swap this falls out of the design rather than out of
        # `--delete`: the staged tree simply never contains the orphan.
        if shutil.which("rsync") is None:
            self.skipTest("rsync unavailable")
        root = tempfile.mkdtemp()
        staging = tempfile.mkdtemp()
        try:
            # Existing install: an OLD orphan module + old VERSION.
            os.makedirs(os.path.join(root, "pi_agent"))
            with open(os.path.join(root, "pi_agent", "orphan.py"), "w") as f:
                f.write("# removed in the new release\n")
            with open(os.path.join(root, "pi_agent", "VERSION"), "w") as f:
                f.write("2.0.0\n")
            # New tarball: pi_agent/agent.py + a NEW VERSION, no orphan.
            src = os.path.join(staging, "pi_agent")
            os.makedirs(src)
            with open(os.path.join(src, "agent.py"), "w") as f:
                f.write("# new agent\n")
            with open(os.path.join(src, "VERSION"), "w") as f:
                f.write("2.1.0\n")
            tarball = os.path.join(staging, "edubotics-pi-agent.tar.gz")
            with tarfile.open(tarball, "w:gz") as tf:
                tf.add(src, arcname="pi_agent")

            app = agent.AgentApp(compose_file=os.path.join(root, "docker-compose.opi.yml"))
            with patch("pi_agent.agent.os.kill") as kill:
                app._apply_agent_update_and_restart(tarball)
                # Join the deferred restart thread so its (patched) os.kill fires
                # INSIDE the patch context — otherwise the real SIGTERM would hit
                # the test runner.
                for t in threading.enumerate():
                    if t.name == "agent-restart":
                        t.join(timeout=5)
            pkg = os.path.join(root, "pi_agent")
            self.assertFalse(os.path.exists(os.path.join(pkg, "orphan.py")))  # deleted
            self.assertTrue(os.path.exists(os.path.join(pkg, "agent.py")))    # added
            with open(os.path.join(pkg, "VERSION")) as f:
                self.assertEqual(f.read().strip(), "2.1.0")                   # refreshed
            self.assertTrue(kill.called)
            self.assertEqual(kill.call_args[0][1], agent.signal.SIGTERM)
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)

    # ── M2: the apply is staged + swapped, never edited in place ─────────────

    @staticmethod
    def _tarball_of(staging, files):
        """Build a tarball whose top-level is pi_agent/ containing ``files``."""
        src = os.path.join(staging, "pi_agent")
        os.makedirs(src, exist_ok=True)
        for name, body in files.items():
            with open(os.path.join(src, name), "w") as f:
                f.write(body)
        tarball = os.path.join(staging, "edubotics-pi-agent.tar.gz")
        with tarfile.open(tarball, "w:gz") as tf:
            tf.add(src, arcname="pi_agent")
        return tarball

    def _install(self, root, files):
        pkg = os.path.join(root, "pi_agent")
        os.makedirs(pkg, exist_ok=True)
        for name, body in files.items():
            with open(os.path.join(pkg, name), "w") as f:
                f.write(body)
        return pkg

    @staticmethod
    def _fake_rsync(argv, *a, **kw):
        """Emulate the ONE rsync invocation the apply makes: a plain recursive
        copy of ``src/`` into an empty ``dst/``.

        The M2 fix is the STAGING + RENAME SWAP (rmtree → os.replace), not rsync
        — so its tests must not be skipped on a host without rsync, which is
        every Windows dev box and exactly where these are most likely to be run
        by hand. Emulating the copy keeps the module's "deps-free, runs
        anywhere" promise while exercising the real swap code. The
        rsync-semantics test above still uses the real binary on Linux/CI.
        """
        if not argv or argv[0] != "rsync":
            raise AssertionError(f"unexpected subprocess in apply: {argv!r}")
        src, dst = argv[-2].rstrip("/"), argv[-1].rstrip("/")
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def _apply(self, root, tarball, run=None):
        app = agent.AgentApp(compose_file=os.path.join(root, "docker-compose.opi.yml"))
        app._log = lambda _m: None
        with patch("pi_agent.agent.os.kill"), \
             patch.object(agent.subprocess, "run", side_effect=run or self._fake_rsync):
            result = app._apply_agent_update_and_restart(tarball)
            for t in threading.enumerate():
                if t.name == "agent-restart":
                    t.join(timeout=5)
        return result

    def test_staged_tree_is_fsynced_before_the_rename(self):
        """The swap must be durable, not just non-mixing.

        `rename` is atomic for concurrent READERS — that is an ordering property,
        not a durability one. ext4's auto_da_alloc force-allocates delayed blocks
        when a file is renamed OVER an existing file; it does NOT cover renaming a
        DIRECTORY of freshly-written files, and rsync does not fsync. So a
        classroom power yank on eMMC could commit the new pi_agent/ directory
        entry while its contents were still unallocated → zero-length modules →
        ImportError under systemd Restart=always → a crash-loop with no self-heal
        (there is no ExecStartPre recovery). Calling "atomic" a swap that can
        surface empty files is the overclaim this pins shut.

        Ordering is the assertion: an fsync AFTER the rename would prove nothing.
        """
        root = tempfile.mkdtemp()
        staging = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, staging, True)
        self._install(root, {"VERSION": "2.0.0\n", "agent.py": "# old\n"})
        tarball = self._tarball_of(staging, {"VERSION": "2.1.0\n", "agent.py": "# new\n"})
        pkg = os.path.join(root, "pi_agent")

        events = []
        real_replace = agent.os.replace

        def spy_fsync(fd):
            try:
                events.append(("fsync", os.fstat(fd).st_size))
            except OSError:
                events.append(("fsync", None))

        def spy_replace(a, b):
            events.append(("replace", a, b))
            return real_replace(a, b)

        app = agent.AgentApp(compose_file=os.path.join(root, "docker-compose.opi.yml"))
        app._log = lambda _m: None
        with patch("pi_agent.agent.os.kill"), \
             patch.object(agent.subprocess, "run", side_effect=self._fake_rsync), \
             patch.object(agent.os, "fsync", spy_fsync), \
             patch.object(agent.os, "replace", spy_replace):
            self.assertTrue(app._apply_agent_update_and_restart(tarball))
            for t in threading.enumerate():
                if t.name == "agent-restart":
                    t.join(timeout=5)

        kinds = [e[0] for e in events]
        self.assertIn("fsync", kinds, "the staged tree was never flushed to disk")
        self.assertIn("replace", kinds, "no rename happened — the test is vacuous")
        self.assertLess(
            kinds.index("fsync"), kinds.index("replace"),
            "the staged data must be fsynced BEFORE the rename swap, otherwise the "
            "rename can be committed ahead of the data it names")
        # And the fsyncs must cover real staged CONTENT, not just empty dirs —
        # the zero-length-module failure is exactly what this prevents.
        self.assertTrue(any(e[0] == "fsync" and e[1] for e in events),
                        "only empty paths were fsynced; the staged files were not")
        with open(os.path.join(pkg, "agent.py")) as f:
            self.assertEqual(f.read(), "# new\n")

    def test_swap_leaves_no_staging_dir_and_keeps_one_rollback(self):
        """After a successful apply: pi_agent/ is the NEW tree, pi_agent.new is
        gone (consumed by the rename), and pi_agent.old holds the previous tree
        for ONE cycle as a manual rollback."""
        root = tempfile.mkdtemp()
        staging = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, staging, True)
        self._install(root, {"VERSION": "2.0.0\n", "agent.py": "# old\n"})
        tarball = self._tarball_of(staging, {"VERSION": "2.1.0\n", "agent.py": "# new\n"})

        self.assertTrue(self._apply(root, tarball))

        pkg = os.path.join(root, "pi_agent")
        with open(os.path.join(pkg, "agent.py")) as f:
            self.assertEqual(f.read(), "# new\n")
        # The staging dir must be consumed, not left as debris that the next
        # apply's rmtree has to clean (and that a confused operator might read).
        self.assertFalse(os.path.exists(pkg + ".new"))
        # ...and the previous tree survives one cycle as a rollback.
        with open(os.path.join(pkg + ".old", "agent.py")) as f:
            self.assertEqual(f.read(), "# old\n")

    def test_a_prior_interrupted_apply_does_not_poison_the_next_one(self):
        """A power cut mid-rsync leaves a partial pi_agent.new. The next apply
        must build a CLEAN tree, not rsync on top of the debris (which could
        resurrect a file the new release deleted — the exact class of bug the
        swap exists to prevent)."""
        root = tempfile.mkdtemp()
        staging = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, staging, True)
        self._install(root, {"VERSION": "2.0.0\n"})
        # Debris from an interrupted previous attempt, incl. a stale module.
        debris = os.path.join(root, "pi_agent.new")
        os.makedirs(debris)
        with open(os.path.join(debris, "half_written.py"), "w") as f:
            f.write("# torn\n")
        tarball = self._tarball_of(staging, {"VERSION": "2.1.0\n", "agent.py": "# new\n"})

        self.assertTrue(self._apply(root, tarball))

        pkg = os.path.join(root, "pi_agent")
        self.assertFalse(os.path.exists(os.path.join(pkg, "half_written.py")))
        with open(os.path.join(pkg, "VERSION")) as f:
            self.assertEqual(f.read().strip(), "2.1.0")

    def test_a_failed_rsync_leaves_the_live_tree_untouched(self):
        """The live tree is the thing systemd restarts into. A failed apply must
        leave it COMPLETE and unmodified — staging into a sibling is what makes
        that structurally true (an in-place --delete could already have deleted).
        """
        root = tempfile.mkdtemp()
        staging = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, staging, True)
        self._install(root, {"VERSION": "2.0.0\n", "agent.py": "# old\n",
                             "constants.py": "# old consts\n"})
        tarball = self._tarball_of(staging, {"VERSION": "2.1.0\n"})

        def fail_rsync(argv, *a, **kw):
            # ENOSPC is the realistic trigger: a full eMMC is exactly what H1's
            # missing prune produces, and it is what makes H2's honest failure
            # reporting matter (see test_failed_apply_reports_failed_...).
            return type("P", (), {"returncode": 23, "stdout": "",
                                  "stderr": "rsync: write failed: No space left"})()

        self.assertFalse(self._apply(root, tarball, run=fail_rsync))

        pkg = os.path.join(root, "pi_agent")
        # Every file still there, still OLD — a complete, importable tree.
        with open(os.path.join(pkg, "agent.py")) as f:
            self.assertEqual(f.read(), "# old\n")
        with open(os.path.join(pkg, "constants.py")) as f:
            self.assertEqual(f.read(), "# old consts\n")
        with open(os.path.join(pkg, "VERSION")) as f:
            self.assertEqual(f.read().strip(), "2.0.0")
        # ...and no half-built sibling left behind.
        self.assertFalse(os.path.exists(pkg + ".new"))

    def test_a_failed_swap_restores_the_live_tree(self):
        """If the live tree was moved aside but the staged tree failed to land,
        the agent would have NO package to restart into — systemd would
        crash-loop it with no self-heal and no remote recovery path. The
        recovery must put the old tree back."""
        root = tempfile.mkdtemp()
        staging = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, staging, True)
        self._install(root, {"VERSION": "2.0.0\n", "agent.py": "# old\n"})
        tarball = self._tarball_of(staging, {"VERSION": "2.1.0\n", "agent.py": "# new\n"})

        real_replace = agent.os.replace
        calls = {"n": 0}

        def fail_second_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:  # the pi_agent.new → pi_agent move
                raise OSError("EXDEV: cross-device link")
            return real_replace(src, dst)

        with patch.object(agent.os, "replace", side_effect=fail_second_replace):
            self.assertFalse(self._apply(root, tarball))

        pkg = os.path.join(root, "pi_agent")
        self.assertTrue(os.path.isdir(pkg))
        with open(os.path.join(pkg, "agent.py")) as f:
            self.assertEqual(f.read(), "# old\n")  # rolled back, still complete

    def test_missing_pi_agent_dir_in_archive_is_noop(self):
        root = tempfile.mkdtemp()
        staging = tempfile.mkdtemp()
        try:
            # Tarball WITHOUT a pi_agent/ top-level dir → refuse, no restart.
            src = os.path.join(staging, "not_pi_agent")
            os.makedirs(src)
            with open(os.path.join(src, "x.txt"), "w") as f:
                f.write("nope\n")
            tarball = os.path.join(staging, "bad.tar.gz")
            with tarfile.open(tarball, "w:gz") as tf:
                tf.add(src, arcname="not_pi_agent")
            app = agent.AgentApp(compose_file=os.path.join(root, "docker-compose.opi.yml"))
            with patch("pi_agent.agent.os.kill") as kill:
                result = app._apply_agent_update_and_restart(tarball)
            self.assertFalse(result)       # no restart scheduled → guard released
            self.assertFalse(kill.called)  # no restart triggered
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)

    # ── pre-PEP-706 extractall fallback (manual member validation) ────────────

    @staticmethod
    def _tarball_with_members(directory, members):
        """Build a .tar.gz whose members carry LITERAL names (TarInfo direct —
        tarfile.add would normalise a traversal arcname)."""
        import io
        tarball = os.path.join(directory, "crafted.tar.gz")
        with tarfile.open(tarball, "w:gz") as tf:
            for name, data in members:
                ti = tarfile.TarInfo(name=name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        return tarball

    @staticmethod
    def _no_filter_extractall():
        """An extractall stand-in mimicking a pre-PEP-706 Python: raises
        TypeError on the `filter=` keyword, otherwise delegates."""
        orig = tarfile.TarFile.extractall

        def fake(tf_self, *args, **kwargs):
            if "filter" in kwargs:
                raise TypeError(
                    "extractall() got an unexpected keyword argument 'filter'")
            return orig(tf_self, *args, **kwargs)
        return fake

    def test_pre_pep706_fallback_refuses_traversal_and_absolute_members(self):
        # Force the TypeError fallback and prove the manual validation refuses
        # a `../` traversal entry and an absolute-path entry instead of
        # extracting them (filter='data' parity for old Pythons).
        root = tempfile.mkdtemp()
        workdir = tempfile.mkdtemp()
        try:
            app = agent.AgentApp(compose_file=os.path.join(root, "docker-compose.opi.yml"))
            for bad in ("../evil.txt", "/abs-evil.txt", "pi_agent/../../evil.txt"):
                tarball = self._tarball_with_members(
                    workdir, [("pi_agent/agent.py", b"# ok\n"), (bad, b"evil\n")])
                with patch.object(tarfile.TarFile, "extractall",
                                  self._no_filter_extractall()), \
                     patch("pi_agent.agent.os.kill") as kill:
                    result = app._apply_agent_update_and_restart(tarball)
                self.assertFalse(result, bad)  # refused, no restart scheduled
                self.assertFalse(kill.called)
                # Nothing was applied to the install root.
                self.assertFalse(os.path.exists(os.path.join(root, "pi_agent")), bad)
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(workdir, ignore_errors=True)

    def test_pre_pep706_fallback_extracts_valid_tarball(self):
        # The fallback must still WORK for the released (benign) tarball.
        if shutil.which("rsync") is None:
            self.skipTest("rsync unavailable")
        root = tempfile.mkdtemp()
        workdir = tempfile.mkdtemp()
        try:
            src = os.path.join(workdir, "pi_agent")
            os.makedirs(src)
            with open(os.path.join(src, "agent.py"), "w") as f:
                f.write("# new agent\n")
            tarball = os.path.join(workdir, "edubotics-pi-agent.tar.gz")
            with tarfile.open(tarball, "w:gz") as tf:
                tf.add(src, arcname="pi_agent")
            app = agent.AgentApp(compose_file=os.path.join(root, "docker-compose.opi.yml"))
            with patch.object(tarfile.TarFile, "extractall",
                              self._no_filter_extractall()), \
                 patch("pi_agent.agent.os.kill"):
                result = app._apply_agent_update_and_restart(tarball)
                for t in threading.enumerate():
                    if t.name == "agent-restart":
                        t.join(timeout=5)
            self.assertTrue(result)
            self.assertTrue(os.path.exists(os.path.join(root, "pi_agent", "agent.py")))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(workdir, ignore_errors=True)

    def test_validate_tar_member_rules(self):
        # The pure validator: PEP-706 'data'-filter parity.
        ok_file = tarfile.TarInfo("pi_agent/agent.py")
        ok_dir = tarfile.TarInfo("pi_agent")
        ok_dir.type = tarfile.DIRTYPE
        agent._validate_tar_member(ok_file)  # no raise
        agent._validate_tar_member(ok_dir)   # no raise
        for bad_name in ("/etc/passwd", "../up.txt", "a/../../b", "\\evil"):
            ti = tarfile.TarInfo(bad_name)
            with self.assertRaises(ValueError, msg=bad_name):
                agent._validate_tar_member(ti)
        link = tarfile.TarInfo("pi_agent/link")
        link.type = tarfile.SYMTYPE
        with self.assertRaises(ValueError):
            agent._validate_tar_member(link)


# ── MJPEG preview generation handshake (supersede without a missable window) ─


class _FakeStreamHandler:
    """Minimal stand-in for BaseHTTPRequestHandler as stream_camera_preview
    uses it: send_response/send_header/end_headers + a wfile sink."""

    def __init__(self):
        import io
        self.wfile = io.BytesIO()
        self.status = None
        self.header_list = []

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.header_list.append((key, value))

    def end_headers(self):
        pass


class TestPreviewHandshake(unittest.TestCase):
    def setUp(self):
        self.app = agent.AgentApp()

    def test_stop_bumps_generation(self):
        g0 = self.app._preview_generation()
        self.app.stop_active_previews()
        self.assertEqual(self.app._preview_generation(), g0 + 1)

    def test_generation_bump_supersedes_running_loop(self):
        # The old Event set→sleep(0.1)→clear stop-window could be MISSED by a
        # loop blocked in a frame read. The generation counter cannot: bump it
        # and the loop provably exits at its next check, releasing the capture
        # and deregistering itself.
        handler = _FakeStreamHandler()
        caps = []

        class _FakeCap:
            def __init__(self, device):
                self.device = device
                self.released = False
                caps.append(self)

            def open(self):
                return True

            def read_jpeg(self):
                time.sleep(0.01)
                return b"\xff\xd8jpeg"

            def release(self):
                self.released = True

        with patch.object(agent, "_PreviewCapture", _FakeCap):
            t = threading.Thread(target=self.app.stream_camera_preview,
                                 args=(handler, "/dev/video0"), daemon=True)
            t.start()
            self.assertTrue(_wait_for(lambda: self.app._preview_active),
                            "loop never registered")
            self.app.stop_active_previews()
            t.join(timeout=3)
            self.assertFalse(t.is_alive(), "superseded loop did not exit")
        self.assertTrue(caps[0].released)          # capture provably released
        self.assertEqual(self.app._preview_active, {})  # deregistered

    def test_new_preview_waits_for_old_release_then_opens(self):
        # Handshake: the new preview blocks until the old loop deregisters
        # (simulated by a helper thread), then opens the device and streams.
        with self.app._preview_cond:
            self.app._preview_active["/dev/video0"] = self.app._preview_gen

        def release_soon():
            time.sleep(0.1)
            with self.app._preview_cond:
                self.app._preview_active.clear()
                self.app._preview_cond.notify_all()

        class _FakeCap:
            def __init__(self, device):
                pass

            def open(self):
                return True

            def read_jpeg(self):
                return None  # end the stream right after the headers

            def release(self):
                pass

        handler = _FakeStreamHandler()
        threading.Thread(target=release_soon, daemon=True).start()
        with patch.object(agent, "_PreviewCapture", _FakeCap):
            self.app.stream_camera_preview(handler, "/dev/video0")
        self.assertEqual(handler.status, 200)          # streamed
        self.assertEqual(self.app._preview_active, {})  # deregistered on exit

    def test_new_preview_503s_if_old_loop_never_releases(self):
        # A stuck holder must NOT be raced for the device: the new preview
        # gives up with a German 503 and never opens a second capture.
        with self.app._preview_cond:
            self.app._preview_active["/dev/video0"] = self.app._preview_gen
        handler = _FakeStreamHandler()
        with patch.object(agent, "_PREVIEW_HANDOFF_TIMEOUT_S", 0.05), \
             patch.object(agent, "_PreviewCapture") as cap_cls:
            self.app.stream_camera_preview(handler, "/dev/video0")
        self.assertEqual(handler.status, 503)
        self.assertIn("Vorschau", handler.wfile.getvalue().decode("utf-8"))
        cap_cls.assert_not_called()  # the held device was never re-opened


# ── _tls_genuine against a real self-signed TLS server (#23) ─────────────────


class TestTlsGenuine(unittest.TestCase):
    def test_self_signed_flagged_not_genuine(self):
        import ssl as _ssl
        d = tempfile.mkdtemp()
        listener = None
        try:
            try:
                cert, key = phone_camera.ensure_cert(d)
            except phone_camera.PhoneCertError as e:
                self.skipTest(f"openssl unavailable: {e}")
            ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            def serve():
                try:
                    conn, _ = listener.accept()
                    try:
                        s = ctx.wrap_socket(conn, server_side=True)
                        s.recv(16)
                        s.close()
                    except Exception:  # noqa: BLE001 — client aborts on verify fail
                        conn.close()
                except Exception:  # noqa: BLE001
                    pass

            t = threading.Thread(target=serve, daemon=True)
            t.start()
            # A self-signed cert is NOT in the system CA store → the detection
            # must flag it as not-genuine (the TLS-inspection signal), proving it
            # is real verification and not stubbed.
            ok, _detail = agent.AgentApp._tls_genuine("127.0.0.1", port)
            t.join(timeout=3)
            self.assertFalse(ok)
        finally:
            if listener is not None:
                listener.close()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
