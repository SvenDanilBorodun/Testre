"""Tests for the Roboter Studio leader-toggle control bridge (handler logic).

Exercises the request-handler methods directly (no socket) — the HTTP plumbing
is a thin shell over handle_status / handle_set_mode.
"""

import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

from gui.app import roboter_studio_control as rsc
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


class TestHasLeader(unittest.TestCase):
    """/roboter-studio/status carries has_leader from the GUI's own profile
    (plan §3.3): false on omx_follower, where the bridge now exists on a
    station rig and LeaderToggle.jsx hides on `bridgeHasLeader === false`."""

    def _status(self, st):
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"), get_status=lambda: st)
        return srv.handle_status()[1]

    def test_false_is_forwarded(self):
        self.assertIs(self._status({"has_leader": False})["has_leader"], False)

    def test_true_is_forwarded(self):
        self.assertIs(self._status({"has_leader": True})["has_leader"], True)

    def test_an_older_status_fn_without_it_reads_true(self):
        self.assertIs(self._status({"follower_only": False})["has_leader"], True)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Station:
    """Records the GUI's stream callback and plays the station status."""

    def __init__(self, enabled=True, ok=True):
        self.enabled = enabled
        self.ok = ok
        self.calls = []

    def status(self):
        return {"enabled": self.enabled, "scene_fps": 30.0,
                "station_fps": 15.0, "error_de": ""}

    def stream(self, on):
        self.calls.append(on)
        return (True, "") if self.ok else (False, "Die Stationskamera läuft nicht.")


def _server(station, clock=None, on_set_mode=None):
    return RoboterStudioControlServer(
        on_set_mode=on_set_mode or (lambda fo, log: (True, "ok")),
        get_status=lambda: {},
        get_station_status=station.status,
        on_station_stream=station.stream,
        clock=clock or _Clock())


class TestStationStream(unittest.TestCase):

    def test_status_shape_and_default_off(self):
        st = _Station()
        code, body = _server(st).handle_station_status()
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {"enabled", "stream", "scene_fps",
                                     "station_fps", "error_de"})
        self.assertTrue(body["enabled"])
        self.assertEqual(body["stream"], "off")
        self.assertEqual(body["station_fps"], 0.0)
        self.assertEqual(body["scene_fps"], 30.0)

    def test_a_server_without_station_callables_is_not_enabled(self):
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"), get_status=lambda: {})
        self.assertFalse(srv.handle_station_status()[1]["enabled"])
        code, body = srv.handle_station_stream(True)
        self.assertEqual(code, 409)
        self.assertEqual(body["message"], rsc.STATION_NOT_ENABLED_DE)

    def test_on_requires_the_station_enabled(self):
        st = _Station(enabled=False)
        code, body = _server(st).handle_station_stream(True)
        self.assertEqual(code, 409)
        self.assertIn("nicht eingeschaltet", body["message"])
        self.assertEqual(st.calls, [])

    def test_on_starts_and_sets_a_ten_second_lease(self):
        st, clock = _Station(), _Clock()
        srv = _server(st, clock)
        code, body = srv.handle_station_stream(True)
        self.assertEqual((code, body["stream"], body["lease_s"]), (200, "on", 10.0))
        self.assertEqual(st.calls, [True])
        self.assertEqual(srv.handle_station_status()[1]["stream"], "on")
        self.assertEqual(srv.handle_station_status()[1]["station_fps"], 15.0)
        clock.t += 9.9
        self.assertFalse(srv.expire_station_lease())
        clock.t += 0.1
        self.assertTrue(srv.expire_station_lease())
        self.assertEqual(st.calls, [True, False])
        self.assertEqual(srv.handle_station_status()[1]["stream"], "off")
        self.assertFalse(srv.expire_station_lease(), "expires once")

    def test_on_renews_the_lease_start_or_renew(self):
        st, clock = _Station(), _Clock()
        srv = _server(st, clock)
        srv.handle_station_stream(True)
        clock.t += 8.0
        srv.handle_station_stream(True)        # the page's 2 s renew
        clock.t += 8.0
        self.assertFalse(srv.expire_station_lease())
        clock.t += 2.0
        self.assertTrue(srv.expire_station_lease())

    def test_off_stops_at_once(self):
        st = _Station()
        srv = _server(st)
        srv.handle_station_stream(True)
        code, body = srv.handle_station_stream(False)
        self.assertEqual((code, body["stream"]), (200, "off"))
        self.assertEqual(st.calls, [True, False])

    def test_a_stray_off_is_undone_by_the_next_on(self):
        st = _Station()
        srv = _server(st)
        srv.handle_station_stream(True)
        srv.handle_station_stream(False)       # a closing co-owner tab
        srv.handle_station_stream(True)        # the owner's next renew
        self.assertEqual(st.calls, [True, False, True])
        self.assertEqual(srv.handle_station_status()[1]["stream"], "on")

    def test_a_failing_switch_is_a_409_and_leaves_the_stream_off(self):
        st = _Station(ok=False)
        srv = _server(st)
        code, body = srv.handle_station_stream(True)
        self.assertEqual(code, 409)
        self.assertEqual(body["message"], "Die Stationskamera läuft nicht.")
        self.assertEqual(srv.handle_station_status()[1]["stream"], "off")

    def test_leader_toggle_is_409_while_the_stream_is_on(self):
        st, modes = _Station(), []
        srv = _server(st, on_set_mode=lambda fo, log: (modes.append(fo) or (True, "ok")))
        srv.handle_station_stream(True)
        for follower_only in (True, False):
            code, body = srv.handle_set_mode(follower_only)
            self.assertEqual(code, 409)
            self.assertEqual(body["message"],
                             "Roboter Studio nutzt gerade die Stationskamera.")
        self.assertEqual(modes, [])
        srv.handle_station_stream(False)
        self.assertEqual(srv.handle_set_mode(False)[0], 200)
        self.assertEqual(modes, [False])

    def test_on_is_refused_while_a_leader_switch_is_in_flight(self):
        st = _Station()
        started, release = threading.Event(), threading.Event()

        def slow(_fo, _log):
            started.set()
            release.wait(2.0)
            return True, "ok"
        srv = _server(st, on_set_mode=slow)
        t = threading.Thread(target=lambda: srv.handle_set_mode(True), daemon=True)
        t.start()
        self.assertTrue(started.wait(2.0))
        code, body = srv.handle_station_stream(True)
        self.assertEqual(code, 409)
        self.assertEqual(body["message"], rsc.STATION_SWITCH_IN_FLIGHT_DE)
        self.assertEqual(st.calls, [])
        release.set()
        t.join(2.0)
        self.assertEqual(srv.handle_station_stream(True)[0], 200)

    def test_stop_switches_the_stream_off(self):
        st = _Station()
        srv = _server(st)
        srv.handle_station_stream(True)
        srv.stop()
        self.assertEqual(st.calls, [True, False])

    def test_the_lease_watchdog_thread_expires_a_real_lease(self):
        st = _Station()
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"), get_status=lambda: {},
            get_station_status=st.status, on_station_stream=st.stream, port=0)
        with patch.object(rsc, "STATION_LEASE_S", 0.3), \
                patch.object(rsc, "_LEASE_POLL_S", 0.05):
            self.assertTrue(srv.start())
            try:
                srv.handle_station_stream(True)
                end = time.monotonic() + 3.0
                while st.calls != [True, False] and time.monotonic() < end:
                    time.sleep(0.02)
                self.assertEqual(st.calls, [True, False])
            finally:
                srv.stop()

    def test_the_lease_is_ten_seconds(self):
        self.assertEqual(rsc.STATION_LEASE_S, 10.0)


class TestStationStreamBody(unittest.TestCase):

    def test_true_and_false(self):
        self.assertIs(rsc.parse_station_stream_body(b'{"on": true}'), True)
        self.assertIs(rsc.parse_station_stream_body(b'{"on":false}'), False)

    def test_anything_else_is_refused(self):
        for body in (b"", b"on", b"[]", b'{"on": 1}', b'{"on": "true"}',
                     b'{"off": true}', b"\xff\xfe", b"null"):
            with self.subTest(body=body):
                self.assertIsNone(rsc.parse_station_stream_body(body))


class TestStationOverHttp(unittest.TestCase):
    """The real handler on 127.0.0.1: both body types, the Origin gate."""

    def setUp(self):
        self.st = _Station()
        self.srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"), get_status=lambda: {},
            get_station_status=self.st.status,
            on_station_stream=self.st.stream, port=0)
        self.assertTrue(self.srv.start())
        self.port = self.srv._httpd.server_address[1]

    def tearDown(self):
        self.srv.stop()

    def _req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, (json.loads(data) if data else None), resp
        finally:
            conn.close()

    def test_text_plain_beacon_body_is_accepted(self):
        code, body, _ = self._req(
            "POST", "/station/stream", b'{"on": true}',
            {"Content-Type": "text/plain;charset=UTF-8",
             "Origin": "http://localhost"})
        self.assertEqual((code, body["stream"]), (200, "on"))
        code, body, _ = self._req(
            "POST", "/station/stream", b'{"on": false}',
            {"Content-Type": "text/plain;charset=UTF-8",
             "Origin": "http://localhost"})
        self.assertEqual((code, body["stream"]), (200, "off"))
        self.assertEqual(self.st.calls, [True, False])

    def test_application_json_body_is_accepted(self):
        code, body, _ = self._req(
            "POST", "/station/stream", json.dumps({"on": True}),
            {"Content-Type": "application/json", "Origin": "http://localhost"})
        self.assertEqual(code, 200)
        self.assertEqual(self.st.calls, [True])

    def test_a_foreign_origin_is_refused_before_anything_happens(self):
        code, body, _ = self._req(
            "POST", "/station/stream", b'{"on": true}',
            {"Content-Type": "text/plain", "Origin": "http://evil.com"})
        self.assertEqual(code, 403)
        self.assertEqual(self.st.calls, [])

    def test_a_bad_body_is_a_400(self):
        code, body, _ = self._req(
            "POST", "/station/stream", b"on please",
            {"Content-Type": "text/plain", "Origin": "http://localhost"})
        self.assertEqual(code, 400)
        self.assertEqual(body["message"], rsc.STATION_BAD_BODY_DE)
        self.assertEqual(self.st.calls, [])

    def test_get_station_status(self):
        code, body, resp = self._req("GET", "/station/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["stream"], "off")
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "*")

    def test_the_preflight_for_json_is_answered(self):
        code, _body, resp = self._req(
            "OPTIONS", "/station/stream", None,
            {"Origin": "http://localhost",
             "Access-Control-Request-Method": "POST",
             "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(code, 204)
        self.assertIn("POST", resp.getheader("Access-Control-Allow-Methods"))
        self.assertIn("Content-Type", resp.getheader("Access-Control-Allow-Headers"))


if __name__ == "__main__":
    unittest.main()
