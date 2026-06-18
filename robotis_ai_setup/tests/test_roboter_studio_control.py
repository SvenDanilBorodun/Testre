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


if __name__ == "__main__":
    unittest.main()
