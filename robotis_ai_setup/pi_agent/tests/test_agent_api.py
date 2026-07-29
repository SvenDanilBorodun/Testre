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
import subprocess
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

    def test_a_foreign_origin_cannot_force_a_cache_bypass(self):
        """MINOR 8, cheap layer. A no-cors GET carries no Origin, so an ABSENT
        header proves nothing and stays allowed (the header-less curl path). But
        a PRESENT, foreign Origin is a cross-site caller with no business
        bypassing the poll-storm cache: it silently gets the cached answer."""
        upd = {"version": "9.9.9", "download_url": "u", "sha256": ""}
        with patch.object(agent.update_checker, "check_for_agent_update",
                          return_value=upd) as chk:
            self._get("/update/check")  # populate the cache (allowed, no Origin)
            req = urllib.request.Request(self._url("/update/check?force=1"))
            req.add_header("Origin", "http://evil.com")
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
                # Still answered — only the refresh is suppressed.
                self.assertTrue(json.loads(r.read().decode())["update_available"])
        self.assertEqual(chk.call_count, 1)


# ── M6: bounded _lifecycle_lock acquire on mutating endpoints ────────────────


class TestLifecycleLockIsBounded(_ServerBase):
    """„Umgebung starten" holds ``_lifecycle_lock`` across a resilient multi-GB
    image pull on the HTTP request thread. Every OTHER mutating endpoint used to
    take the same lock with a blocking ``with``, and nginx's proxy_read_timeout
    is 3600 s — so no 504 rescued them either. The whole wizard simply stopped
    answering for as long as the pull ran, with only the SSE Protokoll alive.
    """

    def _hold_lock(self):
        """Hold _lifecycle_lock for the duration of the test, like a running
        „Umgebung starten" would. The wait is shortened so the suite does not pay
        the real 1 s per assertion; the default itself is pinned separately."""
        p = patch.object(agent, "_LIFECYCLE_LOCK_WAIT_S", 0.05)
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(self.app._lifecycle_lock.acquire(timeout=1))
        self.addCleanup(self.app._lifecycle_lock.release)

    def test_the_wait_is_short_enough_that_a_student_sees_an_answer(self):
        # Bounded AND small: this is the ceiling on how long every wizard control
        # can appear frozen while another operation holds the lock.
        self.assertLessEqual(agent._LIFECYCLE_LOCK_WAIT_S, 5.0)
        self.assertGreater(agent._LIFECYCLE_LOCK_WAIT_S, 0)

    def test_the_wait_outlasts_a_legitimate_preview_handoff(self):
        """stream_camera_preview holds _lifecycle_lock across its
        `_preview_cond.wait(...)` while a superseded preview loop releases its
        VideoCapture — up to _PREVIEW_HANDOFF_TIMEOUT_S. A shorter wait here
        would turn that ordinary handoff into a spurious 503 on „Stoppen", so
        the relationship between the two constants is load-bearing.

        (The lock ORDER is unaffected and stays one-directional: a handler takes
        _lifecycle_lock while holding nothing, then _preview_lock; the preview
        takes _preview_lock first and only ever tries _lifecycle_lock
        NON-blockingly — which is what makes the inversion deadlock-free.)"""
        self.assertGreater(agent._LIFECYCLE_LOCK_WAIT_S,
                           agent._PREVIEW_HANDOFF_TIMEOUT_S)

    def test_stop_answers_503_instead_of_blocking_behind_a_start(self):
        # THE case: „Stoppen" is what a student presses when a start is taking
        # too long. It must never wait behind the operation it interrupts.
        self._hold_lock()
        with patch.object(agent.docker_manager, "stop_robot_tier",
                          return_value=True) as stop:
            started = time.monotonic()
            code, payload = self._post("/environment/stop", origin=None)
        self.assertEqual(code, 503)
        self.assertLess(time.monotonic() - started, 4.0)
        stop.assert_not_called()
        self.assertIn("bitte", payload["message"].lower())

    def test_every_mutating_lifecycle_endpoint_fast_fails(self):
        self._hold_lock()
        for path, body in (
            ("/environment/stop", {}),
            ("/environment/start", {}),
            ("/scan-arms", {}),
            ("/cameras/roles", {"cameras": [{"path": "/dev/video0", "role": "scene"}]}),
            ("/hf-token", {"token": "hf_x"}),
            ("/robot-type", {"robot_type": "omx_full"}),
            ("/factory-reset", {"confirm": True, "confirm_again": True}),
        ):
            with self.subTest(path=path):
                code, payload = self._post(path, body, origin=None)
                self.assertEqual(code, 503, payload)
                self.assertFalse(payload["ok"])

    def test_the_lock_is_released_again_on_every_path(self):
        # try/finally, not `with` — a handler that returns early (or raises)
        # inside the guarded block must not strand the lock and wedge the wizard.
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True):
            self.assertEqual(self._post("/environment/stop", origin=None)[0], 200)
        # An early return from INSIDE the guarded region (no arms scanned).
        self.assertEqual(self._post("/environment/start", origin=None)[0], 400)
        # A raise from inside it.
        with patch.object(agent.config_generator, "upsert_env_var",
                          side_effect=RuntimeError("disk full")):
            self.assertEqual(self._post("/hf-token", {"token": "x"}, origin=None)[0], 500)
        self.assertTrue(self.app._lifecycle_lock.acquire(timeout=1))
        self.app._lifecycle_lock.release()


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
        # boot() now RENEWS drifted systemd units (M9) — a root write to
        # /etc/systemd/system + `systemctl daemon-reload`. Point the unit dir
        # into the sandbox so `_drifted_units` can never see (and this class can
        # never overwrite) a real unit if the suite is ever run on a provisioned
        # Pi. Same reasoning as the prune_superseded_tags note in
        # test_docker_manager: mocked everywhere it is harmless, destructive
        # exactly where it is not.
        p = patch.object(agent, "SYSTEMD_UNIT_DIR", os.path.join(self.dir, "no-units"))
        p.start()
        self.addCleanup(p.stop)
        # boot() also RENEWS a drifted opi compose (WP-0), which is a root write
        # to constants.COMPOSE_FILE (/opt/edubotics/docker-compose.opi.yml).
        # Same reasoning as the unit dir above: point it into the sandbox so this
        # class can never overwrite — nor make its verdict depend on — a REAL
        # provisioned compose if the suite is ever run on a Pi. The sandbox path
        # does not exist, so `_compose_drifted` skips (absent proves nothing).
        pc = patch.object(agent, "COMPOSE_FILE",
                          os.path.join(self.dir, "no-compose", "docker-compose.opi.yml"))
        pc.start()
        self.addCleanup(pc.stop)
        # …and the udev rules, for the third time and the same reason: a root
        # write to /etc/udev/rules.d + `udevadm control --reload-rules`. The
        # sandbox dir does not exist, so `_drifted_udev_rules` skips.
        pu = patch.object(agent, "UDEV_RULES_DIR", os.path.join(self.dir, "no-udev"))
        pu.start()
        self.addCleanup(pu.stop)

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

    # ── boot-path robustness (a crash here is a 5 s systemd restart loop) ─────

    def test_a_taken_loopback_port_never_crash_loops_the_unit(self):
        """`ThreadingHTTPServer(...)` raising OSError used to escape boot() and
        exit the process — which under Restart=always + RestartSec=5 is a 5 s
        crash-loop whose most likely cause (a stale agent still holding :8769)
        keeps it going. The loopback listener is also the less load-bearing of
        the two: the browser reaches the wizard through the ros_net GATEWAY
        listener, which retries forever on its own."""
        self._write('IMAGE_TAG="9.9.9"\n')
        logs = []
        self.app._log = logs.append
        with patch.object(agent, "IMAGE_TAG", "9.9.9"), \
             patch.object(agent, "ThreadingHTTPServer",
                          side_effect=OSError("[Errno 98] Address already in use")), \
             patch.object(agent.docker_manager, "start_manager",
                          return_value=True) as mgr, \
             patch.object(agent.threading, "Thread"):
            self.app.boot()  # must not raise
        text = "\n".join(logs)
        self.assertIn("[WARNUNG]", text)
        self.assertIn("8769", text)
        self.assertIsNone(self.app._loopback_httpd)
        mgr.assert_called_once()   # boot carried on
        self.assertTrue(self.app._ready.is_set())

    def test_a_failed_env_seed_is_reported_loudly_and_twice(self):
        """Without a .env, `_compose` omits --env-file and compose interpolates
        every ${REGISTRY}/${IMAGE_TAG} to the empty string — the manager `up`
        then dies on an unpullable ref like `/physical-ai-manager-opi:`, which
        reads as a registry fault. One unprefixed line 30 lines up in the
        Protokoll was the only symptom. Say [FEHLER], name the file, and repeat
        it next to the `up` it dooms."""
        self.assertFalse(os.path.exists(self.env))  # a freshly flashed Pi
        logs = []
        self.app._log = logs.append
        with patch.object(agent.config_generator, "generate_cloud_only_env",
                          side_effect=OSError("read-only file system")), \
             patch.object(agent.docker_manager, "start_manager", return_value=True), \
             patch.object(self.app, "start_loopback"), \
             patch.object(agent.threading, "Thread"):
            self.app.boot()  # must not raise
        text = "\n".join(logs)
        self.assertEqual(text.count("[FEHLER]"), 2, text)
        self.assertIn(self.env, text)
        # And it must not send the operator to the registry.
        self.assertIn("KEIN Registry", text)


# ── WP-0: boot() is the ONLY caller of the system-file repair ────────────────


class TestBootRunsTheSystemFileRepair(unittest.TestCase):
    """`boot()` calling `_check_system_files_version()` IS the delivery path.

    Every other test in this file exercises the repair by calling it directly,
    so the one line that ever runs it in the field was unguarded: MEASURED
    2026-07-28, deleting the call from `boot()` left the whole pi_agent suite
    green (387 passed), and so did moving it below `start_manager`. Without that
    line the compose twin, the systemd units and the udev rules all ship
    correctly and are never compared to anything — the fleet stays on whatever
    `setup.sh` laid down, silently, which is precisely the fail-open WP-0 closed.

    The ORDERING is load-bearing too, and equally unguarded. `start_manager` is
    `up -d --force-recreate --no-deps physical_ai_manager`, so a compose renewed
    BEFORE it is adopted by the always-on manager tier in the same boot — which
    is exactly what the German Protokoll line promises the student („Die
    Weboberfläche wird noch in diesem Startvorgang darauf umgestellt"). Renewed
    after, the manager would come up on the OLD file and the promise would be
    false until the next reboot.

    `TestBootRealignsImageTag` cannot cover either property: it sandboxes all
    three repair destinations to directories that do not exist, so the repair is
    a guaranteed no-op there whether it runs or not.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = os.path.join(self.dir, ".env")
        with open(self.env, "w", encoding="utf-8") as f:
            f.write(f'IMAGE_TAG="{agent.IMAGE_TAG}"\n')  # matching = no pull
        self.app = agent.AgentApp()
        self.app.env_file = self.env
        self.app._log = lambda _m: None

    def _boot_recording_order(self):
        """Boot with the repair and the manager start replaced by recorders.

        The repair is stubbed rather than sandboxed on purpose: this class asks
        WHETHER and WHEN boot() calls it, never what it does — and stubbing it
        also keeps a suite run on a provisioned Pi from touching /etc.
        """
        order = []
        with patch.object(self.app, "_check_system_files_version",
                          side_effect=lambda: order.append("repair")), \
             patch.object(agent.docker_manager, "start_manager",
                          side_effect=lambda **kw: order.append("manager") or True), \
             patch.object(self.app, "_pull_images_after_self_update",
                          side_effect=lambda: order.append("pull")), \
             patch.object(self.app, "start_loopback"), \
             patch.object(agent.threading, "Thread"):
            self.app.boot()
        return order

    def test_boot_runs_the_system_file_repair_at_all(self):
        order = self._boot_recording_order()
        self.assertIn(
            "repair", order,
            "boot() never called _check_system_files_version — the compose "
            "twin, the systemd units and the udev rules would ship to every Pi "
            "and never be compared to anything",
        )

    def test_the_repair_runs_BEFORE_the_manager_is_started(self):
        order = self._boot_recording_order()
        self.assertLess(
            order.index("repair"), order.index("manager"),
            "a compose renewed after start_manager is not adopted until the "
            "NEXT boot, so the Protokoll's „noch in diesem Startvorgang\" "
            "would be false",
        )
        # Nothing was pulled (the .env pins the running tag), so the whole
        # observable boot is exactly these two steps in exactly this order.
        self.assertEqual(order, ["repair", "manager"])


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
        # The INSTALLED opi compose (/opt/edubotics/docker-compose.opi.yml on a
        # real Pi). Sandboxed for the whole class — without this the compose leg
        # would read the host's real path, so the suite would be green on CI
        # (where it is absent) and red on a dev Pi (where it is not).
        self.installed_compose_dir = os.path.join(self.dir, "opt-edubotics")
        os.makedirs(self.installed_compose_dir)
        self.installed_compose = os.path.join(self.installed_compose_dir,
                                              "docker-compose.opi.yml")
        pc = patch.object(agent, "COMPOSE_FILE", self.installed_compose)
        pc.start()
        self.addCleanup(pc.stop)
        # The INSTALLED udev rules (/etc/udev/rules.d on a real Pi), sandboxed
        # for the same reason: on a dev Pi the real rule exists and the class's
        # verdicts would depend on whether it happened to match.
        self.installed_udev = os.path.join(self.dir, "udev-rules.d")
        os.makedirs(self.installed_udev)
        pu = patch.object(agent, "UDEV_RULES_DIR", self.installed_udev)
        pu.start()
        self.addCleanup(pu.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp()
        self.logs = []
        self.app._log = self.logs.append
        # The units this agent version ships (the real files in pi_agent/systemd/).
        self.shipped_dir = os.path.join(os.path.dirname(os.path.abspath(agent.__file__)),
                                        "systemd")
        # …and the compose twin it ships (the real pi_agent/docker/ copy).
        self.shipped_compose = self.app._shipped_compose_path()
        # …and the udev rules it ships (the real pi_agent/udev/ copies).
        self.shipped_udev_dir = self.app._shipped_dir("udev")

    def _install_udev(self, drift=False):
        """Lay down the /etc/udev/rules.d copies of the shipped rules.

        `drift=True` models the fail-open the udev leg closes: setup.sh
        installed the rule of an OLDER release (one VID/PID line short) and no
        self-update since has ever touched /etc/udev/rules.d."""
        for rule in agent.UDEV_RULE_NAMES:
            with open(os.path.join(self.shipped_udev_dir, rule), "rb") as f:
                data = f.read()
            if drift:
                data += b'\n# provisioned by an older release (no CH343P line)\n'
            with open(os.path.join(self.installed_udev, rule), "wb") as f:
                f.write(data)

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

    def _install_compose(self, drift=False):
        """Lay down the /opt/edubotics copy of the shipped opi compose.

        `drift=True` models the WP-0 failure the twin exists to close: setup.sh
        installed the compose of an OLDER release and every self-update since
        rsync'd `pi_agent/` only, so the container orchestration never moved."""
        with open(self.shipped_compose, "rb") as f:
            data = f.read()
        if drift:
            data += b"\n# provisioned by an older release\n"
        with open(self.installed_compose, "wb") as f:
            f.write(data)

    def _check(self, stamp_contents=None, app_version="2.14.0", renew=True,
               compose_valid=True):
        """Run the drift check. ``renew=False`` models a Pi where the agent could
        NOT repair the units itself (read-only /etc, a hand-mounted unit dir) —
        the only state in which the advisory banner still exists.

        ``subprocess.run`` stands in for BOTH `systemctl daemon-reload` and the
        `docker compose config -q` gate, so it must answer with a real
        CompletedProcess: a bare MagicMock's `.returncode` is a MagicMock, which
        compares unequal to 0 and would silently make every compose repair look
        refused. ``compose_valid=False`` is how a test asks for the refusal."""
        if stamp_contents is not None:
            with open(self.stamp, "w", encoding="utf-8") as f:
                f.write(stamp_contents)

        def _run(argv, *_a, **_kw):
            if "config" in argv:  # the docker compose validate gate
                return subprocess.CompletedProcess(
                    argv, 0 if compose_valid else 1, "",
                    "" if compose_valid else "yaml: did not find expected node content")
            return subprocess.CompletedProcess(argv, 0, "", "")

        stack = [
            patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp),
            patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed),
            patch.object(agent, "APP_VERSION", app_version),
            # Never let the real `systemctl daemon-reload` / `docker compose`
            # reach the test host.
            patch.object(agent.subprocess, "run", MagicMock(side_effect=_run)),
        ]
        if not renew:
            # `(repaired, failed)`, not a bool: the renewal reports per FILE so
            # a partial outcome can name the two halves separately. This double
            # is the total-failure end of that range — nothing repaired, every
            # drifted unit still drifted.
            stack.append(patch.object(
                self.app, "_renew_system_units",
                side_effect=lambda units: ([], list(units))))
        for p in stack:
            p.start()
        try:
            self.app._check_system_files_version()
        finally:
            for p in reversed(stack):
                p.stop()
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

    def test_drifted_units_are_repaired_in_place_and_no_banner_is_raised(self):
        """M9. Every fielded Pi was provisioned before this release, and this
        release CHANGED edubotics-pi.service (EnvironmentFile=-, ExecStartPre) —
        so the byte-compare drifts 100 % of the fleet the moment it self-updates.
        A pure banner would therefore be permanent, and its remedy
        („sudo ./setup.sh aus dem Quellordner") is one a classroom teacher cannot
        perform. The agent runs as root and already holds the verified bytes, so
        it installs them itself; only what it CANNOT repair reaches the banner.
        """
        self._install_units(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
        self.assertNotIn("[WARNUNG]", text)
        self.assertFalse(self.app._system_files_stale)
        self.assertIn("erneuert", text)          # says what it did
        self.assertIn("nächsten Start", text)    # and when it takes effect
        # The installed units are now byte-identical to the shipped ones, so a
        # re-check on the next boot is silent.
        with patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed):
            self.assertEqual(self.app._drifted_units(), [])

    def test_the_repair_writes_the_shipped_bytes_0644_and_keeps_one_backup(self):
        self._install_units(drift=True)
        self._check(stamp_contents="2.13.0\n")
        for unit in agent.SYSTEMD_UNIT_NAMES:
            dst = os.path.join(self.installed, unit)
            with open(os.path.join(self.shipped_dir, unit), "rb") as f:
                shipped = f.read()
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), shipped, unit)
            # setup.sh installs 0644; the unit runs with UMask=0077, so a fresh
            # file would be 0600 without the explicit chmod.
            self.assertEqual(os.stat(dst).st_mode & 0o777, 0o644, unit)
            # A hand-edited unit must be recoverable.
            self.assertTrue(os.path.isfile(dst + ".edubotics-bak"), unit)
            # No temp file left behind (systemd would ignore it, but still).
            self.assertFalse(os.path.exists(dst + ".edubotics-tmp"), unit)

    def test_the_repair_reloads_systemd_but_never_restarts_the_agent(self):
        """daemon-reload makes systemd re-read the files; the new ExecStartPre
        takes effect on the NEXT start. Restarting ourselves from inside boot()
        to adopt a unit change would risk a loop for no gain."""
        self._install_units(drift=True)
        run = MagicMock()
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", run):
            self.app._check_system_files_version()
        argvs = [c.args[0] for c in run.call_args_list if c.args]
        self.assertIn(["systemctl", "daemon-reload"], argvs)
        for argv in argvs:
            self.assertNotIn("restart", argv, argv)
            self.assertNotIn("enable", argv, argv)

    def test_a_failed_daemon_reload_still_counts_as_repaired(self):
        # The FILES are the durable half — systemd re-parses every unit at boot
        # anyway. Never re-raise a permanent banner over a reload hiccup.
        self._install_units(drift=True)
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run",
                          side_effect=FileNotFoundError("no systemctl")):
            self.app._check_system_files_version()
        self.assertFalse(self.app._system_files_stale)

    def test_the_repair_never_installs_a_unit_that_was_not_already_there(self):
        """`_drifted_units` skips a unit it cannot READ on both sides, so an
        absent installed unit is never reported and therefore never written.
        That is what keeps this out of `systemctl enable` territory: the agent
        only ever REPLACES a file setup.sh already put there."""
        text = self._check(stamp_contents="2.13.0\n")  # nothing installed
        self.assertEqual(text, "")
        for unit in agent.SYSTEMD_UNIT_NAMES:
            self.assertFalse(os.path.exists(os.path.join(self.installed, unit)), unit)

    def test_an_unwritable_unit_dir_degrades_to_the_banner_not_a_crash(self):
        """A read-only /etc (or a hand-mounted unit dir) is the one state where
        the advisory banner still earns its place. It must degrade to that, never
        raise out of boot() — and it must leave no half-written unit behind."""
        self._install_units(drift=True)
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", MagicMock()), \
             patch.object(agent.os, "replace",
                          side_effect=OSError("read-only file system")):
            self.app._check_system_files_version()  # must not raise
        self.assertTrue(self.app._system_files_stale)
        self.assertIn("[WARNUNG]", "\n".join(self.logs))
        for unit in agent.SYSTEMD_UNIT_NAMES:
            dst = os.path.join(self.installed, unit)
            self.assertFalse(os.path.exists(dst + ".edubotics-tmp"), unit)

    # ── partial repairs: attempt every file, report only what really failed ──

    def _two_unit_names(self):
        """The first two installed unit names, in the order the repair walks
        them. SKIP rather than fail if the constant ever carries fewer than two:
        with a single-entry set the short-circuit these tests are about is
        inert, so there would be nothing to assert (the shape itself stays
        pinned by `test_the_systemd_leg_has_the_same_no_short_circuit_shape`,
        which supplies its own names)."""
        if len(agent.SYSTEMD_UNIT_NAMES) < 2:
            self.skipTest("needs at least two shipped units to show ordering")
        return agent.SYSTEMD_UNIT_NAMES[0], agent.SYSTEMD_UNIT_NAMES[1]

    def _check_with_one_unit_failing(self, failing: str):
        """Run the drift check with all units drifted and ``os.replace`` raising
        EPERM for exactly ONE of them. Models the realistic field case (one
        immutable / hand-bind-mounted unit file), which is the only way the
        difference between "attempt every file" and "stop at the first failure"
        can show itself."""
        self._install_units(drift=True)
        real_replace = os.replace

        def _replace(src, dst):
            if os.path.basename(str(dst)) == failing:
                raise OSError(1, "Operation not permitted")
            return real_replace(src, dst)

        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent, "APP_VERSION", "2.14.0"), \
             patch.object(agent.subprocess, "run", MagicMock(
                 side_effect=lambda argv, *a, **kw:
                 subprocess.CompletedProcess(argv, 0, "", ""))), \
             patch.object(agent.os, "replace", _replace):
            self.app._check_system_files_version()
        return "\n".join(self.logs)

    def _unit_swapped(self, unit: str) -> bool:
        with open(os.path.join(self.shipped_dir, unit), "rb") as f:
            shipped = f.read()
        with open(os.path.join(self.installed, unit), "rb") as f:
            return f.read() == shipped

    def test_a_failure_on_the_FIRST_file_still_repairs_every_later_one(self):
        """THE short-circuit guard. `all(install(u) for u in drifted)` evaluates
        a GENERATOR, so it stopped at the first False and never attempted the
        rest.

        MEASURED against the pre-fix code with the FIRST unit's os.replace
        raising EPERM: `edubotics-pi-firstboot.service swapped=False` and no
        `.edubotics-bak` beside it — it was never even tried, on a Pi where it
        would have succeeded. The agent silently repaired less than it could,
        and the repair path is the only one a classroom has."""
        first, second = self._two_unit_names()
        self._check_with_one_unit_failing(first)
        self.assertFalse(self._unit_swapped(first), "the failing unit must be intact")
        self.assertTrue(self._unit_swapped(second),
                        "a later file must be attempted even after an earlier failure")
        # …and its backup proves it was actually attempted, not merely equal.
        self.assertTrue(os.path.isfile(
            os.path.join(self.installed, second) + ".edubotics-bak"))

    def test_the_banner_names_only_the_files_that_actually_failed(self):
        """The other half: one bool made the caller report the WHOLE drifted set
        as unrepaired, so a unit that HAD been swapped was still named to the
        operator as broken and got no success line — while the System-tab
        banner's „mit der zuletzt funktionierenden Konfiguration" was false for
        exactly that file (it was on the NEW one). MEASURED pre-fix: with the
        second unit failing, `edubotics-pi.service swapped=True` and the banner
        named both."""
        first, second = self._two_unit_names()
        text = self._check_with_one_unit_failing(second)
        self.assertTrue(self._unit_swapped(first))
        self.assertFalse(self._unit_swapped(second))
        banner = [ln for ln in self.logs if ln.startswith("[WARNUNG]")]
        self.assertEqual(len(banner), 1, self.logs)
        self.assertIn(second, banner[0])
        self.assertNotIn(first, banner[0])
        # …and the repaired one is REPORTED as repaired, naming only itself.
        renewed = [ln for ln in self.logs if "wurden automatisch erneuert" in ln]
        self.assertEqual(len(renewed), 1, self.logs)
        self.assertIn(first, renewed[0])
        self.assertNotIn(second, renewed[0])
        self.assertTrue(self.app._system_files_stale)

    def test_a_partial_repair_still_reloads_systemd(self):
        """systemd would otherwise keep serving the OLD definition of a unit
        whose file was just replaced. The reload is gated on „something actually
        changed", not on „everything succeeded"."""
        first, _second = self._two_unit_names()
        self._install_units(drift=True)
        argvs = []
        real_replace = os.replace

        def _replace(src, dst):
            if os.path.basename(str(dst)) == first:
                raise OSError(1, "Operation not permitted")
            return real_replace(src, dst)

        def _run(argv, *a, **kw):
            argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", _run), \
             patch.object(agent.os, "replace", _replace):
            self.app._check_system_files_version()
        self.assertIn(["systemctl", "daemon-reload"], argvs, argvs)

    def test_nothing_repaired_means_no_reload_is_attempted(self):
        """The converse, and what stops „reload whenever we tried" from creeping
        back in: with every install refused there is no new definition to make
        known, so the agent runs no subprocess at all on that leg."""
        self._install_units(drift=True)
        argvs = []

        def _run(argv, *a, **kw):
            argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", _run), \
             patch.object(agent.os, "replace",
                          side_effect=OSError("read-only file system")):
            self.app._check_system_files_version()
        self.assertNotIn(["systemctl", "daemon-reload"], argvs, argvs)
        self.assertTrue(self.app._system_files_stale)

    def test_the_udev_leg_has_the_same_no_short_circuit_shape(self):
        """UDEV_RULE_NAMES carries ONE entry today, so the short-circuit is
        currently inert there — which is exactly why it has to be pinned now.
        The set grows with the planned CH343P line for the edu6 arm, and a
        latent bug in the only repair path a classroom has is not worth keeping
        until it has something to bite. Driven with a synthetic two-name list so
        the guard does not depend on a second real rule existing yet."""
        seen = []

        def _install(rule):
            seen.append(rule)
            return rule != "a.rules"          # the FIRST one fails

        with patch.object(self.app, "_install_udev_rule", _install), \
             patch.object(agent.subprocess, "run", MagicMock(
                 return_value=subprocess.CompletedProcess([], 0, "", ""))):
            repaired, failed = self.app._renew_udev_rules(["a.rules", "b.rules"])
        self.assertEqual(seen, ["a.rules", "b.rules"], "b.rules was never attempted")
        self.assertEqual(repaired, ["b.rules"])
        self.assertEqual(failed, ["a.rules"])

    def test_the_systemd_leg_has_the_same_no_short_circuit_shape(self):
        """The twin of the udev guard, driven through the same synthetic list so
        both legs are pinned by shape rather than by however many names their
        constants happen to carry."""
        seen = []

        def _install(unit):
            seen.append(unit)
            return unit != "a.service"        # the FIRST one fails

        with patch.object(self.app, "_install_unit_file", _install), \
             patch.object(agent.subprocess, "run", MagicMock(
                 return_value=subprocess.CompletedProcess([], 0, "", ""))):
            repaired, failed = self.app._renew_system_units(["a.service", "b.service"])
        self.assertEqual(seen, ["a.service", "b.service"])
        self.assertEqual(repaired, ["b.service"])
        self.assertEqual(failed, ["a.service"])

    def test_every_unrepairable_file_is_preceded_by_its_german_reason(self):
        """The banner PROMISES „der Grund steht in den Zeilen oberhalb", so the
        promise has to be code, not prose. Every path that appends to
        `unrepaired` runs through `_install_system_file`, whose only two False
        returns each log a German reason first — the OSError branch (here) and
        the validate refusal (the compose test below). Assert the ORDER, not
        just the presence: a reason logged after the banner is a pointer to
        nothing."""
        self._install_units(drift=True)
        self._install_udev(drift=True)
        self._install_compose(drift=True)
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", MagicMock(
                 side_effect=lambda argv, *a, **kw:
                 subprocess.CompletedProcess(argv, 0, "", ""))), \
             patch.object(agent.os, "replace",
                          side_effect=OSError(30, "Read-only file system")):
            self.app._check_system_files_version()
        banner_at = [i for i, ln in enumerate(self.logs)
                     if ln.startswith("[WARNUNG]")]
        self.assertEqual(len(banner_at), 1, self.logs)
        for name in (list(agent.SYSTEMD_UNIT_NAMES) + list(agent.UDEV_RULE_NAMES)
                     + [os.path.basename(agent.COMPOSE_FILE)]):
            reasons = [i for i, ln in enumerate(self.logs)
                       if name in ln and "konnte nicht erneuert werden" in ln]
            self.assertTrue(reasons, f"no German reason logged for {name}: {self.logs}")
            self.assertLess(min(reasons), banner_at[0],
                            f"the reason for {name} must come BEFORE the banner")

    def test_a_refused_compose_swap_also_states_its_reason_before_the_banner(self):
        """The validate-gate half of the same promise: `_install_system_file`'s
        OTHER False return. A refusal is not an error, so it logs „wurde NICHT
        erneuert: …" rather than „konnte nicht erneuert werden" — and it too has
        to land above the banner that points at it."""
        self._install_compose(drift=True)
        self._check(stamp_contents="2.13.0\n", compose_valid=False)
        banner_at = [i for i, ln in enumerate(self.logs)
                     if ln.startswith("[WARNUNG]")]
        reason_at = [i for i, ln in enumerate(self.logs)
                     if "NICHT erneuert" in ln]
        self.assertEqual(len(banner_at), 1, self.logs)
        self.assertTrue(reason_at, self.logs)
        self.assertLess(min(reason_at), banner_at[0], self.logs)

    def test_drifted_units_warn_in_german_and_name_the_unit(self):
        self._install_units(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0", renew=False)
        self.assertIn("[WARNUNG]", text)
        self.assertIn("edubotics-pi.service", text)  # WHICH file drifted
        self.assertIn("2.13.0", text)  # where the on-disk files came from
        # The banner no longer names the AGENT version. That is deliberate: it
        # is the line the System-tab banner points at („den Grund nennt das
        # Protokoll"), so it has to answer „what went wrong and what now", not
        # restate a version the Protokoll already carries — `main()` logs
        # „EduBotics Pi-Agent starting (version=…)" through the same ring
        # handler on every boot, and the repaired-leg lines still name it.
        self.assertNotIn("2.14.0", text)
        # …and it says the two things the banner promises it says.
        self.assertIn("läuft mit den bisherigen, funktionierenden Dateien weiter", text)
        self.assertIn("nächsten Neustart", text)  # the retry, i.e. the remedy
        self.assertTrue(self.app._system_files_stale)
        self.assertEqual(self.app._system_files_version, "2.13.0")

    def test_the_remedy_is_never_destructive(self):
        """A drift warning must NEVER tell anyone to re-flash. The drift is
        usually benign, the students' datasets are irreplaceable and live on the
        same eMMC, and re-running setup.sh fixes the units while leaving the
        Docker volumes alone. This is the whole reason the banner was reworked."""
        self._install_units(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0", renew=False)
        for destructive in ("neu aufsetzen", "neu bespielen", "SD-Karte", "eMMC",
                            "formatieren", "löschen"):
            self.assertNotIn(destructive, text, f"destructive remedy in banner: {destructive}")
        self.assertIn("setup.sh", text)  # the non-destructive remedy
        self.assertIn("bleiben dabei erhalten", text)  # says the data survives

    def test_german_umlauts_are_literal(self):
        # german-strings-lint scans [WARNUNG] lines for ae/oe/ue transliteration.
        # Every token below is drawn from the CURRENT wording, both directions:
        # a transliteration list whose words are no longer in the message is a
        # vacuous guard, and so is a positive assertion on a word that left.
        self._install_units(drift=True)
        text = self._check(stamp_contents="2.13.0\n", renew=False)
        for bad in ("Aenderung", "ueber", "koennen", "laeuft", "naechsten",
                    "Datensaetze", "Schueler"):
            self.assertNotIn(bad, text)
        for good in ("läuft", "nächsten", "Datensätze", "Schüler"):
            self.assertIn(good, text)

    def test_drift_without_a_stamp_still_warns_but_names_no_version(self):
        # A pre-stamp Pi can be genuinely drifted. The evidence is the unit
        # bytes, so the warning stands; it just cannot name an origin version.
        self._install_units(drift=True)
        text = self._check(stamp_contents=None, renew=False)
        self.assertIn("[WARNUNG]", text)
        self.assertTrue(self.app._system_files_stale)
        self.assertIsNone(self.app._system_files_version)
        self.assertNotIn("Stand: Version", text)

    def test_blank_stamp_is_treated_as_absent(self):
        # A truncated write must not be read as the literal version "".
        self._install_units(drift=True)
        self._check(stamp_contents="   \n", renew=False)
        self.assertIsNone(self.app._system_files_version)

    def test_a_stamp_equal_to_the_agent_version_names_no_origin(self):
        """The origin suffix exists to name a version DIFFERENT from this
        agent's. When the stamp EQUALS APP_VERSION it printed the same number
        twice and the sentence contradicted itself:

            „(Stand: Version 2.13.0) entsprachen nicht der Agent-Version 2.13.0"

        That state is reachable, not theoretical — it is every Pi whose
        setup.sh last ran at the version now running (a bench re-provision, or a
        source-checkout install that has not self-updated since), which is also
        exactly where the likeliest real drift lives: a hand-edited udev rule.
        Assert all three repaired legs at once; they share one `origin`."""
        self._install_units(drift=True)
        self._install_udev(drift=True)
        self._install_compose(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.13.0")
        self.assertNotIn("Stand: Version", text)
        # The drift is still REPORTED — suppressing the suffix must not
        # suppress the finding, on any of the three legs.
        self.assertIn("Systemd-Dienste", text)
        self.assertIn("Udev-Regeln", text)
        self.assertIn("Compose-Datei", text)
        self.assertIn("der Agent-Version 2.13.0", text)

    def test_a_stamp_that_differs_still_names_the_origin(self):
        """The other half of the same rule, and the reason it is a separate
        test: "never emit the suffix" would satisfy the assertion above and
        silently delete the feature. Where the stamp carries information it
        must still be printed."""
        self._install_units(drift=True)
        self._install_udev(drift=True)
        self._install_compose(drift=True)
        text = self._check(stamp_contents="2.12.0\n", app_version="2.13.0")
        self.assertIn("(Stand: Version 2.12.0)", text)
        self.assertIn("der Agent-Version 2.13.0", text)

    def test_the_unrepairable_banner_obeys_the_same_origin_rule(self):
        """The FOURTH site. `origin` is built once and interpolated into the
        three repaired-leg lines AND the [WARNUNG] banner, so the contradiction
        reached the one message a teacher actually sees in the System tab."""
        self._install_units(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.13.0",
                           renew=False)
        self.assertIn("[WARNUNG]", text)
        self.assertNotIn("Stand: Version", text)
        # …and the banner still fires, still naming the files. "Never emit the
        # suffix" would satisfy the assertion above while deleting the finding;
        # the file names are what stop that. (The agent version is deliberately
        # no longer restated here — see
        # test_drifted_units_warn_in_german_and_name_the_unit.)
        self.assertTrue(self.app._system_files_stale)
        self.assertIn("edubotics-pi.service", text)
        self.assertNotIn("2.13.0", text)  # the suffix really is gone

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
        self._check(stamp_contents="2.13.0\n", app_version="2.14.0", renew=False)
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

    # ── WP-0: the opi compose joins the repairable system-file set ───────────

    def test_the_shipped_compose_is_byte_identical_to_the_docker_dir_copy(self):
        """The twin guard, mirrored by `ci.yml::pi-compose-twin-guard`.

        `robotis_ai_setup/docker/docker-compose.opi.yml` is the source of truth
        (it is what `compose-validate` validates and what `setup.sh` installs
        from a source checkout); `pi_agent/docker/docker-compose.opi.yml` is the
        copy that rides the release tarball to a fielded Pi. If they diverge the
        agent would "repair" a fielded Pi ONTO a stale file — strictly worse
        than today's do-nothing, because it would look like it worked."""
        source_of_truth = SETUP_DIR / "docker" / "docker-compose.opi.yml"
        self.assertEqual(
            Path(self.shipped_compose).read_bytes(),
            source_of_truth.read_bytes(),
            "pi_agent/docker/docker-compose.opi.yml has drifted from "
            "robotis_ai_setup/docker/docker-compose.opi.yml",
        )

    def test_the_shipped_compose_is_resolved_INSIDE_the_agent_package(self):
        """The accessor's resolution itself, which byte-identity hides.

        The test above reads the path FROM the accessor, so it cannot see WHERE
        the accessor looked; and in a source checkout both copies exist and are
        byte-identical (that is exactly what the twin guard enforces), so every
        other assertion in this class passes just as happily against the
        `robotis_ai_setup/docker/` copy. MEASURED 2026-07-28: repointing
        `_shipped_compose_path` one directory up left the whole pi_agent suite
        green — 387 passed, control and mutant alike.

        A fielded Pi has ONLY the package copy (`/opt/edubotics/pi_agent/`), so
        that mutant resolves to a path that does not exist, `_compose_drifted`
        returns False under its skip-on-OSError rule, and the entire delivery
        mechanism is silently dead: no repair, no banner, no log line. Pin the
        package-relative property directly, since nothing observable does.

        `SHIPPED_COMPOSE_RELPATH` is the constant `release.yml`'s tarball gate
        and `setup.sh` both name; before this test nothing referenced it."""
        pkg = os.path.dirname(os.path.abspath(agent.__file__))
        resolved = self.app._shipped_compose_path()
        self.assertTrue(os.path.isabs(resolved), resolved)
        self.assertEqual(resolved,
                         os.path.join(pkg, agent.SHIPPED_COMPOSE_RELPATH))
        # Said twice on purpose: the equality above pins the whole path, these
        # two pin the two halves that matter on their own — inside the package
        # (so the self-update rsync carries it) and at the relpath the release
        # gate asserts (so the tarball member and the reader agree).
        self.assertEqual(os.path.commonpath([resolved, pkg]), pkg)
        self.assertTrue(resolved.endswith(agent.SHIPPED_COMPOSE_RELPATH),
                        resolved)
        # …and it READS the constant rather than restating its value. Today the
        # two are identical strings, so every assertion above passes just as
        # well against a hardcoded "docker/docker-compose.opi.yml" — and that
        # mutant is the one the CI/release gates cannot catch either, since they
        # derive the path FROM this constant: a rename would move the gates and
        # the shipped file together while the agent kept opening the old path.
        with patch.object(agent, "SHIPPED_COMPOSE_RELPATH",
                          os.path.join("compose", "opi.yml")):
            self.assertEqual(self.app._shipped_compose_path(),
                             os.path.join(pkg, "compose", "opi.yml"))

    def test_the_compose_repair_works_on_a_pi_with_NO_source_checkout(self):
        """The same property as an OBSERVABLE, in the layout a Pi actually has.

        `/opt/edubotics/pi_agent/` is the whole install — `setup.sh` needs a
        source checkout no classroom has, and the release tarball packages
        `pi_agent/` alone. So the sibling `robotis_ai_setup/docker/` this repo
        keeps beside the package simply is not there, and any resolution that
        leans on it degrades to the silent do-nothing WP-0 exists to end.

        Modelled by pointing the module's `__file__` at a package tree that has
        the twin and nothing outside it. The systemd/udev legs go quiet for free
        (their shipped dirs are absent there, and absent proves nothing), which
        leaves exactly the compose leg under test."""
        field = os.path.join(self.dir, "opt", "edubotics")
        pkg = os.path.join(field, "pi_agent")
        os.makedirs(os.path.join(pkg, "docker"))
        shutil.copyfile(self.shipped_compose,
                        os.path.join(pkg, agent.SHIPPED_COMPOSE_RELPATH))
        # The point of the fixture: nothing above the package.
        self.assertFalse(os.path.exists(os.path.join(field, "docker")))
        self._install_compose(drift=True)
        with patch.object(agent, "__file__", os.path.join(pkg, "agent.py")):
            self.assertTrue(self.app._compose_drifted(),
                            "a Pi with only the packaged twin must still be "
                            "able to PROVE its installed compose drifted")
            text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
            self.assertFalse(self.app._compose_drifted())
        self.assertIn("erneuert", text)
        self.assertNotIn("[WARNUNG]", text)
        self.assertFalse(self.app._system_files_stale)
        with open(self.installed_compose, "rb") as f:
            self.assertEqual(f.read(), Path(self.shipped_compose).read_bytes())

    def test_a_drifted_compose_is_renewed_atomically(self):
        """WP-0's whole point. A release that adds a forwarded EDUBOTICS_* env,
        changes a healthcheck or re-points an image ref used to reach the field
        HALF-applied: the tarball rsyncs `pi_agent/` only, so the compose stayed
        frozen at whatever setup.sh laid down, silently."""
        self._install_compose(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
        self.assertNotIn("[WARNUNG]", text)
        self.assertFalse(self.app._system_files_stale)
        self.assertIn("erneuert", text)
        self.assertIn("docker-compose.opi.yml", text)
        # The installed file now IS the shipped one, so the next boot is silent.
        with open(self.installed_compose, "rb") as f:
            installed = f.read()
        with open(self.shipped_compose, "rb") as f:
            self.assertEqual(installed, f.read())
        self.assertFalse(self.app._compose_drifted())
        # setup.sh installs 0644; the unit runs UMask=0077, so a freshly created
        # file would be 0600 without the explicit chmod.
        self.assertEqual(os.stat(self.installed_compose).st_mode & 0o777, 0o644)
        # One rollback copy of the drifted file, and no temp left behind.
        self.assertTrue(os.path.isfile(self.installed_compose + ".edubotics-bak"))
        self.assertFalse(os.path.exists(self.installed_compose + ".edubotics-tmp"))

    def test_the_compose_repair_writes_a_temp_then_fsyncs_then_replaces(self):
        """Pin the ATOMIC SEQUENCE itself, not just its outcome.

        A non-atomic write is indistinguishable from an atomic one on a healthy
        box and catastrophic on a power cut: a truncated docker-compose.opi.yml
        means `docker compose -f` refuses everything, so the always-on manager
        never comes up — and the manager IS the wizard, i.e. the Pi's only
        repair surface. Order matters as much as the parts: fsync before the
        rename, chmod before the rename, rename last."""
        self._install_compose(drift=True)
        events = []
        real_fsync, real_chmod, real_replace = os.fsync, os.chmod, os.replace
        real_fsync_path = agent._fsync_path

        def _fsync_path(p):
            events.append(f"fsync_path:{p}")
            return real_fsync_path(p)

        def _fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        def _chmod(p, mode):
            if str(p).endswith(".edubotics-tmp"):
                events.append(f"chmod:{oct(mode)}")
            return real_chmod(p, mode)

        def _replace(src, dst):
            events.append("replace")
            # The destination must still hold the OLD bytes at this instant —
            # i.e. nothing wrote the installed file in place.
            with open(dst, "rb") as f:
                self.assertIn(b"provisioned by an older release", f.read())
            return real_replace(src, dst)

        with patch.object(agent.os, "fsync", _fsync), \
             patch.object(agent.os, "chmod", _chmod), \
             patch.object(agent.os, "replace", _replace), \
             patch.object(agent, "_fsync_path", _fsync_path):
            self._check(stamp_contents="2.13.0\n")
        self.assertIn("fsync", events)
        self.assertIn("chmod:0o644", events)
        self.assertIn("replace", events)
        self.assertLess(events.index("fsync"), events.index("replace"))
        self.assertLess(events.index("chmod:0o644"), events.index("replace"))
        # …and the DIRECTORY fsync AFTER the rename — the durability half of the
        # atomicity argument, and the half that was unguarded until 2026-07-28
        # (a mutant deleting it survived the whole suite). `rename` is atomic
        # with respect to concurrent readers, which is an ORDERING property, not
        # a DURABILITY one: without this fsync a classroom power yank on eMMC can
        # lose the directory entry and leave the OLD compose (or, if the entry
        # landed but its data did not, a zero-length one) — precisely the
        # unparseable-compose state the validate gate exists to prevent, arrived
        # at from the other side. Pinned by PATH, not just by count, so fsyncing
        # the wrong thing fails too.
        dir_ev = f"fsync_path:{self.installed_compose_dir}"
        self.assertIn(dir_ev, events, events)
        self.assertGreater(events.index(dir_ev), events.index("replace"), events)

    def test_an_unparseable_shipped_compose_is_refused_and_the_working_file_survives(self):
        """VALIDATE-THEN-SWAP — the one deliberate asymmetry with the systemd
        path. A systemd unit that fails to parse is inert (systemd logs it and
        moves on), but `docker compose -f` refuses to do ANYTHING with a broken
        file: the always-on manager would stop coming up and the Pi would lose
        the wizard it reports problems through. So on a failed
        `docker compose config -q` the swap is REFUSED and the working compose
        is left exactly as it was."""
        self._install_compose(drift=True)
        with open(self.installed_compose, "rb") as f:
            before = f.read()
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0",
                           compose_valid=False)
        # The working file is untouched, byte for byte.
        with open(self.installed_compose, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(self.installed_compose + ".edubotics-tmp"))
        # No .edubotics-bak either — i.e. the gate runs BEFORE anything mutating,
        # not merely before the rename. Unguarded until 2026-07-28: a mutant that
        # hoisted the backup copy above the validate call survived the whole
        # suite. It would leave a rollback copy beside a file that was never
        # replaced: a support artefact that says a swap happened when none did,
        # on the one path where the operator most needs to know that the
        # installed file is still the original.
        self.assertFalse(os.path.exists(self.installed_compose + ".edubotics-bak"))
        # …and the refusal is visible: the reason, then the banner.
        self.assertIn("NICHT erneuert", text)
        self.assertIn("[WARNUNG]", text)
        self.assertIn("docker-compose.opi.yml", text)
        self.assertTrue(self.app._system_files_stale)
        self.assertEqual(self.app._system_files_version, "2.13.0")

    def test_a_validate_gate_that_cannot_RUN_refuses_the_swap(self):
        """An UNRUNNABLE check is a refusal too — the gate's documented „safe
        direction", and until 2026-07-28 nothing tested it: `_validate_compose_file`
        appeared in no test that made `subprocess.run` raise, so replacing its
        except branch with `return None` left the suite green (MEASURED, 387
        passed) while turning validate-then-swap into swap-and-hope.

        Both raising causes are real. `FileNotFoundError` is what a missing
        `docker` binary produces, and it is also what a scrubbed PATH would
        produce — the gate deliberately runs under `docker_manager._scrubbed_env()`,
        so a regression there arrives here first. `TimeoutExpired` is a wedged
        or unresponsive dockerd, the case where accepting is worst: the answer
        is not "the file is fine", it is "nobody looked", and the file being
        swapped in is the one the always-on manager — i.e. the wizard, i.e. the
        Pi's only repair surface — has to parse to come up at all.

        Refusing costs nothing by comparison: the Pi keeps running the compose
        it already had, says so in German, and retries on the next boot."""
        for exc, cause in (
            (FileNotFoundError(2, "No such file or directory: 'docker'"),
             "no docker binary"),
            (subprocess.TimeoutExpired(["docker", "compose", "config"], 20),
             "a wedged dockerd"),
        ):
            with self.subTest(cause=cause):
                self.logs.clear()
                self.app._system_files_stale = False
                self.app._system_files_version = None
                self._install_compose(drift=True)
                with open(self.installed_compose, "rb") as f:
                    before = f.read()

                def _run(argv, *_a, _exc=exc, **_kw):
                    if "config" in argv:  # only the gate is unrunnable
                        raise _exc
                    return subprocess.CompletedProcess(argv, 0, "", "")

                with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
                     patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
                     patch.object(agent, "APP_VERSION", "2.14.0"), \
                     patch.object(agent.subprocess, "run", _run):
                    self.app._check_system_files_version()
                text = "\n".join(self.logs)
                # THE assertion: the working file survived byte for byte.
                with open(self.installed_compose, "rb") as f:
                    self.assertEqual(f.read(), before,
                                     "an unrunnable check must never swap")
                self.assertTrue(self.app._compose_drifted())
                # Nothing mutating ran at all — the gate sits above the backup.
                self.assertFalse(
                    os.path.exists(self.installed_compose + ".edubotics-bak"))
                self.assertFalse(
                    os.path.exists(self.installed_compose + ".edubotics-tmp"))
                # …and the refusal is legible: the German reason, then the banner.
                self.assertIn("NICHT erneuert", text)
                self.assertIn("[WARNUNG]", text)
                self.assertIn("docker-compose.opi.yml", text)
                self.assertTrue(self.app._system_files_stale)

    def test_the_validate_gate_runs_docker_compose_config_on_the_staged_file(self):
        """The gate must judge the STAGED bytes, never the installed file — and
        never the student's .env (an unrelated local edit must not be able to
        veto a release's orchestration fix, so no --env-file)."""
        self._install_compose(drift=True)
        argvs = []
        real_run = agent.subprocess.run

        def _run(argv, *a, **kw):
            argvs.append(list(argv))
            if "config" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", _run):
            self.app._check_system_files_version()
        self.assertIs(agent.subprocess.run, real_run)  # patch really unwound
        gate = [a for a in argvs if "config" in a]
        self.assertEqual(len(gate), 1, argvs)
        self.assertEqual(gate[0][:3], ["docker", "compose", "-f"])
        self.assertEqual(gate[0][3], self.installed_compose + ".edubotics-tmp")
        self.assertIn("-q", gate[0])
        self.assertNotIn("--env-file", gate[0])

    def test_the_validate_gate_runs_with_a_SCRUBBED_process_environment(self):
        """Omitting --env-file is only HALF of isolating the gate from the
        student's .env, and the missing half was a live defect.

        The unit is `EnvironmentFile=-/etc/edubotics/.env`, so the entire managed
        .env is already in the agent's os.environ — and compose interpolates from
        the process environment too. MEASURED against real docker 29.4.3 /
        compose v5.1.3, with NO .env file anywhere: a bare
        EDUBOTICS_BIND_HOST='a b' in the process env makes
        `docker compose -f <twin> config -q` exit 1 ("invalid IP address: a b"),
        and the same file with the same variable scrubbed exits 0. Unscrubbed,
        one malformed local value therefore refuses the compose repair for good
        and banners the Pi with a remedy that does not address the cause.

        It also matters that this is docker_manager's ONE allowlist rather than a
        second copy: it is what keeps EDUBOTICS_AGENT_TOKEN out of every other
        docker subprocess, and _compose_up — the real run this gate predicts —
        has always used it."""
        self._install_compose(drift=True)
        seen = {}

        def _run(argv, *a, **kw):
            if "config" in argv:
                seen["env"] = kw.get("env", "MISSING")
            return subprocess.CompletedProcess(argv, 0, "", "")

        poison = {
            "EDUBOTICS_BIND_HOST": "a b",          # the measured false refusal
            "EDUBOTICS_ROS_NET_SUBNET": "not/a/cidr",
            "EDUBOTICS_AGENT_TOKEN": "s3cret-token",  # the security half
            "HF_TOKEN": "hf_dead",
            "PATH": os.environ.get("PATH", "/usr/bin"),
            # A DOCKER_* var must be PRESENT for the equality assertion below to
            # have teeth: without one, a hand-written {PATH,HOME,LANG,LC_ALL}
            # lookalike that silently drops the DOCKER_* passthrough compares
            # equal on a clean CI environment. Dropping it is not cosmetic — a
            # Pi with a non-default socket/context would then have its gate
            # unable to reach dockerd, i.e. every repair refused.
            "DOCKER_CONFIG": "/tmp/edubotics-test-docker-config",
        }
        with patch.dict(os.environ, poison, clear=False), \
             patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", _run):
            self.app._check_system_files_version()
            # Snapshot the allowlist's answer WHILE the poison is in os.environ —
            # patch.dict restores it on exit, and comparing against the restored
            # environment is how this assertion was vacuous in the first place.
            expected_env = agent.docker_manager._scrubbed_env()
        env = seen.get("env")
        self.assertIsInstance(env, dict, "the gate must pass an explicit env=")
        for leaked in ("EDUBOTICS_BIND_HOST", "EDUBOTICS_ROS_NET_SUBNET",
                       "EDUBOTICS_AGENT_TOKEN", "HF_TOKEN"):
            self.assertNotIn(leaked, env, env.keys())
        # …but still runnable: PATH has to survive or `docker` is unfindable and
        # every repair degrades to the OSError refusal branch, and DOCKER_* has
        # to survive or a non-default socket/context is unreachable.
        self.assertIn("PATH", env)
        self.assertEqual(env.get("DOCKER_CONFIG"),
                         "/tmp/edubotics-test-docker-config")
        # Exactly docker_manager's allowlist, not a lookalike written here.
        self.assertEqual(env, expected_env)
        # …and the repair still went through with the poison present.
        self.assertFalse(self.app._compose_drifted())

    # ── the udev rules join the same repairable set ──────────────────────────

    def test_every_shipped_udev_rule_named_in_the_constant_exists(self):
        """A guard against a DEAD guard. `_drifted_in` skips a name it cannot
        read on EITHER side, so a rule renamed on disk without updating
        UDEV_RULE_NAMES (or vice versa) would make the whole udev repair a
        permanent silent no-op — green suite, unrepaired fleet. Same failure
        shape the twin guard exists for, one directory over."""
        self.assertTrue(agent.UDEV_RULE_NAMES)
        for rule in agent.UDEV_RULE_NAMES:
            self.assertTrue(
                os.path.isfile(os.path.join(self.shipped_udev_dir, rule)),
                f"{rule} is named in UDEV_RULE_NAMES but not shipped in pi_agent/udev/",
            )
        # …and setup.sh installs from the same directory, so the two halves of
        # the delivery path cannot drift apart unnoticed.
        setup_sh = (SETUP_DIR / "pi_agent" / "setup.sh").read_text(encoding="utf-8")
        for rule in agent.UDEV_RULE_NAMES:
            self.assertIn(rule, setup_sh)

    def test_a_drifted_udev_rule_is_renewed_and_udev_is_reloaded(self):
        """The fail-open this closes: the rules always rode the tarball (and the
        self-update rsync), but nothing ever COMPARED them, so a release adding a
        VID/PID line — the planned CH343P line for the edu6 arm is exactly that —
        shipped an agent that scans for an arm whose /dev node its scanner
        container may not be permitted to open, on 100 % of the fielded fleet."""
        self._install_udev(drift=True)
        argvs = []

        def _run(argv, *a, **kw):
            argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent, "APP_VERSION", "2.14.0"), \
             patch.object(agent.subprocess, "run", _run):
            self.app._check_system_files_version()
        text = "\n".join(self.logs)
        self.assertNotIn("[WARNUNG]", text)
        self.assertFalse(self.app._system_files_stale)
        self.assertIn("Udev-Regeln", text)
        self.assertIn("99-edubotics-robotis.rules", text)
        self.assertEqual(self.app._drifted_udev_rules(), [])
        for rule in agent.UDEV_RULE_NAMES:
            installed = os.path.join(self.installed_udev, rule)
            self.assertEqual(os.stat(installed).st_mode & 0o777, 0o644)
            self.assertTrue(os.path.isfile(installed + ".edubotics-bak"))
            self.assertFalse(os.path.exists(installed + ".edubotics-tmp"))
        self.assertIn(["udevadm", "control", "--reload-rules"], argvs, argvs)

    def test_the_udev_repair_reloads_the_rules_but_never_TRIGGERS_them(self):
        """The load-bearing asymmetry, and the exact analogue of
        „daemon-reload, never restart".

        `udevadm control --reload-rules` only makes the new rule set known.
        `udevadm trigger` REPLAYS add/change events for devices that are already
        enumerated — on a Pi mid-lesson that re-fires events for the very tty
        nodes an open serial connection holds. A drift check is advisory; it must
        not be able to disturb a running session. setup.sh may trigger (nothing
        is live yet) and does — that difference is deliberate."""
        self._install_udev(drift=True)
        self._install_units(drift=True)
        self._install_compose(drift=True)
        argvs = []

        def _run(argv, *a, **kw):
            argvs.append(list(argv))
            if "config" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", _run):
            self.app._check_system_files_version()
        self.assertTrue(argvs)
        for argv in argvs:
            for forbidden in ("trigger", "settle", "restart", "up", "start",
                              "stop", "rm", "pull", "enable", "add"):
                self.assertNotIn(forbidden, argv, argv)

    def test_a_failed_udevadm_reload_still_counts_as_repaired(self):
        """No udev on the box (a dev laptop) or a reload hiccup: the FILES are
        correct, which is the durable half — udev re-reads /etc/udev/rules.d at
        boot regardless. Never banner over the reload, exactly as the systemd
        path never banners over a failed daemon-reload."""
        self._install_udev(drift=True)
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run",
                          side_effect=FileNotFoundError("udevadm")):
            self.app._check_system_files_version()
        self.assertFalse(self.app._system_files_stale)
        self.assertEqual(self.app._drifted_udev_rules(), [])

    def test_an_absent_installed_udev_rule_is_skipped_not_installed(self):
        """Absent evidence is not evidence of drift — the same rule the compose
        and unit legs follow. A box that never had the rule (a dev machine, a
        hand-managed install) must not be silently written to."""
        for rule in agent.UDEV_RULE_NAMES:
            self.assertFalse(os.path.exists(os.path.join(self.installed_udev, rule)))
        text = self._check(stamp_contents="2.13.0\n")
        self.assertEqual(text, "")
        self.assertFalse(self.app._system_files_stale)
        for rule in agent.UDEV_RULE_NAMES:
            self.assertFalse(os.path.exists(os.path.join(self.installed_udev, rule)))

    def test_an_unrepairable_udev_rule_sets_the_banner(self):
        """A read-only /etc is the state where the advisory banner still earns
        its place. Degrade to it, never raise out of boot()."""
        self._install_udev(drift=True)
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent, "APP_VERSION", "2.14.0"), \
             patch.object(agent.subprocess, "run",
                          MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))), \
             patch.object(agent.os, "replace",
                          side_effect=OSError("read-only file system")):
            self.app._check_system_files_version()  # must not raise
        self.assertTrue(self.app._system_files_stale)
        text = "\n".join(self.logs)
        self.assertEqual(text.count("[WARNUNG]"), 1, text)
        self.assertIn("99-edubotics-robotis.rules", text)
        for rule in agent.UDEV_RULE_NAMES:
            self.assertFalse(os.path.exists(
                os.path.join(self.installed_udev, rule) + ".edubotics-tmp"))

    def test_a_matching_udev_rule_is_silent(self):
        self._install_udev(drift=False)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
        self.assertEqual(text, "")
        self.assertFalse(self.app._system_files_stale)

    def test_all_three_file_kinds_share_ONE_banner(self):
        """"Only what could NOT be repaired reaches the banner" — still at most
        one line, now naming units, udev rules and the compose together."""
        self._install_units(drift=True)
        self._install_udev(drift=True)
        self._install_compose(drift=True)
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent, "APP_VERSION", "2.14.0"), \
             patch.object(self.app, "_renew_system_units",
                          side_effect=lambda units: ([], list(units))), \
             patch.object(self.app, "_renew_udev_rules",
                          side_effect=lambda rules: ([], list(rules))), \
             patch.object(agent.subprocess, "run",
                          MagicMock(side_effect=lambda argv, *a, **kw:
                                    subprocess.CompletedProcess(argv, 1, "", "boom"))):
            self.app._check_system_files_version()
        text = "\n".join(self.logs)
        self.assertEqual(text.count("[WARNUNG]"), 1, text)
        for name in ("edubotics-pi.service", "edubotics-pi-firstboot.service",
                     "99-edubotics-robotis.rules", "docker-compose.opi.yml"):
            self.assertIn(name, text)

    def test_an_unrepairable_compose_sets_the_banner(self):
        """A read-only /opt (or a hand-mounted compose) is the state where the
        advisory banner still earns its place. Degrade to it, never raise out of
        boot(), and leave no half-written file behind."""
        self._install_compose(drift=True)
        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run",
                          MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))), \
             patch.object(agent.os, "replace",
                          side_effect=OSError("read-only file system")):
            self.app._check_system_files_version()  # must not raise
        self.assertTrue(self.app._system_files_stale)
        text = "\n".join(self.logs)
        self.assertIn("[WARNUNG]", text)
        self.assertIn("docker-compose.opi.yml", text)
        self.assertFalse(os.path.exists(self.installed_compose + ".edubotics-tmp"))

    def test_an_absent_installed_compose_is_skipped_not_installed(self):
        """`_compose_drifted` skips a file it cannot READ on either side, so a
        Pi with no installed compose (a dev box, a relocated
        EDUBOTICS_OPI_COMPOSE, a pre-twin provision) is never written to and
        never bannered. Absent evidence is not evidence of drift."""
        self.assertFalse(os.path.exists(self.installed_compose))
        text = self._check(stamp_contents="2.13.0\n")
        self.assertEqual(text, "")
        self.assertFalse(self.app._system_files_stale)
        self.assertFalse(os.path.exists(self.installed_compose))

    def test_an_unreadable_installed_compose_proves_nothing(self):
        self._install_compose(drift=True)
        with patch.object(agent, "open", create=True,
                          side_effect=OSError("permission denied")):
            self.assertFalse(self.app._compose_drifted())

    def test_a_matching_compose_is_silent(self):
        self._install_compose(drift=False)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0")
        self.assertEqual(text, "")
        self.assertFalse(self.app._system_files_stale)

    def test_the_compose_repair_never_starts_or_restarts_a_container(self):
        """The repair installs a FILE. It must never `up`, `restart`, `stop` or
        `pull` anything: boot() owns the manager lifecycle a few steps later,
        and a compose command from inside a drift check would be an unannounced
        container action on a Pi that may be mid-lesson."""
        self._install_compose(drift=True)
        self._install_units(drift=True)
        argvs = []

        def _run(argv, *a, **kw):
            argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(agent, "SYSTEM_FILES_VERSION_FILE", self.stamp), \
             patch.object(agent, "SYSTEMD_UNIT_DIR", self.installed), \
             patch.object(agent.subprocess, "run", _run):
            self.app._check_system_files_version()
        self.assertTrue(argvs)
        for argv in argvs:
            for forbidden in ("up", "restart", "start", "stop", "rm", "pull",
                              "enable", "create"):
                self.assertNotIn(forbidden, argv, argv)

    def test_the_compose_repair_reports_when_it_takes_effect_truthfully(self):
        """boot() calls this BEFORE `docker_manager.start_manager`, which is an
        `up -d --force-recreate --no-deps physical_ai_manager` — so the manager
        tier really does adopt the renewed compose in the SAME boot. Only the
        robot tier waits for „Umgebung starten". The Protokoll line must not
        claim otherwise (an operator who reads „erst beim nächsten Start" would
        mis-attribute a manager change to something else)."""
        self._install_compose(drift=True)
        text = self._check(stamp_contents="2.13.0\n")
        self.assertIn("Weboberfläche", text)
        self.assertIn("Startvorgang", text)
        self.assertIn("Umgebung starten", text)

    def test_compose_german_umlauts_are_literal(self):
        # german-strings-lint scans [WARNUNG] lines for ae/oe/ue transliteration.
        self._install_compose(drift=True)
        text = self._check(stamp_contents="2.13.0\n", compose_valid=False)
        for bad in ("Weboberflaeche", "ueber", "koennen", "ausfuehren",
                    "Datensaetze", "pruefen", "unveraendert"):
            self.assertNotIn(bad, text)
        self.assertIn("unverändert", text)

    def test_a_drifted_compose_and_drifted_units_share_ONE_banner(self):
        """"Only what could NOT be repaired reaches the banner" — and there is
        at most one banner line, naming exactly those files."""
        self._install_units(drift=True)
        self._install_compose(drift=True)
        text = self._check(stamp_contents="2.13.0\n", app_version="2.14.0",
                           renew=False, compose_valid=False)
        self.assertEqual(text.count("[WARNUNG]"), 1, text)
        for name in ("edubotics-pi.service", "edubotics-pi-firstboot.service",
                     "docker-compose.opi.yml"):
            self.assertIn(name, text)

    def test_a_repaired_compose_beside_unrepairable_units_banners_only_the_units(self):
        self._install_units(drift=True)
        self._install_compose(drift=True)
        text = self._check(stamp_contents="2.13.0\n", renew=False)
        banner = [ln for ln in text.splitlines() if "[WARNUNG]" in ln]
        self.assertEqual(len(banner), 1, text)
        self.assertIn("edubotics-pi.service", banner[0])
        self.assertNotIn("docker-compose.opi.yml", banner[0])
        # …and the compose really was repaired.
        self.assertFalse(self.app._compose_drifted())


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

    def test_a_force_storm_is_rate_floored_to_one_cloud_call(self):
        """MINOR 8. `?force=1` skipped UPDATE_CHECK_TTL_S entirely with no rate
        limit, and GETs are not Origin-gated (a no-cors cross-origin GET does not
        even carry an Origin header). Any page a student opens on the school LAN
        could therefore loop the endpoint, each request spawning a
        ThreadingHTTPServer thread that holds a 5 s outbound HTTPS call —
        defeating the poll-storm cache CLAUDE.md names as the guard and turning
        the Pi into a small amplifier against the cloud API.
        """
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": ""}
        with patch.object(agent.update_checker, "check_for_agent_update",
                          return_value=upd) as chk:
            for _ in range(50):
                code, payload = self.app.handle_update_check(force=True)
        self.assertEqual(code, 200)
        # Still a correct answer, just not 50 cloud round-trips.
        self.assertTrue(payload["update_available"])
        self.assertEqual(chk.call_count, 1)

    def test_a_manual_recheck_after_the_floor_is_honoured(self):
        # The floor suppresses the network call, never the feature: once the
        # interval has passed a human's re-check refreshes for real.
        upd = {"version": "9.9.9", "download_url": "http://x/agent.tgz", "sha256": ""}
        with patch.object(agent.update_checker, "check_for_agent_update",
                          return_value=upd) as chk:
            self.app.handle_update_check(force=True)
            self.app._update_check_forced_at -= (
                agent._UPDATE_CHECK_FORCE_MIN_INTERVAL_S + 1)
            self.app.handle_update_check(force=True)
        self.assertEqual(chk.call_count, 2)

    def test_never_raises_under_the_floor(self):
        # Constraint: handle_update_check must NEVER raise, on any path.
        with patch.object(agent.update_checker, "check_for_agent_update",
                          side_effect=RuntimeError("cloud down")):
            for _ in range(3):
                code, payload = self.app.handle_update_check(force=True)
        self.assertEqual(code, 200)
        self.assertFalse(payload["update_available"])


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
        if list(argv[:3]) == [sys.executable, "-m", "compileall"]:
            # Emulate the pre-swap syntax validation IN-PROCESS (still no real
            # subprocess): behavior-accurate, so the broken-tree test below
            # exercises the same reject decision production makes.
            import compileall as _compileall
            ok = _compileall.compile_dir(argv[-1], quiet=2)
            return type("P", (), {"returncode": 0 if ok else 1, "stdout": "",
                                  "stderr": "" if ok else "SyntaxError"})()
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
        ImportError under systemd Restart=always → a crash-loop (the unit's
        ExecStartPre only heals the MISSING-pi_agent window, not a present tree
        with empty files). Calling "atomic" a swap that can surface empty files
        is the overclaim this pins shut.

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

    def test_broken_staged_tree_is_rejected_before_the_swap(self):
        """A tarball that unpacks but does not byte-compile must NEVER replace
        the working agent: post-swap it would crash-loop under Restart=always
        with the agent itself being the Pi's only repair surface. The apply
        must fail, leave the live tree untouched, and leave no staged debris."""
        root = tempfile.mkdtemp()
        staging = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, staging, True)
        self._install(root, {"VERSION": "2.0.0\n", "agent.py": "# old\n"})
        tarball = self._tarball_of(
            staging, {"VERSION": "2.1.0\n", "agent.py": "def broken(:\n"})

        self.assertFalse(self._apply(root, tarball))

        pkg = os.path.join(root, "pi_agent")
        with open(os.path.join(pkg, "agent.py")) as f:
            self.assertEqual(f.read(), "# old\n", "live tree must be untouched")
        self.assertFalse(os.path.exists(pkg + ".new"),
                         "rejected staging must not linger as debris")
        self.assertFalse(os.path.exists(pkg + ".old"),
                         "no swap happened, so no rollback dir may appear")

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


# ── robot-profile selection (WP-3) ───────────────────────────────────────────


class _RobotTypeBase(_ServerBase):
    """A server-backed AgentApp with a sandboxed .env and a stopped robot tier."""

    def setUp(self):
        super().setUp()
        self._prev_domain = os.environ.get("EDUBOTICS_ROS_DOMAIN")
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "42"
        self._d = tempfile.mkdtemp()
        self.env = os.path.join(self._d, ".env")
        self.app.env_file = self.env
        # Nothing is running unless a test says so.
        self._status = {}
        p = patch.object(agent.docker_manager, "get_container_status",
                         side_effect=lambda *a, **k: dict(self._status))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        if self._prev_domain is None:
            os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        else:
            os.environ["EDUBOTICS_ROS_DOMAIN"] = self._prev_domain
        shutil.rmtree(self._d, ignore_errors=True)
        super().tearDown()

    def _tier_running(self):
        self._status = {"open_manipulator": "running",
                        "physical_ai_server": "running",
                        "physical_ai_manager": "running"}

    def _seed_arms(self, leader=True):
        cg = agent.config_generator
        self.app._hardware = cg.HardwareConfig(
            leader=(cg.ArmDevice(serial_path="/dev/serial/by-id/L", role="leader")
                    if leader else None),
            follower=cg.ArmDevice(serial_path="/dev/serial/by-id/F", role="follower"),
            cameras=[cg.CameraDevice(path="/dev/video0", role="scene")],
        )


class TestRobotTypeEndpoint(_RobotTypeBase):
    """POST /robot-type — the Pi's counterpart of the GUI Modus combobox. The Pi
    had NO writer at all: the only way to change the robot type was to SSH in and
    hand-edit the .env."""

    def test_origin_gate_rejects_a_cross_site_post_before_any_side_effect(self):
        code, payload = self._post("/robot-type", {"robot_type": "edu6_studio"},
                                   origin="http://evil.com")
        self.assertEqual(code, 403)
        self.assertIn("Origin", payload["message"])
        # Nothing was written: a drive-by must not be able to re-type the rig.
        self.assertIsNone(agent.config_generator.read_env_var(
            "EDUBOTICS_ROBOT_TYPE", self.env))

    def test_a_valid_id_is_persisted_and_read_back(self):
        code, payload = self._post("/robot-type", {"robot_type": "edu6_studio"},
                                   origin=None)
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["robot_type"], "edu6_studio")
        self.assertEqual(self.app._current_robot_type(), "edu6_studio")

    def test_the_id_is_written_UNQUOTED(self):
        """entrypoint_omx.sh compares this value literally
        (`[ "$EDUBOTICS_ROBOT_TYPE" = "edu6_studio" ]`), so a surviving pair of
        quotes would silently take the OMX path on an edu6 rig — the split brain
        WP-1 closed. The managed emitters write it unquoted; this writer must
        match them byte for byte."""
        self._post("/robot-type", {"robot_type": "edu6_studio"}, origin=None)
        raw = open(self.env, encoding="utf-8").read()
        self.assertIn("EDUBOTICS_ROBOT_TYPE=edu6_studio", raw)
        self.assertNotIn('EDUBOTICS_ROBOT_TYPE="', raw)

    def test_it_is_settable_before_any_hardware_exists(self):
        """The scan reads the arm FAMILY back off this key, so the type must be
        persistable on a rig with nothing scanned yet — otherwise selecting
        edu6 could never lead to an edu6 scan."""
        self.assertIsNone(self.app._hardware.follower)
        code, _ = self._post("/robot-type", {"robot_type": "edu6_studio"},
                             origin=None)
        self.assertEqual(code, 200)
        self.assertEqual(
            agent.arm_family_for_robot_type(self.app._current_robot_type()),
            "edu6")

    def test_a_ready_rig_gets_the_derived_follower_only_immediately(self):
        self._seed_arms(leader=False)
        code, _ = self._post("/robot-type", {"robot_type": "edu6_studio"},
                             origin=None)
        self.assertEqual(code, 200)
        raw = open(self.env, encoding="utf-8").read()
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", raw)
        self.assertNotIn("LEADER_PORT=", raw)

    def test_switching_back_to_omx_full_restores_the_both_arms_env(self):
        self._seed_arms(leader=True)
        self._post("/robot-type", {"robot_type": "omx_follower"}, origin=None)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1",
                      open(self.env, encoding="utf-8").read())
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)
        raw = open(self.env, encoding="utf-8").read()
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", raw)
        self.assertIn("LEADER_PORT=", raw)

    def test_an_unknown_id_is_a_german_400_and_changes_nothing(self):
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)
        code, payload = self._post("/robot-type", {"robot_type": "hovercraft"},
                                   origin=None)
        self.assertEqual(code, 400)
        self.assertIn("Unbekannter Robotertyp", payload["message"])
        self.assertEqual(self.app._current_robot_type(), "omx_full")

    def test_a_missing_or_blank_id_is_a_german_400(self):
        for body in ({}, {"robot_type": ""}, {"robot_type": "   "},
                     {"robot_type": 7}):
            with self.subTest(body=body):
                code, payload = self._post("/robot-type", body, origin=None)
                self.assertEqual(code, 400, payload)
                self.assertIn("Robotertyp", payload["message"])

    def test_it_is_REFUSED_while_the_robot_tier_is_up(self):
        """The type is HARDSET — the arm container branches on it at start and
        the server resolves its ArmProfile once at boot, so changing it under a
        live stack would leave the .env and the running containers disagreeing.
        The GUI grays its combobox for the same reason."""
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)
        self._tier_running()
        code, payload = self._post("/robot-type", {"robot_type": "edu6_studio"},
                                   origin=None)
        self.assertEqual(code, 409)
        self.assertIn("gestoppt", payload["message"])
        self.assertEqual(self.app._current_robot_type(), "omx_full")

    def test_a_manager_only_pi_can_still_choose(self):
        # Only the ROBOT tier blocks; the always-on manager must not.
        self._status = {"physical_ai_manager": "running"}
        code, _ = self._post("/robot-type", {"robot_type": "edu6_studio"},
                             origin=None)
        self.assertEqual(code, 200)

    def test_it_answers_503_instead_of_blocking_on_the_lifecycle_lock(self):
        p = patch.object(agent, "_LIFECYCLE_LOCK_WAIT_S", 0.05)
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(self.app._lifecycle_lock.acquire(timeout=1))
        self.addCleanup(self.app._lifecycle_lock.release)
        started = time.monotonic()
        code, payload = self._post("/robot-type", {"robot_type": "edu6_studio"},
                                   origin=None)
        self.assertEqual(code, 503, payload)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertIsNone(agent.config_generator.read_env_var(
            "EDUBOTICS_ROBOT_TYPE", self.env))

    def test_a_failed_regenerate_does_not_claim_the_type_was_not_saved(self):
        """The id write is the contract; the .env regenerate on top is a
        convenience. Once the id is on disk, answering „konnte nicht gespeichert
        werden" would be false — and the one reachable cause (a hand-edited
        CAMERA_NAME_n that rehydrate carried in verbatim) has nothing to do with
        the robot type."""
        self._seed_arms(leader=True)
        self.app._hardware.cameras = [
            agent.config_generator.CameraDevice(path="/dev/video0", role="kaputt")]
        with self.assertLogs("edubotics-pi-agent", level="INFO") as cap:
            code, payload = self._post("/robot-type", {"robot_type": "omx_full"},
                                       origin=None)
        self.assertEqual(code, 200, payload)
        self.assertEqual(self.app._current_robot_type(), "omx_full")
        # …and only the rewrite is reported as failed, in the Protokoll (the
        # _RingLogHandler mirrors this logger into the SSE stream).
        self.assertTrue(any("Robotertyp gesetzt" in t and "WARNUNG" in t
                            for t in cap.output), cap.output)

    def test_a_failed_ID_write_IS_a_500(self):
        with patch.object(agent.config_generator, "upsert_env_var",
                          side_effect=RuntimeError("disk full")):
            code, payload = self._post("/robot-type", {"robot_type": "omx_full"},
                                       origin=None)
        self.assertEqual(code, 500)
        self.assertIn("Robotertyp konnte nicht gespeichert werden",
                      payload["message"])

    def test_the_lock_is_released_again_on_every_path(self):
        for body in ({"robot_type": "edu6_studio"},   # success
                     {"robot_type": "hovercraft"}):   # 400 before the acquire
            self._post("/robot-type", body, origin=None)
        self._tier_running()
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)  # 409
        self._status = {}
        with patch.object(agent.config_generator, "upsert_env_var",
                          side_effect=RuntimeError("disk full")):
            code, _ = self._post("/robot-type", {"robot_type": "omx_full"},
                                 origin=None)
            self.assertEqual(code, 500)
        self.assertTrue(self.app._lifecycle_lock.acquire(timeout=1))
        self.app._lifecycle_lock.release()


class TestStatusCarriesTheProfileSurface(_RobotTypeBase):
    def test_status_reports_the_selected_type_and_the_selectable_list(self):
        self._post("/robot-type", {"robot_type": "edu6_studio"}, origin=None)
        code, payload = self._get("/status")
        self.assertEqual(code, 200)
        self.assertEqual(payload["robot_type"], "edu6_studio")
        rows = {r["id"]: r for r in payload["robot_profiles"]}
        self.assertEqual(set(rows), set(agent.ROBOT_PROFILES))
        for pid, row in rows.items():
            self.assertTrue(row["display_de"].strip(), pid)
            self.assertEqual(row["scan_requires_leader"],
                             agent.ROBOT_PROFILES[pid]["scan_requires_leader"])

    def test_hardware_ready_is_profile_aware(self):
        # A lone follower: not ready for omx_full, ready for a follower-only kit.
        self._seed_arms(leader=False)
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)
        self.assertFalse(self._get("/status")[1]["hardware_ready"])
        self._post("/robot-type", {"robot_type": "edu6_studio"}, origin=None)
        self.assertTrue(self._get("/status")[1]["hardware_ready"])

    def test_both_arms_still_answers_its_own_question(self):
        # `arms_identified.both` is NOT redefined: a follower-only rig is ready
        # with both=False, and the two keys must not be conflated.
        self._seed_arms(leader=False)
        self._post("/robot-type", {"robot_type": "edu6_studio"}, origin=None)
        payload = self._get("/status")[1]
        self.assertFalse(payload["arms_identified"]["both"])
        self.assertTrue(payload["hardware_ready"])

    def test_an_unknown_on_disk_id_degrades_to_the_default(self):
        # A hand-edited typo must not propagate into the scan family, the
        # readiness gate or the next .env — it reads back as the default.
        agent.config_generator.upsert_env_var(
            "EDUBOTICS_ROBOT_TYPE", "typo_profile", self.env, quote=False)
        self.assertEqual(self.app._current_robot_type(),
                         agent.resolve_robot_type(None))
        self.assertEqual(self._get("/status")[1]["robot_type"], "omx_full")


class TestPersistCarriesTheSessionModeWithoutContradicting(_RobotTypeBase):
    """`_persist_env_if_ready` resolves the mode: derive when the caller has no
    live session mode, and let the PROFILE win on a follower-only rig. Without
    the second half, a stale `EDUBOTICS_FOLLOWER_ONLY=0` (hand-edit, or a .env
    written before the type was chosen) reaches generate_env_file's contradiction
    guard and turns an ordinary camera-role edit into a German 500."""

    def test_a_camera_edit_on_a_follower_only_rig_does_not_500(self):
        self._seed_arms(leader=False)
        self._post("/robot-type", {"robot_type": "edu6_studio"}, origin=None)
        # Simulate the stale/contradictory value.
        agent.config_generator.upsert_env_var(
            "EDUBOTICS_FOLLOWER_ONLY", "0", self.env, quote=False)
        code, payload = self._post(
            "/cameras/roles",
            {"cameras": [{"path": "/dev/video0", "role": "scene"}]}, origin=None)
        self.assertEqual(code, 200, payload)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1",
                      open(self.env, encoding="utf-8").read())

    def test_a_live_roboter_studio_session_is_still_carried_forward(self):
        # omx_full mid-LeaderToggle: FOLLOWER_ONLY=1 must NOT be reset to 0 by a
        # camera edit (the pre-existing prev_val behaviour, unchanged).
        self._seed_arms(leader=True)
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)
        agent.config_generator.upsert_env_var(
            "EDUBOTICS_FOLLOWER_ONLY", "1", self.env, quote=False)
        code, _ = self._post(
            "/cameras/roles",
            {"cameras": [{"path": "/dev/video0", "role": "scene"}]}, origin=None)
        self.assertEqual(code, 200)
        raw = open(self.env, encoding="utf-8").read()
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", raw)
        self.assertNotIn("LEADER_PORT=", raw)

    def test_a_both_arms_session_stays_both_arms(self):
        self._seed_arms(leader=True)
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)
        code, _ = self._post(
            "/cameras/roles",
            {"cameras": [{"path": "/dev/video0", "role": "scene"}]}, origin=None)
        self.assertEqual(code, 200)
        raw = open(self.env, encoding="utf-8").read()
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", raw)
        self.assertIn("LEADER_PORT=", raw)


class TestEnvironmentStartIsProfileAware(_RobotTypeBase):
    """„Umgebung starten" hard-required BOTH arms, which is why omx_follower —
    shipping on Windows since the robot-type system landed — was unreachable on
    a Pi, edu6 aside."""

    def test_a_follower_only_rig_can_start_with_no_leader(self):
        self._seed_arms(leader=False)
        self._post("/robot-type", {"robot_type": "edu6_studio"}, origin=None)
        with patch.object(agent.docker_manager, "start_robot_tier",
                          return_value=True) as start:
            code, payload = self._post("/environment/start", {}, origin=None)
        self.assertEqual(code, 200, payload)
        start.assert_called_once()
        raw = open(self.env, encoding="utf-8").read()
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", raw)
        self.assertIn("EDUBOTICS_ROBOT_TYPE=edu6_studio", raw)

    def test_omx_full_still_demands_both_arms(self):
        self._seed_arms(leader=False)
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)
        with patch.object(agent.docker_manager, "start_robot_tier",
                          return_value=True) as start:
            code, payload = self._post("/environment/start", {}, origin=None)
        self.assertEqual(code, 400)
        self.assertIn("beide Arme", payload["message"])
        start.assert_not_called()

    def test_omx_full_with_both_arms_is_unchanged(self):
        self._seed_arms(leader=True)
        self._post("/robot-type", {"robot_type": "omx_full"}, origin=None)
        with patch.object(agent.docker_manager, "start_robot_tier",
                          return_value=True) as start:
            code, _ = self._post("/environment/start", {}, origin=None)
        self.assertEqual(code, 200)
        start.assert_called_once()
        raw = open(self.env, encoding="utf-8").read()
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", raw)
        self.assertIn("LEADER_PORT=", raw)

    def test_the_follower_only_refusal_does_not_mention_a_leader(self):
        # A follower-only kit HAS no leader; telling the student to scan one is
        # the misdiagnosis this gate exists to remove.
        self._post("/robot-type", {"robot_type": "edu6_studio"}, origin=None)
        code, payload = self._post("/environment/start", {}, origin=None)
        self.assertEqual(code, 400)
        self.assertNotIn("Leader", payload["message"])
        self.assertIn("scannen", payload["message"])


if __name__ == "__main__":
    unittest.main()
