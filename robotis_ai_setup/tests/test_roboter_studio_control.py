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
        self.assertTrue(self._origin_allowed("http://localhost:8769"))
        self.assertTrue(self._origin_allowed("http://127.0.0.1"))
        self.assertTrue(self._origin_allowed("https://localhost"))

    def test_lookalike_host_rejected(self):
        self.assertFalse(self._origin_allowed("http://localhost.evil.com"))
        self.assertFalse(self._origin_allowed("http://127.0.0.1.evil.com"))
        self.assertFalse(self._origin_allowed("http://evil.com"))
        self.assertFalse(self._origin_allowed("http://notlocalhost"))


if __name__ == "__main__":
    unittest.main()
