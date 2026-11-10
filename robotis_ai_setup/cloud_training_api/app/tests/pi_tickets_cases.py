# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The Orange Pi's one-time login tickets (SA-06, 2026-11-13) through the REAL
FastAPI stack: ``POST /pi/tickets`` and ``POST /pi/tickets/redeem`` behind the
middlewares main.py mounts in front of them (CORS, the any-origin bearer CORS,
the rate limits).

Run by test_pi_tickets.py in a CHILD interpreter (the edge_cases.py pattern):
other suites of this package stub ``fastapi`` in ``sys.modules`` during
discovery. Not named ``test_*`` so discovery never imports it there."""

from __future__ import annotations

import ast
import os
import sys
import time
import types
import unittest
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.dirname(APP)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if "app.services.supabase_client" not in sys.modules:  # CI's test venv has no supabase-py
    _sc = types.ModuleType("app.services.supabase_client")
    _sc.get_supabase = lambda: (_ for _ in ()).throw(RuntimeError("no supabase in unit tests"))
    sys.modules["app.services.supabase_client"] = _sc

from app import edge  # noqa: E402
from app.auth import CurrentUser, get_current_user  # noqa: E402
from app.routes import pi_tickets as route  # noqa: E402
from app.services import pi_tickets  # noqa: E402
from app.services.rate_limiter import RateLimiter  # noqa: E402

PI_ID = "q8V0f2Lr3nC1sZtA9xY7wB"
OTHER_PI = "Zz8V0f2Lr3nC1sZtA9xY7w"
ALLOWED = "http://localhost"
PI_ORIGIN = "http://192.168.1.23"


def _main_literal(name):
    with open(os.path.join(APP, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(getattr(t, "id", None) == name for t in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(name)


def build_app(user, *, limiter=None):
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_current_user] = lambda: user
    rules = sorted(_main_literal("_RATE_LIMIT_RULES"), key=lambda r: len(r[1]), reverse=True)
    app.add_middleware(edge.RateLimitMiddleware, rules=rules,
                       per_user_prefixes=_main_literal("_PER_USER_RATE_LIMIT_PREFIXES"),
                       limiter=limiter or RateLimiter(), hops=1)
    app.add_middleware(CORSMiddleware, allow_origins=[ALLOWED], allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Content-Type", "Authorization"])
    app.add_middleware(edge.AnyOriginBearerCorsMiddleware,
                       paths=_main_literal("_ANY_ORIGIN_BEARER_PATHS"))
    return app


def a_user(role="student", exp_in=3600):
    uid = str(uuid.uuid4())
    claims = {"sub": uid, "exp": int(time.time()) + exp_in, "aal": "aal1"}
    return CurrentUser(uid, claims, {"id": uid, "role": role})


class Base(unittest.TestCase):
    def setUp(self):
        pi_tickets.STORE = pi_tickets.TicketStore()
        self.user = a_user()
        self.client = TestClient(build_app(self.user))

    def mint(self, pi_id=PI_ID, origin=PI_ORIGIN, body="default"):
        payload = {"pi_id": pi_id} if body == "default" else body
        headers = {"Authorization": "Bearer x.y.z"}
        if origin:
            headers["Origin"] = origin
        return self.client.post("/pi/tickets", json=payload, headers=headers)

    def redeem(self, ticket, pi_id=PI_ID):
        return self.client.post("/pi/tickets/redeem", json={"ticket": ticket, "pi_id": pi_id})


class MintAndRedeem(Base):
    def test_a_ticket_opens_its_pi_once(self):
        r = self.mint()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        body = r.json()
        self.assertEqual(body["expires_in"], pi_tickets.TICKET_TTL_S)
        self.assertRegex(body["ticket"], r"^[A-Za-z0-9_-]{43}$")
        first = self.redeem(body["ticket"])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers.get("cache-control"), "no-store")
        got = first.json()
        self.assertEqual(set(got), {"user_id", "role", "session_max_s"})
        self.assertEqual(got["user_id"], self.user.id)
        self.assertEqual(got["role"], "student")
        self.assertLessEqual(got["session_max_s"], pi_tickets.SESSION_MAX_S)
        self.assertGreater(got["session_max_s"], 0)
        again = self.redeem(body["ticket"])
        self.assertEqual(again.status_code, 401)

    def test_every_role_may_mint(self):
        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                client = TestClient(build_app(a_user(role)))
                ticket = client.post("/pi/tickets", json={"pi_id": PI_ID},
                                     headers={"Authorization": "Bearer x.y.z"}).json()["ticket"]
                got = client.post("/pi/tickets/redeem", json={"ticket": ticket, "pi_id": PI_ID})
                self.assertEqual(got.json()["role"], role)

    def test_an_unknown_role_mints_nothing(self):
        client = TestClient(build_app(a_user("superuser")))
        r = client.post("/pi/tickets", json={"pi_id": PI_ID}, headers={"Authorization": "Bearer x.y.z"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(pi_tickets.STORE), 0)

    def test_the_session_never_outlives_the_access_token(self):
        client = TestClient(build_app(a_user(exp_in=200)))
        ticket = client.post("/pi/tickets", json={"pi_id": PI_ID},
                             headers={"Authorization": "Bearer x.y.z"}).json()["ticket"]
        got = client.post("/pi/tickets/redeem", json={"ticket": ticket, "pi_id": PI_ID}).json()
        self.assertLessEqual(got["session_max_s"], 200)
        self.assertGreater(got["session_max_s"], 190)

    def test_another_pi_cannot_redeem_and_the_attempt_spends_the_ticket(self):
        ticket = self.mint().json()["ticket"]
        self.assertEqual(self.redeem(ticket, pi_id=OTHER_PI).status_code, 401)
        self.assertEqual(self.redeem(ticket).status_code, 401)

    def test_every_refusal_is_the_same_german_401(self):
        ticket = self.mint().json()["ticket"]
        cases = [
            {"ticket": "A" * 43, "pi_id": PI_ID},           # unknown
            {"ticket": ticket[:-1], "pi_id": PI_ID},        # malformed
            {"ticket": ticket, "pi_id": "short"},           # malformed Pi id
            {"pi_id": PI_ID},                               # missing
            [],                                             # not an object
            "text",
        ]
        details = set()
        for body in cases:
            with self.subTest(body=body):
                r = self.client.post("/pi/tickets/redeem", json=body)
                self.assertEqual(r.status_code, 401)
                details.add(r.json()["detail"])
                self.assertNotIn(ticket, r.text)
        self.assertEqual(details, {"Das Anmeldeticket für den Roboter ist ungültig oder abgelaufen."})

    def test_a_bad_pi_id_is_a_german_400_that_echoes_nothing(self):
        for body in ({"pi_id": "x"}, {"pi_id": "a" * 65}, {"pi_id": 7}, {}, [], "text"):
            with self.subTest(body=body):
                r = self.mint(body=body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("Kennung des Roboters", r.json()["detail"])
        self.assertEqual(len(pi_tickets.STORE), 0)


class Cors(Base):
    """The Pi's page runs at a LAN origin ALLOWED_ORIGINS cannot list; the mint
    must work from there, and only the mint."""

    def test_a_pi_origin_may_preflight_and_mint_without_credentials(self):
        pre = self.client.options("/pi/tickets", headers={
            "Origin": PI_ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type"})
        self.assertEqual(pre.status_code, 204)
        self.assertEqual(pre.headers.get("access-control-allow-origin"), "*")
        self.assertIn("Authorization", pre.headers.get("access-control-allow-headers"))
        self.assertIn("POST", pre.headers.get("access-control-allow-methods"))
        self.assertIsNone(pre.headers.get("access-control-allow-credentials"))
        r = self.mint(origin=PI_ORIGIN)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertIsNone(r.headers.get("access-control-allow-credentials"))

    def test_a_refusal_is_readable_by_the_pi_page_too(self):
        r = self.mint(pi_id="x", origin=PI_ORIGIN)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")

    def test_a_preflight_for_another_method_is_refused(self):
        pre = self.client.options("/pi/tickets", headers={
            "Origin": PI_ORIGIN, "Access-Control-Request-Method": "DELETE"})
        self.assertEqual(pre.status_code, 405)

    def test_the_redeem_and_every_other_path_keep_the_normal_cors(self):
        pre = self.client.options("/pi/tickets/redeem", headers={
            "Origin": PI_ORIGIN, "Access-Control-Request-Method": "POST"})
        self.assertEqual(pre.status_code, 400)  # CORSMiddleware: an unlisted origin
        ticket = self.mint().json()["ticket"]
        r = self.client.post("/pi/tickets/redeem", json={"ticket": ticket, "pi_id": PI_ID},
                             headers={"Origin": PI_ORIGIN})
        self.assertIsNone(r.headers.get("access-control-allow-origin"))

    def test_main_mounts_it_outermost(self):
        with open(os.path.join(APP, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(_main_literal("_ANY_ORIGIN_BEARER_PATHS"), ("/pi/tickets",))
        mount = src.index("app.add_middleware(_edge.AnyOriginBearerCorsMiddleware, paths=_ANY_ORIGIN_BEARER_PATHS)")
        self.assertGreater(mount, src.index("    CORSMiddleware,"))
        self.assertIn("app.include_router(pi_tickets_router)", src)


class RateLimits(Base):
    def test_minting_is_per_user_and_redeeming_per_address(self):
        rules = _main_literal("_RATE_LIMIT_RULES")
        self.assertIn(("POST", "/pi/tickets", 30, 60.0), rules)
        self.assertIn(("POST", "/pi/tickets/redeem", 600, 60.0), rules)
        self.assertIn("/pi/tickets", _main_literal("_PER_USER_RATE_LIMIT_PREFIXES"))
        statuses = [self.mint().status_code for _ in range(31)]
        self.assertEqual(statuses[:30], [200] * 30)
        self.assertEqual(statuses[30], 429)
        # The redeem has its own bucket: the mint's 429 does not reach it.
        self.assertEqual(self.redeem("A" * 43).status_code, 401)


if __name__ == "__main__":
    unittest.main()
