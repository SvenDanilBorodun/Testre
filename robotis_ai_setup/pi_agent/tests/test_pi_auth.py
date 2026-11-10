"""Deps-free tests for the Orange Pi's robot login (pi_agent.pi_auth + the
agent's /auth routes, its session gate, the factory-reset confirmation, the
setup-code CLI and the login guard).

The servers here bind 127.0.0.1, so their TCP peer is loopback. A browser never
is: it reaches the agent through nginx on ros_net. ``_GatewayBase`` therefore
patches ``AgentApp.peer_is_trusted`` to False to stand in for the gateway
listener, and ``peer_is_trusted`` itself is tested on real addresses.

No cloud is contacted: ``redeem_with_cloud`` runs against a local stub server,
and the agent routes patch it. Since SA-06 (2026-11-13) the browser sends only a
one-time TICKET in the body; no request to the Pi carries a Supabase token.
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


# A one-time login ticket's shape (secrets.token_urlsafe(32)); a distinctive
# value so the no-secret-in-the-log test can look for it.
TICKET = ("tk-Ticket_" + "0123456789abcdefghijklmnopqrstuvwxyz")[:43]
GRANT = 900  # the cloud's session_max_s in the patched redeems


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


class TestTicketHelpers(unittest.TestCase):
    def test_a_ticket_has_the_shape_of_secrets_token_urlsafe_32(self):
        self.assertEqual(len(TICKET), 43)
        self.assertEqual(pi_auth.ticket_from(TICKET), TICKET)
        for bad in (None, "", 7, TICKET[:-1], TICKET + "x", "a.b.c",
                    "Bearer " + TICKET, TICKET[:-1] + "!", ["x"]):
            self.assertIsNone(pi_auth.ticket_from(bad), bad)

    def test_a_session_never_outlives_the_window_or_the_login(self):
        self.assertEqual(pi_auth.session_ttl(3600), pi_auth.SESSION_TTL_S)
        self.assertEqual(pi_auth.session_ttl(pi_auth.SESSION_TTL_S), pi_auth.SESSION_TTL_S)
        self.assertEqual(pi_auth.session_ttl(120), 120)
        # Nearly expired: floored, the cloud just accepted the login.
        self.assertEqual(pi_auth.session_ttl(3), pi_auth.SESSION_MIN_TTL_S)

    def test_the_pi_id_shape(self):
        pid = pi_auth.generate_pi_id()
        self.assertTrue(pi_auth.valid_pi_id(pid))
        self.assertEqual(len(pid), 22)
        self.assertNotEqual(pid, pi_auth.generate_pi_id())
        for bad in (None, "", "short", "a" * 65, "x y" * 8, 7):
            self.assertFalse(pi_auth.valid_pi_id(bad), bad)


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

    def test_one_account_holds_at_most_its_cap_and_loses_its_own_least_recently_used(self):
        """VA-09 (2026-11-12): any signed-in account may use a claimed Pi, and
        before this cap 512 exchanges of ONE account pushed every other
        session out. A new session of an account at its cap now evicts that
        account's own least recently USED session."""
        self._claimed()
        cap = pi_auth.MAX_SESSIONS_PER_ACCOUNT
        sids = []
        for _ in range(cap):
            self.clock.t += 1
            sids.append(self.store.create_session("student", 600, account=ACCOUNT_A))
        self.clock.t += 1
        self.assertIsNotNone(self.store.session(sids[0]))     # the oldest is still in use
        self.clock.t += 1
        newest = self.store.create_session("student", 600, account=ACCOUNT_A)
        self.assertEqual(self.store.session_count(), cap)
        self.assertIsNone(self.store.session(sids[1]), "the least recently used goes")
        self.assertIsNotNone(self.store.session(sids[0]))
        self.assertIsNotNone(self.store.session(newest))

    def test_one_account_cannot_evict_a_class(self):
        """The verifier's scenario: a class of 30, then one account exchanging
        far beyond the global cap. Every student keeps their session."""
        self._claimed()
        students = []
        for i in range(30):
            self.clock.t += 1
            students.append(self.store.create_session("student", 600, account=f"acct-{i}"))
        for _ in range(pi_auth.MAX_SESSIONS + 50):
            self.clock.t += 0.01
            self.store.create_session("student", 600, account=ACCOUNT_A)
        self.assertEqual(self.store.session_count(), 30 + pi_auth.MAX_SESSIONS_PER_ACCOUNT)
        for sid in students:
            self.assertIsNotNone(self.store.session(sid))

    def test_at_the_global_cap_the_busiest_account_pays(self):
        self._claimed()
        with patch.object(pi_auth, "MAX_SESSIONS", 6), patch.object(pi_auth, "MAX_SESSIONS_PER_ACCOUNT", 4):
            a = []
            for _ in range(4):
                self.clock.t += 1
                a.append(self.store.create_session("student", 600, account=ACCOUNT_A))
            self.clock.t += 1
            b = [self.store.create_session("student", 600, account=ACCOUNT_B) for _ in range(2)]
            self.clock.t += 1
            c = self.store.create_session("student", 600, account="account-c")   # at the cap: A has the most
            self.assertEqual(self.store.session_count(), 6)
            self.assertIsNone(self.store.session(a[0]))
            self.clock.t += 1
            b.append(self.store.create_session("student", 600, account=ACCOUNT_B))   # A (3) still has the most
            self.assertIsNone(self.store.session(a[1]))
            for sid in b + [c, a[2], a[3]]:
                self.assertIsNotNone(self.store.session(sid))
            # never a session of an account with fewer: A and B now hold 2 and 3
            self.clock.t += 1
            self.store.create_session("student", 600, account="account-d")
            self.assertEqual(sum(self.store.session(s) is not None for s in b), 2)
            self.assertIsNotNone(self.store.session(a[2]))
            self.assertIsNotNone(self.store.session(a[3]))
            self.assertIsNotNone(self.store.session(c))

    def test_the_account_is_kept_as_a_hash_only(self):
        self._claimed()
        self.store.create_session("student", 600, account=ACCOUNT_A)
        self.assertNotIn(ACCOUNT_A, repr(self.store._sessions))

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


ACCOUNT_A = "0b3b8e1c-0000-4000-8000-00000000000a"
ACCOUNT_B = "0b3b8e1c-0000-4000-8000-00000000000b"


class _CloudStub(http.server.BaseHTTPRequestHandler):
    status = 200
    body = (b'{"user_id": "6f1c2a3e-1b2c-4d5e-8f90-123456789abc", "role": "student", '
            b'"session_max_s": 600}')
    location = None
    seen = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        sent = self.rfile.read(length)
        type(self).seen.append((self.path, self.headers.get("Authorization"),
                                self.headers.get("Content-Type"), sent))
        self.send_response(self.status)
        if self.location:
            self.send_header("Location", self.location)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


PI_ID = "q8V0f2Lr3nC1sZtA9xY7wB"


class TestRedeemWithCloud(unittest.TestCase):
    def setUp(self):
        _CloudStub.status, _CloudStub.location = 200, None
        _CloudStub.body = (b'{"user_id": "6f1c2a3e-1b2c-4d5e-8f90-123456789abc", '
                           b'"role": "student", "session_max_s": 600}')
        _CloudStub.seen = []
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _CloudStub)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def test_ok_names_role_account_and_session_and_sends_only_the_ticket(self):
        self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, self.url),
                         ("ok", "student", "6f1c2a3e-1b2c-4d5e-8f90-123456789abc", 600))
        (path, authorization, ctype, sent), = _CloudStub.seen
        self.assertEqual(path, "/pi/tickets/redeem")
        self.assertIsNone(authorization)  # nothing but the ticket leaves the Pi
        self.assertEqual(ctype, "application/json")
        self.assertEqual(json.loads(sent), {"ticket": TICKET, "pi_id": PI_ID})

    def test_the_account_is_what_the_cloud_names_or_nothing(self):
        """VA-09: the per-account session cap keys on the cloud's own answer
        (the redeem's ``user_id``), never on anything the browser sends."""
        for uid in (None, "u-1", 7, ""):
            with self.subTest(uid=uid):
                _CloudStub.body = json.dumps({"user_id": uid, "role": "student",
                                              "session_max_s": 600}).encode()
                self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, self.url),
                                 ("ok", "student", None, 600))
        _CloudStub.body = (b'{"user_id": "6F1C2A3E-1B2C-4D5E-8F90-123456789ABC", '
                           b'"role": "teacher", "session_max_s": 900}')
        self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, self.url),
                         ("ok", "teacher", "6f1c2a3e-1b2c-4d5e-8f90-123456789abc", 900))

    def test_the_cloud_refusing_the_ticket_is_rejected(self):
        for status in (401, 403):
            _CloudStub.status = status
            self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, self.url),
                             ("rejected", None, None, None))

    def test_an_unknown_role_is_rejected(self):
        _CloudStub.body = b'{"user_id": null, "role": "superuser", "session_max_s": 600}'
        self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, self.url),
                         ("rejected", None, None, None))

    def test_could_not_ask_is_unreachable(self):
        for status in (404, 429, 500, 502, 503):
            _CloudStub.status = status
            self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, self.url),
                             ("unreachable", None, None, None))
        _CloudStub.status = 200
        for body in (b"<html>portal</html>", b"[]",
                     b'{"role": "student"}',
                     b'{"role": "student", "session_max_s": 0}',
                     b'{"role": "student", "session_max_s": true}',
                     b'{"role": "student", "session_max_s": 999999}'):
            with self.subTest(body=body):
                _CloudStub.body = body
                self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, self.url),
                                 ("unreachable", None, None, None))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, f"http://127.0.0.1:{closed}",
                                                   timeout=2),
                         ("unreachable", None, None, None))

    def test_a_redirect_is_never_followed_with_the_ticket(self):
        """A captive portal answers with a 3xx; following it would hand the
        ticket to the portal."""
        _CloudStub.status = 307
        _CloudStub.location = self.url + "/portal"
        self.assertEqual(pi_auth.redeem_with_cloud(TICKET, PI_ID, self.url),
                         ("unreachable", None, None, None))
        self.assertEqual([seen[0] for seen in _CloudStub.seen], ["/pi/tickets/redeem"])


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
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=("ok", role, None, GRANT)):
            status, payload, headers = self.req(
                "POST", "/auth/claim", {"code": pi_auth.format_code(code), "ticket": TICKET})
        return status, payload, headers

    def session_cookie(self, role):
        """A claimed Pi plus a robot session of ``role``."""
        if not self.app._auth.claimed:
            self.claim()
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=("ok", role, None, GRANT)):
            status, _, headers = self.req("POST", "/auth/session", {"ticket": TICKET})
        self.assertEqual(status, 200)
        return self.cookie_of(headers)

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
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=("ok", "student", None, GRANT)):
            status, payload, _ = self.req(
                "POST", "/auth/claim", {"code": "ABCD-EFGH", "ticket": TICKET})
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "role_not_allowed")
        self.assertEqual(self.app._auth._state["code"]["failures"], 0)
        self.assertEqual(self.app._auth.try_claim(code, "teacher"), "ok")

    def test_a_wrong_code_is_refused_and_counted(self):
        code = self.app._auth.mint_code()
        wrong = "ABCDEFGH" if code != "ABCDEFGH" else "HGFEDCBA"
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=("ok", "admin", None, GRANT)):
            status, payload, headers = self.req(
                "POST", "/auth/claim", {"code": wrong, "ticket": TICKET})
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "code_wrong")
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.app._auth._state["code"]["failures"], 1)

    def test_without_a_login_nothing_is_asked(self):
        self.app._auth.mint_code()
        with patch.object(agent.pi_auth, "redeem_with_cloud") as cloud:
            status, payload, _ = self.req("POST", "/auth/claim", {"code": "ABCD-EFGH"})
        self.assertEqual(status, 401)
        cloud.assert_not_called()

    def test_a_cloud_that_cannot_be_asked_spends_nothing(self):
        self.app._auth.mint_code()
        with patch.object(agent.pi_auth, "redeem_with_cloud",
                          return_value=("unreachable", None, None, None)):
            status, payload, _ = self.req(
                "POST", "/auth/claim", {"code": "ABCD-EFGH", "ticket": TICKET})
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "cloud_unreachable")
        self.assertEqual(self.app._auth._state["code"]["failures"], 0)

    def test_claims_are_rate_limited_per_client(self):
        self.app._auth.mint_code()
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=("ok", "student", None, GRANT)):
            codes = [self.req("POST", "/auth/claim", {"code": "ABCD-EFGH", "ticket": TICKET})[0]
                     for _ in range(agent._CLAIM_RATE_PER_CLIENT[0] + 2)]
        self.assertEqual(codes[-1], 429)

    def test_no_code_and_a_void_code_name_the_cli(self):
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=("ok", "teacher", None, GRANT)):
            status, payload, _ = self.req("POST", "/auth/claim",
                                          {"code": "ABCD-EFGH", "ticket": TICKET})
        self.assertEqual(status, 409)
        self.assertIn("pi_agent.setup_code", payload["message"])

    def test_a_claimed_pi_refuses_another_claim(self):
        self.claim()
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=("ok", "teacher", None, GRANT)):
            status, payload, _ = self.req("POST", "/auth/claim",
                                          {"code": "ABCD-EFGH", "ticket": TICKET})
        self.assertEqual(status, 409)


class TestExchange(_GatewayBase):
    def exchange(self, verdict=("ok", "student", None, GRANT), cookie=None):
        headers = {"Cookie": cookie} if cookie else {}
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=verdict) as cloud:
            result = self.req("POST", "/auth/session", {"ticket": TICKET}, headers)
        return result, cloud

    def test_any_role_gets_a_session_on_a_claimed_pi(self):
        self.claim()
        for role in ("student", "teacher", "admin"):
            (status, payload, headers), cloud = self.exchange(("ok", role, None, GRANT))
            self.assertEqual(status, 200)
            self.assertEqual(payload["role"], role)
            self.assertLessEqual(payload["expires_in"], pi_auth.SESSION_TTL_S)
            cloud.assert_called_once_with(TICKET, self.app._auth.pi_id(), agent.UPDATE_API_URL)
            self.assertEqual(self.req("GET", "/auth/check", headers={
                "Cookie": self.cookie_of(headers)})[0], 204)

    def test_an_unclaimed_pi_gives_no_session(self):
        (status, payload, headers), cloud = self.exchange()
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unclaimed")
        cloud.assert_not_called()

    def test_rejected_and_unreachable(self):
        self.claim()
        (status, payload, _), _ = self.exchange(("rejected", None, None, None))
        self.assertEqual((status, payload["code"]), (401, "ticket_rejected"))
        (status, payload, _), _ = self.exchange(("unreachable", None, None, None))
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

    def test_one_account_holds_at_most_its_cap_of_sessions(self):
        """VA-09: the account comes from the cloud's answer (patched here as
        the verdict's third element), never from the request."""
        self.claim()
        cap = pi_auth.MAX_SESSIONS_PER_ACCOUNT
        cookies = [self.cookie_of(self.exchange(("ok", "student", ACCOUNT_A, GRANT))[0][2])
                   for _ in range(cap + 3)]
        (_, _, other), _ = self.exchange(("ok", "student", ACCOUNT_B, GRANT))
        # the claim's session (no account) + A's cap + B's one
        self.assertEqual(self.app._auth.session_count(), 1 + cap + 1)
        checks = [self.req("GET", "/auth/check", headers={"Cookie": c})[0] for c in cookies]
        self.assertEqual(checks, [401] * 3 + [204] * cap)
        self.assertEqual(self.req("GET", "/auth/check", headers={"Cookie": self.cookie_of(other)})[0], 204)

    def test_exchanges_are_rate_limited(self):
        self.claim()
        statuses = [self.exchange()[0][0]
                    for _ in range(agent._SESSION_RATE_PER_CLIENT[0] + 1)]
        self.assertEqual(statuses[-1], 429)

    def test_a_missing_ticket_never_reaches_the_cloud(self):
        self.claim()
        with patch.object(agent.pi_auth, "redeem_with_cloud") as cloud:
            for body in ({}, {"ticket": "short"}, {"ticket": 7}, [TICKET]):
                status, payload, _ = self.req("POST", "/auth/session", body)
                self.assertEqual(status, 401)
                self.assertEqual(payload["code"], "ticket_missing")
        cloud.assert_not_called()

    def test_a_bearer_header_opens_nothing_any_more(self):
        """SA-06: the old exchange (the Supabase token as a Bearer header) is
        gone; a request that sends one and no ticket gets no session and never
        reaches the cloud."""
        self.claim()
        with patch.object(agent.pi_auth, "redeem_with_cloud") as cloud:
            status, payload, headers = self.req(
                "POST", "/auth/session", {}, {"Authorization": "Bearer a.b.c"})
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        cloud.assert_not_called()

    def test_no_secret_reaches_a_log_line(self):
        """Never log a session id, a setup code or a Supabase token."""
        code = self.app._auth.mint_code()
        # Every logger of the process at DEBUG, captured at the root.
        with self.assertLogs(level="DEBUG") as logs:
            with patch.object(agent.pi_auth, "redeem_with_cloud",
                              return_value=("ok", "teacher", None, GRANT)):
                _, _, h = self.req("POST", "/auth/claim", {"code": code, "ticket": TICKET})
            sid = self.cookie_of(h).split("=", 1)[1]
            (_, _, h2), _ = self.exchange()
            sid2 = self.cookie_of(h2).split("=", 1)[1]
        text = "\n".join(logs.output)
        self.assertIn("Roboter eingerichtet", text)  # the capture works
        for secret in (TICKET, code, pi_auth.format_code(code), sid, sid2):
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


# ── SA-06: the Pi's id a ticket is bound to ──────────────────────────────────


class TestPiId(_StoreBase):
    def test_minted_once_persisted_and_stable(self):
        first = self.store.pi_id()
        self.assertTrue(pi_auth.valid_pi_id(first))
        self.assertEqual(self.store.pi_id(), first)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pi_id"], first)
        again = pi_auth.AuthStore(self.path, iterations=FAST)
        self.assertEqual(again.pi_id(), first)

    def test_a_factory_reset_forgets_it(self):
        first = self.store.pi_id()
        self.store.try_claim(self.store.mint_code(), "teacher")
        self.store.reset_to_unclaimed()
        self.assertNotEqual(self.store.pi_id(), first)

    def test_a_malformed_stored_id_is_replaced(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"claimed": False, "pi_id": "x"}, f)
        store = pi_auth.AuthStore(self.path, iterations=FAST)
        self.assertTrue(pi_auth.valid_pi_id(store.pi_id()))


class TestPiIdOnTheWire(_GatewayBase):
    def test_auth_state_answers_the_pi_id_publicly(self):
        status, state, _ = self.req("GET", "/auth/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["pi_id"], self.app._auth.pi_id())
        self.claim()
        status, state, _ = self.req("GET", "/auth/state")
        self.assertEqual(state["pi_id"], self.app._auth.pi_id())

    def test_the_redeem_names_this_pi(self):
        self.claim()
        with patch.object(agent.pi_auth, "redeem_with_cloud",
                          return_value=("ok", "student", None, GRANT)) as cloud:
            self.req("POST", "/auth/session", {"ticket": TICKET})
        cloud.assert_called_once_with(TICKET, self.app._auth.pi_id(), agent.UPDATE_API_URL)

    def test_the_session_is_bound_by_the_clouds_grant(self):
        self.claim()
        with patch.object(agent.pi_auth, "redeem_with_cloud",
                          return_value=("ok", "student", None, 120)):
            status, payload, headers = self.req("POST", "/auth/session", {"ticket": TICKET})
        self.assertEqual(status, 200)
        self.assertEqual(payload["expires_in"], 120)
        self.assertIn("Max-Age=120", headers.get("Set-Cookie", ""))


# ── SA-12: maintenance routes need a teacher's or an admin's session ─────────


class TestMaintenanceRoutes(_GatewayBase):
    RESET = {"confirm": True, "confirm_again": True, "confirm_text": "ZURÜCKSETZEN"}

    def test_the_table(self):
        self.assertEqual(agent._MAINTENANCE_POST_PATHS, frozenset({"/factory-reset", "/robot-type"}))
        self.assertEqual(agent._MAINTENANCE_ROLES, frozenset({"teacher", "admin"}))

    def test_a_student_session_cannot_reset_or_change_the_robot_type(self):
        cookie = self.session_cookie("student")
        with patch.object(agent.docker_manager, "factory_reset") as wipe, \
                patch.object(agent.config_generator, "upsert_env_var") as write:
            for path, body in (("/factory-reset", self.RESET),
                               ("/robot-type", {"robot_type": "omx_follower"})):
                with self.subTest(path=path):
                    status, payload, _ = self.req("POST", path, body, {"Cookie": cookie})
                    self.assertEqual(status, 403)
                    self.assertEqual(payload["code"], "role_not_allowed")
                    self.assertIn("Lehrkraft oder ein Admin", payload["message"])
        wipe.assert_not_called()
        write.assert_not_called()
        self.assertTrue(self.app._auth.claimed)

    def test_a_student_session_still_runs_a_lesson(self):
        cookie = self.session_cookie("student")
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True) as stop:
            status, _, _ = self.req("POST", "/environment/stop", {}, {"Cookie": cookie})
        self.assertEqual(status, 200)
        stop.assert_called_once()
        with patch.object(agent.AgentApp, "handle_update_start",
                          return_value=(202, {"ok": True, "job_id": "j"})) as update:
            status, _, _ = self.req("POST", "/update", {}, {"Cookie": cookie})
        self.assertEqual(status, 202)
        update.assert_called_once()
        with patch.object(agent.AgentApp, "handle_scan_arms",
                          return_value=(200, {"ok": True})) as scan:
            self.assertEqual(self.req("POST", "/scan-arms", {}, {"Cookie": cookie})[0], 200)
        scan.assert_called_once()

    def test_a_teacher_or_admin_session_resets_and_gets_the_new_code_and_the_console_too(self):
        for role in ("teacher", "admin"):
            with self.subTest(role=role):
                cookie = self.session_cookie(role)
                console = os.path.join(self.dir, f"tty-{role}")
                with patch.dict(os.environ, {"EDUBOTICS_PI_CONSOLE": console}), \
                        patch.object(agent.docker_manager, "factory_reset",
                                     return_value=(True, "Gelöscht.")):
                    status, payload, _ = self.req("POST", "/factory-reset", self.RESET,
                                                  {"Cookie": cookie})
                self.assertEqual(status, 200, payload)
                self.assertRegex(payload["setup_code"], r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
                shown = Path(console).read_text(encoding="utf-8")
                self.assertIn(payload["setup_code"], shown)
                self.assertFalse(self.app._auth.claimed)

    def test_a_teacher_may_change_the_robot_type(self):
        cookie = self.session_cookie("teacher")
        with patch.object(agent.AgentApp, "handle_set_robot_type",
                          return_value=(200, {"ok": True})) as handler:
            status, _, _ = self.req("POST", "/robot-type", {"robot_type": "omx_full"},
                                    {"Cookie": cookie})
        self.assertEqual(status, 200)
        handler.assert_called_once()


class TestMaintenanceOnLoopback(_GatewayBase):
    """A root process on the Pi (the loopback peer) needs no session at all."""

    trusted = True

    def test_a_loopback_peer_may_maintain(self):
        with patch.object(agent.AgentApp, "handle_set_robot_type",
                          return_value=(200, {"ok": True})) as handler:
            status, _, _ = self.req("POST", "/robot-type", {"robot_type": "omx_full"}, origin=None)
        self.assertEqual(status, 200)
        handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
