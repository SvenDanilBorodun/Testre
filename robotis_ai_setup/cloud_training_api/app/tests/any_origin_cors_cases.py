# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The Pi's SPA reaches the cloud API from its LAN origin (2026-11-14).

A Pi serves the student SPA at ``http://<LAN address>``, an origin
``ALLOWED_ORIGINS`` cannot list, and that SPA calls the cloud API with an
``Authorization`` header — a non-simple request, so the browser preflights it.
Measured before this change with the real app (``app.main``) under httpx:
every student route answered that preflight 400 „Disallowed CORS origin";
only ``POST /pi/tickets`` passed.

These cases mount the edge EXACTLY the way main.py does — the any-origin bearer
CORS outermost, ``CORSMiddleware``, the failed-authentication cap, the body and
rate limits, the public-read star, main.py's own tables — in front of a
catch-all route that answers 401 to a bearer that does not verify, and pin:

* every route of ``main.py::_ANY_ORIGIN_BEARER_ROUTES`` preflights and answers
  from a LAN origin, with the star and never a credentials header;
* teacher, admin and worker routes, and every declared route NOT on the list,
  keep the strict rule (``CORSMiddleware``'s 400);
* no answer to an unlisted origin carries ``Access-Control-Allow-Credentials``;
  a listed origin keeps its echo and its credentials header, untouched;
* the list is exact: every entry is a declared route (read from the routers'
  decorators), and no entry matches another declared route's paths;
* the API reads no cookie anywhere;
* the downside (any website may now make a visitor's browser send preflighted
  requests with junk bearers) stays harmless: the failed-authentication cap
  refuses the address after 60 failures while a verified bearer behind the same
  address still passes (VA-02), and the per-user and per-address rate limits
  still apply.

Run by test_any_origin_cors.py in a CHILD interpreter (the edge_cases.py
pattern): other suites of this package stub ``fastapi`` during discovery."""

from __future__ import annotations

import ast
import glob
import json
import os
import re
import sys
import time
import types
import unittest
import uuid
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
from app.services import jwt_auth as J  # noqa: E402
from app.services.rate_limiter import FailureCounter, RateLimiter  # noqa: E402

BASE = "https://unit.supabase.test"
KEY = ec.generate_private_key(ec.SECP256R1())
JWK = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "k1", "alg": "ES256", "use": "sig"})

LISTED = ("http://localhost", "https://lehrer.edubotics.test")
PI_ORIGINS = ("http://192.168.1.50", "http://edubotics-pi.local", "http://10.20.30.40")
EVIL = "https://evil.example"
NAT = "203.0.113.7"
ASK_HEADERS = "authorization,content-type"


def _main_literal(name):
    with open(os.path.join(APP, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(getattr(t, "id", None) == name for t in targets):
            if isinstance(node.value, ast.BinOp):  # `384 * 1024`
                return eval(compile(ast.Expression(node.value), "main.py", "eval"))  # noqa: S307
            return ast.literal_eval(node.value)
    raise AssertionError(name)


ROUTES = _main_literal("_ANY_ORIGIN_BEARER_ROUTES")


def declared_routes():
    """Every (METHOD, path template) the routers declare, read from their
    decorators (importing them needs modal and supabase-py)."""
    out = set()
    for path in sorted(glob.glob(os.path.join(APP, "routes", "*.py"))):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), path)
        prefix = ""
        for node in tree.body:
            if (isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "router" for t in node.targets)
                    and isinstance(node.value, ast.Call)):
                for kw in node.value.keywords:
                    if kw.arg == "prefix":
                        prefix = ast.literal_eval(kw.value)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and getattr(dec.func.value, "id", None) == "router"
                        and dec.func.attr in ("get", "post", "put", "patch", "delete")):
                    out.add((dec.func.attr.upper(), prefix + ast.literal_eval(dec.args[0])))
    return out


def concrete(template, value="x1"):
    return re.sub(r"\{[^}]+\}", value, template)


def user_token(sub=None, **over):
    now = int(time.time())
    claims = {"iss": BASE + "/auth/v1", "aud": "authenticated", "sub": sub or str(uuid.uuid4()),
              "role": "authenticated", "iat": now, "exp": now + 3600, "aal": "aal1"}
    claims.update(over)
    return jwt.encode(claims, KEY, algorithm="ES256", headers={"kid": "k1"})


def junk():
    return f"{uuid.uuid4().hex}.{uuid.uuid4().hex}.{uuid.uuid4().hex}"


def build_app():
    """main.py's edge, in main.py's order, with main.py's tables."""
    app = FastAPI()
    app.add_middleware(edge.PublicReadCorsMiddleware, paths=_main_literal("_PUBLIC_READ_PATHS"))
    rules = sorted(_main_literal("_RATE_LIMIT_RULES"), key=lambda r: len(r[1]), reverse=True)
    app.add_middleware(edge.RateLimitMiddleware, rules=rules,
                       per_user_prefixes=_main_literal("_PER_USER_RATE_LIMIT_PREFIXES"),
                       limiter=RateLimiter(), hops=1)
    app.add_middleware(edge.BodySizeLimitMiddleware, limited_prefixes=_main_literal("_BODY_SIZE_LIMITED_PREFIXES"),
                       limited_max_bytes=_main_literal("_MAX_WRITE_BODY_BYTES"))
    app.add_middleware(edge.AuthFailureLimitMiddleware, counter=FailureCounter(), hops=1)
    app.add_middleware(CORSMiddleware, allow_origins=list(LISTED), allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Content-Type", "Authorization"])
    app.add_middleware(edge.AnyOriginBearerCorsMiddleware, routes=ROUTES, listed_origins=LISTED)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def anything(path: str, request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or J.verified_subject(auth[len("Bearer "):]) is None:
            return JSONResponse(status_code=401, content={"detail": "Ungültige oder abgelaufene Sitzung."})
        return {"path": path}

    return app


class _Base(unittest.TestCase):
    def setUp(self):
        J.KEYS.clear()
        J.KEYS.replace(J.parse_jwks({"keys": [JWK]}))
        self.addCleanup(J.KEYS.clear)
        env = patch.dict(os.environ, {"SUPABASE_URL": BASE, "AUTH_VERIFY_MODE": "local"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(build_app())

    def preflight(self, method, path, origin, headers=ASK_HEADERS):
        h = {"Origin": origin, "Access-Control-Request-Method": method}
        if headers:
            h["Access-Control-Request-Headers"] = headers
        return self.client.options(path, headers=h)

    def call(self, method, path, origin=None, token=None, ip=NAT):
        h = {"X-Forwarded-For": ip}
        if origin:
            h["Origin"] = origin
        if token:
            h["Authorization"] = f"Bearer {token}"
        body = {} if method in ("POST", "PUT", "PATCH") else None
        return self.client.request(method, path, headers=h, json=body)


class StudentRoutesFromALanOrigin(_Base):
    def test_every_listed_route_preflights_from_a_pi_origin(self):
        self.assertGreaterEqual(len(ROUTES), 30)
        for origin in PI_ORIGINS:
            for method, template in ROUTES:
                with self.subTest(origin=origin, method=method, route=template):
                    pre = self.preflight(method, concrete(template), origin)
                    self.assertEqual(pre.status_code, 204, pre.text)
                    self.assertEqual(pre.headers.get("access-control-allow-origin"), "*")
                    self.assertIn(method, pre.headers["access-control-allow-methods"].split(", "))
                    self.assertEqual(pre.headers.get("access-control-allow-headers"), "Authorization, Content-Type")
                    self.assertIsNone(pre.headers.get("access-control-allow-credentials"))

    def test_the_real_request_is_readable_without_credentials(self):
        student = user_token()
        for method, template in ROUTES:
            with self.subTest(method=method, route=template):
                r = self.call(method, concrete(template), PI_ORIGINS[0], student)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
                self.assertIsNone(r.headers.get("access-control-allow-credentials"))
                self.assertIn("origin", r.headers.get("vary", "").lower())

    def test_a_refusal_is_readable_too(self):
        r = self.call("GET", "/me", PI_ORIGINS[0], junk())
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertEqual(r.json()["detail"], "Ungültige oder abgelaufene Sitzung.")

    def test_a_preflight_asking_for_another_header_falls_through_to_the_strict_rule(self):
        for headers in ("authorization,x-forwarded-for", "cookie", "authorization, content-type, x-evil"):
            with self.subTest(headers=headers):
                pre = self.preflight("GET", "/me", PI_ORIGINS[0], headers=headers)
                self.assertEqual(pre.status_code, 400)
                self.assertIsNone(pre.headers.get("access-control-allow-origin"))
                self.assertIsNone(pre.headers.get("access-control-allow-credentials"))
        self.assertEqual(self.preflight("GET", "/me", PI_ORIGINS[0], headers=None).status_code, 204)


class EverythingElseKeepsTheStrictRule(_Base):
    def test_teacher_admin_and_worker_routes(self):
        strict = sorted(r for r in declared_routes()
                        if r[1].startswith(("/teacher", "/admin", "/trainings/worker")))
        self.assertGreater(len(strict), 40)
        for method, template in strict:
            with self.subTest(method=method, route=template):
                pre = self.preflight(method, concrete(template), PI_ORIGINS[0])
                self.assertEqual(pre.status_code, 400)
                self.assertIsNone(pre.headers.get("access-control-allow-origin"))
                self.assertIsNone(pre.headers.get("access-control-allow-credentials"))
                r = self.call(method, concrete(template), PI_ORIGINS[0], user_token())
                self.assertIsNone(r.headers.get("access-control-allow-origin"))
                self.assertIsNone(r.headers.get("access-control-allow-credentials"))

    def test_every_declared_route_that_is_not_listed(self):
        listed = set(ROUTES)
        others = sorted(r for r in declared_routes() if r not in listed)
        for method, template in others:
            with self.subTest(method=method, route=template):
                pre = self.preflight(method, concrete(template), EVIL)
                self.assertEqual(pre.status_code, 400)
                self.assertIsNone(pre.headers.get("access-control-allow-credentials"))

    def test_a_listed_origin_keeps_its_echo_and_its_credentials_header(self):
        for origin in LISTED:
            for method, path in (("GET", "/me"), ("GET", "/teacher/classrooms"), ("POST", "/pi/tickets")):
                with self.subTest(origin=origin, path=path):
                    pre = self.preflight(method, path, origin)
                    self.assertEqual(pre.status_code, 200)
                    self.assertEqual(pre.headers.get("access-control-allow-origin"), origin)
                    self.assertEqual(pre.headers.get("access-control-allow-credentials"), "true")
                    r = self.call(method, path, origin, user_token())
                    self.assertEqual(r.headers.get("access-control-allow-origin"), origin)
                    self.assertEqual(r.headers.get("access-control-allow-credentials"), "true")

    def test_a_request_without_origin_is_untouched(self):
        r = self.call("GET", "/me", None, user_token())
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.headers.get("access-control-allow-origin"))

    def test_no_answer_to_an_unlisted_origin_carries_a_credentials_header(self):
        probes = [("GET", "/me"), ("GET", "/teacher/classrooms"), ("DELETE", "/workflows/x1"),
                  ("GET", "/version"), ("POST", "/pi/tickets/redeem"), ("GET", "/nowhere")]
        for origin in PI_ORIGINS + (EVIL, "null"):
            for method, path in probes:
                with self.subTest(origin=origin, method=method, path=path):
                    for resp in (self.preflight(method, path, origin), self.call(method, path, origin, junk()),
                                 self.call(method, path, origin, user_token())):
                        self.assertIsNone(resp.headers.get("access-control-allow-credentials"))

    def test_version_keeps_its_public_star(self):
        r = self.call("GET", "/version", PI_ORIGINS[0])
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertIsNone(r.headers.get("access-control-allow-credentials"))


class TheListIsExact(unittest.TestCase):
    def test_every_entry_is_a_declared_route(self):
        declared = declared_routes()
        for entry in ROUTES:
            with self.subTest(entry=entry):
                self.assertIn(entry, declared)
        self.assertEqual(len(set(ROUTES)), len(ROUTES), "an entry listed twice")

    def test_no_entry_matches_another_declared_route(self):
        mw = edge.AnyOriginBearerCorsMiddleware(None, routes=ROUTES)
        listed = set(ROUTES)
        for method, template in sorted(declared_routes() - listed):
            for sample in ("x1", "list", "quota", "sync", "usage", "sessions"):
                with self.subTest(method=method, route=template, sample=sample):
                    path = concrete(template, sample)
                    if (method, path) in listed:  # e.g. GET /trainings/{id} sampled as /trainings/list
                        continue
                    self.assertFalse(mw.allows(method, path))

    def test_no_teacher_admin_or_worker_route_is_listed(self):
        for method, template in ROUTES:
            self.assertFalse(template.startswith(("/teacher", "/admin", "/trainings/worker")), template)

    def test_the_matcher_matches_whole_paths_only(self):
        mw = edge.AnyOriginBearerCorsMiddleware(None, routes=ROUTES)
        self.assertTrue(mw.allows("GET", "/me"))
        self.assertTrue(mw.allows("GET", "/workflows/0f8b/trajectories/by-name/Greifen"))
        for method, path in (("GET", "/me/"), ("GET", "/me/x"), ("GET", "/mex"), ("GET", "/workflowsx"),
                             ("GET", "/workflows/a/b"), ("GET", "/workflows//clone"), ("POST", "/me"),
                             ("PATCH", "/me"), ("DELETE", "/workflows/x1"), ("GET", "/trainings/x1"),
                             ("GET", "/teacher/classrooms"), ("POST", "/pi/tickets/redeem"),
                             ("OPTIONS", "/me"), ("HEAD", "/me")):
            with self.subTest(method=method, path=path):
                self.assertFalse(mw.allows(method, path))

    def test_the_middleware_refuses_a_strict_route_or_a_bad_entry(self):
        for bad in ([("GET", "/teacher/classrooms")], [("GET", "/admin")], [("POST", "/trainings/worker/{a}")],
                    [("OPTIONS", "/me")], [("HEAD", "/me")], [("GET", "me")], [("GET", "/me/")],
                    [("GET", "/me/{x")], []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    edge.AnyOriginBearerCorsMiddleware(None, routes=bad)

    def test_main_mounts_it_outermost_with_the_listed_origins(self):
        with open(os.path.join(APP, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        mount = src.index("    _edge.AnyOriginBearerCorsMiddleware,\n    routes=_ANY_ORIGIN_BEARER_ROUTES,\n"
                          "    listed_origins=allowed_origins,\n)")
        self.assertGreater(mount, src.index("    CORSMiddleware,"))
        self.assertGreater(mount, src.index("app.add_middleware(_edge.AuthFailureLimitMiddleware"))
        self.assertNotIn("_ANY_ORIGIN_BEARER_PATHS", src)


class TheApiReadsNoCookie(unittest.TestCase):
    """A browser attaches no Authorization header by itself, and the API reads
    no cookie anywhere, so a foreign page holds nothing to send: `*` without
    credentials is safe for every bearer route."""

    def test_no_cookie_read_in_the_app(self):
        found = []
        for path in sorted(glob.glob(os.path.join(APP, "**", "*.py"), recursive=True)):
            if os.sep + "tests" + os.sep in path:
                continue
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "cookies":
                    found.append((path, node.lineno, "request.cookies"))
                elif isinstance(node, ast.ImportFrom) and any(a.name == "Cookie" for a in node.names):
                    found.append((path, node.lineno, "fastapi.Cookie"))
                elif isinstance(node, ast.Name) and node.id in ("Cookie", "APIKeyCookie"):
                    found.append((path, node.lineno, node.id))
                elif (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                      and isinstance(node.args[0].value, str) and node.args[0].value.lower() == "cookie"):
                    found.append((path, node.lineno, "a cookie header read"))
        self.assertEqual(found, [])


class ForeignPagesStayHarmless(_Base):
    """The downside of the star: any website can make a visitor's browser send
    preflighted requests with whatever Authorization header the page invents.
    It holds no victim's token (the SPA's session lives in its own origin's
    storage), so it can only send junk or its own token — what curl could send
    anyway — and the edge prices junk per ADDRESS."""

    def test_a_junk_flood_from_a_foreign_page_locks_out_only_junk(self):
        student = user_token()
        for _ in range(edge.AUTH_FAILURE_LIMIT):
            r = self.call("GET", "/me", EVIL, junk())
            self.assertEqual(r.status_code, 401)
        refused = self.call("GET", "/me", EVIL, junk())
        self.assertEqual((refused.status_code, refused.json()["detail"]), (429, edge.AUTH_FAILURES_DE))
        self.assertEqual(refused.headers.get("access-control-allow-origin"), "*")
        # VA-02: the signed-in students behind the same school address still pass,
        # from the Pi's origin as from any other
        self.assertEqual(self.call("GET", "/me", PI_ORIGINS[0], student).status_code, 200)
        self.assertEqual(self.call("GET", "/me", EVIL, user_token()).status_code, 200)
        # and another address is untouched
        self.assertEqual(self.call("GET", "/me", EVIL, junk(), ip="198.51.100.4").status_code, 401)

    def test_rotating_junk_meets_the_address_rate_limit(self):
        # POST /trainings/start: 10 a minute; an unverifiable bearer is keyed by
        # its address however it rotates, so a page cannot mint fresh buckets.
        statuses = [self.call("POST", "/trainings/start", EVIL, junk(), ip="198.51.100.9").status_code
                    for _ in range(12)]
        self.assertEqual(statuses[:10], [401] * 10)
        self.assertEqual(statuses[10:], [429, 429])

    def test_a_verified_callers_per_user_limit_still_applies(self):
        student = user_token()
        statuses = [self.call("POST", "/trainings/start", PI_ORIGINS[0], student).status_code for _ in range(11)]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)

    def test_the_cap_never_fetches_keys_for_junk(self):
        unknown_kid = jwt.encode({"sub": str(uuid.uuid4()), "aud": "authenticated", "iss": BASE + "/auth/v1",
                                  "role": "authenticated", "exp": int(time.time()) + 60, "iat": int(time.time())},
                                 KEY, algorithm="ES256", headers={"kid": "k-unknown"})
        with patch.object(J, "fetch_jwks_blocking", side_effect=AssertionError("fetched")):
            for _ in range(5):
                self.assertEqual(self.call("GET", "/me", EVIL, unknown_kid).status_code, 401)


if __name__ == "__main__":
    unittest.main()
