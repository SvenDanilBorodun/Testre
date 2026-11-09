"""Deps-free tests for the Orange Pi's robot login (pi_agent.pi_auth + the
agent's /auth routes, its session gate, the factory-reset confirmation, the
setup-code CLI and the login guard).

The servers here bind 127.0.0.1, so their TCP peer is loopback. A browser never
is: it reaches the agent through nginx on ros_net. ``_GatewayBase`` therefore
patches ``AgentApp.peer_is_trusted`` to False to stand in for the gateway
listener, and ``peer_is_trusted`` itself is tested on real addresses.

No cloud is contacted: ``verify_with_cloud`` runs against a local stub server,
and the agent routes patch it.
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

os.environ.setdefault("EDUBOTICS_PI_AUTH_FILE", os.path.join(
    tempfile.mkdtemp(prefix="pi-auth-test-"), "pi_auth.json"))
os.environ.setdefault("EDUBOTICS_PI_CONSOLE", "")

from pi_agent import agent  # noqa: E402
from pi_agent import pi_auth  # noqa: E402
from pi_agent import setup_code as setup_code_cli  # noqa: E402

FAST = 1000  # PBKDF2 iterations for tests (the default is deliberately slow)


def _jwt(payload: dict) -> str:
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{seg({'alg': 'ES256', 'typ': 'JWT'})}.{seg(payload)}.c2lnbmF0dXJl"


TOKEN = _jwt({"sub": "u-1", "exp": int(time.time()) + 3600})


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ── pure helpers ─────────────────────────────────────────────────────────────


class TestCodeShape(unittest.TestCase):
    def test_a_code_has_eight_unambiguous_characters(self):
        for _ in range(200):
            code = pi_auth.generate_code()
            self.assertEqual(len(code), pi_auth.CODE_LEN)
            self.assertTrue(set(code) <= set(pi_auth.CODE_ALPHABET))
        for ch in "01OIL":
            self.assertNotIn(ch, pi_auth.CODE_ALPHABET)

    def test_the_teacher_may_type_it_loosely(self):
        self.assertEqual(pi_auth.normalize_code("abcd-efgh"), "ABCDEFGH")
        self.assertEqual(pi_auth.normalize_code(" AB CD EF GH "), "ABCDEFGH")
        self.assertEqual(pi_auth.format_code("ABCDEFGH"), "ABCD-EFGH")

    def test_a_malformed_entry_is_not_a_code(self):
        for raw in (None, 12345678, "", "ABC", "ABCDEFGHJ", "ABCD-EFG0", "ABCDEFGI"):
            self.assertIsNone(pi_auth.normalize_code(raw), raw)


class TestTokenHelpers(unittest.TestCase):
    def test_bearer_token_shape(self):
        self.assertEqual(pi_auth.bearer_token(f"Bearer {TOKEN}"), TOKEN)
        self.assertEqual(pi_auth.bearer_token(f"bearer {TOKEN}"), TOKEN)
        for bad in (None, "", "Bearer", "Bearer x", "Basic abc", "Bearer a.b",
                    "Bearer " + "a" * 9000 + ".b.c", "Bearer a.b.c d"):
            self.assertIsNone(pi_auth.bearer_token(bad), bad)

    def test_jwt_exp_reads_the_claim_and_tolerates_garbage(self):
        self.assertEqual(pi_auth.jwt_exp(_jwt({"exp": 1234})), 1234)
        self.assertIsNone(pi_auth.jwt_exp(_jwt({"sub": "x"})))
        self.assertIsNone(pi_auth.jwt_exp(_jwt({"exp": True})))
        self.assertIsNone(pi_auth.jwt_exp("a.!!!.c"))

    def test_a_session_never_outlives_the_window_or_the_token(self):
        now = 10_000.0
        wall = lambda: now  # noqa: E731
        self.assertEqual(pi_auth.session_ttl(_jwt({"exp": now + 3600}), wall),
                         pi_auth.SESSION_TTL_S)
        self.assertEqual(pi_auth.session_ttl(_jwt({"exp": now + 120}), wall), 120)
        # Nearly expired: floored, the cloud just accepted it.
        self.assertEqual(pi_auth.session_ttl(_jwt({"exp": now + 3}), wall),
                         pi_auth.SESSION_MIN_TTL_S)
        # A skewed Pi clock (exp already "past") keeps the fixed window instead
        # of locking everybody out.
        self.assertEqual(pi_auth.session_ttl(_jwt({"exp": now - 5000}), wall),
                         pi_auth.SESSION_TTL_S)
        self.assertEqual(pi_auth.session_ttl(_jwt({}), wall), pi_auth.SESSION_TTL_S)


class TestCookie(unittest.TestCase):
    def test_the_cookie_carries_every_attribute_decision_4_names(self):
        header = pi_auth.session_cookie_header("x" * 43, 600)
        self.assertTrue(header.startswith(f"{pi_auth.COOKIE_NAME}=" + "x" * 43 + ";"))
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=600"):
            self.assertIn(attr, header)
        # No TLS at launch: a Secure cookie would never be sent over http.
        self.assertNotIn("Secure", header)
        self.assertIn("Max-Age=0", pi_auth.clear_cookie_header())

    def test_parse_finds_only_a_well_formed_session_id(self):
        sid = "A" * 43
        self.assertEqual(pi_auth.parse_session_cookie(
            f"other=1; {pi_auth.COOKIE_NAME}={sid}; x=y"), sid)
        self.assertIsNone(pi_auth.parse_session_cookie(f"{pi_auth.COOKIE_NAME}=short"))
        self.assertIsNone(pi_auth.parse_session_cookie(f"x{pi_auth.COOKIE_NAME}={sid}"))
        self.assertIsNone(pi_auth.parse_session_cookie(""))
        self.assertIsNone(pi_auth.parse_session_cookie(None))


class TestRateLimiter(unittest.TestCase):
    def test_sliding_window(self):
        clock = _Clock()
        rl = pi_auth.RateLimiter(3, 60.0, now=clock)
        self.assertTrue(all(rl.allow("a") for _ in range(3)))
        self.assertFalse(rl.allow("a"))
        self.assertTrue(rl.allow("b"))  # per client
        clock.t += 61
        self.assertTrue(rl.allow("a"))

    def test_memory_is_bounded(self):
        rl = pi_auth.RateLimiter(1, 60.0, max_keys=10)
        for i in range(100):
            rl.allow(str(i))
        self.assertLessEqual(len(rl._hits), 10)


# ── the store ────────────────────────────────────────────────────────────────


class _StoreBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "state", "pi_auth.json")
        self.clock = _Clock()
        self.store = pi_auth.AuthStore(self.path, now=self.clock, iterations=FAST)


class TestSetupCode(_StoreBase):
    def test_a_fresh_pi_is_unclaimed_without_a_code(self):
        self.assertFalse(self.store.claimed)
        self.assertEqual(self.store.code_status(), "none")
        self.assertEqual(self.store.try_claim("ABCD-EFGH", "teacher"), "no_code")

    def test_the_code_is_stored_only_as_a_hash(self):
        code = self.store.mint_code()
        raw = Path(self.path).read_text(encoding="utf-8")
        self.assertNotIn(code, raw)
        self.assertNotIn(pi_auth.format_code(code), raw)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        rec = json.loads(raw)["code"]
        self.assertEqual(set(rec) >= {"salt", "hash", "iterations"}, True)

    def test_a_right_code_claims_once_and_is_spent(self):
        code = self.store.mint_code()
        self.assertEqual(self.store.try_claim(pi_auth.format_code(code).lower(), "admin"), "ok")
        self.assertTrue(self.store.claimed)
        self.assertEqual(self.store.code_status(), "none")
        self.assertEqual(self.store.try_claim(code, "admin"), "claimed")
        # The claim survives a restart; the code does not come back.
        again = pi_auth.AuthStore(self.path, iterations=FAST)
        self.assertTrue(again.claimed)
        self.assertEqual(again.code_status(), "none")
        self.assertNotIn(code, Path(self.path).read_text(encoding="utf-8"))

    def test_wrong_attempts_are_counted_and_void_the_code(self):
        code = self.store.mint_code()
        wrong = "ABCDEFGH" if code != "ABCDEFGH" else "HGFEDCBA"
        for i in range(pi_auth.CODE_MAX_FAILURES - 1):
            self.assertEqual(self.store.try_claim(wrong, "teacher"), "wrong", i)
        self.assertEqual(self.store.try_claim(wrong, "teacher"), "void")
        # Void is final: even the RIGHT code is refused now, and it persists.
        self.assertEqual(self.store.try_claim(code, "teacher"), "void")
        self.assertEqual(pi_auth.AuthStore(self.path, iterations=FAST).code_status(), "void")
        self.assertFalse(self.store.claimed)

    def test_a_malformed_entry_does_not_burn_the_budget(self):
        self.store.mint_code()
        for _ in range(pi_auth.CODE_MAX_FAILURES + 5):
            self.assertEqual(self.store.try_claim("1234", "teacher"), "malformed")
        self.assertEqual(self.store.code_status(), "active")

    def test_a_new_code_replaces_the_old_one(self):
        first = self.store.mint_code()
        second = self.store.mint_code()
        if first != second:
            self.assertEqual(self.store.try_claim(first, "teacher"), "wrong")
        self.assertEqual(self.store.try_claim(second, "teacher"), "ok")

    def test_a_claimed_pi_mints_no_code(self):
        self.store.try_claim(self.store.mint_code(), "teacher")
        with self.assertRaises(RuntimeError):
            self.store.mint_code()

    def test_the_comparison_is_constant_time(self):
        src = Path(pi_auth.__file__).read_text(encoding="utf-8")
        self.assertIn("hmac.compare_digest(candidate, expected)", src)
        self.assertIn("pbkdf2_hmac", src)

    def test_an_unreadable_state_file_fails_closed(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        store = pi_auth.AuthStore(self.path, iterations=FAST)
        self.assertFalse(store.claimed)
        self.assertTrue(store.load_failed)
        self.assertEqual(store.code_status(), "none")

    def test_a_reset_revokes_everything_and_mints_a_new_code(self):
        self.store.try_claim(self.store.mint_code(), "teacher")
        sid = self.store.create_session("student", 600)
        new = self.store.reset_to_unclaimed()
        self.assertFalse(self.store.claimed)
        self.assertIsNone(self.store.session(sid))
        self.assertEqual(self.store.session_count(), 0)
        self.assertEqual(self.store.try_claim(new, "teacher"), "ok")


class TestSessions(_StoreBase):
    def _claimed(self):
        self.store.try_claim(self.store.mint_code(), "teacher")

    def test_a_session_expires(self):
        self._claimed()
        sid = self.store.create_session("student", 60)
        self.assertEqual(self.store.session(sid)["role"], "student")
        self.clock.t += 59
        self.assertIsNotNone(self.store.session(sid))
        self.clock.t += 2
        self.assertIsNone(self.store.session(sid))

    def test_revoke_and_unknown_ids(self):
        self._claimed()
        sid = self.store.create_session("teacher", 600)
        self.store.revoke(sid)
        self.assertIsNone(self.store.session(sid))
        self.assertIsNone(self.store.session("x" * 43))
        self.assertIsNone(self.store.session(None))

    def test_no_session_is_valid_on_an_unclaimed_pi(self):
        sid = self.store.create_session("teacher", 600)
        self.assertIsNone(self.store.session(sid))

    def test_sessions_live_in_memory_only(self):
        self._claimed()
        sid = self.store.create_session("student", 600)
        self.assertNotIn(sid, Path(self.path).read_text(encoding="utf-8"))
        restarted = pi_auth.AuthStore(self.path, iterations=FAST)
        self.assertTrue(restarted.claimed)
        self.assertIsNone(restarted.session(sid))

    def test_the_store_keys_on_a_hash_of_the_id(self):
        self._claimed()
        sid = self.store.create_session("student", 600)
        self.assertNotIn(sid, self.store._sessions)

    def test_the_number_of_sessions_is_bounded(self):
        self._claimed()
        with patch.object(pi_auth, "MAX_SESSIONS", 5):
            sids = []
            for _ in range(8):
                self.clock.t += 1
                sids.append(self.store.create_session("student", 600))
            self.assertEqual(self.store.session_count(), 5)
            self.assertIsNone(self.store.session(sids[0]))
            self.assertIsNotNone(self.store.session(sids[-1]))


# ── the cloud check (a local stub stands in for the cloud API) ───────────────


class _CloudStub(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b'{"id": "u-1", "role": "student"}'
    location = None
    seen_auth = []

    def do_GET(self):  # noqa: N802
        type(self).seen_auth.append((self.path, self.headers.get("Authorization")))
        self.send_response(self.status)
        if self.location:
            self.send_header("Location", self.location)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


class TestVerifyWithCloud(unittest.TestCase):
    def setUp(self):
        _CloudStub.status, _CloudStub.location = 200, None
        _CloudStub.body = b'{"id": "u-1", "role": "student"}'
        _CloudStub.seen_auth = []
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _CloudStub)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def test_ok_names_the_role_and_sends_the_token_to_me_only(self):
        self.assertEqual(pi_auth.verify_with_cloud(TOKEN, self.url), ("ok", "student"))
        self.assertEqual(_CloudStub.seen_auth, [("/me", f"Bearer {TOKEN}")])

    def test_the_cloud_judging_the_token_is_rejected(self):
        for status in (401, 403, 404):
            _CloudStub.status = status
            self.assertEqual(pi_auth.verify_with_cloud(TOKEN, self.url), ("rejected", None))

    def test_an_unknown_role_is_rejected(self):
        _CloudStub.body = b'{"role": "superuser"}'
        self.assertEqual(pi_auth.verify_with_cloud(TOKEN, self.url), ("rejected", None))

    def test_could_not_ask_is_unreachable(self):
        for status in (500, 502, 503):
            _CloudStub.status = status
            self.assertEqual(pi_auth.verify_with_cloud(TOKEN, self.url), ("unreachable", None))
        _CloudStub.status, _CloudStub.body = 200, b"<html>portal</html>"
        self.assertEqual(pi_auth.verify_with_cloud(TOKEN, self.url), ("unreachable", None))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        self.assertEqual(pi_auth.verify_with_cloud(TOKEN, f"http://127.0.0.1:{closed}", timeout=2),
                         ("unreachable", None))

    def test_a_redirect_is_never_followed_with_the_token(self):
        """A captive portal answers with a 3xx; urllib would copy ordinary
        headers onto the redirected request and hand the token to the portal."""
        _CloudStub.status = 302
        _CloudStub.location = self.url + "/portal"
        self.assertEqual(pi_auth.verify_with_cloud(TOKEN, self.url), ("unreachable", None))
        self.assertEqual([p for p, _ in _CloudStub.seen_auth], ["/me"])


# ── the agent routes ─────────────────────────────────────────────────────────


class _GatewayBase(unittest.TestCase):
    """A real agent server whose peer is treated as the GATEWAY (not loopback):
    every protected route needs a robot session."""

    trusted = False

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp(env_file=os.path.join(self.dir, ".env"),
                                  auth_file=os.path.join(self.dir, "pi_auth.json"))
        self.app._auth = pi_auth.AuthStore(os.path.join(self.dir, "pi_auth.json"),
                                           iterations=FAST)
        p = patch.object(agent.AgentApp, "peer_is_trusted",
                         staticmethod(lambda _ip: self.trusted))
        p.start()
        self.addCleanup(p.stop)
        self.httpd = agent.ThreadingHTTPServer(("127.0.0.1", 0), self.app._make_handler())
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.origin = "http://127.0.0.1"  # an own-address Origin, as a browser sends

    def req(self, method, path, body=None, headers=None, origin="default"):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if origin == "default":
            origin = self.origin if method == "POST" else None
        if origin:
            r.add_header("Origin", origin)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {}), resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            return e.code, payload, e.headers

    def claim(self, role="teacher"):
        code = self.app._auth.mint_code()
        with patch.object(agent.pi_auth, "verify_with_cloud", return_value=("ok", role)):
            status, payload, headers = self.req(
                "POST", "/auth/claim", {"code": pi_auth.format_code(code)},
                {"Authorization": f"Bearer {TOKEN}"})
        return status, payload, headers

    @staticmethod
    def cookie_of(headers):
        set_cookie = headers.get("Set-Cookie", "")
        return set_cookie.split(";")[0]


class TestTheGateOnTheAgent(_GatewayBase):
    PROTECTED_GETS = ("/status", "/cameras/scan", "/protokoll",
                      "/roboter-studio/status", "/cameras/preview?device=/dev/video0",
                      "/does-not-exist")
    PROTECTED_POSTS = ("/scan-arms", "/cameras/roles", "/cameras/preview/stop",
                       "/phone/toggle", "/robot-type", "/environment/start",
                       "/environment/stop", "/update", "/factory-reset",
                       "/roboter-studio/leader-disable", "/roboter-studio/leader-enable")

    def test_every_protected_route_refuses_without_a_session(self):
        self.claim()  # a claimed Pi: the refusal is "login required"
        with patch.object(agent.docker_manager, "stop_robot_tier") as stop, \
                patch.object(agent.docker_manager, "factory_reset") as wipe, \
                patch.object(agent.docker_manager, "start_robot_tier") as start:
            for path in self.PROTECTED_GETS:
                with self.subTest(get=path):
                    status, payload, _ = self.req("GET", path)
                    self.assertEqual(status, 401)
                    self.assertEqual(payload.get("code"), "login_required")
            for path in self.PROTECTED_POSTS:
                with self.subTest(post=path):
                    status, payload, _ = self.req("POST", path, {
                        "confirm": True, "confirm_again": True,
                        "confirm_text": "ZURÜCKSETZEN"})
                    self.assertEqual(status, 401)
        stop.assert_not_called()
        wipe.assert_not_called()
        start.assert_not_called()

    def test_the_written_acceptance_curl_from_another_lan_host(self):
        """`curl -X POST http://<pi>/api/system/environment/stop` from another
        LAN host: no Origin → 403; with a forged own-address Origin but no
        session → 401. The side effect never fires either way."""
        self.claim()
        with patch.object(agent.docker_manager, "stop_robot_tier") as stop:
            status, _, _ = self.req("POST", "/environment/stop", origin=None)
            self.assertEqual(status, 403)
            status, _, _ = self.req("POST", "/environment/stop", origin=self.origin)
            self.assertEqual(status, 401)
        stop.assert_not_called()

    def test_an_unclaimed_pi_says_so(self):
        status, payload, _ = self.req("GET", "/status")
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unclaimed")
        self.assertIn("nicht eingerichtet", payload["message"])

    def test_the_public_routes_answer_without_a_session(self):
        for path in ("/health", "/", "/auth/state"):
            with self.subTest(path=path):
                self.assertEqual(self.req("GET", path)[0], 200)
        with patch.object(agent.update_checker, "check_for_agent_update", return_value=None):
            self.assertEqual(self.req("GET", "/update/check")[0], 200)
        self.assertEqual(self.req("GET", "/update/status/abc")[0], 404)  # job id, not a gate
        with patch.object(agent.AgentApp, "handle_netzwerk_check",
                          return_value=(200, {"checks": []})):
            self.assertEqual(self.req("GET", "/netzwerk-check")[0], 200)

    def test_a_session_opens_the_protected_routes(self):
        status, payload, headers = self.claim()
        self.assertEqual(status, 200, payload)
        cookie = self.cookie_of(headers)
        with patch.object(agent.docker_manager, "get_container_status", return_value={}):
            status, payload, _ = self.req("GET", "/status", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("lan_ip", payload)
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True) as stop:
            status, _, _ = self.req("POST", "/environment/stop", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        stop.assert_called_once()

    def test_an_empty_origin_post_is_refused_even_with_a_session(self):
        _, _, headers = self.claim()
        cookie = self.cookie_of(headers)
        with patch.object(agent.docker_manager, "stop_robot_tier") as stop:
            status, _, _ = self.req("POST", "/environment/stop", headers={"Cookie": cookie},
                                    origin=None)
        self.assertEqual(status, 403)
        stop.assert_not_called()

    def test_auth_check_is_the_cookie_alone(self):
        self.assertEqual(self.req("GET", "/auth/check")[0], 401)
        _, _, headers = self.claim()
        cookie = self.cookie_of(headers)
        status, _, h = self.req("GET", "/auth/check", headers={"Cookie": cookie})
        self.assertEqual(status, 204)
        self.assertEqual(h.get("Cache-Control"), "no-store")
        self.assertEqual(self.req("GET", "/auth/check",
                                  headers={"Cookie": f"{pi_auth.COOKIE_NAME}={'z' * 43}"})[0], 401)


class TestAuthCheckNeverTrustsTheTcpPeer(_GatewayBase):
    trusted = True

    def test_even_a_loopback_peer_needs_a_cookie_for_auth_check(self):
        # nginx always asks from ros_net; the check must not depend on the peer.
        self.assertEqual(self.req("GET", "/auth/check")[0], 401)

    def test_a_loopback_peer_needs_no_session_elsewhere(self):
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True) as stop:
            status, _, _ = self.req("POST", "/environment/stop", origin=None)
        self.assertEqual(status, 200)
        stop.assert_called_once()


class TestClaim(_GatewayBase):
    def test_a_teacher_claims_with_the_code_and_gets_a_session(self):
        status, payload, headers = self.claim("teacher")
        self.assertEqual(status, 200, payload)
        self.assertTrue(self.app._auth.claimed)
        cookie = headers.get("Set-Cookie", "")
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        status, state, _ = self.req("GET", "/auth/state",
                                    headers={"Cookie": self.cookie_of(headers)})
        self.assertTrue(state["claimed"])
        self.assertTrue(state["session"])
        self.assertEqual(state["role"], "teacher")

    def test_a_student_cannot_claim_and_does_not_spend_an_attempt(self):
        code = self.app._auth.mint_code()
        with patch.object(agent.pi_auth, "verify_with_cloud", return_value=("ok", "student")):
            status, payload, _ = self.req(
                "POST", "/auth/claim", {"code": "ABCD-EFGH"},
                {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "role_not_allowed")
        self.assertEqual(self.app._auth._state["code"]["failures"], 0)
        self.assertEqual(self.app._auth.try_claim(code, "teacher"), "ok")

    def test_a_wrong_code_is_refused_and_counted(self):
        code = self.app._auth.mint_code()
        wrong = "ABCDEFGH" if code != "ABCDEFGH" else "HGFEDCBA"
        with patch.object(agent.pi_auth, "verify_with_cloud", return_value=("ok", "admin")):
            status, payload, headers = self.req(
                "POST", "/auth/claim", {"code": wrong}, {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "code_wrong")
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.app._auth._state["code"]["failures"], 1)

    def test_without_a_login_nothing_is_asked(self):
        self.app._auth.mint_code()
        with patch.object(agent.pi_auth, "verify_with_cloud") as cloud:
            status, payload, _ = self.req("POST", "/auth/claim", {"code": "ABCD-EFGH"})
        self.assertEqual(status, 401)
        cloud.assert_not_called()

    def test_a_cloud_that_cannot_be_asked_spends_nothing(self):
        self.app._auth.mint_code()
        with patch.object(agent.pi_auth, "verify_with_cloud",
                          return_value=("unreachable", None)):
            status, payload, _ = self.req(
                "POST", "/auth/claim", {"code": "ABCD-EFGH"},
                {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "cloud_unreachable")
        self.assertEqual(self.app._auth._state["code"]["failures"], 0)

    def test_claims_are_rate_limited_per_client(self):
        self.app._auth.mint_code()
        with patch.object(agent.pi_auth, "verify_with_cloud", return_value=("ok", "student")):
            codes = [self.req("POST", "/auth/claim", {"code": "ABCD-EFGH"},
                              {"Authorization": f"Bearer {TOKEN}"})[0]
                     for _ in range(agent._CLAIM_RATE_PER_CLIENT[0] + 2)]
        self.assertEqual(codes[-1], 429)

    def test_no_code_and_a_void_code_name_the_cli(self):
        with patch.object(agent.pi_auth, "verify_with_cloud", return_value=("ok", "teacher")):
            status, payload, _ = self.req("POST", "/auth/claim", {"code": "ABCD-EFGH"},
                                          {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 409)
        self.assertIn("pi_agent.setup_code", payload["message"])

    def test_a_claimed_pi_refuses_another_claim(self):
        self.claim()
        with patch.object(agent.pi_auth, "verify_with_cloud", return_value=("ok", "teacher")):
            status, payload, _ = self.req("POST", "/auth/claim", {"code": "ABCD-EFGH"},
                                          {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 409)


class TestExchange(_GatewayBase):
    def exchange(self, verdict=("ok", "student"), cookie=None):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if cookie:
            headers["Cookie"] = cookie
        with patch.object(agent.pi_auth, "verify_with_cloud", return_value=verdict) as cloud:
            result = self.req("POST", "/auth/session", {}, headers)
        return result, cloud

    def test_any_role_gets_a_session_on_a_claimed_pi(self):
        self.claim()
        for role in ("student", "teacher", "admin"):
            (status, payload, headers), cloud = self.exchange(("ok", role))
            self.assertEqual(status, 200)
            self.assertEqual(payload["role"], role)
            self.assertLessEqual(payload["expires_in"], pi_auth.SESSION_TTL_S)
            cloud.assert_called_once_with(TOKEN, agent.UPDATE_API_URL)
            self.assertEqual(self.req("GET", "/auth/check", headers={
                "Cookie": self.cookie_of(headers)})[0], 204)

    def test_an_unclaimed_pi_gives_no_session(self):
        (status, payload, headers), cloud = self.exchange()
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unclaimed")
        cloud.assert_not_called()

    def test_rejected_and_unreachable(self):
        self.claim()
        (status, payload, _), _ = self.exchange(("rejected", None))
        self.assertEqual((status, payload["code"]), (401, "token_rejected"))
        (status, payload, _), _ = self.exchange(("unreachable", None))
        self.assertEqual((status, payload["code"]), (503, "cloud_unreachable"))

    def test_a_new_exchange_rotates_the_old_session_out(self):
        self.claim()
        (_, _, h1), _ = self.exchange()
        old = self.cookie_of(h1)
        (_, _, h2), _ = self.exchange(cookie=old)
        self.assertEqual(self.req("GET", "/auth/check", headers={"Cookie": old})[0], 401)
        self.assertEqual(self.req("GET", "/auth/check",
                                  headers={"Cookie": self.cookie_of(h2)})[0], 204)

    def test_logout_ends_the_session_and_clears_the_cookie(self):
        self.claim()
        (_, _, h), _ = self.exchange()
        cookie = self.cookie_of(h)
        status, _, headers = self.req("POST", "/auth/logout", {}, {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))
        self.assertEqual(self.req("GET", "/auth/check", headers={"Cookie": cookie})[0], 401)

    def test_exchanges_are_rate_limited(self):
        self.claim()
        statuses = [self.exchange()[0][0]
                    for _ in range(agent._SESSION_RATE_PER_CLIENT[0] + 1)]
        self.assertEqual(statuses[-1], 429)

    def test_a_missing_token_never_reaches_the_cloud(self):
        self.claim()
        with patch.object(agent.pi_auth, "verify_with_cloud") as cloud:
            status, payload, _ = self.req("POST", "/auth/session", {})
        self.assertEqual(status, 401)
        cloud.assert_not_called()

    def test_no_secret_reaches_a_log_line(self):
        """Never log a session id, a setup code or a Supabase token."""
        code = self.app._auth.mint_code()
        # Every logger of the process at DEBUG, captured at the root.
        with self.assertLogs(level="DEBUG") as logs:
            with patch.object(agent.pi_auth, "verify_with_cloud",
                              return_value=("ok", "teacher")):
                _, _, h = self.req("POST", "/auth/claim", {"code": code},
                                   {"Authorization": f"Bearer {TOKEN}"})
            sid = self.cookie_of(h).split("=", 1)[1]
            (_, _, h2), _ = self.exchange()
            sid2 = self.cookie_of(h2).split("=", 1)[1]
        text = "\n".join(logs.output)
        self.assertIn("Roboter eingerichtet", text)  # the capture works
        for secret in (TOKEN, code, pi_auth.format_code(code), sid, sid2):
            self.assertNotIn(secret, text)


class TestXRealIp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp(env_file=os.path.join(self.dir, ".env"),
                                  auth_file=os.path.join(self.dir, "a.json"))

    def test_x_real_ip_is_believed_only_from_ros_net(self):
        self.assertEqual(self.app.client_ip("172.28.0.4", "192.168.1.20"), "192.168.1.20")
        # A peer that reached the gateway listener directly (a static route).
        self.assertEqual(self.app.client_ip("192.168.1.66", "10.9.9.9"), "192.168.1.66")
        self.assertEqual(self.app.client_ip("172.28.0.4", "not-an-ip"), "172.28.0.4")

    def test_peer_is_trusted_means_loopback(self):
        self.assertTrue(agent.AgentApp.peer_is_trusted("127.0.0.1"))
        self.assertTrue(agent.AgentApp.peer_is_trusted("::1"))
        for peer in ("172.28.0.4", "192.168.1.20", "", "x"):
            self.assertFalse(agent.AgentApp.peer_is_trusted(peer), peer)

    def test_an_empty_origin_follows_the_peer(self):
        self.assertTrue(self.app.origin_allowed("", allow_empty=True))
        self.assertFalse(self.app.origin_allowed("", allow_empty=False))
        self.assertFalse(self.app.origin_allowed("http://evil.com", allow_empty=True))


# ── setup code: the loopback route, the CLI, the boot mint ───────────────────


class TestSetupCodeRoute(_GatewayBase):
    def test_the_route_exists_only_for_a_process_on_the_pi(self):
        status, _, _ = self.req("POST", "/auth/setup-code", {})
        self.assertEqual(status, 404)
        self.assertEqual(self.app._auth.code_status(), "none")


class TestSetupCodeRouteOnLoopback(_GatewayBase):
    trusted = True

    def test_mint_then_claimed(self):
        status, payload, _ = self.req("POST", "/auth/setup-code", {}, origin=None)
        self.assertEqual(status, 200)
        code = payload["setup_code"]
        self.assertEqual(self.app._auth.try_claim(code, "teacher"), "ok")
        status, payload, _ = self.req("POST", "/auth/setup-code", {}, origin=None)
        self.assertEqual(status, 409)

    def test_the_cli_prints_the_code(self):
        url = self.base + "/auth/setup-code"
        out = io.StringIO()
        with patch.object(setup_code_cli, "_URL", url), redirect_stdout(out):
            self.assertEqual(setup_code_cli.main(["--label"]), 0)
        text = out.getvalue()
        self.assertRegex(text, r"Einrichtungscode:\s+[A-Z0-9]{4}-[A-Z0-9]{4}")
        self.assertIn("nur jetzt angezeigt", text)
        code = text.split("Einrichtungscode:")[2].split()[0] if text.count(
            "Einrichtungscode:") > 1 else text.split("Einrichtungscode:")[1].split()[0]
        self.assertEqual(self.app._auth.try_claim(code, "teacher"), "ok")
        out = io.StringIO()
        with patch.object(setup_code_cli, "_URL", url), redirect_stdout(out):
            self.assertEqual(setup_code_cli.main([]), 3)
        self.assertIn("bereits eingerichtet", out.getvalue())

    def test_the_cli_says_when_the_agent_is_down(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        err = io.StringIO()
        with patch.object(setup_code_cli, "_URL", f"http://127.0.0.1:{closed}/auth/setup-code"), \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(setup_code_cli.main([]), 1)
        self.assertIn("antwortet nicht", err.getvalue())


class TestBootMint(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp(env_file=os.path.join(self.dir, ".env"),
                                  auth_file=os.path.join(self.dir, "a.json"))
        self.app._auth = pi_auth.AuthStore(os.path.join(self.dir, "a.json"), iterations=FAST)
        self.console = os.path.join(self.dir, "tty1")

    def test_an_unclaimed_pi_without_a_code_gets_one_shown_on_the_console(self):
        with patch.dict(os.environ, {"EDUBOTICS_PI_CONSOLE": self.console}), \
                self.assertLogs("edubotics-pi-agent", level="INFO") as logs:
            self.app._ensure_setup_code_at_boot()
        shown = Path(self.console).read_text(encoding="utf-8")
        self.assertRegex(shown, r"Einrichtungscode:\s+[A-Z0-9]{4}-[A-Z0-9]{4}")
        code = shown.split("Einrichtungscode:")[1].split()[0]
        logged = "\n".join(logs.output)
        self.assertIn("nicht eingerichtet", logged)
        self.assertNotIn(code, logged)
        self.assertNotIn(code.replace("-", ""), logged)
        self.assertEqual(self.app._auth.try_claim(code, "teacher"), "ok")

    def test_an_existing_code_is_never_rotated_at_boot(self):
        code = self.app._auth.mint_code()
        with patch.dict(os.environ, {"EDUBOTICS_PI_CONSOLE": self.console}):
            self.app._ensure_setup_code_at_boot()
        self.assertFalse(os.path.exists(self.console))
        self.assertEqual(self.app._auth.try_claim(code, "teacher"), "ok")

    def test_a_claimed_pi_mints_nothing_at_boot(self):
        self.app._auth.try_claim(self.app._auth.mint_code(), "teacher")
        with patch.dict(os.environ, {"EDUBOTICS_PI_CONSOLE": self.console}):
            self.app._ensure_setup_code_at_boot()
        self.assertFalse(os.path.exists(self.console))


# ── the manager's nginx: measured, never assumed (version skew) ──────────────


class _NginxStub(http.server.BaseHTTPRequestHandler):
    statuses: dict = {}

    def do_GET(self):  # noqa: N802
        status = self.statuses.get(self.path.split("?")[0], 404)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class TestManagerLoginProbe(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp(env_file=os.path.join(self.dir, ".env"),
                                  auth_file=os.path.join(self.dir, "a.json"))
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _NginxStub)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        p = patch.object(agent, "_MANAGER_PROBE_BASE",
                         f"http://127.0.0.1:{self.srv.server_address[1]}")
        p.start()
        self.addCleanup(p.stop)

    def _set(self, rosbridge, video, daten):
        _NginxStub.statuses = {"/rosbridge": rosbridge, "/video/stream": video,
                               "/daten-api/v1/health": daten}

    def test_the_current_config_answers_401_everywhere(self):
        self._set(401, 401, 401)
        self.assertEqual(self.app._manager_login_state(), "enforced")

    def test_an_old_config_proxies_straight_through(self):
        for statuses in ((502, 502, 502), (400, 200, 200), (401, 401, 502), (401, 200, 401)):
            with self.subTest(statuses=statuses):
                self._set(*statuses)
                self.assertEqual(self.app._manager_login_state(), "open")

    def test_a_failing_auth_subrequest_is_not_proof_either_way(self):
        self._set(500, 401, 401)
        self.assertEqual(self.app._manager_login_state(), "unknown")

    def test_nothing_listening_is_unknown(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        with patch.object(agent, "_MANAGER_PROBE_BASE", f"http://127.0.0.1:{closed}"):
            self.assertEqual(self.app._manager_login_state(), "unknown")


class TestEnvironmentStartNeedsProof(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp(env_file=os.path.join(self.dir, ".env"),
                                  auth_file=os.path.join(self.dir, "a.json"))
        cg = agent.config_generator
        self.app._hardware = cg.HardwareConfig(
            leader=cg.ArmDevice(serial_path="/dev/serial/by-id/L", role="leader"),
            follower=cg.ArmDevice(serial_path="/dev/serial/by-id/F", role="follower"),
            cameras=[cg.CameraDevice(path="/dev/video0", role="scene")],
        )
        p = patch.object(agent.AgentApp, "_hardware_ready", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_an_outdated_manager_never_gets_a_robot_tier(self):
        for state, expected in (("open", 409), ("unknown", 503)):
            with self.subTest(state=state), \
                    patch.object(agent.AgentApp, "_manager_login_state", return_value=state), \
                    patch.object(agent.docker_manager, "start_robot_tier") as start:
                code, payload = self.app.handle_environment_start({})
                self.assertEqual(code, expected)
                self.assertFalse(payload["ok"])
                start.assert_not_called()

    def test_a_proven_gate_starts_as_before(self):
        with patch.object(agent.AgentApp, "_manager_login_state", return_value="enforced"), \
                patch.object(agent.config_generator, "generate_env_file"), \
                patch.object(agent.docker_manager, "start_robot_tier", return_value=True) as start:
            code, _ = self.app.handle_environment_start({})
        self.assertEqual(code, 200)
        start.assert_called_once()


class TestLoginGuard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = agent.AgentApp(env_file=os.path.join(self.dir, ".env"),
                                  auth_file=os.path.join(self.dir, "a.json"))

    def _tick(self, state, tier="running"):
        self.logged = []
        logger = logging.getLogger("edubotics-pi-agent")
        grab = logging.Handler()
        grab.emit = lambda record: self.logged.append(record.getMessage())
        logger.addHandler(grab)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            with patch.object(agent.AgentApp, "_manager_login_state", return_value=state), \
                    patch.object(agent.docker_manager, "get_container_status",
                                 return_value={"physical_ai_server": tier}), \
                    patch.object(agent.docker_manager, "stop_robot_tier") as stop, \
                    patch.object(agent.docker_manager, "check_for_updates") as pull, \
                    patch.object(agent.docker_manager, "start_manager") as recreate:
                self.app._login_guard_tick()
        finally:
            logger.removeHandler(grab)
            logger.setLevel(old_level)
        return stop, pull, recreate

    def test_an_open_manager_stops_the_tier_and_heals_with_backoff(self):
        stop, pull, recreate = self._tick("open")
        stop.assert_called_once()
        pull.assert_called_once()
        recreate.assert_called_once()
        self.assertTrue(any("[FEHLER]" in line for line in self.logged))
        # Within the backoff: the tier is still kept down, but no second heal.
        stop, pull, recreate = self._tick("open")
        stop.assert_called_once()
        pull.assert_not_called()
        recreate.assert_not_called()

    def test_unknown_and_enforced_do_nothing(self):
        for state in ("unknown", "enforced"):
            stop, pull, recreate = self._tick(state)
            stop.assert_not_called()
            pull.assert_not_called()
            recreate.assert_not_called()

    def test_a_heal_waits_for_a_running_update(self):
        self.app._update_busy = True
        stop, pull, recreate = self._tick("open", tier="exited")
        stop.assert_not_called()
        pull.assert_not_called()


if __name__ == "__main__":
    unittest.main()
