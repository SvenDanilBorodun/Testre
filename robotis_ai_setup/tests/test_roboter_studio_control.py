"""Tests for the Roboter Studio leader-toggle control bridge (handler logic).

Exercises the request-handler methods directly (no socket) — the HTTP plumbing
is a thin shell over handle_status / handle_set_mode.
"""

import threading
import unittest

from gui.app.roboter_studio_control import RoboterStudioControlServer


class TestRoboterStudioControl(unittest.TestCase):

    def test_status_reflects_get_status(self):
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"),
            get_status=lambda: {"follower_only": True},
        )
        code, body = srv.handle_status()
        self.assertEqual(code, 200)
        self.assertTrue(body["follower_only"])
        self.assertFalse(body["busy"])
        # Absent ready key defaults True (back-compat with a status fn that omits it).
        self.assertTrue(body["ready"])

    def test_status_surfaces_ready_and_busy_from_status(self):
        # The GUI reports ready=False / busy=True while a switch is in flight;
        # the bridge must forward both so the badge never claims "ready" early.
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"),
            get_status=lambda: {"follower_only": True, "ready": False, "busy": True},
        )
        code, body = srv.handle_status()
        self.assertEqual(code, 200)
        self.assertFalse(body["ready"])
        self.assertTrue(body["busy"])

    def test_set_mode_ok_calls_callback(self):
        calls = []
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (calls.append(fo) or (True, "bereit")),
            get_status=lambda: {"follower_only": False},
        )
        code, body = srv.handle_set_mode(True)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["follower_only"])
        self.assertEqual(calls, [True])

    def test_set_mode_failure_returns_500_with_message(self):
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (False, "kaputt"),
            get_status=lambda: {},
        )
        code, body = srv.handle_set_mode(False)
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertEqual(body["message"], "kaputt")

    def test_set_mode_exception_returns_500(self):
        def boom(_fo, _log):
            raise RuntimeError("x")
        srv = RoboterStudioControlServer(on_set_mode=boom, get_status=lambda: {})
        code, body = srv.handle_set_mode(True)
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])

    def test_concurrent_set_mode_rejected_with_409(self):
        started = threading.Event()
        release = threading.Event()

        def slow(_fo, _log):
            started.set()
            release.wait(2.0)
            return True, "ok"

        srv = RoboterStudioControlServer(on_set_mode=slow, get_status=lambda: {})
        t = threading.Thread(target=lambda: srv.handle_set_mode(True), daemon=True)
        t.start()
        self.assertTrue(started.wait(2.0))
        # A second toggle while the first restart is in flight must be rejected,
        # not race two `compose up` calls on the same container.
        code, body = srv.handle_set_mode(False)
        self.assertEqual(code, 409)
        self.assertFalse(body["ok"])
        release.set()
        t.join(2.0)

    def test_busy_flag_clears_after_set_mode(self):
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"),
            get_status=lambda: {"follower_only": False},
        )
        srv.handle_set_mode(True)
        _code, body = srv.handle_status()
        self.assertFalse(body["busy"])

    def test_task_busy_refuses_switch_with_409(self):
        calls = []
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (calls.append(fo) or (True, "ok")),
            get_status=lambda: {},
            get_task_busy=lambda: True,
        )
        code, body = srv.handle_set_mode(True)
        self.assertEqual(code, 409)
        self.assertFalse(body["ok"])
        self.assertIn("Aufnahme", body["message"])
        # The mode switch must NOT have run while a task was busy.
        self.assertEqual(calls, [])

    def test_task_busy_probe_exception_does_not_block(self):
        def boom():
            raise RuntimeError("x")
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"),
            get_status=lambda: {},
            get_task_busy=boom,
        )
        code, body = srv.handle_set_mode(True)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])


class TestOriginCheck(unittest.TestCase):
    """The Origin allow-list must require an EXACT loopback host, not a prefix —
    http://localhost.evil.com must be rejected."""

    def _origin_allowed(self, origin):
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"), get_status=lambda: {})
        handler_cls = srv._make_handler()
        # Build a bare instance without running BaseHTTPRequestHandler.__init__
        # (which would need a live socket); we only exercise _origin_allowed.
        h = handler_cls.__new__(handler_cls)
        h.headers = {"Origin": origin}
        return h._origin_allowed()

    def test_empty_origin_allowed(self):
        self.assertTrue(self._origin_allowed(""))

    def test_localhost_allowed(self):
        # The origins the app really sends. The SPA is served from
        # http://localhost:80 and browsers ELIDE the default port from the
        # Origin serialization, so LeaderToggle.jsx / RunControls.jsx reach
        # http://localhost:8769 carrying `Origin: http://localhost` — the
        # port-less spelling below, not the :8769 one.
        self.assertTrue(self._origin_allowed("http://localhost"))
        self.assertTrue(self._origin_allowed("http://localhost:80"))
        self.assertTrue(self._origin_allowed("http://127.0.0.1"))
        self.assertTrue(self._origin_allowed("http://127.0.0.1:80"))
        self.assertTrue(self._origin_allowed("https://localhost"))
        # `~*` on the nginx side is case-insensitive; urlsplit lower-cases the
        # scheme and host, so both sides agree here.
        self.assertTrue(self._origin_allowed("HTTP://LOCALHOST"))

    def test_lookalike_host_rejected(self):
        self.assertFalse(self._origin_allowed("http://localhost.evil.com"))
        self.assertFalse(self._origin_allowed("http://127.0.0.1.evil.com"))
        self.assertFalse(self._origin_allowed("http://evil.com"))
        self.assertFalse(self._origin_allowed("http://notlocalhost"))

    def test_a_NON_DEFAULT_loopback_port_is_rejected(self):
        """Any other HTML-serving surface on this machine is not this origin.

        nginx.conf's map is `~*^https?://localhost(:80)?$` and nginx returns
        403 for every one of these (measured on 1.27.5), while this handler
        used to ALLOW them all — it read `urlsplit(origin).hostname` and
        nothing else. That is not academic: the phone receiver binds
        0.0.0.0:8444 on this very host, so a page it served carried
        `Origin: http://localhost:8444` and could POST the arm into a restart.

        `http://localhost:8769` — the bridge's OWN port — belongs in this list
        too. Nothing is ever served as HTML from :8769 (every handler answers
        application/json), so no page can carry that Origin; the assertion that
        used to accept it pinned the old permissive behaviour, not a real flow.
        """
        for origin in ("http://localhost:8444", "http://localhost:3000",
                       "http://localhost:8769", "https://localhost:8443",
                       "http://127.0.0.1:3000", "http://127.0.0.1:8444"):
            with self.subTest(origin=origin):
                self.assertFalse(self._origin_allowed(origin))

    def test_a_NON_HTTP_scheme_is_rejected(self):
        """`https?` in the nginx map is a scheme allowlist, not decoration."""
        for origin in ("ftp://localhost", "ws://localhost", "wss://localhost",
                       "file://localhost", "chrome-extension://localhost"):
            with self.subTest(origin=origin):
                self.assertFalse(self._origin_allowed(origin))

    def test_the_null_origin_is_rejected(self):
        # A `file://` page sends the literal string `null`, which is non-empty
        # and must therefore NOT take the empty-Origin allowance.
        self.assertFalse(self._origin_allowed("null"))

    def test_a_malformed_origin_is_refused_not_raised(self):
        # urlsplit defers the port parse, so an unparseable port raises
        # ValueError out of `.port` — inside the try, and fail-closed.
        for origin in ("http://localhost:notaport", "http://localhost:99999",
                       "http://[::1", "://localhost"):
            with self.subTest(origin=origin):
                self.assertFalse(self._origin_allowed(origin))

    def test_the_policy_constants_are_the_ones_the_nginx_map_spells(self):
        """Twin lockstep, Python side. `test_rosbridge_origin_gate.py` reads
        these same three constants back out of nginx.conf's map."""
        from gui.app import roboter_studio_control as rsc
        self.assertEqual(set(rsc._ALLOWED_ORIGIN_HOSTS),
                         {"localhost", "127.0.0.1"})
        self.assertEqual(set(rsc._ALLOWED_ORIGIN_SCHEMES), {"http", "https"})
        self.assertEqual(set(rsc._ALLOWED_ORIGIN_PORTS), {None, 80})


if __name__ == "__main__":
    unittest.main()
