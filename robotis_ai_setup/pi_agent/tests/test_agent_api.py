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
from unittest.mock import patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

from pi_agent import agent  # noqa: E402
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


# ── ACK-early async update job ───────────────────────────────────────────────


class TestUpdateJob(unittest.TestCase):
    def setUp(self):
        self.app = agent.AgentApp()

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


# ── Agent self-update apply (rsync --delete over pi_agent/) ───────────────────


class TestApplyAgentUpdate(unittest.TestCase):
    def test_rsync_delete_removes_orphan_and_refreshes_version(self):
        # A module REMOVED in a release must not linger after a self-update, and
        # the refreshed pi_agent/VERSION (which the agent reports) must land.
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
                app._apply_agent_update_and_restart(tarball)
            self.assertFalse(kill.called)  # no restart triggered
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)


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
